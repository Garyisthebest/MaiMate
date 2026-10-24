"""Synthetic regressions for requested SKU identity and user-taught names."""
import pytest

from dining_memory import DiningMemory, canonical, menu_matches
from .test_dining_memory import agent
from .test_ordering_agent import NoModel


MENU = {
    'value-1': {'name': '人气经典随心配', 'currentPrice': '14.9'},
    'value-2': {'name': '精选超值随心配', 'currentPrice': '19.9'},
}


class WrongIndexModel(NoModel):
    def status(self):
        return {**super().status(), 'configured': True}

    def interpret(self, text, context):
        # Deliberately disagree with the user's named SKU and return an old index.
        return {'action': 'select_meal', 'meal_index': 1, 'product_codes': ['meal-1']}


def setup_menu(agent):
    agent.chat('owner', '上海示例广场')
    agent.chat('owner', '选店1')
    s = agent.session('owner')
    s.catalog['meals'].update(MENU)
    original = agent.app.query

    def query(name, args):
        data = original(name, args)
        if name == 'query-meal-detail' and args['code'] in MENU:
            data['name'] = MENU[args['code']]['name']
        return data

    agent.app.query = query
    return s


@pytest.mark.parametrize('text', [
    '人气经典随心配', '我要人气随心配', '我现在要吃人气随心配 （即穷鬼套餐）',
    '就要人气经典随心配', '人气经典随心配 ×1',
])
def test_named_meal_beats_wrong_model_index_old_cart_and_memory(agent, text):
    setup_menu(agent)
    agent.chat('owner', action='select_meal', payload={'product_code': 'meal-1'})
    agent.memory.learn('owner', '我爱吃牛肉汉堡套餐', ['牛肉汉堡套餐'])
    agent.model = WrongIndexModel()
    result = agent.chat('owner', text)
    assert result['cart']['name'] == '人气经典随心配'
    assert agent.session('owner').item['productCode'] == 'value-1'
    assert not agent.app.adapter.writes


def test_ambiguous_abbreviation_clears_old_preview_and_lists_only_matches(agent):
    setup_menu(agent)
    before = agent.chat('owner', action='select_meal', payload={'product_code': 'meal-1'})
    result = agent.chat('owner', '我要随心配')
    assert {v['code'] for v in result['recommendations']} == {'value-1', 'value-2'}
    assert not result['can_confirm'] and result['cart'] is None
    agent.chat('owner', action='confirm', payload={'preview_id': before['cart']['preview_id']})
    assert not agent.app.adapter.writes


def test_named_recommendation_is_a_constraint_not_a_score(agent):
    s = setup_menu(agent)
    agent.memory.learn('owner', '我爱吃牛肉汉堡套餐', ['牛肉汉堡套餐'])
    s.request['preference'] = '推荐人气经典随心配'
    agent.model = WrongIndexModel()
    result = agent._recommend(s, ['meal-1'])
    assert [v['code'] for v in result['recommendations']] == ['value-1']


def test_named_meal_keeps_requested_quantity_drink_and_takeway(agent):
    setup_menu(agent)
    agent.app.adapter.ways.append({'code': 'dine-in', 'title': '堂食'})
    result = agent.chat('owner', '人气经典随心配2份，饮料无糖，堂食')
    assert result['cart']['name'] == '人气经典随心配'
    assert result['cart']['quantity'] == 2
    assert '零度可乐（无糖）' in result['cart']['components']
    assert agent.session('owner').pending['args']['takeWayCode'] == 'dine-in'


def test_model_recommendation_cannot_rewrite_unknown_specific_request(agent):
    setup_menu(agent)

    class Model(WrongIndexModel):
        def interpret(self, text, context):
            return {'action': 'recommend', 'preference': '牛肉汉堡套餐', 'product_codes': ['meal-1']}

    agent.model = Model()
    result = agent.chat('owner', '我要穷鬼套餐')
    assert not result['recommendations'] and result['cart'] is None
    assert '没有匹配' in result['reply']


def test_explicit_ordinal_uses_current_list_even_if_model_code_conflicts(agent):
    s = setup_menu(agent)
    s.candidates = [{'code': 'meal-1', 'name': '牛肉汉堡套餐'},
                    {'code': 'value-1', 'name': '人气经典随心配'}]
    result = agent._intent(s, {'action': 'select_meal', 'meal_index': 2, 'product_codes': ['meal-1']}, '第二个')
    assert result['cart']['name'] == '人气经典随心配'
    assert not agent.app.adapter.writes


def test_current_named_meal_remains_in_model_context_after_truncation(agent):
    s = setup_menu(agent)
    s.catalog['meals'] = {**{f'f{i}': {'name': f'普通汉堡{i}套餐'} for i in range(250)}, **MENU}
    s.request['preference'] = '我要人气随心配'
    context = s.context()
    assert context['menu_truncated']
    assert context['menu'][0]['code'] == 'value-1'


def test_unknown_nickname_is_not_persisted_or_silently_selected(agent):
    setup_menu(agent)
    agent.model = WrongIndexModel()
    result = agent.chat('owner', '我要穷鬼套餐')
    assert not result['can_confirm'] and result['cart'] is None
    assert agent.memory.summary('owner')['aliases'] == {}
    assert not any(tool == 'query-meal-detail' for tool, _ in agent.app.adapter.calls)


def test_explicit_definition_is_verified_persistent_isolated_and_correctable(agent):
    setup_menu(agent)
    result = agent.chat('owner', '记住，穷鬼套餐就是人气随心配')
    assert '记住' in result['reply']
    memory = DiningMemory(agent.memory.path)
    assert memory.summary('owner')['aliases'] == {'穷鬼套餐': '人气经典随心配'}
    assert memory.summary('owner')['alias_evidence']['穷鬼套餐']['source'] == 'user_definition'
    assert memory.summary('other')['aliases'] == {}
    assert agent.chat('owner', '我要穷鬼套餐')['cart']['name'] == '人气经典随心配'
    agent.chat('owner', '穷鬼套餐就是精选超值随心配')
    assert agent.chat('owner', '我要穷鬼套餐')['cart']['name'] == '精选超值随心配'
    agent.chat('owner', '忘记穷鬼套餐')
    assert memory.summary('owner')['aliases'] == {}
    assert memory.summary('owner')['alias_evidence'] == {}
    assert not agent.app.adapter.writes


def test_forgetting_nickname_keeps_independent_taste_and_feedback(agent):
    setup_menu(agent)
    agent.chat('owner', '我喜欢鸡肉')
    agent.chat('owner', '小确幸就是鸡肉汉堡套餐')
    agent.chat('owner', '小确幸')
    agent.chat('owner', '这餐好吃')
    agent.chat('owner', '忘记小确幸')
    memory = agent.memory.summary('owner')
    assert memory['aliases'] == {}
    assert memory['likes'] == ['鸡肉']
    assert memory['insights'][0]['name'] == '鸡肉汉堡套餐'


def test_parenthetical_explanation_learns_only_unique_verified_target(agent):
    setup_menu(agent)
    result = agent.chat('owner', '我要人气随心配（即穷鬼套餐）')
    assert result['cart']['name'] == '人气经典随心配'
    assert result['memory']['aliases']['穷鬼套餐'] == '人气经典随心配'
    agent.chat('owner', '我要随心配（即省钱饭）')
    assert '省钱饭' not in agent.memory.summary('owner')['aliases']


@pytest.mark.parametrize('definition', ['省钱饭就是随心配', '省钱饭就是不存在的套餐'])
def test_ambiguous_or_missing_alias_target_cannot_be_saved(agent, definition):
    setup_menu(agent)
    reply = agent.chat('owner', definition)
    assert '没有保存' in reply['reply']
    assert reply['memory']['aliases'] == {}
    assert not agent.app.adapter.writes


def test_alias_missing_from_new_menu_does_not_substitute_even_with_similar_name(agent):
    s = setup_menu(agent)
    agent.chat('owner', '穷鬼套餐就是人气经典随心配')
    agent.chat('owner', action='select_meal', payload={'product_code': 'meal-1'})
    del s.catalog['meals']['value-1']
    s.catalog['meals']['other-value'] = {'name': '人气经典随心配升级套餐', 'currentPrice': '28'}
    agent.model = WrongIndexModel()
    result = agent.chat('owner', '我要穷鬼套餐')
    assert '没有返回' in result['reply']
    assert result['cart'] is None and not result['can_confirm']


def test_disabled_memory_does_not_resolve_or_learn_aliases(agent):
    setup_menu(agent)
    agent.chat('owner', '穷鬼套餐就是人气经典随心配')
    agent.chat('owner', '关闭记忆')
    assert not agent.session('owner').context()['preferences']['enabled']
    result = agent.chat('owner', '我要穷鬼套餐')
    assert result['cart'] is None
    agent.chat('owner', '省钱饭就是人气经典随心配')
    assert '省钱饭' not in agent.memory.profile('owner')['aliases']


def test_cancellation_invalidates_preview_even_with_model_configured(agent):
    setup_menu(agent)
    agent.chat('owner', '人气经典随心配')
    agent.model = WrongIndexModel()
    assert not agent.chat('owner', '取消')['can_confirm']
    agent.chat('owner', '确认下单')
    assert not agent.app.adapter.writes


def test_correction_invalidates_old_checkout_even_if_model_only_chats(agent):
    setup_menu(agent)
    agent.chat('owner', '人气经典随心配')

    class Model(WrongIndexModel):
        def interpret(self, text, context):
            return {'action': 'chat', 'question': '你想换哪个？'}

    agent.model = Model()
    assert not agent.chat('owner', '不是这个，你搞错了')['can_confirm']
    agent.chat('owner', '确认下单')
    assert not agent.app.adapter.writes


def test_full_catalog_name_beats_overlapping_learned_nickname():
    assert menu_matches('人气经典随心配', MENU, {'随心配': '其他套餐'}) == ['value-1']
    assert canonical('省钱饭', {'省钱饭': '小确幸', '小确幸': '其他套餐'}) == '小确幸'


def test_negative_and_conditional_statements_do_not_teach_aliases(agent):
    setup_menu(agent)
    for text in ('如果我要人气经典随心配（即穷鬼套餐）', '朋友要人气经典随心配（即穷鬼套餐）'):
        agent.chat('owner', text)
    assert agent.memory.summary('owner')['aliases'] == {}
