"""Small, inspectable dining preferences; no raw chats or account data on disk."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

from confirmation import digest
from mcd_adapter import ROOT


ALIASES = {'穷鬼套餐': '1+1超值', '穷鬼': '1+1超值', '随心配': '1+1超值',
           '超值1+1': '1+1超值', '1加1': '1+1超值', '一加一': '1+1超值'}
FOOD_TERMS = {
    '1+1超值': r'1\s*\+\s*1|穷鬼|随心配|一加一|1加1',
    '牛肉': r'牛肉|牛堡|巨无霸|安格斯', '鸡肉': r'鸡肉|鸡腿|鸡堡',
    '鱼肉': r'鱼肉|鱼堡|鳕鱼', '不辣': r'不辣|不要辣|不能辣',
    '辣味': r'爱吃辣|喜欢辣|要辣|辣味|^辣$', '无糖饮料': r'无糖|零度',
    '含糖饮料': r'含糖|有糖|普通可乐|正常可乐', '加冰': r'正常冰|(?<!不)加冰|(?<!不)要冰',
    '去冰': r'去冰|不要冰|不加冰|无冰', '薯条': r'薯条',
    '麦乐鸡': r'麦乐鸡|鸡块', '麦旋风': r'麦旋风', '咖啡': r'咖啡|美式|拿铁',
    '可乐': r'可乐', '豆浆': r'豆浆', '玉米杯': r'玉米杯',
}
SENSITIVE = re.compile(r'过敏|不耐受|疾病|病史|血糖|糖尿病|手机号|密码|密钥|token|api.?key|https?://|\d{7,}', re.I)


def normalized(value):
    return re.sub(r'[\s()（）【】\[\]·—_-]', '', unicodedata.normalize('NFKC', str(value))).lower()


def canonical(value, aliases=None):
    result = unicodedata.normalize('NFKC', str(value))
    for name, target in sorted({**ALIASES, **(aliases or {})}.items(), key=lambda pair: -len(pair[0])):
        result = result.replace(name, target)
    return result


def family(value, aliases=None):
    text = normalized(canonical(value, aliases))
    return '1+1超值' if '1+1' in text else None


def requested_family(value, aliases=None):
    text = canonical(value, aliases)
    if re.search(r'(?:不要|不想吃|不吃|不喜欢|不爱吃|别推荐|不是|除了|换掉)[^，。；;]{0,6}1\s*\+\s*1|1\s*\+\s*1[^，。；;]{0,6}(?:不要|不想吃|不吃)', text):
        return None
    return family(text)


def family_matches(name, target):
    return target == '1+1超值' and ('1+1' in normalized(name) or '随心配' in str(name))


def redact(text, limit=2000):
    text = re.sub(r'https?://[^\s]+', '[链接已隐藏]', str(text))
    text = re.sub(r'(?i)\b(?:sk-[A-Za-z0-9_-]+|bearer\s+\S+)', '[密钥已隐藏]', text)
    text = re.sub(r'(?i)((?:api[ _-]?key|token|密钥|密码)\s*[:=：]\s*)[^\s，。]+', r'\1[已隐藏]', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号已隐藏]', text)
    text = re.sub(r'(?<![\d.])-?\d{1,3}\.\d{4,}(?![\d.])', '[坐标已隐藏]', text)
    text = re.sub(r'((?:订单号|取餐码)[：:]\s*)\S+', r'\1[已隐藏]', text)
    return text[:limit]


class DiningMemory:
    def __init__(self, path: Path | None = None):
        self.path = path or ROOT / 'runtime/dining-memory.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.lock = threading.RLock()
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS preferences (actor_hash TEXT PRIMARY KEY, data TEXT NOT NULL)')

    @staticmethod
    def empty():
        return {'enabled': True, 'likes': [], 'dislikes': [], 'choices': {}, 'aliases': {},
                'evidence': {}, 'feedback': {}, 'last_selection': None, 'revision': 0}

    def profile(self, actor):
        with self.lock, sqlite3.connect(self.path) as db:
            row = db.execute('SELECT data FROM preferences WHERE actor_hash=?', (digest(actor),)).fetchone()
        return {**self.empty(), **json.loads(row[0])} if row else self.empty()

    def _save(self, actor, profile):
        profile['revision'] = profile.get('revision', 0) + 1
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('INSERT INTO preferences VALUES (?,?) ON CONFLICT(actor_hash) DO UPDATE SET data=excluded.data',
                       (digest(actor), json.dumps(profile, ensure_ascii=False)))

    def control(self, actor, operation):
        with self.lock:
            profile = self.profile(actor)
            if operation == 'clear':
                # Keep the user's off switch while deleting everything learned.
                enabled = profile['enabled']
                profile = self.empty()
                profile['enabled'] = enabled
            elif operation in ('enable', 'disable'):
                profile['enabled'] = operation == 'enable'
            else:
                raise ValueError('Unknown preference operation')
            self._save(actor, profile)
            return profile

    def forget(self, actor, text):
        with self.lock:
            profile = self.profile(actor)
            needle = normalized(canonical(text, profile['aliases']))
            removed = []
            for key in ('likes', 'dislikes'):
                for value in list(profile[key]):
                    if normalized(value) in needle or needle in normalized(value):
                        profile[key].remove(value)
                        removed.append(value)
            for key in list(profile['choices']):
                if normalized(key) in needle or needle in normalized(key):
                    del profile['choices'][key]
                    removed.append(key)
            for key, value in list(profile['aliases'].items()):
                if normalized(key) in needle or normalized(value) in needle:
                    del profile['aliases'][key]
                    removed.append(key)
            for key in list(profile['feedback']):
                if normalized(key) in needle or needle in normalized(key):
                    del profile['feedback'][key]
                    removed.append(key)
            if profile['last_selection'] and (normalized(profile['last_selection']['name']) in needle or needle in normalized(profile['last_selection']['name'])):
                profile['last_selection'] = None
            profile['evidence'] = {key: value for key, value in profile['evidence'].items()
                                   if key in profile['likes'] + profile['dislikes']}
            self._save(actor, profile)
            return sorted(set(removed))

    def learn(self, actor, text, menu_names=()):
        """Only explicit first-person preferences, reduced to known food names.

        Models cannot write memories. One-off orders, quotations, questions,
        negated memories, medical statements, and instruction text are excluded.
        """
        if not isinstance(text, str) or SENSITIVE.search(text) or re.search(r'[?？“”「」"<>]|不要记|别记|不记|清除|忘掉|忘记', text):
            return []
        positive = re.compile(r'(?:我(?:平时|通常|一直|一般|最|很|特别|比较|更|最近|现在)?(?:都)?(?:喜欢|爱吃|爱喝|偏爱|中意)|以后(?:优先|多推荐))')
        negative = re.compile(r'(?:我(?:平时|通常|一直|一般|最近|现在)?(?:都)?(?:不喜欢|不太喜欢|不再喜欢|不爱吃|不爱喝|讨厌|不吃|不喝)|以后(?:不要推荐|不推荐))')
        alias_match = re.fullmatch(r'(?:记住[，,：:\s]*)?(?:以后)?[“"]?([^\s，。；;：:"”]{1,16})[”"]?(?:就是|代指|指的是|是我说的)([^，。；;]{1,60})[。]?', text.strip())
        with self.lock:
            profile = self.profile(actor)
            if not profile['enabled']:
                return []
            updates = []
            if alias_match:
                name, target = alias_match.groups()
                target = canonical(target.strip(), profile['aliases'])
                if name not in ALIASES and (family(target) or target in menu_names) and len(profile['aliases']) < 20:
                    target = family(target) or target
                    profile['aliases'][name] = target
                    updates.append(name + '＝' + target)
            first_person = False
            for clause in re.split(r'[，,。；;！!\n]|但是|不过|而且|并且|但|而', text):
                clause = clause.strip()
                if first_person and re.match(r'(?:也)?(?:喜欢|爱吃|爱喝|偏爱|不喜欢|不爱吃|不爱喝|讨厌|不吃|不喝)', clause):
                    clause = '我' + clause.removeprefix('也')
                dislike, like = negative.search(clause), positive.search(clause)
                match = dislike or like
                if not match:
                    continue
                # "今天" / "这次" describes only this order, not a lasting habit.
                if re.search(r'今天|这次|本次|暂时|偶尔|朋友|同事|别人|例如|比如|如果|假如', clause):
                    continue
                first_person = True
                value = canonical(clause[match.end():], profile['aliases'])
                terms = [name for name, pattern in FOOD_TERMS.items() if re.search(pattern, value)]
                terms += [name for name in menu_names if isinstance(name, str) and len(name) <= 80 and name in value and not SENSITIVE.search(name)]
                for name in dict.fromkeys(terms):
                    key, opposite = ('dislikes', 'likes') if dislike else ('likes', 'dislikes')
                    if name in profile[opposite]:
                        profile[opposite].remove(name)
                    if not dislike:
                        conflicting = {'不辣': '辣味', '辣味': '不辣', '无糖饮料': '含糖饮料',
                                       '含糖饮料': '无糖饮料', '去冰': '加冰', '加冰': '去冰'}.get(name)
                        if conflicting in profile['likes']:
                            profile['likes'].remove(conflicting)
                    if name not in profile[key]:
                        profile[key] = (profile[key] + [name])[-30:]
                    profile['evidence'][name] = {'source': 'explicit', 'polarity': 'dislike' if dislike else 'like', 'updated_at': time.time()}
                    updates.append(('不喜欢' if dislike else '偏爱') + name)
            if updates:
                profile['evidence'] = {key: value for key, value in profile['evidence'].items()
                                       if key in profile['likes'] + profile['dislikes']}
                self._save(actor, profile)
            return updates

    def observe(self, actor, name, selection_id=None):
        """A confirmed meal selection is a weak signal, never an explicit like."""
        if not isinstance(name, str) or not name or len(name) > 80 or SENSITIVE.search(name):
            return
        with self.lock:
            profile = self.profile(actor)
            if not profile['enabled']:
                return
            profile['last_selection'] = {'name': name, 'id': selection_id or digest([name, time.time()]), 'at': time.time()}
            name = family(name, profile['aliases']) or name
            profile['choices'][name] = min(1000, profile['choices'].get(name, 0) + 1)
            profile['choices'] = dict(sorted(profile['choices'].items(), key=lambda item: -item[1])[:30])
            self._save(actor, profile)

    def feedback(self, actor, rating, *, name=None, selection_id=None):
        """One revisable rating per selection; store a bounded summary, no chat.

        A rating is the user's report, never evidence of a payment. Old databases
        migrate through profile defaults, retaining the existing actor hashes.
        """
        if rating not in ('like', 'neutral', 'dislike'):
            raise ValueError('Unknown rating')
        with self.lock:
            profile = self.profile(actor)
            if not profile['enabled']:
                return None
            selected = profile['last_selection']
            # The caller can only rate a real, locally observed selection.
            if not selected or name is not None and name != selected['name'] or selection_id is not None and selection_id != selected['id']:
                return None
            target = selected['name']
            record = profile['feedback'].get(target, {'ratings': []})
            event_id = digest(selected['id'])
            ratings = [v for v in record['ratings'] if v['id'] != event_id]
            ratings.append({'id': event_id, 'rating': rating, 'at': time.time()})
            profile['feedback'][target] = {'ratings': ratings[-10:], 'updated_at': time.time()}
            profile['feedback'] = dict(sorted(profile['feedback'].items(), key=lambda v: -v[1]['updated_at'])[:30])
            self._save(actor, profile)
            return target

    def reflections(self, actor):
        """Evidence-backed retrieval; weak signals expire and never become likes."""
        profile = self.profile(actor)
        if not profile['enabled']:
            return []
        rows = []
        for name, record in profile['feedback'].items():
            ratings = record['ratings']
            active = [v for v in ratings if time.time() - v['at'] <= 90 * 86400]
            if not active:
                continue
            score = sum({'like': 1, 'neutral': 0, 'dislike': -1}[v['rating']] * max(.1, 1 - (time.time() - v['at']) / (90 * 86400)) for v in active)
            rows.append({'name': name, 'signal': 'positive' if score > .2 else 'negative' if score < -.2 else 'mixed',
                         'score': round(max(-30, min(30, score * 10)), 2), 'samples': len(active),
                         'updated_at': record['updated_at'], 'source': 'meal_feedback'})
        return sorted(rows, key=lambda v: -v['updated_at'])[:10]

    def summary(self, actor):
        profile = self.profile(actor)
        return {'enabled': profile['enabled'], 'likes': profile['likes'], 'dislikes': profile['dislikes'],
                'often_selected': [{'name': name, 'selections': count} for name, count in profile['choices'].items() if count >= 2][:5],
                'aliases': {**ALIASES, **(profile['aliases'] if profile['enabled'] else {})},
                'insights': self.reflections(actor), 'evidence': profile['evidence'],
                'last_selection': profile['last_selection'] if profile['enabled'] else None,
                'revision': profile['revision']}
