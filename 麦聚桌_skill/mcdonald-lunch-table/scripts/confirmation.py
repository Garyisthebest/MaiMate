"""Local, atomic confirmation ledger. Not a server-side idempotency guarantee."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from mcd_adapter import AdapterError, McdAdapter, ROOT, WRITE_TOOLS


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class ConfirmationLedger:
    def __init__(self, path: Path | None = None):
        self.path = path or ROOT / "runtime/confirmations.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Precreate without opening the database world-readable even briefly.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS confirmations (
                token_hash TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                scope TEXT NOT NULL, actor_hash TEXT NOT NULL,
                expires REAL NOT NULL, status TEXT NOT NULL,
                order_id TEXT, created REAL NOT NULL, quote_hash TEXT)''')
            columns = {row[1] for row in db.execute("PRAGMA table_info(confirmations)")}
            if "quote_hash" not in columns:
                db.execute("ALTER TABLE confirmations ADD COLUMN quote_hash TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_scope ON confirmations(scope) WHERE status IN ('in_flight','unknown','complete')")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def request_hash(adapter: McdAdapter, tool: str, args: dict[str, Any], summary: dict[str, Any]) -> str:
        return digest({"endpoint": adapter.config.url, "account": digest(adapter.config.key),
                       "tool": tool, "arguments": args, "summary": summary})

    @staticmethod
    def quote(adapter: McdAdapter, tool: str, args: dict[str, Any], summary: dict[str, Any]) -> str | None:
        if tool != "create-order":
            return None
        if adapter.tools is None:
            adapter.discover()
        assert adapter.tools is not None
        schema = adapter.tools.get("calculate-price", {}).get("inputSchema", {})
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise AdapterError("quote_unavailable", "无法确定实时算价参数；禁止创建订单")
        # Reuse actual order contents. Transport/confirmation-only values never enter arguments.
        supplied = dict(args)
        if "orderType" not in supplied and "orderType" in properties:
            if args.get("beType") not in (1, 2, 5, 6):
                raise AdapterError("quote_unavailable", "无法判断履约类型")
            supplied["orderType"] = 2 if args["beType"] == 2 else 1
        if "items" in args and "items" not in properties:
            raise AdapterError("quote_unavailable", "算价schema未提供items；不能丢弃订单内容继续算价")
        quote_args = {key: value for key, value in supplied.items() if key in properties}
        result = adapter.call("calculate-price", quote_args)
        business = adapter.business_payload(result)
        if not isinstance(business, dict) or business.get("success") is not True or not isinstance(business.get("data"), dict):
            raise AdapterError("quote_unavailable", "算价未返回可验证的业务数据")
        data = business["data"]
        amount = data.get("price")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise AdapterError("quote_unavailable", "算价金额单位或格式无法确认；需要整数分")
        displayed = summary.get("total_cents")
        if isinstance(displayed, bool) or not isinstance(displayed, int) or displayed != amount:
            raise AdapterError("price_changed", "summary.total_cents必须等于最新官方算价（整数分）；重新展示并确认")
        # Compare all documented monetary/content fields, not changing trace IDs/timestamps.
        fields = ("price", "originalPrice", "discount", "productPrice", "productOriginalPrice",
                  "deliveryPrice", "deliveryOriginalPrice", "tablewarePrice", "productList",
                  "takeWayList", "mealAssistanceList")
        return digest({key: data[key] for key in fields if key in data})

    def prepare(self, adapter: McdAdapter, tool: str, args: dict[str, Any], summary: dict[str, Any],
                *, scope: str, actor: str, ttl: int = 120) -> str:
        if tool not in WRITE_TOOLS:
            raise AdapterError("unsupported_mutation", "该工具未列入已审阅写操作列表")
        if not scope or not actor or not summary or not 1 <= ttl <= 300:
            raise AdapterError("confirmation_invalid", "需要桌/订单 scope、操作者和完整可见预览；有效期最长300秒")
        adapter.validate(tool, args)
        quote_hash = self.quote(adapter, tool, args, summary)
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            blocked = db.execute("SELECT status FROM confirmations WHERE scope=? AND status IN ('in_flight','unknown','complete')", (scope,)).fetchone()
            if blocked:
                raise AdapterError("duplicate_or_unknown", "该桌/订单已执行或结果未知；先核查订单")
            db.execute("UPDATE confirmations SET status='superseded' WHERE scope=? AND status='prepared'", (scope,))
            db.execute("INSERT INTO confirmations (token_hash,request_hash,scope,actor_hash,expires,status,order_id,created,quote_hash) VALUES (?,?,?,?,?,'prepared',NULL,?,?)",
                       (digest(token), self.request_hash(adapter, tool, args, summary), scope,
                        digest(actor), time.time() + ttl, time.time(), quote_hash))
        return token

    def execute(self, adapter: McdAdapter, tool: str, args: dict[str, Any], summary: dict[str, Any],
                *, token: str, scope: str, actor: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise AdapterError("confirmation_required", "用户尚未明确确认当前预览")
        if tool not in WRITE_TOOLS:
            raise AdapterError("unsupported_mutation", "该工具未列入已审阅写操作列表")
        adapter.validate(tool, args)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request_hash,scope,actor_hash,expires,status,quote_hash FROM confirmations WHERE token_hash=?", (digest(token),)).fetchone()
            expected = self.request_hash(adapter, tool, args, summary)
            if row is None or row[0] != expected or row[1] != scope or row[2] != digest(actor) or row[3] <= time.time() or row[4] != "prepared":
                raise AdapterError("confirmation_invalid", "确认已过期、已使用，或预览/操作者发生变化")
            if db.execute("SELECT 1 FROM confirmations WHERE scope=? AND status IN ('in_flight','unknown','complete')", (scope,)).fetchone():
                raise AdapterError("duplicate_or_unknown", "该桌/订单已有执行记录")
            db.execute("UPDATE confirmations SET status='in_flight' WHERE token_hash=?", (digest(token),))
        try:
            # Already locked before revalidation, preventing concurrent duplicate submits.
            if self.quote(adapter, tool, args, summary) != row[5]:
                raise AdapterError("price_changed", "商品明细、优惠或费用变化；旧预览已失效")
            if time.time() >= row[3]:
                raise AdapterError("confirmation_invalid", "重新算价后确认已过期；请重新预览并确认")
        except Exception:
            # No mutation has been sent yet. The user must prepare and confirm again.
            with self.connect() as db:
                db.execute("UPDATE confirmations SET status='invalidated' WHERE token_hash=?", (digest(token),))
            raise
        try:
            result = adapter._execute(tool, args)
        except Exception:
            # A lost reply can mean the server committed the order. Never unlock.
            with self.connect() as db:
                db.execute("UPDATE confirmations SET status='unknown' WHERE token_hash=?", (digest(token),))
            raise
        data = adapter.business_payload(result)
        order_id = data.get("data", {}).get("orderId") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
        with self.connect() as db:
            db.execute("UPDATE confirmations SET status='complete',order_id=? WHERE token_hash=?", (str(order_id) if order_id else None, digest(token)))
        return result
