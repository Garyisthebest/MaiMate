from preview_server import PreviewApp
from mcd_adapter import AdapterError
import pytest


class FakeAdapter:
    def __init__(self):
        self.calls = []
    def call(self, name, args):
        self.calls.append((name,args))
        if name == 'query-nearby-stores':
            data=[{'storeCode':'s1','storeName':'Fixture store'}]
        elif name == 'query-meals':
            data={'meals':{'p1':{'name':'Fixture meal','currentPrice':'20'}},'categories':[], 'frequent':[{'code':'private'}]}
        else:
            data={'name':'Fixture meal','rounds':[]}
        return {'structuredContent':{'success':True,'data':data}}
    @staticmethod
    def business_payload(result):
        return result['structuredContent']


def test_only_explicit_position_search_and_selected_store_menu():
    adapter=FakeAdapter()
    app=PreviewApp(adapter)
    app.handle('/api/stores',{'city':'上海','keyword':'人民广场'})
    assert adapter.calls[0][1] == {'searchType':2,'beType':1,'city':'上海','keyword':'人民广场'}
    menu=app.handle('/api/menu',{'storeCode':'s1'})
    assert 'frequent' not in menu
    assert adapter.calls[-1][1] == {'storeCode':'s1','orderType':1,'beType':1}
    app.handle('/api/detail',{'storeCode':'s1','code':'p1'})
    assert all(n in ('query-nearby-stores','query-meals','query-meal-detail') for n,_ in adapter.calls)


def test_unknown_store_and_product_are_blocked_before_call():
    adapter=FakeAdapter()
    app=PreviewApp(adapter)
    with pytest.raises(AdapterError):
        app.handle('/api/menu',{'storeCode':'unknown'})
    assert adapter.calls == []
    app.handle('/api/stores',{'city':'上海','keyword':'人民广场'})
    with pytest.raises(AdapterError):
        app.handle('/api/detail',{'storeCode':'s1','code':'unknown'})
    assert len(adapter.calls) == 1
