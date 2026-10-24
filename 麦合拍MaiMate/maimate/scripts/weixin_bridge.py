"""Project-local Weixin text bot, following Tencent/openclaw-weixin 2.4.10.

This is a compatible Python bridge, not the official OpenClaw plugin. Only the
QR-binding owner may talk to it; no group access or proactive messages.
Orders use the shared engine's explicit preview/confirmation gate. No raw
messages or credentials enter logs.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import qrcode
import qrcode.image.svg

from ordering_agent import AgentConversation
from mcd_adapter import ROOT

BASE_URL='https://ilinkai.weixin.qq.com'
CHANNEL_VERSION='2.4.10'
CLIENT_VERSION=str((2<<16)|(4<<8)|10)


class WeixinError(RuntimeError):
    pass


def safe_base(value: str) -> str:
    parsed=urlparse(value)
    host=parsed.hostname or ''
    if parsed.scheme!='https' or not (host=='ilinkai.weixin.qq.com' or host.endswith('.weixin.qq.com')) or parsed.username or parsed.password or parsed.port not in (None,443) or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise WeixinError('微信返回的服务地址无法验证，已停止连接')
    return value.rstrip('/')


def private_json(path: Path, value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.chmod(temp,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as out:
        json.dump(value,out,ensure_ascii=False)
    temp.replace(path)


class WeixinAPI:
    def __init__(self, client=None):
        self.client=client or httpx.Client(timeout=20,follow_redirects=False)

    def call(self,path,*,base=BASE_URL,token=None,body=None,params=None,timeout=20):
        base=safe_base(base)
        headers={'iLink-App-Id':'bot','iLink-App-ClientVersion':CLIENT_VERSION}
        if body is not None:
            headers.update({'Content-Type':'application/json','AuthorizationType':'ilink_bot_token',
                            'X-WECHAT-UIN':base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
            if token: headers['Authorization']='Bearer '+token
        try:
            response=self.client.request('POST' if body is not None else 'GET',base+'/'+path.lstrip('/'),
                                         headers=headers,json=body,params=params,timeout=timeout)
            if response.status_code!=200:
                raise WeixinError(f'微信服务HTTP {response.status_code}，请稍后再试')
            data=response.json()
            if not isinstance(data,dict):raise ValueError
            if data.get('ret',0)!=0 or data.get('errcode',0)!=0:
                if -14 in (data.get('ret'),data.get('errcode')):
                    raise WeixinError('微信授权已失效，请重新扫码连接')
                raise WeixinError('微信服务暂未接受这次请求，未自动重发消息')
            return data
        except httpx.TimeoutException:
            raise WeixinError('微信请求等待超时') from None
        except (httpx.HTTPError,ValueError):
            raise WeixinError('微信连接或数据解析失败') from None

    @staticmethod
    def base_info():
        return {'channel_version':CHANNEL_VERSION,'bot_agent':'MaiMate/0.3.0'}


class WeixinBridge:
    def __init__(self,app,state_dir:Path|None=None,api=None):
        self.app=app
        self.root=state_dir or ROOT/'runtime/weixin'
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(self.root,0o700)
        self.api=api or WeixinAPI()
        self.lock=threading.RLock()
        self.login_lock=threading.Lock()
        self.worker_stop=threading.Event()
        self.login_stop=threading.Event()
        self.worker=None
        self.login_thread=None
        self.account=None
        self.qr=None
        self.qr_svg=None
        self.login_base=BASE_URL
        self.pending_code=None
        self.state='disconnected'
        self.notice='连接后，在微信里发一句午餐需求即可。'
        self.last_inbound=None
        self.last_reply=None
        self.reply_count=0
        self.started_at=time.time()
        self.conversation=AgentConversation(app)
        self.cursor=''
        self.account_path=self.root/'account.json'
        self.dbpath=self.root/'delivery.sqlite3'
        fd=os.open(self.dbpath,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
        os.chmod(self.dbpath,0o600)
        with sqlite3.connect(self.dbpath) as db:
            db.execute('CREATE TABLE IF NOT EXISTS deliveries (message_hash TEXT PRIMARY KEY, status TEXT, created REAL)')
            # Hash-only dedup; no text, recipient or context token stored here.
            db.execute('DELETE FROM deliveries WHERE created < ?', (time.time()-30*86400,))
        if self.account_path.exists():
            try:
                account=json.loads(self.account_path.read_text())
                if not all(isinstance(account.get(k),str) and account[k] for k in ('bot_token','bot_id','owner_id','base_url')):raise ValueError
                safe_base(account['base_url'])
                self.account=account
                self.state='paused'
                self.notice='已有微信绑定，点击恢复接收即可。'
                cursor_path=self.root/'cursor.json'
                if cursor_path.exists():self.cursor=json.loads(cursor_path.read_text()).get('cursor','')
            except (ValueError,OSError,WeixinError):
                self.notice='本地微信凭证无法读取，请重新扫码。'

    def status(self):
        with self.lock:
            return {'state':self.state,'notice':self.notice,'bound':bool(self.account),
                    'qr_available':bool(self.qr_svg),'last_inbound':self.last_inbound,
                    'last_reply':self.last_reply,'reply_count':self.reply_count,
                    'owner_only':True,'mode':'ordering_agent'}

    def owner_actor(self):
        with self.lock:
            return 'weixin:' + hashlib.sha256(self.account['owner_id'].encode()).hexdigest() if self.account else None

    def recent(self):
        actor = self.owner_actor()
        if not actor or not getattr(self.app, 'agent', None):
            return {'turns': [], 'memory': None, 'notice': '先绑定微信；只显示本次服务运行期间的新对话，无法读取微信旧聊天。'}
        return self.app.agent.recent(actor)

    def memory_control(self, body):
        actor = self.owner_actor()
        if not actor or not getattr(self.app, 'agent', None):
            raise WeixinError('请先绑定微信')
        commands = {'clear': '清除偏好', 'enable': '开启记忆', 'disable': '关闭记忆'}
        command = commands.get(body.get('operation'))
        if body.get('operation') == 'forget' and isinstance(body.get('name'), str) and 0 < len(body['name']) <= 80:
            command = '忘记' + body['name']
        if command is None:
            raise WeixinError('偏好操作不可用')
        result = self.app.agent.chat(actor, command)
        return {'memory': self.app.agent.memory.summary(actor), 'notice': result['reply']}

    def start_login(self):
        if not self.login_lock.acquire(blocking=False):
            raise WeixinError('二维码正在获取，请稍等')
        try:
            with self.lock:
                if self.account:
                    raise WeixinError('已有绑定，请先断开并删除本地绑定再更换账号')
                if self.login_thread and self.login_thread.is_alive():
                    return self.status()
            data=self.api.call('ilink/bot/get_bot_qrcode',body={'local_token_list':[]},params={'bot_type':'3'})
            if not isinstance(data.get('qrcode'),str) or not isinstance(data.get('qrcode_img_content'),str):
                raise WeixinError('微信未返回有效二维码')
            # QR content is encoded locally, not fetched as a remote image.
            qr_content=data['qrcode_img_content']
            if len(qr_content)>12000: raise WeixinError('微信二维码内容异常')
            output=io.BytesIO()
            qrcode.make(qr_content,image_factory=qrcode.image.svg.SvgPathImage,box_size=8,border=4).save(output)
            with self.lock:
                self.qr=data['qrcode'];self.qr_svg=output.getvalue()
                self.login_base=BASE_URL;self.pending_code=None
                self.login_stop=threading.Event()
                self.state='waiting_scan';self.notice='用手机微信扫描二维码，并在手机上确认连接。'
                self.login_thread=threading.Thread(target=self._poll_login,name='weixin-qr-login',daemon=True)
                self.login_thread.start()
            return self.status()
        finally:
            self.login_lock.release()

    def verify(self,code):
        if not isinstance(code,str) or not code.isdigit() or not 2<=len(code)<=12:
            raise WeixinError('请填写手机微信上显示的数字')
        with self.lock:
            if self.state!='need_verifycode':raise WeixinError('当前未要求输入配对数字')
            self.pending_code=code
            self.notice='正在核验手机显示的数字…'
        return self.status()

    def _poll_login(self):
        deadline=time.monotonic()+300
        errors=0
        while time.monotonic()<deadline and not self.login_stop.is_set():
            with self.lock:
                code=self.pending_code
                self.pending_code=None
                base=self.login_base;qr=self.qr
                waiting=self.state=='need_verifycode' and not code
            if waiting:
                self.login_stop.wait(.5);continue
            try:
                params={'qrcode':qr}
                if code:params['verify_code']=code
                data=self.api.call('ilink/bot/get_qrcode_status',base=base,params=params,timeout=35)
                if self.login_stop.is_set():return
                status=data.get('status');errors=0
                with self.lock:
                    if status=='confirmed':
                        if not all(isinstance(data.get(k),str) and data[k] for k in ('bot_token','ilink_bot_id','ilink_user_id')):
                            raise WeixinError('微信未返回完整绑定身份，已停止自动回复')
                        account={'bot_token':data['bot_token'],'bot_id':data['ilink_bot_id'],
                                 'owner_id':data['ilink_user_id'],'base_url':safe_base(data.get('baseurl') or BASE_URL)}
                        private_json(self.account_path,account)
                        self.account=account;self.qr=None;self.qr_svg=None
                        self.state='paused';self.notice='绑定成功。只回复扫码绑定者的新消息。'
                        self.start_worker()
                        return
                    if status=='scaned_but_redirect':
                        host=data.get('redirect_host')
                        if isinstance(host,str): self.login_base=safe_base('https://'+host)
                    elif status=='need_verifycode':
                        self.state='need_verifycode';self.notice='请在下面输入手机微信上显示的配对数字。'
                    elif status in ('expired','verify_code_blocked','binded_redirect'):
                        self.state='expired';self.qr_svg=None
                        self.notice={'expired':'二维码已过期，请重新获取。','verify_code_blocked':'配对数字多次不符，请稍后重新获取二维码。','binded_redirect':'微信提示已绑定其他本地实例，请核查原绑定后重试。'}[status]
                        return
                    elif status=='scaned':
                        self.state='scanned';self.notice='已扫码，请在手机上确认授权。'
                self.login_stop.wait(1)
            except WeixinError:
                errors+=1
                if errors>=5:
                    with self.lock:self.state='error';self.notice='微信扫码状态查询连续失败，请重新获取二维码。';self.qr_svg=None
                    return
                self.login_stop.wait(3)
        with self.lock:
            if not self.account and not self.login_stop.is_set():
                self.state='expired';self.notice='二维码已过期，请重新获取。';self.qr_svg=None

    def start_worker(self):
        with self.lock:
            if not self.account: raise WeixinError('请先扫码绑定微信')
            if self.worker and self.worker.is_alive():
                if self.worker_stop.is_set():raise WeixinError('接收器正在停止，请数秒后恢复')
                return self.status()
            self.worker_stop=threading.Event()
            self.started_at=time.time()
            self.state='connected';self.notice='已开启：只回复扫码绑定者，支持文字和微信已转写的语音。'
            self.worker=threading.Thread(target=self._run,name='weixin-lunch-receiver',daemon=True)
            self.worker.start()
            return self.status()

    def pause(self):
        with self.lock:
            self.worker_stop.set();self.login_stop.set();self.qr_svg=None;self.qr=None
            self.state='paused' if self.account else 'disconnected';self.notice='已暂停微信回复。'
        return self.status()

    def forget(self):
        self.pause()
        with self.lock:
            self.account=None;self.cursor=''
            self.conversation=AgentConversation(self.app)
            for path in (self.account_path,self.root/'cursor.json'):
                path.unlink(missing_ok=True)
            self.state='disconnected';self.notice='本地绑定凭证已删除；微信端连接也可在手机内移除。'
        return self.status()

    def _claim(self,account_id,message_id):
        hashed=hashlib.sha256((account_id+'\0'+message_id).encode()).hexdigest()
        with sqlite3.connect(self.dbpath) as db:
            count=db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?)',(hashed,'claimed',time.time())).rowcount
        return hashed if count else None

    def process_message(self,msg,account):
        if not isinstance(msg,dict) or msg.get('message_type')!=1 or msg.get('group_id'):
            return False
        if msg.get('from_user_id')!=account['owner_id'] or not isinstance(msg.get('context_token'),str) or not msg['context_token']:
            return False
        if msg.get('to_user_id') and msg['to_user_id']!=account['bot_id']:
            return False
        created=msg.get('create_time_ms')
        if not isinstance(created,(int,float)) or created/1000<self.started_at-60:
            return False
        ident=msg.get('message_id')
        if ident is None or not isinstance(ident,(str,int)):return False
        texts=[]
        for item in msg.get('item_list',[]):
            if not isinstance(item,dict):continue
            source=item.get('text_item') if item.get('type')==1 else item.get('voice_item') if item.get('type')==3 else None
            if isinstance(source,dict) and isinstance(source.get('text'),str):texts.append(source['text'])
        text='\n'.join(texts).strip()
        if not text:return False
        claimed=self._claim(account['bot_id'],str(ident))
        if not claimed:return False
        self.last_inbound=time.time()
        self.conversation.actor='weixin:'+hashlib.sha256(account['owner_id'].encode()).hexdigest()
        if len(text)>2000:reply='请用2000字以内描述需求，例如“我到店了，推荐牛肉套餐，饮料无糖”。'
        else:reply=self.conversation.reply(text)
        if self.worker_stop.is_set() or self.account!=account:
            return False
        body={'msg':{'from_user_id':'','to_user_id':account['owner_id'],
                     'client_id':'maimate-'+uuid.uuid4().hex,'message_type':2,'message_state':2,
                     'context_token':msg['context_token'],
                     'item_list':[{'type':1,'text_item':{'text':reply[:3800]}}]},'base_info':self.api.base_info()}
        try:
            self.api.call('ilink/bot/sendmessage',base=account['base_url'],token=account['bot_token'],body=body)
        except Exception:
            with sqlite3.connect(self.dbpath) as db:db.execute('UPDATE deliveries SET status=? WHERE message_hash=?',('unknown',claimed))
            raise
        with sqlite3.connect(self.dbpath) as db:db.execute('UPDATE deliveries SET status=? WHERE message_hash=?',('sent',claimed))
        self.last_reply=time.time();self.reply_count+=1
        return True

    def _run(self):
        account=self.account
        failures=0
        while not self.worker_stop.is_set() and self.account==account:
            try:
                data=self.api.call('ilink/bot/getupdates',base=account['base_url'],token=account['bot_token'],
                    body={'get_updates_buf':self.cursor,'base_info':self.api.base_info()},timeout=40)
                if self.worker_stop.is_set() or self.account!=account:return
                msgs=data.get('msgs',[])
                if not isinstance(msgs,list):raise WeixinError('微信消息格式异常')
                for msg in msgs:
                    if self.worker_stop.is_set():return
                    self.process_message(msg,account)
                cursor=data.get('get_updates_buf')
                if isinstance(cursor,str) and cursor:
                    self.cursor=cursor
                    private_json(self.root/'cursor.json',{'cursor':cursor})
                failures=0
                # Avoid a busy loop when the backend immediately returns an empty page.
                self.worker_stop.wait(.3)
            except WeixinError as exc:
                if self.worker_stop.is_set() or self.account!=account:return
                if '授权已失效' in str(exc):
                    with self.lock:
                        if self.account!=account:return
                        self.state='session_expired';self.notice='微信授权已失效，请删除本地绑定后重新扫码。'
                    return
                failures+=1
                with self.lock:self.notice='微信连接暂时波动，正在等待恢复；未自动重发失败的回复。'
                self.worker_stop.wait(min(30,2**min(failures,5)))
            except Exception:
                if self.worker_stop.is_set() or self.account!=account:return
                failures+=1
                with self.lock:self.notice='消息处理暂未完成，稍后可重发需求；不会自动创建订单。'
                self.worker_stop.wait(min(30,2**min(failures,5)))
