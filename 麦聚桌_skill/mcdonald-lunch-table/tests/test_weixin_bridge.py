import json
import time
from pathlib import Path
import httpx
import pytest

from weixin_bridge import WeixinBridge, WeixinAPI, WeixinError, safe_base


class FakeAPI:
    def __init__(self):self.calls=[];self.fail=False
    @staticmethod
    def base_info():return {'channel_version':'2.4.10','bot_agent':'Maijuzhuo/0.3.0'}
    def call(self,path,**kwargs):
        self.calls.append((path,kwargs))
        if self.fail:raise WeixinError('reply response lost')
        return {'ret':0}


class FakeApp:
    def handle(self,*args):raise AssertionError('help should not call MCP')


def setup_bridge(tmp_path):
    api=FakeAPI();bridge=WeixinBridge(FakeApp(),state_dir=tmp_path,api=api)
    account={'owner_id':'owner','bot_id':'bot','bot_token':'fake-test-token','base_url':'https://ilinkai.weixin.qq.com'}
    bridge.account=account
    message={'message_id':'100','message_type':1,'from_user_id':'owner','to_user_id':'bot',
             'context_token':'context-fixture','create_time_ms':time.time()*1000,
             'item_list':[{'type':1,'text_item':{'text':'帮助'}}]}
    return bridge,api,account,message


@pytest.mark.parametrize('updates',[
    {'from_user_id':'other'}, {'group_id':'group'}, {'message_type':2},
    {'context_token':''},{'to_user_id':'different-bot'}, {'create_time_ms':1},
    {'message_id':None}, {'item_list':[{'type':2,'image_item':{'url':'https://example.invalid'}}]},
])
def test_only_new_bound_owner_text_with_context_is_processed(tmp_path,updates):
    bridge,api,account,message=setup_bridge(tmp_path)
    message.update(updates)
    assert bridge.process_message(message,account) is False
    assert api.calls==[]


def test_reply_uses_inbound_context_and_claim_deduplicates_after_restart(tmp_path):
    bridge,api,account,message=setup_bridge(tmp_path)
    assert bridge.process_message(message,account)
    assert not bridge.process_message(message,account)
    assert len(api.calls)==1
    path,options=api.calls[0]
    assert path=='ilink/bot/sendmessage'
    assert options['body']['msg']['context_token']=='context-fixture'
    assert options['body']['msg']['to_user_id']=='owner'
    assert '3人' in options['body']['msg']['item_list'][0]['text_item']['text']
    second=WeixinBridge(FakeApp(),state_dir=tmp_path,api=api);second.account=account
    assert not second.process_message(message,account)
    assert len(api.calls)==1
    db_bytes=(tmp_path/'delivery.sqlite3').read_bytes()
    assert b'fake-test-token' not in db_bytes and b'context-fixture' not in db_bytes
    assert (tmp_path/'delivery.sqlite3').stat().st_mode & 0o777 == 0o600


def test_lost_send_reply_is_not_automatically_resent(tmp_path):
    bridge,api,account,message=setup_bridge(tmp_path);api.fail=True
    with pytest.raises(WeixinError):bridge.process_message(message,account)
    api.fail=False
    assert not bridge.process_message(message,account)
    assert len(api.calls)==1


def test_status_never_contains_credentials_or_identity(tmp_path):
    bridge,api,account,message=setup_bridge(tmp_path)
    status=json.dumps(bridge.status())
    assert 'fake-test-token' not in status and 'context-fixture' not in status and 'owner_id' not in status


@pytest.mark.parametrize('value',[
    'http://ilinkai.weixin.qq.com','https://evil.test','https://weixin.qq.com.evil.test',
    'https://ilinkai.weixin.qq.com@evil.test','https://ilinkai.weixin.qq.com/private',
    'https://ilinkai.weixin.qq.com:444','https://ilinkai.weixin.qq.com?token=private',
])
def test_credentials_cannot_follow_an_arbitrary_service_host(value):
    with pytest.raises(WeixinError):safe_base(value)


def test_official_protocol_headers_and_errors_are_redacted():
    seen=[]
    def handler(req):
        seen.append(req)
        if len(seen)==1:return httpx.Response(200,json={'ret':0,'qrcode':'fake','qrcode_img_content':'https://example.invalid/qr'})
        return httpx.Response(403,text='fake-token should never appear')
    api=WeixinAPI(httpx.Client(transport=httpx.MockTransport(handler)))
    api.call('ilink/bot/get_bot_qrcode',body={'local_token_list':[]},params={'bot_type':'3'})
    assert seen[0].headers['AuthorizationType']=='ilink_bot_token'
    assert seen[0].headers['iLink-App-Id']=='bot'
    assert 'Authorization' not in seen[0].headers
    with pytest.raises(WeixinError) as error:
        api.call('ilink/bot/getupdates',token='fake-token',body={})
    assert 'fake-token' not in str(error.value)
    assert seen[1].headers['Authorization']=='Bearer fake-token'
