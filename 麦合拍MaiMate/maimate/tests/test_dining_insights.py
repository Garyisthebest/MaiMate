import copy
import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from dining_insights import DiningInsights, dining_clock
from model_gateway import INTENT_SCHEMA
from preview_server import PreviewApp, handler_for
from http.server import ThreadingHTTPServer
from .test_ordering_agent import agent, select, NoModel


def completed(ident='private-id', name='牛肉汉堡套餐', status='订单已完成', days=1):
    return {'orderId': ident, 'createTime': (datetime.now(ZoneInfo('Asia/Shanghai'))-timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S'),
            'orderStatus': status, 'realTotalAmount': '28.90', 'storeName': 'private-store',
            'mobilePhone': 'private-phone', 'deliveryAddress': 'private-address',
            'orderProductList': [{'productName': name, 'quantity': 3, 'comboItemList': [{'name': 'private-child'}]}]}


def attach(agent, rows):
    query = agent.app.query
    def call(name, args):
        if name == 'order-list':
            assert args == {}
            return {'list': rows}
        if name == 'query-my-account':
            return {'availablePoint': '900', 'accountId': 'private-account-id'}
        return query(name, args)
    agent.app.query = call


def test_history_bounded_completed_unique_yuan_and_reversible(agent):
    a = completed('one')
    rows = [a, a, completed('two'), completed('cancelled', status='订单已取消'),
            completed('pending', status='待支付'), completed('unknown', status='6'), completed('old', days=100)]
    attach(agent, rows)
    data = agent.insights.refresh('owner')
    assert data['orders'] == 2 and data['total_cents'] == 5780
    assert data['foods'] == [{'name': '牛肉汉堡套餐', 'orders': 2, 'source': 'completed_orders'}]
    assert data['skipped'] == 5
    raw = agent.insights.path.read_bytes()
    for secret in ('private-id', 'private-store', 'private-phone', 'private-address', 'private-child', 'fake-test-key'):
        assert secret.encode() not in raw
    assert agent.insights.summary('other')['imported_at'] is None
    agent.app.adapter.config.key = 'different-token'
    assert agent.insights.summary('owner')['imported_at'] is None
    agent.app.adapter.config.key = 'fake-test-key'
    agent.chat('owner', '清除历史订单分析')
    assert not agent.insights.model_summary('owner')


def test_import_limits_and_does_not_fan_out(agent):
    attach(agent, [completed(str(i)) for i in range(100)])
    data = agent.insights.refresh('owner')
    assert data['examined'] == data['orders'] == 30
    assert not agent.app.adapter.calls


def test_history_account_and_restaurant_do_not_interrupt_cart_or_unknown(agent):
    cart = select(agent)
    ident = cart['cart']['preview_id']
    attach(agent, [completed('1'), completed('2')])
    history = agent.chat('owner', '分析历史订单')
    assert history['cart']['preview_id'] == ident and history['can_confirm']
    assert agent.chat('owner', '我的积分')['cart']['preview_id'] == ident
    status = agent.chat('owner', '这家店现在空不空？')
    assert '营业中' in status['reply'] and '官方未提供' in status['reply']
    assert status['cart']['preview_id'] == ident
    state = agent.session('owner')
    ctx = json.dumps(state.context(), ensure_ascii=False)
    assert 'private-' not in ctx and '900' not in ctx
    assert state.context()['order_habits'][0]['orders'] == 2
    state.unknown = True
    assert '可用积分' in agent.chat('owner', '我的积分')['reply']
    assert not agent.app.adapter.writes


def test_forget_disable_and_clear_apply_to_history_layer(agent):
    attach(agent, [completed('1'), completed('2')])
    agent.insights.refresh('owner')
    agent.chat('owner', '关闭记忆')
    assert agent.session('owner').order_habits == []
    agent.chat('owner', '开启记忆')
    assert agent.session('owner').order_habits
    agent.chat('owner', '忘记牛肉')
    assert not agent.session('owner').order_habits
    agent.insights.refresh('owner')
    assert not agent.session('owner').order_habits
    agent.chat('owner', '清除偏好')
    assert agent.insights.summary('owner')['imported_at'] is None


def test_delegate_prepares_in_one_turn_but_never_submits(agent):
    agent.chat('owner', '上海人民广场')
    agent.chat('owner', '选店1')
    result = agent.chat('owner', '赶时间，牛肉套餐，你帮我挑一份，饮料无糖，打包')
    assert result['can_confirm']
    assert '零度可乐（无糖）' in result['cart']['components']
    assert not agent.app.adapter.writes
    assert '赶时间' in agent.session('owner').context()['current_situation']
    assert agent.session('owner').context()['clock']['timezone'] == 'Asia/Shanghai'


def test_named_meal_before_store_selection_resumes_direct_preview(agent):
    agent.chat('owner', '上海人民广场，我要牛肉汉堡套餐，饮料无糖')
    result = agent.chat('owner', '选店1')
    assert result['can_confirm'] and result['cart']['name'] == '牛肉汉堡套餐'
    assert '零度可乐（无糖）' in result['cart']['components']
    assert not agent.app.adapter.writes


def test_exact_full_store_name_skips_selection_generic_landmark_does_not(agent):
    result = agent.chat('owner', '上海测试门店')
    assert result['recommendations']
    assert not result['stores'] and not agent.app.adapter.writes
    result = agent.chat('other', '上海人民广场')
    assert result['stores']


def test_model_side_tasks_work_after_order_created(agent):
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, text, context):
            return {'action': 'account', 'account_kind': 'points'}
    state = agent.session('owner')
    state.order = {'orderId': 'already-created'}
    agent.model = Model()
    attach(agent, [])
    assert '可用积分' in agent.chat('owner', '我的麦当劳还剩多少积分呢')['reply']
    assert not agent.app.adapter.writes


def test_management_get_has_no_remote_reads_post_scope_and_origin(agent):
    attach(agent, [completed('1'), completed('2')])
    app = PreviewApp(agent.app.adapter, model=NoModel(), ledger=agent.ledger)
    app.query = agent.app.query
    app.weixin = SimpleNamespace(owner_actor=lambda: 'weixin:owner', status=lambda: {'state': 'paused'})
    host = '127.0.0.1'
    server = ThreadingHTTPServer((host, 0), handler_for(app, {f'{host}:0'}))
    port = server.server_port
    server.RequestHandlerClass = handler_for(app, {f'{host}:{port}'})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://{host}:{port}', trust_env=False) as c:
            assert c.get('/').status_code == 200 and '饭搭子管理台' in c.get('/').text
            assert c.get('/chat').status_code == 200
            assert c.get('/api/dashboard?source=weixin').status_code == 200
            assert not agent.app.adapter.calls
            assert c.post('/api/insights', json={'operation': 'refresh'}).status_code == 403
            headers={'Origin': f'http://{host}:{port}'}
            result = c.post('/api/insights', headers=headers, json={'operation': 'refresh', 'source':'weixin'})
            assert result.status_code == 200 and result.json()['history']['orders'] == 2
            assert c.get('/api/dashboard?source=web').json()['history']['imported_at'] is None
            result = c.post('/api/insights', headers=headers, json={'operation': 'points', 'source':'web'})
            assert result.status_code == 200 and '900' in result.json()['reply']
            summary = json.dumps(c.get('/api/dashboard?source=web').json(), ensure_ascii=False)
            assert 'availablePoint' not in summary and 'private-account-id' not in summary
            assert c.post('/api/insights', headers=headers, json={'operation': 'create-order'}).status_code == 502
            assert c.get('/api/dashboard?source=invalid').status_code == 400
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('text', ['不要读取历史订单', '别分析历史订单', '你怎么分析历史订单？', '如果我要分析历史订单呢'])
def test_history_refusal_explanation_and_hypothetical_do_not_query(agent, text):
    before = agent.insights.summary('owner')
    result = agent.chat('owner', text)
    assert not agent.app.adapter.calls
    assert agent.insights.summary('owner') == before
    assert '不会自动扫描' in result['reply']


def test_model_cannot_override_asset_refusal(agent):
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, *args):
            return {'action':'account', 'account_kind':'points'}
    agent.model = Model()
    result = agent.chat('owner', '不要查询我的积分')
    assert not agent.app.adapter.calls
    assert '账号' in result['reply']


def test_refusing_deletion_preserves_import_and_natural_memory_query_preserves_preview(agent):
    attach(agent, [completed('1'), completed('2')])
    agent.insights.refresh('owner')
    agent.chat('owner', '不要删除历史订单分析')
    assert agent.insights.summary('owner')['orders'] == 2
    assert '完成订单推断' in agent.chat('owner', '我的偏好')['reply']
    cart = select(agent)
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, *args):
            return {'action':'preferences'}
    agent.model = Model()
    assert agent.chat('owner', '我现在有哪些口味记录呢')['cart']['preview_id'] == cart['cart']['preview_id']


def test_connected_model_greetings_receive_clock_instead_of_canned_response(agent):
    seen = []
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, text, context):
            seen.append(context)
            return {'action':'chat', 'question':'在呢，今天过得怎么样？'}
    agent.model = Model()
    assert '今天过得怎么样' in agent.chat('owner', '嗨')['reply']
    assert seen[0]['clock']['timezone'] == 'Asia/Shanghai'
    assert not agent.app.adapter.calls


def test_explicit_taste_wins_over_combined_history_model_selection_feedback(agent):
    attach(agent, [completed(str(i)) for i in range(10)])
    agent.insights.refresh('owner')
    agent.memory.learn('owner', '我喜欢鸡肉')
    for i in range(10):
        ident = 'selection-'+str(i)
        agent.memory.observe('owner', '牛肉汉堡套餐', ident)
        agent.memory.feedback('owner', 'like', selection_id=ident)
    agent.chat('owner', '上海人民广场')
    agent.chat('owner', '选店1')
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, *args):
            return {'action':'recommend','product_codes':['meal-1']}
    agent.model = Model()
    result = agent.chat('owner', '推荐套餐')
    assert result['recommendations'][0]['code'] == 'meal-2'
    result = agent.chat('owner', '今天我要牛肉汉堡套餐')
    assert result['cart']['name'] == '牛肉汉堡套餐'
    assert not agent.app.adapter.writes
