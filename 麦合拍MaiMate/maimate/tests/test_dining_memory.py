import json

import pytest

from confirmation import ConfirmationLedger
from dining_memory import DiningMemory, alias_target, canonical, menu_matches, redact
from location_service import LocationResolver
from mcd_adapter import AdapterError
from ordering_agent import OrderingAgent
from .test_ordering_agent import App, NoModel, select


@pytest.fixture
def memory(tmp_path):
    return DiningMemory(tmp_path / 'preferences.sqlite3')


@pytest.mark.parametrize('name', ['穷鬼套餐', '穷鬼', '随心配', '一加一', '1加1', '超值1+1'])
def test_no_builtin_nickname_mapping(name):
    assert canonical(name) == name
    assert alias_target(name) is None


def test_explicit_preferences_persist_without_raw_chats_or_actor(memory):
    phrase = '记住，我爱吃精选超值随心配，平时备注不用存'
    assert memory.learn('private-owner-identifier', phrase, ['精选超值随心配']) == ['偏爱精选超值随心配']
    restored = DiningMemory(memory.path)
    assert restored.summary('private-owner-identifier')['likes'] == ['精选超值随心配']
    assert restored.summary('another')['likes'] == []
    raw = memory.path.read_bytes()
    assert phrase.encode() not in raw and b'private-owner-identifier' not in raw
    assert memory.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('phrase', [
    '今天来一份精选超值随心配', '今天我喜欢鸡肉', '朋友说我喜欢精选超值随心配',
    '比如我喜欢无糖饮料', '如果我喜欢精选超值随心配', '我喜欢无糖饮料吗？',
    '不要记住我喜欢精选超值随心配', '我喜欢无糖饮料，token=fixture-secret',
    '记住我喜欢无糖，因为我有糖尿病', '“我爱吃精选超值随心配”只是举个例子',
    '记住我的手机号13900000000',
])
def test_transient_quoted_sensitive_or_hypothetical_text_is_not_memory(memory, phrase):
    assert memory.learn('owner', phrase) == []
    assert memory.summary('owner')['likes'] == []


def test_preference_correction_disable_forget_and_clear(memory):
    memory.learn('owner', '我喜欢无糖饮料')
    memory.learn('owner', '我不喜欢无糖饮料')
    assert memory.summary('owner')['likes'] == []
    assert memory.summary('owner')['dislikes'] == ['无糖饮料']
    memory.control('owner', 'disable')
    assert memory.learn('owner', '我喜欢薯条') == []
    memory.observe('owner', '薯条')
    assert memory.profile('owner')['choices'] == {}
    assert memory.forget('owner', '无糖') == ['无糖饮料']
    memory.control('owner', 'clear')
    assert not memory.summary('owner')['enabled']
    memory.control('owner', 'enable')
    assert memory.learn('owner', '我爱吃薯条')


def test_custom_food_aliases_have_verified_targets(memory):
    assert memory.learn('owner', '省钱饭就是精选超值随心配', ['精选超值随心配']) == ['省钱饭＝精选超值随心配']
    assert alias_target('省钱饭', memory.summary('owner')['aliases']) == '精选超值随心配'
    assert memory.learn('owner', '魔法饭就是神奇套餐') == []
    assert '魔法饭' not in memory.summary('owner')['aliases']
    memory.control('owner', 'disable')
    assert '省钱饭' not in memory.summary('owner')['aliases']


def test_repeated_choices_are_weaker_than_explicit_likes(memory):
    memory.observe('owner', '精选超值随心配')
    assert memory.summary('owner')['often_selected'] == []
    memory.observe('owner', '精选超值随心配')
    assert memory.summary('owner')['often_selected'] == [{'name': '精选超值随心配', 'selections': 2}]
    assert memory.summary('owner')['likes'] == []


@pytest.fixture
def agent(tmp_path):
    app = App()
    original = app.query
    def query(name, args):
        data = original(name, args)
        if name == 'query-meals':
            data['meals']['value-1'] = {'name': '精选超值随心配', 'currentPrice': '13.9'}
        elif name == 'query-meal-detail' and args['code'] == 'value-1':
            data['name'] = '精选超值随心配'
        return data
    app.query = query
    return OrderingAgent(app, NoModel(), LocationResolver(), ConfirmationLedger(tmp_path / 'ledger.sqlite3'))


def test_city_is_requested_before_search_and_food_request_survives(agent):
    result = agent.chat('owner', '示例广场店，想吃精选超值随心配')
    assert '城市' in result['reply']
    assert agent.app.adapter.calls == []
    assert '精选超值随心配' in agent.session('owner').request['preference']
    stores = agent.chat('owner', '北京')
    assert stores['stores']
    assert agent.app.adapter.calls[-1][1]['city'] == '北京'
    recommended = agent.chat('owner', '选店1')
    assert [m['code'] for m in recommended['recommendations']] == ['value-1']
    assert '精选超值随心配' in recommended['reply']
    assert not agent.app.adapter.writes


def test_missing_value_menu_is_honest_and_view_menu_still_works(agent):
    original = agent.app.query
    def query(name, args):
        data = original(name, args)
        if name == 'query-meals':
            data['meals'].pop('value-1')
        return data
    agent.app.query = query
    agent.chat('owner', '上海人民广场，想吃精选超值随心配')
    result = agent.chat('owner', '选店1')
    assert result['recommendations'] == []
    assert '没有匹配' in result['reply']
    assert agent.chat('owner', '查看菜单')['recommendations']
    assert not agent.app.adapter.writes


def test_value_family_is_kept_visible_beyond_first_200_menu_items(agent):
    session = agent.session('owner')
    session.request['preference'] = '人气随心配'
    session.catalog = {'meals': {**{f'p{i}': {'name': f'常规汉堡{i}'} for i in range(240)},
                                 'real-value': {'name': '人气经典随心配'}}}
    context = session.context()
    assert context['menu_truncated']
    assert context['menu'][0]['code'] == 'real-value'


def test_long_term_likes_are_soft_and_current_food_request_wins(agent):
    agent.memory.learn('owner', '我爱吃精选超值随心配', ['精选超值随心配'])
    agent.chat('owner', '上海人民广场')
    result = agent.chat('owner', '选店1')
    assert result['recommendations'][0]['code'] == 'value-1'
    result = agent.chat('owner', '今天想吃牛肉套餐')
    assert result['cart']['name'] == '牛肉汉堡套餐'
    assert not agent.app.adapter.writes


def test_remembered_sugar_free_drink_applies_but_explicit_cola_overrides(agent):
    agent.chat('owner', '我喜欢无糖饮料')
    quote = select(agent)
    assert '零度可乐（无糖）' in quote['cart']['components']
    agent.chat('owner', '重新点餐')
    quote = select(agent, request='上海人民广场，牛肉套餐饮料今天要普通可乐')
    assert '可口可乐' in quote['cart']['components']
    assert '零度可乐（无糖）' not in quote['cart']['components']


def test_transcript_records_both_sides_buttons_and_resets_without_chats_on_disk(agent):
    select(agent)
    turns = agent.recent('owner')['turns']
    assert len(turns) == 3
    assert turns[1]['user'] == '选店1' and '给你这几个选择' in turns[1]['assistant']
    context = agent.session('owner').context()
    assert context['selected_meal']['code'] == 'meal-1'
    assert {m['role'] for m in context['history']} == {'user', 'assistant'}
    assert agent.recent('another')['turns'] == []
    agent.chat('owner', '重新点餐')
    assert len(agent.recent('owner')['turns']) == 4
    assert not agent.session('owner').detail
    for _ in range(25):
        agent.chat('owner', '帮助')
    assert len(agent.recent('owner')['turns']) == 20
    assert len(agent.session('owner').history) == 8


def test_account_requests_are_local_read_only_and_not_sent_to_model(agent):
    original = agent.app.query
    def query(name, args):
        if name == 'query-my-account':
            return {'accountId': 'private-account', 'availablePoint': '123456', 'accumulativePoint': '500000'}
        if name == 'query-my-coupons':
            return {'coupons': [{'title': '测试优惠券', 'code': 'private-coupon-code'}]}
        return original(name, args)
    agent.app.query = query
    explanation = agent.chat('owner', '我的账号')['reply']
    assert '两份独立授权' in explanation
    assert '123456' in agent.chat('owner', '我的积分')['reply']
    assert '测试优惠券' in agent.chat('owner', '我的优惠券')['reply']
    context = json.dumps(agent.session('owner').context(), ensure_ascii=False)
    assert 'private-account' not in context and '123456' not in context and '测试优惠券' not in context
    assert 'private-coupon-code' not in json.dumps(agent.recent('owner'), ensure_ascii=False)
    assert not agent.app.adapter.writes


def test_failed_store_read_has_explicit_retry_that_really_queries(agent):
    original = agent.app.query
    failed = []
    def query(name, args):
        if name == 'query-nearby-stores' and not failed:
            failed.append(True)
            raise AdapterError('business_error', '麦当劳业务接口报告失败；未自动重试')
        return original(name, args)
    agent.app.query = query
    result = agent.chat('owner', '上海人民广场，想吃精选超值随心配')
    assert '查询门店未成功' in result['reply'] and '重试' in result['reply']
    assert agent.app.adapter.calls == []
    result = agent.chat('owner', '重试')
    assert result['stores']
    assert len(agent.app.adapter.calls) == 1
    assert agent.session('owner').failed_read is None
    assert not agent.app.adapter.writes


def test_rejected_city_error_can_be_corrected_without_reentering_food(agent):
    original = agent.app.query
    def query(name, args):
        if name == 'query-nearby-stores' and args.get('city') != '北京':
            raise AdapterError('business_city_required', '官方门店查询需要有效城市')
        return original(name, args)
    agent.app.query = query
    assert '有效城市' in agent.chat('owner', '上海人民广场，想吃精选超值随心配')['reply']
    assert agent.chat('owner', '北京')['stores']
    assert agent.chat('owner', '选店1')['recommendations'][0]['code'] == 'value-1'


def test_redaction_removes_secrets_links_coordinates_and_phone():
    result = redact('token=fixture-secret sk-fixture-secret 13900000000 31.234567 121.987654 https://m.mcd.cn/pay?secret=fixture 订单号：private-id')
    for value in ('fixture-secret', '13900000000', '31.234567', '121.987654', 'https://', 'private-id'):
        assert value not in result


def test_memory_commands_do_not_authorize_or_invalidate_a_current_preview(agent):
    quote = select(agent)
    preview = quote['cart']['preview_id']
    assert '记忆' in agent.chat('owner', '我的偏好')['reply']
    assert agent.session('owner').pending['id'] == preview
    assert '关闭' in agent.chat('owner', '关闭记忆')['reply']
    assert not agent.memory.summary('owner')['enabled']
    assert '清除' in agent.chat('owner', '清除偏好')['reply']
    assert not agent.app.adapter.writes


def test_negating_a_food_alias_does_not_force_that_food(agent):
    assert alias_target('今天不要省钱饭，想吃牛肉', {'省钱饭': '精选超值随心配'}) is None
    agent.chat('owner', '上海人民广场')
    agent.chat('owner', '选店1')
    result = agent.chat('owner', '今天不要精选超值随心配，想吃牛肉')
    assert [v['code'] for v in result['recommendations']] == ['meal-1']


def test_model_cannot_replace_a_query_with_a_promise_or_silently_read_assets(agent):
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, text, context):
            return {'action': 'clarify', 'question': '抱歉刚才没处理成功。我帮您重新查一下这家门店和套餐。'}
    agent.model = Model()
    result = agent.chat('owner', '示例广场店')
    assert '我帮您重新查' not in result['reply']
    assert '城市' in result['reply']
    assert not agent.app.adapter.calls
    result = agent._intent(agent.session('owner'), {'action': 'account', 'account_kind': 'points'}, '今天吃什么')
    assert '两份独立授权' in result['reply']
    assert not agent.app.adapter.calls


def test_failed_menu_recommendation_does_not_reuse_previous_candidates(agent):
    agent.chat('owner', '上海人民广场')
    agent.chat('owner', '选店1')
    class Model(NoModel):
        def status(self):
            return {**super().status(), 'configured': True}
        def interpret(self, text, context):
            if text.startswith('根据当前'):
                return {'action': 'clarify', 'question': '请说一下想吃的口味？'}
            return {'action': 'recommend'}
    agent.model = Model()
    agent.chat('owner', '换个推荐')
    assert agent.session('owner').candidates == []
    assert '请从当前推荐' in agent.chat('owner', '套餐1')['reply']


def test_weixin_recent_and_controls_never_send_messages(agent, tmp_path):
    from weixin_bridge import WeixinBridge
    from .test_weixin_bridge import FakeAPI
    api = FakeAPI()
    agent.app.agent = agent
    bridge = WeixinBridge(agent.app, state_dir=tmp_path / 'weixin', api=api)
    assert bridge.recent()['memory'] is None
    bridge.account = {'owner_id': 'private-owner', 'bot_id': 'fixture', 'bot_token': 'secret', 'base_url': 'https://ilinkai.weixin.qq.com'}
    actor = bridge.owner_actor()
    agent.chat(actor, '记住，我爱吃麦乐鸡')
    recent = bridge.recent()
    assert recent['memory']['likes'] == ['麦乐鸡']
    assert recent['turns']
    assert 'private-owner' not in json.dumps(recent)
    assert not bridge.memory_control({'operation': 'disable'})['memory']['enabled']
    assert bridge.memory_control({'operation': 'clear'})['memory']['likes'] == []
    assert not api.calls


def test_joined_first_person_preferences_and_later_corrections(memory):
    memory.learn('owner', '记住，我爱吃麦乐鸡，喜欢无糖饮料，也爱吃薯条')
    assert set(memory.summary('owner')['likes']) == {'麦乐鸡', '无糖饮料', '薯条'}
    memory.learn('owner', '我喜欢普通可乐')
    assert '含糖饮料' in memory.summary('owner')['likes']
    assert '无糖饮料' not in memory.summary('owner')['likes']
    memory.learn('owner', '我喜欢不加冰')
    assert '去冰' in memory.summary('owner')['likes']
    assert '加冰' not in memory.summary('owner')['likes']
