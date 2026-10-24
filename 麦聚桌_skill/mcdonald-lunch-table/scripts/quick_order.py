"""Deterministic Chinese lunch commands and menu-based draft recommendations.

No model API or extra key. Constraints that cannot be checked from menu data
stop automatic recommendation rather than being silently dropped.
"""
from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation

from mcd_adapter import AdapterError


class NeedInfo(ValueError):
    pass


def money_cents(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            return None
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_request(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 1000:
        raise NeedInfo('需求请控制在1000字以内。')
    text = text.strip()
    # No silent interpretation of food safety, timing, delivery or per-person exceptions.
    for pattern, reason in [
        (r'过敏|花生|忌口|不辣|不要辣|不能辣|清真|素食|乳糖|麸质|糖尿|减脂|低糖|无糖|孕|不吃|不要|不能吃|忌',
         '这条需求含饮食限制，当前菜单无法完整核实配料和交叉接触，先不自动配餐。请向门店确认后手动选择。'),
        (r'外送|配送|送到|得来速|团餐', '快速配餐目前只支持到店取餐；外送、得来速和团餐请走专项流程。'),
        (r'\d{1,2}[:：]\d{2}|\d+\s*分钟|预约|明天|后天|点前|点后|点取|点半|点钟',
         '当前菜单不能保证指定取餐时间，先不生成承诺准时的餐单。请改为当前到店取餐，或核实预约时段。'),
        (r'一[个位人].*(?:鸡|牛|鱼|猪)|分别|其中|各自', '这条需求包含个人差异，请先用统一偏好生成草稿，再逐人调整，避免猜错成员需求。'),
    ]:
        if re.search(pattern, text):
            raise NeedInfo(reason)
    number = re.search(r'(\d{1,2})\s*(?:个)?(?:人|位|份)', text)
    count = int(number.group(1)) if number else None
    budget_match = re.search(r'(?:每人|人均|预算|每份)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?', text)
    budget = money_cents(budget_match.group(1)) if budget_match else None
    if count is not None and not 1 <= count <= 20:
        raise NeedInfo('快速配餐支持1–20人，请明确人数。')
    if budget is not None and not 100 <= budget <= 100000:
        raise NeedInfo('每人预算请设为1–1000元。')
    # Prefer explicit location separators. Do not infer city from geolocation.
    location = re.split(r'[,，;；\n]', text)[0].strip()
    city = keyword = None
    if not re.search(r'\d|预算|人均|每人', location):
        parts = location.split()
        if len(parts) >= 2:
            city, keyword = parts[0], ' '.join(parts[1:])
        else:
            known = ['北京','上海','广州','深圳','杭州','南京','苏州','成都','重庆','武汉','西安','天津','长沙','郑州','青岛','厦门','福州','济南','合肥','宁波','东莞','佛山','大连','沈阳','昆明','南宁','南昌','贵阳','无锡','珠海','海口','长春','哈尔滨','石家庄','太原','兰州','乌鲁木齐','呼和浩特','西宁','银川','拉萨']
            for name in known:
                if location.startswith(name):
                    city, keyword = name, location[len(name):].removeprefix('市').strip()
                    break
    preference = next((key for key in ('鸡肉','牛肉','鱼肉','汉堡','小食','鸡','牛','鱼') if key in text), '')
    return {'city': city, 'keyword': keyword or None, 'count': count, 'budget_cents': budget,
            'preference': preference, 'fulfillment': 'pickup'}


def recommend(catalog: dict, request: dict) -> list[dict]:
    count, budget = request.get('count'), request.get('budget_cents')
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
        raise NeedInfo('请补充人数，例如“3人”。')
    if not isinstance(budget, int) or isinstance(budget, bool) or not 100 <= budget <= 100000:
        raise NeedInfo('请补充每人预算，例如“每人35元”。')
    preference = request.get('preference', '')
    if preference not in ('', '鸡肉','牛肉','鱼肉','汉堡','小食','鸡','牛','鱼'):
        raise NeedInfo('暂不识别这项口味偏好，请明确鸡肉、牛肉、鱼肉或不限。')
    preference = {'鸡肉':'鸡','牛肉':'牛','鱼肉':'鱼'}.get(preference, preference)
    candidates=[]
    for code, item in catalog.get('meals', {}).items():
        if not isinstance(item,dict): continue
        name = str(item.get('name',''))
        price = money_cents(item.get('currentPrice'))
        # Exclude conditional membership add-ons and coupon-dependent items.
        if not price or price > budget or re.search(r'随单购|会员卡|早餐卡|麦金卡|券|兑换', name + str(item.get('discountType',''))):
            continue
        if preference and preference not in name: continue
        if not re.search(r'套餐|汉堡|堡|卷|饭|小食', name): continue
        score = (30 if '套餐' in name else 0) + (10 if '堡' in name else 0)
        candidates.append({'productCode':str(code),'name':name,'unit_cents':price,'quantity':count,
                           'requires_options':bool(re.search(r'套餐|组合|自选|任选|拼盘',name)), 'score':score})
    if not candidates:
        raise NeedInfo('当前菜单里没有符合偏好和每人预算的候选。可以提高预算或把口味改为不限。')
    stable=sorted(candidates,key=lambda i:(-i['score'],i['unit_cents'],i['productCode']))
    cheapest=sorted(candidates,key=lambda i:(i['unit_cents'],-i['score'],i['productCode']))
    variants=[('balanced','优先套餐' if '套餐' in stable[0]['name'] else '预算内原选',stable[0]),('saving','少花一点',cheapest[0])]
    other=next((i for i in stable if i['productCode'] not in {v[2]['productCode'] for v in variants}),None)
    if other: variants.append(('alternative','另一种选择',other))
    seen=set();plans=[]
    for ident,label,item in variants:
        if item['productCode'] in seen: continue
        seen.add(item['productCode'])
        plans.append({'id':ident,'label':label,'items':[{k:v for k,v in item.items() if k!='score'}],
            'count':count,'per_person_cents':item['unit_cents'],'total_cents':item['unit_cents']*count,
            'budget_cents':budget,'preference':preference,
            'notes':['按当前菜单参考价，每人同款一份，可逐人调整。',
                     '套餐子项尚未确认，最终费用待官方算价。' if item['requires_options'] else '这是餐单草稿，尚未下单；最终费用待官方算价。']})
    return plans


HELP = ('我是麦聚桌，可以帮你把午餐需求变成餐单草稿。\n\n'
        '直接发：上海人民广场，3人，每人35元，想吃鸡肉\n'
        '我会先列门店；回复“选店1”，再回复“方案1”。\n'
        '当前支持到店取餐。只查询和规划，不会下单、领券或付款。\n'
        '重新开始：发“重来”；查看示例：发“帮助”。')


class LunchConversation:
    """One binding owner, ephemeral conversations; shared logic for web and Weixin."""
    def __init__(self, app):
        self.app=app
        self.request={}
        self.stores=[]
        self.menu=None
        self.selected=None
        self.plans=[]

    def reply(self,text:str) -> str:
        text=text.strip()
        if text in ('帮助','help','你好','开始','菜单','麦聚桌'):
            return HELP
        if text in ('重来','重新开始','清空'):
            self.request={};self.stores=[];self.menu=None;self.selected=None;self.plans=[]
            return '已清空当前对话餐单。\n'+HELP
        if re.search(r'下单|付款|支付|领券|兑换|取消订单',text):
            return '当前微信入口只查询和生成草稿，还没有创建订单或付款。需要消费时请回到已核验预览与确认流程，不能把普通聊天当作扣款确认。'
        try:
            match=re.fullmatch(r'(?:选店|门店|选门店)\s*(\d+)',text)
            if match:
                n=int(match.group(1))
                if not 1<=n<=len(self.stores): return '请从刚才列出的门店编号中选择，例如“选店1”。'
                self.selected=self.stores[n-1]
                result=self.app.handle('/api/menu',{'storeCode':str(self.selected['storeCode'])})
                self.menu=result
                return self.make_plans()
            match=re.fullmatch(r'(?:方案|选方案)\s*(\d+)',text)
            if match:
                n=int(match.group(1))
                if not 1<=n<=len(self.plans): return '请先选门店生成候选，再回复“方案1”。'
                plan=self.plans[n-1];item=plan['items'][0]
                return f"这桌的餐单草稿\n{self.selected.get('storeName','')}\n{item['name']} × {plan['count']}\n每人 ¥{plan['per_person_cents']/100:.2f}，参考总计 ¥{plan['total_cents']/100:.2f}\n\n"+'\n'.join(plan['notes'])+'\n尚未创建订单或付款。改预算可发“3人，每人30元，想吃鸡肉”。'
            parsed=parse_request(text)
            # Clear old plans immediately whenever the request changes.
            self.plans=[]
            location_changed = bool(parsed.get('city') and parsed.get('keyword'))
            for key,value in parsed.items():
                if value is not None and (value != '' or key=='preference'):
                    self.request[key]=value
            if location_changed:
                self.selected=None;self.menu=None;self.stores=[]
            if not self.request.get('city') or not self.request.get('keyword'):
                return '请告诉我城市和位置，例如“上海 人民广场，3人，每人35元”。其他城市可用空格分开城市和地标。'
            if not self.request.get('count') or not self.request.get('budget_cents'):
                return '还需要人数和每人预算，例如“3人，每人35元”。'
            if self.selected and self.menu:
                return self.make_plans()
            data=self.app.handle('/api/stores',{'city':self.request['city'],'keyword':self.request['keyword']})
            self.stores=[s for s in data['stores'] if s.get('businessStatus') is not False][:5]
            if not self.stores:return '这个位置没有找到可用门店，请换个地标。'
            lines=['找到以下门店，请回复“选店1”（到店取餐）：']
            for n,s in enumerate(self.stores,1):
                lines.append(f"{n}. {s.get('storeName','')}\n{s.get('address','地址未返回')}")
            return '\n\n'.join(lines)
        except NeedInfo as exc:
            self.plans=[];self.request={};self.selected=None;self.menu=None;self.stores=[]
            return str(exc)
        except AdapterError as exc:
            return '这次查询未完成：'+str(exc)+'。可以稍后重新发送需求。'

    def make_plans(self):
        self.plans=recommend(self.menu,self.request)
        lines=[f"{self.selected.get('storeName','')}\n按{self.request['count']}人、每人¥{self.request['budget_cents']/100:.2f}的预算，找到这些草稿："]
        for n,p in enumerate(self.plans,1):
            lines.append(f"方案{n} · {p['label']}\n{p['items'][0]['name']} × {p['count']}\n每人¥{p['per_person_cents']/100:.2f}，合计¥{p['total_cents']/100:.2f}")
        return '\n\n'.join(lines)+'\n\n回复“方案1”整理餐单。以上是菜单参考价，套餐子项和最终费用待确认，未下单。'
