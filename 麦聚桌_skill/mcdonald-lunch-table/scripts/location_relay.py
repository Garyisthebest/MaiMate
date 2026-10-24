"""An isolated, one-use GPS handoff for Weixin. Never exposes the app/settings."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from mcd_adapter import AdapterError, ROOT

HTML = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>麦聚桌 · 定位门店</title><link rel="stylesheet" href="/locate.css">
<main><div class="logo">聚</div><h1>你在哪家店？</h1><p>授权本次位置，自动查找所在区域的麦当劳。只为这次点餐使用。</p>
<button id="locate">使用当前位置</button><p id="notice" role="status">定位时将坐标发给 Photon／OpenStreetMap 解析地址，再向麦当劳查询门店。</p>
<pre id="result"></pre><p>找到门店后，回微信回复门店编号，例如“选店1”。</p></main><script src="/locate.js"></script></html>'''
CSS = '''*{box-sizing:border-box}body{margin:0;background:#f4f5f7;color:#242424;font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:540px;margin:8vh auto;padding:32px;background:white;border-radius:24px}.logo{width:48px;height:48px;display:grid;place-items:center;background:#ffbc0d;border-radius:14px;font-size:25px;font-weight:800}h1{font-size:30px}button{border:0;border-radius:12px;background:#c82028;color:white;font:inherit;font-weight:700;min-height:52px;padding:10px 24px;width:100%}button:disabled{opacity:.5}pre{white-space:pre-wrap;font:inherit}#notice{font-size:13px;color:#606773}p{overflow-wrap:anywhere}@media(max-width:560px){main{margin:0;min-height:100vh;border-radius:0;padding:30px 24px}}'''
JS = '''const button=document.getElementById('locate'),notice=document.getElementById('notice'),result=document.getElementById('result');button.onclick=()=>{if(!navigator.geolocation){notice.textContent='当前浏览器不支持定位。请在手机系统浏览器中打开。';return;}button.disabled=true;notice.textContent='正在获取本次位置，请允许定位…';navigator.geolocation.getCurrentPosition(async p=>{try{notice.textContent='正在查找门店…';const r=await fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({latitude:p.coords.latitude,longitude:p.coords.longitude})});const data=await r.json();if(!r.ok)throw Error(data.error||'定位未完成');result.textContent=data.reply;notice.textContent='位置已使用一次，此链接已失效。回到微信选门店即可。';}catch(e){notice.textContent=e.message+'。回微信发送“定位”可获取新链接。';}},e=>{button.disabled=false;notice.textContent=e.code===1?'定位权限未开启，请允许定位，或直接在微信发送门店名。':'暂时无法取得位置，请在手机系统浏览器打开后重试。';},{enableHighAccuracy:true,timeout:20000,maximumAge:30000});};'''


class LocationRelay:
    def __init__(self, agent, port=8766):
        self.agent, self.port = agent, port
        self.public_url = ''
        self.tokens = {}
        self.lock = threading.RLock()
        self.process = None
        self.server = None
        self.state = 'stopped'

    def issue(self, actor):
        with self.lock:
            self.status()
            if not self.public_url:
                return None
            now = time.time()
            self.tokens = {k: v for k, v in self.tokens.items() if v['expires'] > now and v['actor'] != actor}
            token = secrets.token_urlsafe(32)
            self.tokens[hashlib.sha256(token.encode()).hexdigest()] = {'actor': actor, 'expires': now + 300}
            return self.public_url + '/location/' + token

    def lookup(self, token, consume=False):
        with self.lock:
            key = hashlib.sha256(token.encode()).hexdigest()
            value = self.tokens.get(key)
            if not value or value['expires'] <= time.time():
                raise AdapterError('location_expired', '定位链接无效、已使用或已过期，请回微信发送“定位”获取新链接')
            if consume:
                del self.tokens[key]
            return value['actor']

    def start_server(self):
        if self.server:
            return
        relay = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def send(self, status, value, content_type='application/json; charset=utf-8'):
                raw = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                for k, v in {'Content-Type': content_type, 'Content-Length': str(len(raw)), 'Cache-Control': 'no-store',
                             'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
                             'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                             'Permissions-Policy': 'geolocation=(self)'}.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == '/locate.js':
                    return self.send(200, JS, 'text/javascript; charset=utf-8')
                if path == '/locate.css':
                    return self.send(200, CSS, 'text/css; charset=utf-8')
                match = re.fullmatch(r'/location/([A-Za-z0-9_-]{40,64})', path)
                if not match:
                    return self.send(404, {'error': '未提供此页面'})
                try:
                    relay.lookup(match[1])
                    return self.send(200, HTML, 'text/html; charset=utf-8')
                except AdapterError as exc:
                    return self.send(410, {'error': str(exc)})
            def do_POST(self):
                match = re.fullmatch(r'/location/([A-Za-z0-9_-]{40,64})', urlsplit(self.path).path)
                if not match:
                    return self.send(404, {'error': '未提供此操作'})
                if self.headers.get('Origin') != relay.public_url or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.send(403, {'error': '请从原定位页面操作'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 2048:
                        raise ValueError
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError
                    actor = relay.lookup(match[1], consume=True)
                    data = relay.agent.chat(actor, action='locate', payload=body)
                    return self.send(200, {'reply': data['reply']})
                except (ValueError, TypeError):
                    return self.send(400, {'error': '定位请求无效'})
                except AdapterError as exc:
                    return self.send(410, {'error': str(exc)})
                except Exception:
                    return self.send(500, {'error': '定位未完成，请获取新链接后重试'})
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        threading.Thread(target=self.server.serve_forever, name='weixin-location-relay', daemon=True).start()

    def status(self):
        if self.process and self.process.poll() is not None:
            self.state, self.public_url = 'stopped', ''
        return {'state': self.state, 'enabled': bool(self.public_url), 'public_url': self.public_url,
                'binary_ready': (ROOT / 'runtime/bin/cloudflared').is_file()}

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return self.status()
            binary = ROOT / 'runtime/bin/cloudflared'
            if not binary.is_file():
                raise AdapterError('tunnel_missing', '定位通道组件尚未安装，请运行项目内 install-location-tunnel.py')
            self.start_server()
            directory = ROOT / 'runtime/location'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            config = directory / 'cloudflared.yml'
            config.write_text('{}\n')
            log = directory / 'tunnel.log'
            handle = open(log, 'w')
            os.chmod(log, 0o600)
            self.process = subprocess.Popen([str(binary), '--no-autoupdate', '--config', str(config), 'tunnel',
                                              '--url', f'http://127.0.0.1:{self.port}', '--protocol', 'http2'],
                                             cwd=ROOT, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                             env={**os.environ, 'TMPDIR': str(ROOT / 'runtime/tmp')})
            handle.close()
            self.state = 'starting'
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            with self.lock:
                if self.process.poll() is not None:
                    self.state = 'error'
                    raise AdapterError('tunnel_error', '定位通道启动失败，请查看项目 runtime/location/tunnel.log')
                log_text = log.read_text()
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', log_text)
                if match and 'Registered tunnel connection' in log_text:
                    self.public_url = match[0]
                    self.state = 'running'
                    return self.status()
            time.sleep(.2)
        self.stop()
        raise AdapterError('tunnel_error', '暂时没有取得微信可用的 HTTPS 定位地址，请稍后重试')

    def stop(self):
        with self.lock:
            self.public_url = ''
            self.tokens.clear()
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process = None
            self.state = 'stopped'
            return self.status()
