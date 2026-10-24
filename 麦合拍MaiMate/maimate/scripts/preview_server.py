#!/usr/bin/env python3
"""Loopback ordering agent with owner-bound Weixin and confirmed MCP orders."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import secrets
import hashlib
import hmac
import os
from http.cookies import SimpleCookie
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from mcd_adapter import AdapterConfig, AdapterError, McdAdapter, ROOT
from quick_order import parse_request, recommend, NeedInfo
from weixin_bridge import WeixinBridge, WeixinError
from model_gateway import ModelGateway
from location_service import LocationResolver
from location_relay import LocationRelay
from ordering_agent import OrderingAgent
from dining_insights import dining_clock

WEB = ROOT / "web"


class PreviewApp:
    def __init__(self, adapter, *, model=None, location=None, ledger=None):
        self.adapter = adapter
        self.lock = threading.RLock()
        self.stores: dict[str, dict] = {}
        self.catalogs: dict[str, dict] = {}
        self.updated_at = None
        self.connected = False
        self.count = 0
        self.weixin = None
        self.model = model or ModelGateway()
        self.location = location or LocationResolver()
        self.agent = OrderingAgent(self, self.model, self.location, ledger=ledger)
        self.relay = LocationRelay(self.agent)
        self.agent.location_link = self.relay.issue
        self.web_sessions = set()
        secret_path = ROOT / "runtime/web-session.key"
        secret_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
        self.session_secret = secret_path.read_bytes()

    def connect(self):
        discovered = self.adapter.discover()
        self.count = len(discovered["tools"])
        self.connected = True
        self.updated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()

    def query(self, name, args):
        with self.lock:
            result = self.adapter.call(name, args)
        payload = self.adapter.business_payload(result)
        if not isinstance(payload, dict) or payload.get("success") is False or payload.get("code", 200) not in (0, 200):
            raise AdapterError("business_error", "门店暂未返回可用数据，请稍后重试或更换搜索位置")
        return payload.get("data")

    def insight_actor(self, source, web_actor):
        if source == 'web':
            return web_actor
        if source == 'weixin' and self.weixin and self.weixin.owner_actor():
            return self.weixin.owner_actor()
        raise AdapterError('invalid_source', '请先绑定微信，或选择当前网页会话')

    def dashboard(self, actor):
        s = self.agent.session(actor)
        with s.lock:
            return {'history': self.agent.insights.summary(actor), 'memory': self.agent.memory.summary(actor),
                    'phase': s.phase, 'clock': dining_clock(),
                    'model': self.model.status(), 'mcp_connected': self.connected,
                    'weixin': self.weixin.status() if self.weixin else {'state': 'unavailable'},
                    'restaurant': {'name': s.store.get('storeName'), 'business_status': s.store.get('businessStatus'),
                                   'realtime_crowding': None} if s.store else None}

    def handle(self, path, body):
        if path == "/api/settings/model":
            return self.model.save(body)
        if path == "/api/settings/model/test":
            return self.model.test()
        if path == "/api/location/start":
            return self.relay.start()
        if path == "/api/location/stop":
            return self.relay.stop()
        if path == "/api/quick-parse":
            try:
                return {"request": parse_request(body.get("text"))}
            except NeedInfo as exc:
                raise AdapterError("need_information", str(exc)) from exc
        if path.startswith("/api/weixin/"):
            if self.weixin is None:
                raise AdapterError("unavailable", "微信接收器尚未启动")
            try:
                if path == "/api/weixin/login": return self.weixin.start_login()
                if path == "/api/weixin/verify": return self.weixin.verify(body.get("code"))
                if path == "/api/weixin/resume": return self.weixin.start_worker()
                if path == "/api/weixin/pause": return self.weixin.pause()
                if path == "/api/weixin/forget": return self.weixin.forget()
                if path == "/api/weixin/memory": return self.weixin.memory_control(body)
            except WeixinError as exc:
                raise AdapterError("weixin_error", str(exc)) from exc
            raise AdapterError("not_found", "未提供此微信操作")
        with self.lock:
            if path == "/api/stores":
                city, keyword = body.get("city"), body.get("keyword")
                if not isinstance(city, str) or not isinstance(keyword, str) or not city.strip() or not keyword.strip() or len(city) > 80 or len(keyword) > 160:
                    raise AdapterError("invalid_input", "请填写城市和地标，如上海、人民广场")
                data = self.query("query-nearby-stores", {"searchType": 2, "beType": 1, "city": city.strip(), "keyword": keyword.strip()})
                if not isinstance(data, list):
                    raise AdapterError("schema_error", "门店数据格式无法识别")
                for store in data:
                    if isinstance(store, dict) and store.get("storeCode"):
                        self.stores[str(store["storeCode"])] = store
                return {"stores": data}
            code = body.get("storeCode")
            if not isinstance(code, str) or code not in self.stores:
                raise AdapterError("invalid_input", "请先搜索并选择门店")
            args = {"storeCode": code, "orderType": 1, "beType": 1}
            if path == "/api/menu":
                data = self.query("query-meals", args)
                if not isinstance(data, dict) or not isinstance(data.get("meals"), dict):
                    raise AdapterError("schema_error", "该门店菜单暂不可用，请换一家或稍后重试")
                # Do not expose the account's frequent/history fields.
                catalog = {"categories": data.get("categories", []), "meals": data["meals"]}
                self.catalogs[code] = catalog
                return {**catalog, "store": self.stores[code], "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
            if path == "/api/recommend":
                catalog = self.catalogs.get(code)
                if catalog is None:
                    raise AdapterError("invalid_input", "请先加载当前门店菜单")
                try:
                    request = parse_request(body.get("text"))
                    return {"plans": recommend(catalog, request), "request": request}
                except NeedInfo as exc:
                    raise AdapterError("need_information", str(exc)) from exc
            if path == "/api/detail":
                product_code = body.get("code")
                if not isinstance(product_code, str) or product_code not in self.catalogs.get(code, {}).get("meals", {}):
                    raise AdapterError("invalid_input", "请先加载菜单并选择其中的餐品")
                return {"detail": self.query("query-meal-detail", {**args, "code": product_code})}
            raise AdapterError("not_found", "未提供此操作")


def handler_for(app, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log bodies, tokens or private preferences.

        def send(self, status, payload, content_type="application/json; charset=utf-8"):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "geolocation=(self)")
            if getattr(self, "new_cookie", None):
                self.send_header("Set-Cookie", "maimate=" + self.new_cookie + "; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def allowed(self):
            return self.headers.get("Host") in allowed_hosts and self.headers.get("Sec-Fetch-Site") not in ("cross-site",)

        def actor(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                # Migrate the old signed cookie without changing actor hashes or
                # losing order recovery. The legacy name is compatibility only.
                sid = cookie['maimate'].value if 'maimate' in cookie else cookie['maijuzhuo'].value if 'maijuzhuo' in cookie else ''
            except Exception:
                sid = ""
            with app.lock:
                parts = sid.rsplit(".", 1)
                valid = len(parts) == 2 and 32 <= len(parts[0]) <= 64 and hmac.compare_digest(parts[1], hmac.new(app.session_secret, parts[0].encode(), hashlib.sha256).hexdigest())
                if not valid:
                    if len(app.web_sessions) >= 200:
                        raise AdapterError("session_limit", "会话过多，请重启本地服务后重试")
                    nonce = secrets.token_urlsafe(32)
                    sid = nonce + "." + hmac.new(app.session_secret, nonce.encode(), hashlib.sha256).hexdigest()
                    app.web_sessions.add(sid)
                    self.new_cookie = sid
                elif 'maimate' not in cookie:
                    self.new_cookie = sid
            return "web:" + sid.split(".", 1)[0]

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "仅允许本机访问"})
            path = urlsplit(self.path).path
            if path == "/api/status":
                return self.send(200, {"connected": app.connected, "tool_count": app.count, "updated_at": app.updated_at, "mode": "ordering_agent", "model": app.model.status()})
            if path == "/api/agent/current":
                try:
                    return self.send(200, app.agent.current(self.actor()))
                except AdapterError as exc:
                    return self.send(400, {"error": str(exc)})
            if path == '/api/agent/memory':
                try:
                    return self.send(200, {'memory': app.agent.memory.summary(self.actor())})
                except AdapterError as exc:
                    return self.send(400, {'error': str(exc)})
            if path == '/api/dashboard':
                try:
                    from urllib.parse import parse_qs
                    source = parse_qs(urlsplit(self.path).query).get('source', ['web'])[0]
                    return self.send(200, app.dashboard(app.insight_actor(source, self.actor())))
                except AdapterError as exc:
                    return self.send(400, {'error': str(exc)})
            if path == "/api/settings":
                return self.send(200, {"model": app.model.status(), "location": app.relay.status()})
            if path == "/api/weixin/status":
                return self.send(200, app.weixin.status() if app.weixin else {"state":"unavailable"})
            if path == "/api/weixin/recent":
                return self.send(200, app.weixin.recent() if app.weixin else {"turns": [], "memory": None, "notice": "微信未绑定"})
            if path == "/api/weixin/qr.svg":
                svg = app.weixin.qr_svg if app.weixin else None
                return self.send(200, svg, "image/svg+xml") if svg else self.send(404, {"error":"请先获取二维码"})
            files = {'/memory': ('memory.html', 'text/html; charset=utf-8'), '/memory.js': ('memory.js', 'text/javascript; charset=utf-8'),
                     "/weixin": ("weixin.html", "text/html; charset=utf-8"), "/weixin.js": ("weixin.js", "text/javascript; charset=utf-8"),
                     "/settings": ("settings.html", "text/html; charset=utf-8"), "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
                     "/": ("dashboard.html", "text/html; charset=utf-8"), "/chat": ("index.html", "text/html; charset=utf-8"),
                     "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"), "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8"), "/agent.css": ("agent.css", "text/css; charset=utf-8")}
            if path not in files:
                return self.send(404, {"error": "未找到页面"})
            name, kind = files[path]
            return self.send(200, (WEB / name).read_bytes(), kind)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.allowed() or origin not in {f"http://{host}" for host in allowed_hosts} or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(403, {"error": "请求来源不受支持，请从本地预览页操作"})
            path = urlsplit(self.path).path
            if path not in {"/api/agent/chat", '/api/agent/memory', '/api/insights', "/api/settings/model", "/api/settings/model/test", "/api/location/start", "/api/location/stop",
                            "/api/stores", "/api/menu", "/api/detail", "/api/quick-parse", "/api/recommend", "/api/weixin/login", "/api/weixin/verify", "/api/weixin/resume", "/api/weixin/pause", "/api/weixin/forget", "/api/weixin/memory"}:
                return self.send(404, {"error": "未提供此操作"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    return self.send(400, {"error": "请求体大小无效"})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError
                if path == "/api/agent/chat":
                    return self.send(200, app.agent.chat(self.actor(), body.get("text", ""), action=body.get("action"), payload=body.get("payload")))
                if path == '/api/insights':
                    actor = app.insight_actor(body.get('source', 'web'), self.actor())
                    operation = body.get('operation')
                    # GET never contacts account tools. Only this deliberate POST
                    # imports a bounded batch, or reads points without storing them.
                    state = app.agent.session(actor)
                    with state.lock:
                        if operation == 'refresh':
                            app.agent.insights.refresh(actor)
                        elif operation == 'clear':
                            app.agent.insights.clear(actor)
                        elif operation == 'points':
                            return self.send(200, {'reply': app.agent._account(state, 'points')['reply']})
                        else:
                            raise AdapterError('invalid_input', '请选择导入历史、删除分析或查询积分')
                    return self.send(200, app.dashboard(actor))
                if path == '/api/agent/memory':
                    actor = self.actor()
                    commands = {'clear': '清除偏好', 'enable': '开启记忆', 'disable': '关闭记忆'}
                    operation = body.get('operation')
                    if operation == 'forget' and isinstance(body.get('name'), str) and 0 < len(body['name']) <= 80:
                        text = '忘记' + body['name']
                    elif operation in commands:
                        text = commands[operation]
                    else:
                        raise AdapterError('invalid_input', '请选择有效的记忆管理操作')
                    result = app.agent.chat(actor, text)
                    return self.send(200, {'memory': result['memory'], 'notice': result['reply']})
                return self.send(200, app.handle(path, body))
            except (ValueError, UnicodeError):
                return self.send(400, {"error": "请求格式无效"})
            except AdapterError as exc:
                return self.send(502, {"error": str(exc), "code": exc.code})
            except Exception:
                return self.send(500, {"error": "操作未完成；如刚提交订单，请先查询或在官方客户端核查，勿重复提交"})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument('--resume-weixin', action='store_true', help='恢复本机已有的微信绑定；不自动开启定位隧道')
    opts = parser.parse_args()
    with McdAdapter(AdapterConfig.from_env()) as adapter:
        app = PreviewApp(adapter)
        try:
            app.connect()
        except AdapterError as exc:
            print(json.dumps({"code": exc.code, "message": str(exc)}, ensure_ascii=False), flush=True)
            return 2
        app.weixin = WeixinBridge(app)
        hosts = {f"127.0.0.1:{opts.port}", f"localhost:{opts.port}"}
        server = ThreadingHTTPServer(("127.0.0.1", opts.port), handler_for(app, hosts))
        if opts.resume_weixin and app.weixin.status().get('bound'):
            try:
                app.weixin.start_worker()
            except WeixinError:
                print('微信未能恢复，请在本机管理台重新连接。', flush=True)
        print(f"麦合拍MaiMate已启动：http://127.0.0.1:{opts.port} · 官方MCP已连接 · {app.count}工具 · 点餐智能体", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.weixin.pause()
            app.relay.stop()
            server.server_close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
