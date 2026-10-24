import pytest
from quick_order import parse_request, recommend, NeedInfo, LunchConversation

CATALOG={'meals':{
 'a':{'name':'鸡腿堡套餐','currentPrice':'30'},
 'b':{'name':'鸡肉汉堡','currentPrice':'20'},
 'c':{'name':'双层牛肉堡','currentPrice':'25'},
 'd':{'name':'鸡肉会员卡随单购','currentPrice':'9.9'},
 'e':{'name':'鸡肉套餐券','currentPrice':'8'},
 'f':{'name':'鸡肉大套餐','currentPrice':'40'},
 'g':{'name':'薯条','currentPrice':'10'},
}}


def test_single_sentence_extracts_location_people_budget_and_preference():
    req=parse_request('上海人民广场，3个人，每人35元，想吃鸡肉')
    assert req=={'city':'上海','keyword':'人民广场','count':3,'budget_cents':3500,'preference':'鸡肉','fulfillment':'pickup'}
    plans=recommend(CATALOG,req)
    assert len(plans)>=2
    assert plans[0]['items'][0]['productCode']=='a'
    assert plans[1]['items'][0]['productCode']=='b'
    assert all(p['per_person_cents']<=3500 and p['total_cents']==p['per_person_cents']*3 for p in plans)
    assert not any(p['items'][0]['productCode'] in ['c','d','e','f'] for p in plans)


@pytest.mark.parametrize('text',[
    '上海人民广场，3人，每人35元，花生过敏',
    '3人，每人35元，不辣',
    '3人，每人35元，12:10前取',
    '3人，每人35元，外送',
    '3人，每人35元，一个吃鸡一个吃牛',
])
def test_unverifiable_constraints_are_not_silently_dropped(text):
    with pytest.raises(NeedInfo): parse_request(text)


def test_no_conditional_offer_or_duplicate_alternatives():
    plans=recommend({'meals':{'a':CATALOG['meals']['a']}},parse_request('3人，每人35元'))
    assert len(plans)==1
    assert plans[0]['items'][0]['requires_options']


def test_missing_budget_and_no_candidate_return_actionable_errors():
    with pytest.raises(NeedInfo,match='预算'): recommend(CATALOG,parse_request('3人'))
    with pytest.raises(NeedInfo,match='没有符合'): recommend(CATALOG,parse_request('3人，每人5元'))


class App:
    def __init__(self):self.calls=[]
    def handle(self,path,body):
        self.calls.append((path,body))
        if path=='/api/stores':return {'stores':[{'storeName':'测试门店','storeCode':'s1','address':'测试地址','businessStatus':True}]}
        if path=='/api/menu':return CATALOG
        raise AssertionError('unexpected operation')


def test_three_messages_complete_draft_without_any_order_operation():
    app=App();conversation=LunchConversation(app)
    assert '选店1' in conversation.reply('上海人民广场，3人，每人35元，鸡肉')
    assert '方案1' in conversation.reply('选店1')
    assert '餐单草稿' in conversation.reply('方案1')
    assert '没有创建订单' in conversation.reply('确认下单')
    assert [p for p,_ in app.calls]==['/api/stores','/api/menu']


def test_new_allergy_invalidates_previous_plans_and_store_selection():
    conversation=LunchConversation(App())
    conversation.reply('上海人民广场，3人，每人35元，鸡肉')
    conversation.reply('选店1')
    assert conversation.plans
    assert '限制' in conversation.reply('其中一人花生过敏')
    assert not conversation.plans and not conversation.request and not conversation.stores
    assert '先选门店' in conversation.reply('方案1')
