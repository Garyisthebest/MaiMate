"""Behavioral checks for persistent, correctable memory and companion chat."""
import json
import sqlite3
import time

import pytest

from confirmation import digest
from dining_memory import DiningMemory
from model_gateway import INTENT_SCHEMA
from jsonschema import Draft202012Validator
from .test_dining_memory import agent
from .test_ordering_agent import NoModel, select


def test_existing_database_migrates_without_changing_identity(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE preferences (actor_hash TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT INTO preferences VALUES (?, ?)', (digest('owner'), json.dumps({'enabled': True, 'likes': ['鸡肉'], 'dislikes': [], 'choices': {}, 'aliases': {}})))
    memory = DiningMemory(path)
    assert memory.summary('owner')['likes'] == ['鸡肉']
    assert memory.summary('owner')['insights'] == []
    memory.observe('owner', '鸡肉汉堡套餐', 'selection-a')
    memory.feedback('owner', 'like')
    restored = DiningMemory(path)
    assert restored.summary('owner')['insights'][0]['signal'] == 'positive'
    assert restored.summary('other')['insights'] == []
    assert b'owner' not in path.read_bytes()


def test_rating_updates_the_same_selection_and_rejects_stale_buttons(tmp_path):
    memory = DiningMemory(tmp_path / 'memory.sqlite3')
    memory.observe('owner', '鸡肉套餐', 'a')
    assert memory.feedback('owner', 'like', selection_id='a') == '鸡肉套餐'
    assert memory.feedback('owner', 'dislike', selection_id='a') == '鸡肉套餐'
    insight = memory.summary('owner')['insights'][0]
    assert insight['samples'] == 1 and insight['signal'] == 'negative'
    memory.observe('owner', '牛肉套餐', 'b')
    assert memory.feedback('owner', 'like', selection_id='a') is None
    assert len(memory.summary('owner')['insights']) == 1
    assert memory.feedback('other', 'like', selection_id='b') is None


def test_feedback_expires_without_overwriting_explicit_preferences(tmp_path, monkeypatch):
    memory = DiningMemory(tmp_path / 'memory.sqlite3')
    memory.learn('owner', '我喜欢鸡肉')
    memory.observe('owner', '鸡肉套餐', 'a')
    memory.feedback('owner', 'dislike')
    now = time.time()
    before = memory.summary('owner')['insights'][0]['score']
    monkeypatch.setattr('dining_memory.time.time', lambda: now + 45 * 86400)
    assert abs(memory.summary('owner')['insights'][0]['score']) < abs(before)
    monkeypatch.setattr('dining_memory.time.time', lambda: now + 91 * 86400)
    assert memory.summary('owner')['insights'] == []
    assert memory.summary('owner')['likes'] == ['鸡肉']


def test_disable_forget_clear_cover_new_memory_layers(tmp_path):
    memory = DiningMemory(tmp_path / 'memory.sqlite3')
    memory.learn('owner', '我喜欢鸡肉')
    memory.observe('owner', '鸡肉套餐', 'a')
    memory.feedback('owner', 'like')
    memory.control('owner', 'disable')
    assert memory.feedback('owner', 'dislike') is None
    assert memory.summary('owner')['insights'] == []
    memory.control('owner', 'enable')
    assert memory.forget('owner', '鸡肉')
    state = memory.summary('owner')
    assert not state['likes'] and not state['insights'] and not state['last_selection'] and not state['evidence']
    memory.observe('owner', '牛肉套餐', 'b')
    memory.feedback('owner', 'like')
    memory.control('owner', 'clear')
    state = memory.profile('owner')
    assert not state['feedback'] and state['last_selection'] is None and not state['choices']


def test_remembered_taste_overrides_model_ranking_and_has_reasons(agent):
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, text, context):
            return {'action': 'recommend', 'product_codes': ['meal-1', 'meal-2']}
    agent.chat('owner', '我喜欢鸡肉')
    agent.chat('owner', '上海人民广场')
    agent.model = Model()
    result = agent.chat('owner', '选店1')
    assert result['recommendations'][0]['code'] == 'meal-2'
    assert any('明确偏爱鸡肉' in reason for reason in result['recommendations'][0]['reasons'])
    assert not agent.app.adapter.writes


def test_once_disliked_meal_changes_next_ranking_but_not_explicit_likes(agent):
    agent.chat('owner', '上海人民广场')
    first = agent.chat('owner', '选店1')['recommendations'][0]
    agent.memory.observe('owner', first['name'], 'a')
    result = agent.chat('owner', '这餐不喜欢')
    assert result['memory']['insights'][0]['signal'] == 'negative'
    assert result['memory']['dislikes'] == []
    ranked = agent.chat('owner', '推荐套餐')['recommendations']
    assert ranked[0]['name'] != first['name']
    assert not agent.app.adapter.writes


def test_chat_preserves_preview_and_does_not_apply_model_changes(agent):
    original = select(agent)
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, text, context):
            return {'action': 'chat', 'question': '那今天就慢慢选，想吃点熟悉的还是试试新的？', 'count': 10, 'preference': '不应该保存'}
    agent.model = Model()
    result = agent.chat('owner', '今天有点累，想随便聊聊')
    assert result['cart']['preview_id'] == original['cart']['preview_id']
    assert result['cart']['quantity'] == original['cart']['quantity']
    assert '慢慢选' in result['reply']
    assert not agent.app.adapter.writes


@pytest.mark.parametrize('reply', ['已经帮你下单了', '我帮你查询价格', '取餐码是A123', '特价19元', 'https://example.invalid/pay'])
def test_smalltalk_does_not_claim_unperformed_business_actions(agent, reply):
    result = agent._intent(agent.session('owner'), {'action': 'chat', 'question': reply}, '聊聊')
    assert reply not in result['reply']
    assert not agent.app.adapter.writes


def test_feedback_still_works_after_order_creation_and_can_be_revised(agent):
    select(agent)
    agent.chat('owner', '确认下单')
    writes = len(agent.app.adapter.writes)
    first = agent.chat('owner', '这餐好吃')
    assert first['order']
    assert first['memory']['insights'][0]['samples'] == 1
    second = agent.chat('owner', '这餐一般')
    assert second['memory']['insights'][0]['samples'] == 1
    assert second['memory']['insights'][0]['signal'] == 'mixed'
    assert agent.chat('owner', '我更中意薯条')['memory']['likes'] == ['薯条']
    assert len(agent.app.adapter.writes) == writes


def test_no_memory_does_not_pretend_to_know_and_disabled_memory_is_not_sent(agent):
    assert '没有足够' in agent.chat('owner', '按老样子')['reply']
    agent.chat('owner', '我喜欢牛肉')
    agent.chat('owner', '关闭记忆')
    assert agent.session('owner').context()['preferences'] == {'enabled': False}
    assert agent.session('owner').context()['history'][-2]['text'] == '关闭记忆'


def test_chat_is_in_strict_intent_schema():
    assert not list(Draft202012Validator(INTENT_SCHEMA).iter_errors({'action': 'chat', 'question': '今天想聊什么？'}))
    assert list(Draft202012Validator(INTENT_SCHEMA).iter_errors({'action': 'chat', 'save_memory': 'anything'}))


def test_natural_feedback_only_learns_the_users_own_report(agent):
    agent.memory.observe('owner', '鸡肉汉堡套餐', 'a')
    assert agent.chat('owner', '上一餐不好吃，以后少推荐一点')['memory']['insights'][0]['signal'] == 'negative'
    before = agent.memory.profile('owner')['feedback']
    agent.chat('owner', '这个套餐好吃吗？')
    agent.chat('owner', '如果这餐好吃就好了')
    assert agent.memory.profile('owner')['feedback'] == before


def test_current_food_exclusion_beats_long_term_like(agent):
    agent.chat('owner', '我喜欢鸡肉')
    agent.chat('owner', '上海人民广场')
    agent.chat('owner', '选店1')
    result = agent.chat('owner', '今天不要鸡肉，想吃牛肉')
    assert all('鸡' not in item['name'] for item in result['recommendations'])
    assert agent.memory.summary('owner')['likes'] == ['鸡肉']


def test_preference_correction_keeps_updated_source_and_removes_contradiction(tmp_path):
    memory=DiningMemory(tmp_path/'memory.sqlite3')
    memory.learn('owner','我喜欢无糖饮料')
    memory.learn('owner','我现在不太喜欢无糖饮料')
    summary=memory.summary('owner')
    assert '无糖饮料' not in summary['likes']
    assert summary['evidence']['无糖饮料']['polarity']=='dislike'
    assert summary['evidence']['无糖饮料']['source']=='explicit'


def test_cookie_migration_keeps_identity_and_memory_routes_are_local(tmp_path, monkeypatch):
    import hashlib
    import hmac
    import threading
    from http.server import ThreadingHTTPServer
    import httpx
    import preview_server
    from preview_server import PreviewApp, handler_for
    from confirmation import ConfirmationLedger
    from .test_ordering_agent import Adapter
    monkeypatch.setattr(preview_server, 'ROOT', tmp_path)
    app = PreviewApp(Adapter(), model=NoModel(), ledger=ConfirmationLedger(tmp_path/'ledger.sqlite3'))
    nonce = 'a' * 43
    sid = nonce + '.' + hmac.new(app.session_secret, nonce.encode(), hashlib.sha256).hexdigest()
    app.agent.memory.learn('web:' + nonce, '我喜欢鸡肉')
    hosts = set()
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app, hosts))
    host = f'127.0.0.1:{server.server_port}'
    hosts.add(host)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url='http://' + host, trust_env=False) as client:
            response = client.get('/api/agent/memory', headers={'Cookie': 'maijuzhuo=' + sid})
            assert response.json()['memory']['likes'] == ['鸡肉']
            assert 'maimate=' + sid in response.headers['set-cookie']
            assert client.get('/api/agent/memory', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
            assert client.post('/api/agent/memory', headers={'Origin': 'https://example.invalid'}, json={'operation': 'clear'}).status_code == 403
            response = client.post('/api/agent/memory', headers={'Origin': 'http://' + host}, json={'operation': 'forget', 'name': '鸡肉'})
            assert response.status_code == 200 and response.json()['memory']['likes'] == []
            assert app.agent.memory.summary('web:' + nonce)['likes'] == []
    finally:
        server.shutdown()
        server.server_close()


def test_repeated_explicit_preference_is_acknowledged_without_ordering(agent):
    agent.chat('owner','我喜欢无糖饮料')
    result=agent.chat('owner','我喜欢无糖饮料')
    assert '记住' in result['reply']
    assert result['memory']['likes']==['无糖饮料']
    assert not agent.app.adapter.calls and not agent.app.adapter.writes


def test_exploration_replaces_previous_family_request_but_keeps_drink_need(agent):
    agent.memory.learn('owner','我爱吃精选超值随心配，喜欢无糖饮料', ['精选超值随心配'])
    agent.chat('owner','上海人民广场，想吃精选超值随心配')
    first=agent.chat('owner','选店1')
    assert [v['code'] for v in first['recommendations']]==['value-1']
    result=agent.chat('owner','换换口味')
    assert result['recommendations'][0]['code']!='value-1'
    assert '无糖' in agent.session('owner').request['preference']
    assert '精选超值随心配' in agent.memory.summary('owner')['likes']
