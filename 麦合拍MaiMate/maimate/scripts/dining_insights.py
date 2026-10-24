"""Opt-in, bounded order analysis. Raw account payloads never reach disk/models."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from confirmation import digest
from dining_memory import SENSITIVE, redact
from mcd_adapter import AdapterError
from quick_order import money_cents

TZ = ZoneInfo('Asia/Shanghai')


def dining_clock(now=None):
    now = now or datetime.now(TZ)
    hour = now.hour
    period = '早餐' if 5 <= hour < 11 else '午餐' if hour < 14 and hour >= 11 else '下午' if 14 <= hour < 17 else '晚餐' if 17 <= hour < 21 else '夜间'
    return {'local_time': now.isoformat(timespec='minutes'), 'timezone': 'Asia/Shanghai',
            'period': period, 'notice': '时间仅供选餐参考，供应时段以当前门店菜单为准'}


class DiningInsights:
    def __init__(self, path, app):
        self.path, self.app = path, app
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS order_insights (owner TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA secure_delete=ON')
        return db

    def owner(self, actor):
        # A token change never reuses another account's imported history.
        config = getattr(self.app.adapter, 'config', None)
        return digest(actor + ':' + digest(getattr(config, 'key', '')))

    def summary(self, actor):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT data FROM order_insights WHERE owner=?', (self.owner(actor),)).fetchone()
        return json.loads(row[0]) if row else {'imported_at': None, 'orders': 0, 'foods': [], 'months': [], 'periods': [], 'total_cents': 0}

    def clear(self, actor):
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM order_insights WHERE owner=?', (self.owner(actor),))
        return self.summary(actor)

    def forget(self, actor, name):
        from dining_memory import normalized
        with self.lock:
            data = self.summary(actor)
            excluded = list(dict.fromkeys(data.get('excluded', []) + [name]))[-50:]
            data['excluded'] = excluded
            data['foods'] = [v for v in data['foods'] if not any(normalized(n) in normalized(v['name']) for n in excluded)]
            with self.connect() as db:
                db.execute('INSERT INTO order_insights VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET data=excluded.data',
                           (self.owner(actor), json.dumps(data, ensure_ascii=False)))

    def refresh(self, actor):
        from dining_memory import normalized
        excluded = self.summary(actor).get('excluded', [])
        data = self.app.query('order-list', {})
        if not isinstance(data, dict) or not isinstance(data.get('list'), list):
            raise AdapterError('history_invalid', '官方历史订单格式无法识别；没有更新分析')
        # The official API has no pagination/range input. Only analyse its latest
        # returned batch (at most 30 entries); do not fan out into order details.
        rows = data['list'][:30]
        foods, periods, months = Counter(), Counter(), {}
        total, counted, skipped, seen = 0, 0, 0, set()
        now = datetime.now(TZ)
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            ident = row.get('orderId')
            status = str(row.get('orderStatus', '')).strip()
            # Do not guess an undocumented numeric status or equate payment
            # with consumption. Only confirmed completed textual states count.
            if not isinstance(ident, str) or not ident or ident in seen or status not in ('订单已完成', '已完成', '订单已评价', '已评价'):
                skipped += 1
                continue
            seen.add(ident)
            try:
                at = datetime.fromisoformat(str(row.get('createTime', '')).replace('/', '-').replace('Z', '+00:00'))
                at = at.replace(tzinfo=TZ) if at.tzinfo is None else at.astimezone(TZ)
            except (ValueError, TypeError):
                skipped += 1
                continue
            if not 0 <= (now - at).total_seconds() <= 90 * 86400:
                skipped += 1
                continue
            cents = money_cents(row.get('realTotalAmount'))
            if cents is None or cents < 0:
                skipped += 1
                continue
            counted += 1
            total += cents
            month = at.strftime('%Y-%m')
            if month not in months:
                months[month] = {'month': month, 'orders': 0, 'total_cents': 0}
            months[month]['orders'] += 1
            months[month]['total_cents'] += cents
            periods[dining_clock(at)['period']] += 1
            # Count presence per completed order, not quantities/combo children;
            # ordering for others is still only weak preference evidence.
            names = set()
            products = row.get('orderProductList')
            for p in (products if isinstance(products, list) else [])[:50]:
                name = p.get('productName') if isinstance(p, dict) else None
                if isinstance(name, str) and 1 <= len(name) <= 80 and not SENSITIVE.search(name) and redact(name) == name and not re.search(r'[<>\r\n]', name):
                    names.add(name)
            foods.update(names)
        summary = {'imported_at': time.time(), 'orders': counted, 'skipped': skipped,
                   'examined': len(rows), 'total_cents': total,
                   'excluded': excluded,
                   'foods': [{'name': name, 'orders': count, 'source': 'completed_orders'} for name, count in foods.most_common(15)
                             if not any(normalized(n) in normalized(name) for n in excluded)],
                   'months': sorted(months.values(), key=lambda r: r['month']),
                   'periods': [{'period': name, 'orders': count} for name, count in periods.most_common()],
                   'notice': '仅官方本次返回的最近30条以内、近90天且状态明确完成的订单；不是完整账单，也不代表每份都由你吃。'}
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO order_insights VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET data=excluded.data',
                       (self.owner(actor), json.dumps(summary, ensure_ascii=False)))
        return summary

    def model_summary(self, actor, enabled=True):
        if not enabled:
            return []
        summary = self.summary(actor)
        # Stale imports stop influencing suggestions until a fresh user request.
        if not summary.get('imported_at') or time.time() - summary['imported_at'] > 30 * 86400:
            return []
        return [dict(v) for v in summary['foods'] if v['orders'] >= 2][:8]
