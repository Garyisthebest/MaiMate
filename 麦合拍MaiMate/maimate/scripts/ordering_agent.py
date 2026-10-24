"""Shared web/Weixin ordering agent. Models understand; verified state executes."""
from __future__ import annotations

import copy
import re
import secrets
import threading
import time
import uuid

from confirmation import ConfirmationLedger, digest
from dining_memory import DiningMemory, canonical, family, family_matches, normalized, redact, requested_family
from mcd_adapter import AdapterError
from quick_order import money_cents


HELP = ('我是麦合拍MaiMate，帮你从选餐走到官方付款。\n'
        '到店后发门店名，或使用“定位”；再说“推荐一个牛肉套餐，饮料无糖”。\n'
        '挑好后可直接说“第二个”“可乐换零度”。我会查真实选配并算价。\n'
        '看到门店、餐品和最终金额后，回复“确认下单”，即可获取官方付款链接。\n'
        '也支持“3人吃”“30元以内”；人数、预算都不是必填。\n'
        '查进度发“订单状态”，新点一单发“重新点餐”。\n'
        '“穷鬼套餐”按1+1超值理解；可说“记住，我爱吃穷鬼套餐”。\n'
        '查看偏好发“我的偏好”，也支持“忘记无糖”“清除偏好”“关闭记忆”。\n'
        '吃完可说“这餐好吃”“这餐一般”“这餐不喜欢”，再发“口味复盘”看看推荐如何变化。\n'
        '账户信息发“账号说明”，查积分发“我的积分”，查券发“我的优惠券”。')

CITY_NAMES = ('北京', '上海', '广州', '深圳', '杭州', '南京', '成都', '重庆', '武汉', '天津', '苏州', '西安',
              '长沙', '厦门', '郑州', '宁波', '青岛', '济南', '合肥', '福州', '昆明', '沈阳', '大连',
              '东莞', '佛山', '无锡', '南昌', '南宁', '贵阳', '珠海', '海口', '长春', '哈尔滨',
              '石家庄', '太原', '兰州', '乌鲁木齐', '呼和浩特', '西宁', '银川', '拉萨')


def amount(value):
    return f'¥{value / 100:.2f}'


def positive(value, fallback=1):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback


def build_item(detail, quantity=1, overrides=None):
    """Resolve documented defaults/explicit option codes, including modifier keys."""
    overrides = overrides or {}
    if not isinstance(detail, dict) or not isinstance(detail.get('code'), str):
        raise AdapterError('detail_invalid', '餐品详情缺少编码，不能组装订单')
    item = {'productCode': detail['code'], 'quantity': quantity}
    components, groups, missing = [], [], []

    def choose(group_id, title, choices, minimum, maximum, defaults, *, modifier=False):
        if not isinstance(minimum, int) or not isinstance(maximum, int) or minimum < 0 or maximum < minimum:
            raise AdapterError('options_invalid', '官方选配数量无法确认，请重新选择餐品')
        valid = {str(c['code']): c for c in choices if isinstance(c, dict) and c.get('code')}
        selected = copy.deepcopy(overrides.get(group_id, defaults))
        if not isinstance(selected, dict):
            raise AdapterError('options_invalid', '选配格式无效')
        for code, qty in selected.items():
            if code not in valid or isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= 20:
                raise AdapterError('options_invalid', '选配必须来自当前餐品详情')
            low = valid[code].get('minQuantity', 0)
            high = valid[code].get('maxQuantity', maximum or 1)
            low = low if isinstance(low, int) else 0
            high = high if isinstance(high, int) and high > 0 else maximum or 1
            if qty > high or qty > 0 and qty < low:
                raise AdapterError('options_invalid', '该选配数量不在官方允许范围')
        if not minimum <= sum(selected.values()) <= maximum:
            missing.append(group_id)
        groups.append({'id': group_id, 'title': title, 'min': minimum, 'max': maximum,
                       'kind': 'modifier' if modifier else 'round',
                       'choices': [{'code': code, 'name': str(c.get('name', code)), 'quantity': selected.get(code, 0),
                                    'max': positive(c.get('maxQuantity'), maximum or 1)} for code, c in valid.items()]})
        return valid, selected

    def modifications(value, prefix):
        if not isinstance(value, dict):
            return None
        output = []
        for index, group in enumerate(value.get('items') or []):
            choices = group.get('values') or []
            defaults = {str(c['code']): c['selectedQuantity'] for c in choices
                        if isinstance(c, dict) and c.get('code') and isinstance(c.get('selectedQuantity'), int) and c['selectedQuantity'] > 0}
            valid, selected = choose(prefix + ':' + str(index), str(group.get('name') or '特调选项'), choices,
                                     group.get('minValues', 0), group.get('maxValues', len(choices)), defaults, modifier=True)
            for code, choice in valid.items():
                qty = selected.get(code, 0)
                key = choice.get('selectedKey') if qty else choice.get('unselectedKey')
                if qty and not isinstance(key, str):
                    raise AdapterError('options_invalid', '特调缺少官方 selectedKey，无法下单')
                if key:
                    output.append({'code': code, 'key': key, 'quantity': qty})
                if qty:
                    components.append(str(choice.get('name', code)))
        return {'values': output} if output else None

    main_mod = modifications(detail.get('modification'), 'mod:main')
    if main_mod:
        item['modification'] = main_mod
    rounds = []
    for group in detail.get('rounds') or []:
        ident = str(group['id'])
        choices = group.get('choices') or []
        defaults = {str(c['code']): positive(c.get('quantity')) for c in choices
                    if isinstance(c, dict) and c.get('code') and c.get('isDefault') == 1}
        valid, selected = choose(ident, str(group.get('name', '套餐选配')), choices,
                                 group.get('minQuantity', 1), group.get('maxQuantity', 1), defaults)
        combo = []
        for code, qty in selected.items():
            if not qty:
                continue
            choice = valid[code]
            entry = {'code': code, 'quantity': qty}
            mod = modifications(choice.get('modification'), 'mod:' + ident + ':' + code)
            if mod:
                entry['modification'] = mod
            combo.append(entry)
            components.append(str(choice.get('name', code)) + (f' ×{qty}' if qty != 1 else ''))
        rounds.append({'round': ident, 'comboItemList': combo})
    if rounds:
        item['roundList'] = rounds
    return item, components, groups, missing


class OrderSession:
    def __init__(self, actor):
        self.actor = actor
        self.lock = threading.RLock()
        self.touched = time.time()
        self.history = []
        self.transcript = []
        self.preferences = {}
        self.search_pending = None
        self.search_city = None
        self.failed_read = None
        self.stores = []
        self.store = None
        self.catalog = None
        self.candidates = []
        self.request = {'count': 1, 'preference': ''}
        self.detail = None
        self.selection_id = None
        self.overrides = {}
        self.item = None
        self.components = []
        self.options = []
        self.pending = None
        self.quote = None
        self.takeway = None
        self.order = None
        self.unknown = False
        self.scope = 'agent:' + digest(actor)[:16] + ':' + uuid.uuid4().hex
        self.phase = 'location'

    def context(self):
        meals = self.catalog.get('meals', {}) if self.catalog else {}
        target = requested_family(self.request.get('preference', ''), self.preferences.get('aliases'))
        # Keep a requested family visible even when it is beyond item 200.
        menu = sorted(meals.items(), key=lambda item: not family_matches(item[1].get('name', ''), target) if isinstance(item[1], dict) else True)
        return {'phase': self.phase, 'request': self.request, 'store': self.store,
                'pending_store_search': self.search_pending, 'search_city': self.search_city,
                'stores': [{'index': i, 'name': s.get('storeName')} for i, s in enumerate(self.stores, 1)],
                'menu': [{'code': str(code), 'name': m.get('name'), 'reference_price_yuan': m.get('currentPrice')}
                         for code, m in menu[:200] if isinstance(m, dict)],
                'menu_truncated': len(menu) > 200, 'preferences': self.preferences if self.preferences.get('enabled') else {'enabled': False},
                'selected_meal': {'code': self.detail.get('code'), 'name': self.detail.get('name'),
                                  'components': self.components} if self.detail else None,
                'recommendations': [{'index': i, 'code': c['code'], 'name': c['name']} for i, c in enumerate(self.candidates, 1)],
                'options': self.options, 'history': self.history[-8:]}


class OrderingAgent:
    def __init__(self, app, model, location, ledger=None, memory=None):
        self.app, self.model, self.location = app, model, location
        self.ledger = ledger or ConfirmationLedger()
        self.memory = memory or DiningMemory(self.ledger.path.parent / 'dining-memory.sqlite3')
        self.sessions = {}
        self.lock = threading.RLock()
        self.location_link = None

    def session(self, actor):
        with self.lock:
            if actor not in self.sessions:
                if len(self.sessions) >= 100:
                    expired = [k for k, s in self.sessions.items() if time.time() - s.touched > 3600]
                    for key in expired:
                        del self.sessions[key]
                    if len(self.sessions) >= 100:
                        raise AdapterError('session_limit', '会话过多，请稍后再试')
                state = OrderSession(actor)
                with self.ledger.connect() as db:
                    blocked = db.execute("SELECT scope FROM confirmations WHERE status IN ('in_flight','unknown') AND scope LIKE 'agent:%' LIMIT 1").fetchone()
                    previous = db.execute("SELECT order_id FROM confirmations WHERE actor_hash=? AND status='complete' AND order_id IS NOT NULL AND scope LIKE 'agent:%' ORDER BY created DESC LIMIT 1", (digest(actor),)).fetchone()
                if blocked:
                    state.unknown = True
                    state.scope = blocked[0]
                    state.phase = 'unknown'
                elif previous:
                    state.order = {'orderId': previous[0], 'status': '待查询', 'recovered': True}
                    state.phase = 'order'
                self.sessions[actor] = state
            state = self.sessions[actor]
            state.preferences = self.memory.summary(actor)
            state.touched = time.time()
            return state

    def view(self, s, reply='', suggestions=None):
        cart = None
        if s.detail:
            cart = {'name': s.detail.get('name'), 'quantity': s.request.get('count', 1),
                    'components': s.components, 'options': s.options,
                    'store': s.store, 'quote': s.quote,
                    'preview_id': s.pending['id'] if s.pending else None,
                    'expires_at': s.pending['expires'] if s.pending else None}
        return {'reply': reply, 'phase': s.phase, 'model': self.model.status(),
                'suggestions': suggestions or [], 'stores': s.stores if s.phase == 'stores' else [],
                'recommendations': s.candidates if s.phase == 'recommendations' else [],
                'cart': cart, 'order': s.order, 'unknown': s.unknown,
                'memory': self.memory.summary(s.actor),
                'can_confirm': bool(s.pending) and s.pending['expires'] > time.time() and not s.unknown,
                'needs_location': not bool(s.store)}

    def current(self, actor):
        s = self.session(actor)
        with s.lock:
            return self.view(s)

    def chat(self, actor, text='', action=None, payload=None):
        """Record both sides of every turn in RAM, including button commands.

        Raw transcripts never touch disk; only reduced dining preferences do.
        """
        s = self.session(actor)
        with s.lock:
            result = self._chat(actor, text, action, payload)
            user_text = text.strip() if isinstance(text, str) else ''
            if not user_text and action:
                user_text = {'select_store': '选择门店', 'select_meal': '选择餐品', 'choose_option': '修改选配',
                             'locate': '授权一次定位', 'confirm': '确认当前预览', 'takeway': '选择取餐方式'}.get(action, action)
            if user_text:
                turn = {'at': time.time(), 'user': redact(user_text), 'assistant': redact(result.get('reply', '')),
                        'phase': result.get('phase')}
                s.transcript = (s.transcript + [turn])[-20:]
                history_reply = '已按需展示账户信息；具体资产信息不进入语言模型。' if result.pop('_private_reply', False) else result.get('reply', '')
                s.history = (s.history + [{'role': 'user', 'text': redact(user_text, 400)},
                                          {'role': 'assistant', 'text': redact(history_reply, 700)}])[-8:]
            return result

    def recent(self, actor):
        s = self.session(actor)
        with s.lock:
            return {'turns': copy.deepcopy(s.transcript), 'memory': self.memory.summary(actor),
                    'notice': '仅本次服务运行期间最近20轮；内容已脱敏，只在内存中，重启清空。'}

    def _chat(self, actor, text='', action=None, payload=None):
        s = self.session(actor)
        with s.lock:
            payload = payload or {}
            try:
                if not isinstance(payload, dict):
                    raise AdapterError('invalid_input', '操作参数必须是对象')
                with self.ledger.connect() as db:
                    blocked = db.execute("SELECT 1 FROM confirmations WHERE status IN ('in_flight','unknown') AND scope LIKE 'agent:%' LIMIT 1").fetchone()
                if blocked:
                    s.unknown = True
                    s.phase = 'unknown'
                if s.unknown and action not in (None, 'status', 'feedback'):
                    return self.view(s, '存在提交结果未知的订单，请先在官方客户端核查，不能继续提交。')
                if s.order and action not in (None, 'status', 'confirm', 'feedback'):
                    return self.view(s, '这一单已经创建，请先查看订单；另点一单可发送“重新点餐”。', ['订单状态'])
                if action in ('locate', 'select_store', 'select_meal', 'choose_option', 'takeway'):
                    s.pending = None
                if action == 'confirm':
                    return self._confirm(s, payload.get('preview_id'))
                if action == 'locate':
                    located = self.location.resolve(payload.get('latitude'), payload.get('longitude'))
                    result = self._search(s, located['city'], located['keyword'], located['label'])
                    if located.get('attribution'):
                        result['reply'] += '\n\n定位地址数据：' + located['attribution'] + '。请按实际到店选择。'
                    return result
                if action == 'select_store':
                    return self._store(s, payload.get('store_code'))
                if action == 'select_meal':
                    return self._meal(s, payload.get('product_code'))
                if action == 'choose_option':
                    return self._modify(s, [payload])
                if action == 'takeway':
                    return self._takeway(s, payload.get('code'))
                if action == 'status':
                    return self._status(s)
                if action == 'feedback':
                    if not isinstance(payload.get('selection_id'), str) or not payload['selection_id']:
                        raise AdapterError('invalid_feedback', '评价按钮缺少选餐记录，请重新打开“评价上一餐”')
                    return self._feedback(s, payload.get('rating'), payload.get('selection_id'))
                if action is not None:
                    raise AdapterError('invalid_action', '此点餐操作不可用')
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise AdapterError('invalid_input', '请用2000字以内描述点餐需求')
                text = text.strip()
                # These operations neither authorize nor modify any order.
                memory_reply = self._memory_command(s, text)
                if memory_reply is not None:
                    return self.view(s, memory_reply)
                ratings = {'这餐好吃': 'like', '这餐不错': 'like', '这餐一般': 'neutral', '这餐不喜欢': 'dislike', '这餐不好吃': 'dislike'}
                if text in ratings:
                    return self._feedback(s, ratings[text])
                if not re.search(r'[?？“”"「」]|如果|假如|比如|朋友|同事|别人', text) and re.match(r'^(?:这餐|这个套餐|刚才的套餐|上一餐|上次那份|今天这份)', text):
                    rating = 'dislike' if re.search(r'不好吃|不喜欢|难吃|太腻', text) else 'neutral' if re.search(r'一般|还行', text) else 'like' if re.search(r'好吃|不错|很满意', text) else None
                    if rating:
                        return self._feedback(s, rating)
                if text in ('评价上一餐', '餐后反馈'):
                    selected = self.memory.summary(actor).get('last_selection')
                    return self.view(s, ('你最近选过的是“' + selected['name'] + '”。吃过后觉得怎样？评价只影响推荐，不代表付款记录。') if selected else '还没有可评价的选餐记录。选过真实餐品后，再告诉我这餐的感受。', ['这餐好吃', '这餐一般', '这餐不喜欢'] if selected else [])
                if text in ('为什么推荐', '为什么推荐这个', '为什么懂我'):
                    lines = [c['name'] + '：' + '；'.join(c.get('reasons', ['来自当前门店菜单'])) for c in s.candidates]
                    return self.view(s, '\n'.join(lines) if lines else '选店后，我会结合当前菜单、你这次的需求和已保存口味说明推荐理由。可以先发“我的偏好”。')
                if text in ('嗨', '你好呀', '早上好', '晚上好', '中午好', '在吗', '谢谢', '谢谢你'):
                    return self.view(s, '在呢。今天想稳稳吃点喜欢的，还是换个口味？' if '谢' not in text else '好呀，吃得合口味就好。吃完也可以告诉我这餐怎么样。', ['按老样子推荐', '换换口味', '我的偏好'])
                if text in ('账号说明', '账户说明', '我的账号', '我的账户', '登录状态') or re.search(r'(?:需要|怎么|是否|有没有|要不要).{0,6}登[录陆]|(?:获取|查看|查到).{0,6}账号信息', text):
                    return self.view(s, self._account_explanation())
                if text in ('我的积分', '查积分', '积分余额', '查询积分'):
                    return self._account(s, 'points')
                if text in ('我的优惠券', '我的券', '查优惠券', '我的卡包'):
                    return self._account(s, 'coupons')
                # Only a current, direct user confirmation reaches mutation. Never the model.
                if text in ('确认下单', '确认创建订单'):
                    return self._confirm(s, s.pending['id'] if s.pending else None)
                if text in ('订单状态', '查订单', '取餐码', '付款链接', '支付链接', '付好了吗'):
                    return self._status(s)
                if text == '你好':
                    return self.view(s, '我是麦合拍 MaiMate。今天吃点什么？你也可以先跟我聊聊，慢慢挑。', ['按老样子推荐', '我的偏好'])
                if text in ('帮助', 'help', '开始', '麦合拍MaiMate', '麦合拍', 'MaiMate'):
                    return self.view(s, HELP, ['我到店了，推荐套餐', '定位', '订单状态'])
                names = [m.get('name', '') for m in (s.catalog or {}).get('meals', {}).values() if isinstance(m, dict)]
                learned = self.memory.learn(actor, text, names)
                s.preferences = self.memory.summary(actor)
                if learned and not re.search(r'今天|这次|本次|帮我|来一|点一|想吃|给我|推荐', text):
                    return self.view(s, '记住了：' + '、'.join(learned) + '。以后推荐会参考这些偏好；你这次的要求优先。可发“我的偏好”查看。')
                if s.unknown:
                    return self.view(s, '存在提交结果未知的订单，已阻止继续下单。请在麦当劳官方客户端核查；刷新页面或重启不会解除锁定。')
                if s.order and text != '重新点餐':
                    if self.model.status()['configured']:
                        intent = self.model.interpret(text, s.context())
                        if intent['action'] == 'chat':
                            return self._intent(s, intent, text)
                    return self.view(s, '这一单已经创建。可以发“订单状态”查进度；如要另点一单，请明确发送“重新点餐”。', ['订单状态'])
                if text in ('算了', '不要了', '取消选餐', '清空餐单'):
                    s.pending = None
                    s.detail = s.item = s.quote = None
                    s.components = s.options = []
                    s.phase = 'recommendations' if s.candidates else 'location'
                    return self.view(s, '已清空未提交的选餐，没有创建订单。可以重新挑套餐。')
                if text in ('重新点餐', '重来', '重新开始'):
                    if s.unknown:
                        return self.view(s, '上一笔提交的结果仍未知，请先在麦当劳官方客户端核查，不能通过重新点餐绕过重复提交保护。')
                    # Reset the same session object so its lock and conversation
                    # stay consistent for in-flight web/Weixin requests.
                    old_history, old_transcript, old_lock = s.history, s.transcript, s.lock
                    s.__dict__.update(OrderSession(actor).__dict__)
                    s.lock, s.history, s.transcript = old_lock, old_history, old_transcript
                    s.preferences = self.memory.summary(actor)
                    return self.view(s, '开启新的一单，保留你的饮食偏好。告诉我门店名，或先授权定位。', ['定位'])
                if text in ('重试', '重新查询', '再查一次', '重新查一下'):
                    return self._retry(s)
                if s.search_pending and text.removesuffix('市') in CITY_NAMES:
                    return self._search(s, text.removesuffix('市'), s.search_pending['keyword'], s.search_pending.get('label'))
                if re.search(r'过敏|乳糖不耐|麸质不耐|清真', text):
                    s.pending = None
                    return self.view(s, '这涉及配料或过敏风险，当前接口不能确认交叉接触。请先向门店核实，再继续选餐；我不会把菜单名称当作安全证明。')
                if text in ('定位', '我到店了', '附近门店') and not s.store:
                    url = self.location_link(actor) if self.location_link else None
                    reply = '点“使用当前位置”授权一次定位，我会查出所在区域的真实门店供你选择。'
                    if actor.startswith('weixin:'):
                        reply = ('打开这个单次定位链接，授权后回到微信发“推荐套餐”：\n' + url) if url else '当前微信通道不能直接读取手机定位。可以发当前门店名；如要使用微信一键定位，请在电脑的连接设置中启用定位通道。'
                    return self.view(s, reply, ['使用当前位置'])
                if text == '重新算价' and s.detail:
                    return self._quote(s)
                if text in ('按老样子', '按老样子推荐', '你了解我，帮我选', '来点新鲜的', '换换口味'):
                    if text.startswith('按') or text.startswith('你了解'):
                        if not s.preferences.get('enabled') or not (s.preferences.get('likes') or s.preferences.get('often_selected') or s.preferences.get('insights')):
                            return self.view(s, '还没有足够的口味依据。你更想吃牛肉、鸡肉，还是先看看1+1超值？也可以说“记住，我喜欢……”让我以后参考。')
                    s.pending = None
                    s.request['explore'] = text in ('来点新鲜的', '换换口味')
                    # These phrases replace the previous main-meal request,
                    # while keeping drink/preparation needs for this order.
                    previous = self._preference(s)
                    s.request['preference'] = '；'.join(term for term in ('无糖', '含糖', '普通可乐', '去冰', '正常冰', '不辣') if term in previous)
                    return self._recommend(s) if s.store else self.view(s, '好，' + ('这次试试新口味' if s.request['explore'] else '参考你已经保存的口味来选') + '。先告诉我城市和门店，或授权一次定位，我再查实际可选餐品。', ['使用当前位置'])
                if s.quote:
                    chosen_way = next((w for w in s.quote['takeways'] if text in (w['code'], w.get('title')) or text in ('堂食', '打包', '外带') and self._way_matches('堂食' if text == '堂食' else '打包', w)), None)
                    if chosen_way:
                        return self._takeway(s, chosen_way['code'])
                if text in ('查看搭配', '查看选配', '查看特调') and s.options:
                    s.pending = None
                    lines = [g['title'] + '：' + ' / '.join(c['name'] for c in g['choices']) for g in s.options]
                    return self.view(s, '\n\n'.join(lines) + '\n\n可直接回复上面的选项名称。调整后会重新算价。')
                # Number/name commands remain usable without any model key.
                match = re.fullmatch(r'(?:选店|门店|选门店)\s*(\d+)', text)
                if match:
                    return self._store_index(s, int(match[1]))
                match = re.fullmatch(r'(?:套餐|方案|选|第)\s*(\d+)(?:个|号)?', text)
                if match:
                    return self._meal_index(s, int(match[1]))
                if text.isdigit() and s.phase in ('stores', 'recommendations'):
                    return self._store_index(s, int(text)) if s.phase == 'stores' else self._meal_index(s, int(text))
                if text in ('菜单', '查看菜单', '看看菜单'):
                    return self._menu(s)
                if text in ('取消预算', '不限预算', '不要预算限制'):
                    s.pending = None
                    s.request.pop('budget_cents', None)
                    return self._recommend(s) if s.store else self.view(s, '已取消本单预算限制。先选门店即可继续。')
                if self.model.status()['configured']:
                    try:
                        intent = self.model.interpret(text, s.context())
                    except AdapterError:
                        # If a requested change could not be understood, the
                        # old checkout must not remain confirmable.
                        s.pending = None
                        raise
                else:
                    intent = self._fallback(s, text)
                if requested_family(text, s.preferences.get('aliases')):
                    intent['preference'] = canonical(text, s.preferences.get('aliases'))[:240]
                    # Alias translation belongs to food selection, never location.
                    if intent['action'] == 'search' and not intent.get('city') and not re.search(r'店|广场|路|市|区|中心', text):
                        intent['action'] = 'recommend'
                return self._intent(s, intent, text)
            except AdapterError as exc:
                if exc.code in ('price_changed', 'confirmation_invalid'):
                    s.pending = None
                return self.view(s, str(exc), ['重新算价'] if s.detail and not s.unknown else [])

    def _memory_command(self, s, text):
        commands = {'清除偏好': 'clear', '清空偏好': 'clear', '清除记忆': 'clear', '忘记我的偏好': 'clear',
                    '关闭记忆': 'disable', '不要记住我的偏好': 'disable', '暂停记忆': 'disable',
                    '开启记忆': 'enable', '恢复记忆': 'enable'}
        if text in commands:
            operation = commands[text]
            self.memory.control(s.actor, operation)
            s.preferences = self.memory.summary(s.actor)
            s.history = []
            return {'clear': '已清除你的饮食偏好、选择统计和餐后反馈。',
                    'disable': '已关闭偏好学习和使用。需要时发“开启记忆”；已有偏好可用“清除偏好”删除。',
                    'enable': '已开启本地偏好记忆。只保存饮食标签与选择统计，你这次的要求始终优先。'}[operation]
        match = re.fullmatch(r'(?:忘记|忘掉|删除偏好)[：:\s]*(.{1,80})', text)
        if match:
            removed = self.memory.forget(s.actor, match[1])
            s.preferences = self.memory.summary(s.actor)
            s.history = []
            return '已忘记：' + '、'.join(removed) + '。' if removed else '没有找到这项偏好。可发“我的偏好”查看，或“清除偏好”全部删除。'
        if text in ('口味复盘', '我最近的口味', '你越来越懂我了吗'):
            memory = self.memory.summary(s.actor)
            if not memory['enabled']:
                return '记忆已关闭，这段时间不会分析或使用你的口味。需要时发“开启记忆”。'
            lines = ['我会把你明确说过的喜好放在前面，再参考多次选择和最近90天的餐后评价。']
            for row in memory['insights']:
                label = {'positive': '最近评价偏好', 'negative': '最近评价偏低，会少推荐', 'mixed': '评价一般或有变化，不下定论'}[row['signal']]
                lines.append(row['name'] + '：' + label + f"（{row['samples']}次评价）")
            if not memory['insights']:
                lines.append('还没有餐后评价。选过餐品后，可以发“评价上一餐”。')
            lines.append('明确喜好不会被一次评价覆盖；旧评价会逐渐降低影响。可以随时忘记某项或关闭记忆。')
            return '\n'.join(lines)
        if text in ('我的偏好', '查看偏好', '我的口味', '你记住了什么', '我的记忆'):
            memory = self.memory.summary(s.actor)
            lines = ['本地偏好记忆：' + ('已开启' if memory['enabled'] else '已关闭，当前不会使用')]
            lines.append('明确偏爱：' + ('、'.join(memory['likes']) or '尚未记录'))
            lines.append('明确不喜欢：' + ('、'.join(memory['dislikes']) or '尚未记录'))
            if memory['often_selected']:
                lines.append('多次选择（不代表已付款）：' + '、'.join(v['name'] + f"（{v['selections']}次）" for v in memory['often_selected']))
            if memory['insights']:
                lines.append('餐后反馈：' + '、'.join(v['name'] + '（' + {'positive': '好评', 'negative': '少推荐', 'mixed': '待了解'}[v['signal']] + '）' for v in memory['insights']))
            lines += ['套餐别名：穷鬼套餐＝1+1超值；具体可售搭配和价格以当前门店为准。',
                      '可发“记住，我喜欢无糖饮料”“忘记无糖”“清除偏好”“关闭记忆”。']
            return '\n'.join(lines)
        return None

    def _feedback(self, s, rating, selection_id=None):
        if rating not in ('like', 'neutral', 'dislike'):
            raise AdapterError('invalid_feedback', '请选择好吃、一般或不喜欢')
        target = self.memory.feedback(s.actor, rating, selection_id=selection_id)
        if not target:
            return self.view(s, '这次评价没有保存。请先开启记忆，并用“评价上一餐”核对最近选过的餐品；旧评价按钮不会改动新的选餐。')
        s.preferences = self.memory.summary(s.actor)
        effect = {'like': '下次会适当优先考虑', 'neutral': '先不把它当作你特别偏爱的餐品', 'dislike': '下次会少推荐一些'}[rating]
        return self.view(s, f'记下了，针对“{target}”：{effect}。这只是这餐的反馈，不会覆盖你明确说过的长期喜好；再次评价会更新同一次记录。', ['口味复盘', '忘记' + target])

    @staticmethod
    def _account_explanation():
        return ('麦当劳和微信是两份独立授权。\n'
                '查询和下单使用当前麦当劳 MCP Token 所授权的账号；微信扫码只负责收发消息，不会自动切换麦当劳账号。\n'
                '有效 Token 通常不需要在这里再登录；若官方收银台要求登录或验证，按页面完成即可。换麦当劳账号需在官方平台重新授权并更新本项目 Token。\n'
                '当前接口可查询积分、优惠券及获授权的订单信息，没有发现用于读取完整个人资料、手机号或会员等级的专用接口。\n'
                '发“我的积分”或“我的优惠券”可按需查询；不会自动扫描历史订单来建立偏好。')

    def _account(self, s, kind):
        # Read account assets only after a direct request. Neither these data nor
        # the account identifier enter the model context or preference database.
        try:
            data = self._query(s, 'query-my-account' if kind == 'points' else 'query-my-coupons',
                               {} if kind == 'points' else {'page': '1', 'pageSize': '20'}, 'account', {'kind': kind})
        except AdapterError as exc:
            if exc.code in ('unauthorized', 'forbidden'):
                return self.view(s, '麦当劳账户授权无效或已过期，请在官方 MCP 平台重新授权并更新本项目 Token；无需重填模型 key 或重新绑定微信。')
            raise
        if not isinstance(data, dict):
            raise AdapterError('account_invalid', '官方账户数据暂时不可用，请稍后重试')
        if kind == 'points':
            lines = ['当前麦当劳 Token 授权账号的积分：']
            for key, label in (('availablePoint', '可用积分'), ('accumulativePoint', '累计积分'),
                               ('currentMouthExpirePoint', '本月将过期'), ('nextMouthExpirePoint', '下月将过期')):
                value = str(data.get(key, ''))
                lines.append(label + '：' + (value if re.fullmatch(r'\d+(?:\.\d+)?', value) else '官方未返回'))
            lines.append('积分查询不等于现金余额；没有扣积分、领券或修改账户。')
        else:
            coupons = data.get('coupons')
            if not isinstance(coupons, list):
                raise AdapterError('account_invalid', '官方优惠券格式暂时无法识别')
            lines = ['当前账号的优惠券（第1页，最多展示10张）：']
            lines += [f"{i}. {redact(c.get('title') or '官方未返回券名', 80)}" for i, c in enumerate(coupons[:10], 1) if isinstance(c, dict)]
            if not coupons:
                lines.append('本次没有返回优惠券。')
            lines.append('卡包里的券不保证可用于当前门店或订单；是否适用仍需官方校验，没有自动用券或领券。')
        result = self.view(s, '\n'.join(lines))
        result['_private_reply'] = True
        return result

    @staticmethod
    def _preference(s):
        explicit = s.request.get('preference', '')
        memory = s.preferences if s.preferences.get('enabled') else {}
        defaults = list(memory.get('likes', []))
        # A current named food/family wins over remembered food choices.
        if family(explicit, memory.get('aliases')) or re.search(r'牛|鸡|鱼|堡|薯条|麦旋风|玉米|套餐', explicit):
            defaults = [p for p in defaults if p in ('无糖饮料', '去冰')]
        if re.search(r'含糖|有糖|普通可乐|正常可乐|不要无糖|不喝无糖|不要零度', explicit):
            defaults = [p for p in defaults if p != '无糖饮料']
            explicit = re.sub(r'不要无糖|不喝无糖|不要零度', '含糖饮料', explicit)
        if re.search(r'加冰|正常冰|要冰|不要去冰', explicit):
            defaults = [p for p in defaults if p != '去冰']
            explicit = explicit.replace('不要去冰', '正常冰')
        return canonical('；'.join(defaults + [explicit]), memory.get('aliases'))

    @staticmethod
    def _question(intent, fallback):
        question = intent.get('question')
        if not isinstance(question, str) or not question.strip():
            return fallback
        if re.search(r'(?:我|我们|正在|马上|已经|将为|帮你|帮您).{0,30}(?:查|处理|付款|支付|下单|重试|操作)', question):
            return fallback
        return question

    def _query(self, s, tool, args, retry_kind, retry_data):
        try:
            data = self.app.query(tool, args)
        except AdapterError as exc:
            s.failed_read = {'kind': retry_kind, 'data': retry_data}
            label = {'search': '查询门店', 'store': '读取门店菜单', 'meal': '读取餐品详情',
                     'quote': '查询官方价格', 'status': '查询订单状态', 'account': '查询账户信息'}[retry_kind]
            if exc.code == 'business_city_required':
                s.search_pending = {'keyword': args.get('keyword', '')}
                raise AdapterError(exc.code, '官方门店查询需要有效城市。请回复城市名，例如“北京”；我会保留你想吃的餐品。') from None
            raise AdapterError(exc.code, label + '未成功：' + str(exc) + '。可回复“重试”重新执行这次只读查询；不会重发下单请求。') from None
        s.failed_read = None
        return data

    def _retry(self, s):
        failed = s.failed_read
        if not failed:
            return self.view(s, '没有待重试的查询。可以发门店名、查看菜单或重新算价。')
        kind, data = failed['kind'], failed['data']
        if kind == 'search':
            return self._search(s, **data)
        if kind == 'store':
            return self._store(s, data['code'])
        if kind == 'meal':
            return self._meal(s, data['code'])
        if kind == 'quote' and s.detail:
            return self._quote(s)
        if kind == 'status':
            return self._status(s)
        if kind == 'account':
            return self._account(s, data['kind'])
        return self.view(s, '本轮选餐状态已改变，请重新选择餐品后查询。')

    def _menu(self, s):
        if not s.catalog:
            return self.view(s, '先选定你正在的门店，我再展示该店实时菜单。', ['定位'])
        s.pending = None
        s.detail = s.item = s.quote = None
        s.options = s.components = []
        candidates = []
        for code, meal in s.catalog['meals'].items():
            if not isinstance(meal, dict):
                continue
            price = money_cents(meal.get('currentPrice'))
            if price is not None and price > 0 and not re.search(r'随单购|会员卡|早餐卡|麦金卡|券|兑换', str(meal.get('name', ''))):
                candidates.append({'code': str(code), 'name': str(meal.get('name', '')), 'reference_cents': price,
                                   'quantity': s.request['count']})
        s.candidates = sorted(candidates, key=lambda meal: (meal['reference_cents'], meal['name']))[:12]
        s.phase = 'recommendations'
        lines = [s.store.get('storeName', '') + '当前菜单，按菜单价列出前12项：']
        lines += [f"套餐{i}  {meal['name']}  {amount(meal['reference_cents'])}/份" for i, meal in enumerate(s.candidates, 1)]
        lines.append('回复“套餐1”选餐，或说想吃的餐品名称。菜单价为参考，最终金额需要官方算价。')
        return self.view(s, '\n\n'.join(lines))

    def _fallback(self, s, text):
        match = re.search(r'(\d{1,2})\s*(?:个)?(?:人|份)', text)
        budget = re.search(r'(?:预算|人均|每人)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)(?:以内|以下)?', text)
        intent = {'action': 'recommend', 'preference': text}
        if match:
            intent['count'] = int(match[1])
        if budget:
            intent['budget_yuan'] = float(budget[1])
        if s.detail:
            for group in s.options:
                selected = [c for c in group['choices'] if c['name'] and c['name'] in text]
                if len(selected) == 1:
                    return {**intent, 'action': 'modify', 'changes': [{'round_id': group['id'], 'code': selected[0]['code'], 'quantity': 1}]}
        for index, candidate in enumerate(s.candidates, 1):
            if candidate['name'] == text or text in ('就要' + candidate['name'], '选' + candidate['name']):
                return {'action': 'select_meal', 'meal_index': index}
        if not s.store:
            if text in ('推荐', '推荐套餐', '吃什么', '我到店了，推荐套餐'):
                return intent
            location = re.split(r'[,，;；]', text)[0].strip()
            for city in ('上海', '北京', '广州', '深圳', '杭州', '南京', '成都', '重庆', '武汉', '天津', '苏州', '西安', '长沙', '厦门', '郑州', '宁波', '青岛', '济南', '合肥', '福州', '昆明', '沈阳', '大连'):
                if city in location:
                    return {**intent, 'action': 'search', 'city': city, 'keyword': location.replace(city, '').removeprefix('市') or '麦当劳'}
            if '店' in location or '麦当劳' in location or '广场' in location or '商场' in location:
                return {**intent, 'action': 'search', 'keyword': location}
        return intent

    def _intent(self, s, intent, text):
        if intent['action'] == 'chat':
            reply = intent.get('question') or '我在，今天想聊聊口味，还是一起挑一餐？'
            if re.search(r'https?://|¥|\d+\s*元|(?:随时|保证|肯定|一定|绝对).{0,8}(?:有售|能点|能买|安排|有货)|(?:已|正在|马上|我帮你|我帮您).{0,14}(?:下单|付款|支付|扣款|查询|查好|保存|记住|联系)|取餐码|订单号|token|api.?key', reply, re.I):
                reply = '我在。聊口味可以慢慢来；涉及菜单、价格和订单时，我会先查到真实结果再告诉你。'
            return self.view(s, redact(reply, 200), ['我的偏好', '按老样子推荐'])
        # Any substantive change invalidates the displayed checkout confirmation.
        if intent['action'] not in ('status', 'help', 'clarify', 'chat'):
            s.pending = None
        if intent.get('count') is not None:
            if isinstance(intent['count'], bool) or not 1 <= intent['count'] <= 20:
                raise AdapterError('invalid_input', '每单支持1–20份，请明确数量')
            s.request['count'] = intent['count']
        if intent.get('budget_yuan') is not None:
            cents = money_cents(intent['budget_yuan'])
            if cents is None or not 100 <= cents <= 100000:
                raise AdapterError('invalid_input', '预算请设为1–1000元，或不设置预算')
            s.request['budget_cents'] = cents
        if intent.get('preference'):
            s.request['preference'] = canonical(intent['preference'], s.preferences.get('aliases'))
        if intent.get('takeway'):
            s.request['takeway'] = intent['takeway']
        action = intent['action']
        if action == 'account':
            query_words = r'查|看|多少|余额|剩|还有|我的|我有'
            asset = '积分' if intent.get('account_kind') == 'points' else r'优惠券|我的券|卡包'
            if intent.get('account_kind') in ('points', 'coupons') and re.search(query_words, text) and re.search(asset, text):
                return self._account(s, intent['account_kind'])
            return self.view(s, self._account_explanation())
        if action == 'preferences':
            return self.view(s, self._memory_command(s, '我的偏好'))
        if action == 'menu':
            return self._menu(s)
        if action == 'status':
            return self._status(s)
        if action == 'help':
            return self.view(s, HELP)
        if action == 'clarify':
            fallback = '请告诉我城市和门店名，选店后我会查询真实菜单。' if not s.store else '请明确想选的餐品或要修改的搭配。'
            return self.view(s, self._question(intent, fallback))
        if action == 'search' and intent.get('keyword'):
            return self._search(s, intent.get('city'), intent['keyword'])
        if action == 'select_store':
            return self._store_index(s, intent.get('store_index'))
        if action == 'select_meal':
            if intent.get('meal_index'):
                return self._meal_index(s, intent['meal_index'])
            if intent.get('product_codes'):
                return self._meal(s, intent['product_codes'][0])
            return self.view(s, '请选择刚才列出的套餐，或直接说餐品名称。')
        if action == 'modify':
            if intent.get('changes'):
                return self._modify(s, intent['changes'])
            if intent.get('takeway') and s.quote:
                for way in s.quote['takeways']:
                    if self._way_matches(intent['takeway'], way):
                        return self._takeway(s, way['code'])
            if s.detail and re.search(r'份|人|数量|重新算价', text):
                return self._quote(s)
            return self.view(s, '请从当前餐品的可选搭配中选择，我会重新算价。')
        if s.store:
            if text == '重新算价' and s.detail:
                return self._quote(s)
            return self._recommend(s, intent.get('product_codes'))
        if s.stores:
            lines = ['定位已有结果，请选择你正在的门店：']
            lines.extend(f"{i}. {v.get('storeName', '')}\n{v.get('address', '')}" for i, v in enumerate(s.stores, 1))
            s.phase = 'stores'
            return self.view(s, '\n\n'.join(lines) + '\n\n回复“选店1”。')
        return self.view(s, '先确定你正在的门店。点“使用当前位置”，或直接告诉我门店名。', ['使用当前位置'])

    def _search(self, s, city, keyword, label=None):
        if not isinstance(keyword, str) or not 0 < len(keyword.strip()) <= 160:
            raise AdapterError('invalid_input', '请提供可识别的门店名或地标')
        city = city or next((name for name in CITY_NAMES if name in keyword), None) or s.search_city
        if not city:
            s.pending = None
            s.search_pending = {'keyword': keyword.strip(), 'label': label}
            return self.view(s, '这家门店在哪个城市？官方查询需要城市名，直接回复如“北京”即可；已保留你的门店名和选餐需求。')
        if not isinstance(city, str) or not 0 < len(city.strip()) <= 80:
            raise AdapterError('invalid_input', '请提供有效城市名')
        args = {'searchType': 2, 'beType': 1, 'keyword': keyword.strip(), 'city': city.strip()}
        data = self._query(s, 'query-nearby-stores', args, 'search', {'city': city, 'keyword': keyword, 'label': label})
        if not isinstance(data, list):
            raise AdapterError('stores_invalid', '官方门店返回格式无法识别')
        s.pending = None
        s.search_pending = None
        s.search_city = city.strip()
        s.store = s.catalog = s.detail = s.item = s.quote = None
        s.options = s.components = s.candidates = []
        s.stores = [{k: v for k, v in store.items() if k in ('storeCode', 'storeName', 'address', 'businessStatus', 'distance', 'businessHours')}
                    for store in data if isinstance(store, dict) and store.get('storeCode') and store.get('businessStatus') is not False][:8]
        s.phase = 'stores'
        if not s.stores:
            return self.view(s, '这个位置暂时没有查到可用门店。换一个门店名或附近地标试试。')
        prefix = f'定位到{label}。' if label else ''
        lines = [prefix + '请选择你正在的门店，避免下错店：']
        lines.extend(f"{i}. {store.get('storeName', '')}\n{store.get('address', '')}" for i, store in enumerate(s.stores, 1))
        return self.view(s, '\n\n'.join(lines) + '\n\n直接回复“选店1”。')

    def _store_index(self, s, index):
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(s.stores):
            raise AdapterError('invalid_selection', '请从当前列出的门店中选择')
        return self._store(s, s.stores[index - 1]['storeCode'])

    def _store(self, s, code):
        store = next((v for v in s.stores if v['storeCode'] == code), None)
        if not store:
            raise AdapterError('invalid_selection', '该门店不在本轮搜索结果中')
        s.pending = None
        data = self._query(s, 'query-meals', {'storeCode': code, 'orderType': 1, 'beType': 1}, 'store', {'code': code})
        if not isinstance(data, dict) or not isinstance(data.get('meals'), dict):
            raise AdapterError('menu_invalid', '当前门店菜单不可用，请换一家门店')
        s.store = store
        s.catalog = {'meals': data['meals'], 'categories': data.get('categories', [])}
        s.detail = s.item = s.quote = s.takeway = None
        s.options = s.components = []
        return self._recommend(s)

    def _recommend(self, s, codes=None):
        if not s.store or not s.catalog:
            raise AdapterError('location_required', '请先定位或告诉我门店名')
        s.pending = None
        s.detail = s.item = s.quote = None
        s.options = s.components = []
        s.candidates = []
        s.phase = 'recommendations'
        meals = s.catalog['meals']
        preference = self._preference(s)
        target_family = requested_family(s.request.get('preference', ''), s.preferences.get('aliases'))
        # Recognized colloquial names resolve against real menu names before
        # consulting a model. Never fabricate a SKU, price, or 1+1 composition.
        if target_family:
            codes = [str(code) for code, meal in meals.items()
                     if isinstance(meal, dict) and family_matches(meal.get('name', ''), target_family)]
            if not codes:
                s.candidates = []
                s.phase = 'recommendations'
                return self.view(s, '“穷鬼套餐”按1+1超值理解，但这家店当前可售菜单没有返回可核实的1+1商品。不能把其他套餐或两件单品冒充该优惠；可换店或查看菜单。', ['查看菜单'])
        if self.model.status()['configured'] and not codes:
            intent = self.model.interpret('根据当前门店真实菜单和我的最新需求，推荐最多三个餐品。长期偏好只用于排序，本次要求优先。', s.context())
            codes = intent.get('product_codes')
            if intent.get('action') == 'clarify' and not codes:
                return self.view(s, self._question(intent, '想吃什么口味？也可以先查看菜单。'))
            # Model ranking is a suggestion. Validated local candidates and
            # explicit preferences still decide the final order.
            codes = [str(code) for code in codes or [] if str(code) in meals][:3]
        pool = []
        memory = s.preferences if s.preferences.get('enabled') else {}
        for code, meal in meals.items():
            if not isinstance(meal, dict):
                continue
            name = str(meal.get('name', ''))
            cents = money_cents(meal.get('currentPrice'))
            if cents is None or cents <= 0 or re.search(r'随单购|会员卡|早餐卡|麦金卡|券|兑换', name):
                continue
            if s.request.get('budget_cents') and cents > s.request['budget_cents']:
                continue
            if re.search(r'不辣|不要辣|不能辣', preference) and '辣' in name:
                continue
            if target_family and codes and str(code) not in codes:
                continue
            if not target_family:
                explicit = s.request.get('preference', '')
                avoided = [w for w in ('牛', '鸡', '鱼') if re.search(r'(?:不想吃|不吃|不要|别推荐|不喜欢)[^，。；;]{0,3}' + w, explicit)]
                if any(w in name for w in avoided):
                    continue
                wanted = next((w for w in ('牛', '鸡', '鱼') if w in explicit and w not in avoided), None)
                if wanted and wanted not in name:
                    continue
                if not re.search(r'套餐|堡|卷|饭|小食|组合|拼盘|1\s*\+\s*1|随心配', name):
                    continue
            score = (40 if '套餐' in name else 0) + (15 if '堡' in name else 0)
            reasons = ['来自当前门店可售菜单']
            if s.request.get('budget_cents'):
                reasons.append('菜单价在本次预算内，最终以官方算价为准')
            if codes and str(code) in codes and not target_family:
                score += (3 - codes.index(str(code))) * 8
            for liked in memory.get('likes', []):
                if normalized(liked) in normalized(name) or family_matches(name, family(liked)):
                    score += 80
                    reasons.append('你明确偏爱' + liked)
            for disliked in memory.get('dislikes', []):
                if (normalized(disliked) in normalized(name) or family_matches(name, family(disliked))) and normalized(disliked) not in normalized(s.request.get('preference', '')):
                    score -= 100
                    reasons.append('你明确不喜欢的口味，会降低排序')
            for selected in memory.get('often_selected', []):
                if normalized(selected['name']) in normalized(name) or family_matches(name, family(selected['name'])):
                    score += min(20, selected['selections'] * 3)
                    reasons.append('你多次选过，不代表已购买')
            for feedback in memory.get('insights', []):
                if normalized(feedback['name']) == normalized(name):
                    score += feedback['score']
                    reasons.append('参考最近餐后反馈（' + str(feedback['samples']) + '次）')
            familiar = [v['name'] for v in memory.get('often_selected', [])] + memory.get('likes', [])
            if memory.get('last_selection'):
                familiar.append(memory['last_selection']['name'])
            if s.request.get('explore') and any(normalized(v) in normalized(name) or family_matches(name, family(v)) for v in familiar):
                score -= 100
                reasons.append('这次想换口味，熟悉餐品往后放一点')
            pool.append({'code': str(code), 'name': name, 'reference_cents': cents,
                         'quantity': s.request['count'], 'score': score, 'reasons': reasons})
        pool.sort(key=lambda v: (-v['score'], v['reference_cents'], v['code']))
        s.candidates = [{k: v for k, v in p.items() if k != 'score'} for p in pool[:3]]
        s.phase = 'recommendations'
        if not s.candidates:
            return self.view(s, '这家店的当前菜单里没找到符合需求的候选。可以改口味、取消预算限制或换店。')
        lines = [s.store.get('storeName', '') + '，给你这几个选择：']
        if target_family:
            lines.append('穷鬼套餐＝1+1超值；下面是这家店当前真实可选的商品。')
        elif memory.get('likes') or memory.get('often_selected'):
            lines.append('已参考你的本地口味偏好，本次要求优先。')
        for i, c in enumerate(s.candidates, 1):
            lines.append(f"套餐{i}  {c['name']}\n菜单价 {amount(c['reference_cents'])}/份 ×{c['quantity']}\n" + '；'.join(c['reasons'][1:] or c['reasons']))
        suffix = '选一个即可查看默认搭配与官方应付金额；例如“套餐1”，也可以说“饮料换无糖”。'
        if not self.model.status()['configured']:
            suffix += '\n当前是指令模式；连接千问或 DeepSeek 后可用更自由的说法。'
        return self.view(s, '\n\n'.join(lines) + '\n\n' + suffix)

    def _meal_index(self, s, index):
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(s.candidates):
            raise AdapterError('invalid_selection', '请从当前推荐中选择，例如“套餐1”')
        return self._meal(s, s.candidates[index - 1]['code'])

    def _meal(self, s, code):
        if not s.store or not s.catalog or code not in s.catalog['meals']:
            raise AdapterError('invalid_selection', '餐品不在当前门店菜单中')
        previous_code = s.detail.get('code') if s.detail else None
        s.pending = None
        detail = self._query(s, 'query-meal-detail', {'storeCode': s.store['storeCode'], 'orderType': 1, 'beType': 1, 'code': code}, 'meal', {'code': code})
        if not isinstance(detail, dict) or detail.get('code') != code:
            raise AdapterError('detail_invalid', '官方详情未匹配所选餐品，无法继续算价')
        s.detail = detail
        if previous_code != code:
            s.selection_id = uuid.uuid4().hex
            self.memory.observe(s.actor, detail.get('name'), s.selection_id)
            s.preferences = self.memory.summary(s.actor)
        s.overrides = {}
        s.takeway = None
        s.quote = None
        _, s.components, s.options, _ = build_item(detail, s.request['count'])
        preference = self._preference(s)
        if preference and s.options and self.model.status()['configured']:
            interpreted = self.model.interpret('请将我之前的饮料、口味等要求应用到刚选的套餐。只选择 context.options 中真实可选项。若没有要求需要修改，返回 action=modify, changes=[]；无法满足则 clarify。', s.context())
            if interpreted['action'] == 'clarify':
                s.phase = 'options'
                return self.view(s, self._question(interpreted, '当前套餐无法满足选配要求，请在可选搭配中选择。'))
            if interpreted.get('changes'):
                self._apply_changes(s, interpreted['changes'])
                _, s.components, s.options, _ = build_item(detail, s.request['count'], s.overrides)
        if preference and re.search(r'无糖|零度|不要冰|不加冰|去冰', preference):
            changes = []
            for pattern, trigger in ((r'无糖|零度', r'无糖|零度'), (r'去冰|无冰|不加冰', r'不要冰|不加冰|去冰')):
                if not re.search(trigger, preference):
                    continue
                if re.search(pattern, ' '.join(s.components) + ' ' + str(s.detail.get('name', ''))):
                    continue
                found = next(((g, c) for g in s.options for c in g['choices'] if re.search(pattern, c['name'])), None)
                if not found:
                    s.phase = 'options'
                    return self.view(s, '当前套餐未找到可核实的' + ('无糖饮料' if '无糖' in pattern else '去冰') + '选项，请选择搭配或换餐；没有替你忽略这项要求。')
                group, choice = found
                changes.append({'round_id': group['id'], 'code': choice['code'], 'quantity': 1})
            if changes:
                return self._modify(s, changes)
        return self._quote(s)

    def _modify(self, s, changes):
        if not s.detail:
            raise AdapterError('selection_required', '请先选择套餐，再修改搭配')
        s.pending = None
        self._apply_changes(s, changes)
        return self._quote(s)

    def _apply_changes(self, s, changes):
        updated = copy.deepcopy(s.overrides)
        for change in changes:
            group_id = str(change.get('round_id', ''))
            code, qty = change.get('code'), change.get('quantity', 1)
            group = next((g for g in s.options if g['id'] == group_id), None)
            if not group or not any(c['code'] == code for c in group['choices']):
                raise AdapterError('options_invalid', '这个选配不在当前套餐可选项中')
            if isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= 20:
                raise AdapterError('options_invalid', '选配数量无效')
            if group['max'] == 1:
                updated[group_id] = {code: qty} if qty else {}
            else:
                selected = updated.setdefault(group_id, {c['code']: c['quantity'] for c in group['choices'] if c['quantity']})
                selected[code] = qty
        build_item(s.detail, s.request['count'], updated)  # validate before committing
        s.overrides = updated

    @staticmethod
    def _way_matches(text, way):
        title = str(way.get('title', ''))
        return ('堂食' in title or '店内' in title) if text == '堂食' else bool(re.search(r'打包|外带|带走|自取', title))

    def _quote(self, s):
        s.pending = None
        s.quote = None
        s.item, s.components, s.options, missing = build_item(s.detail, s.request['count'], s.overrides)
        s.phase = 'options'
        if missing:
            titles = [g['title'] for g in s.options if g['id'] in missing]
            choices = '\n'.join(g['title'] + '：' + ' / '.join(c['name'] for c in g['choices']) for g in s.options if g['id'] in missing)
            return self.view(s, '这个套餐还需要选择：' + '、'.join(titles) + '。\n' + choices + '\n直接回复选项名称，选好后我会查官方应付金额。')
        data = self._query(s, 'calculate-price', {'storeCode': s.store['storeCode'], 'orderType': 1, 'beType': 1, 'items': [s.item]}, 'quote', {})
        total = data.get('price') if isinstance(data, dict) else None
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise AdapterError('quote_invalid', '官方算价未返回整数分金额，不能创建订单')
        ways = [w for w in data.get('takeWayList', []) if isinstance(w, dict) and isinstance(w.get('code'), str) and w['code']]
        if not ways:
            raise AdapterError('quote_invalid', '官方没有返回可用取餐方式，暂时无法下单')
        if s.takeway not in {w['code'] for w in ways}:
            s.takeway = ways[0]['code'] if len(ways) == 1 else None
            if s.request.get('takeway'):
                chosen = next((w for w in ways if self._way_matches(s.request['takeway'], w)), None)
                if chosen:
                    s.takeway = chosen['code']
        s.quote = {'total_cents': total, 'discount_cents': data.get('discount', 0), 'delivery_cents': data.get('deliveryPrice', 0),
                   'tableware_cents': data.get('tablewarePrice', 0), 'takeways': ways,
                   'takeway_code': s.takeway, 'quoted_at': time.time()}
        lines = [s.store.get('storeName', ''), f"{s.detail.get('name', '')} ×{s.request['count']}"]
        if s.components:
            lines.append('搭配：' + '、'.join(s.components))
        lines.append('官方应付 ' + amount(total))
        for key, name in (('discount', '已优惠'), ('deliveryPrice', '配送费'), ('tablewarePrice', '餐具费')):
            value = data.get(key)
            if isinstance(value, int) and value:
                lines.append(name + ' ' + amount(value))
        if s.request.get('budget_cents') and total > s.request['budget_cents'] * s.request['count']:
            return self.view(s, '\n'.join(lines) + '\n\n官方金额超过你设定的预算，先不下单。可以换餐或明确提高预算。')
        if not s.takeway:
            lines.append('堂食还是打包？请选择官方返回的取餐方式：')
            lines.extend(f"{w.get('title', '取餐方式')} ({w['code']})" for w in ways)
            return self.view(s, '\n\n'.join(lines), ['查看搭配'])
        way = next(w for w in ways if w['code'] == s.takeway)
        lines.extend(['取餐方式：' + str(way.get('title', '到店取餐')),
                      '当前到店订单；备餐时间、取消规则以门店及官方订单页为准。',
                      '回复“确认下单”，我会创建待支付订单并给你官方收银台。'])
        args = {'storeCode': s.store['storeCode'], 'orderType': 1, 'beType': 1, 'items': [copy.deepcopy(s.item)], 'takeWayCode': s.takeway}
        summary = {'store': s.store.get('storeName'), 'fulfillment': way.get('title'),
                   'items': [{'name': s.detail.get('name'), 'quantity': s.request['count'], 'components': s.components}],
                   'total_cents': total, 'fees': {'delivery': data.get('deliveryPrice', 0), 'tableware': data.get('tablewarePrice', 0)},
                   'payer': '当前麦当劳账号；在官方收银台自行确认付款', 'timing': '当前到店，备餐时间未知',
                   'cancellation': '以官方订单页和门店规则为准'}
        with self.app.lock:
            token = self.ledger.prepare(self.app.adapter, 'create-order', args, summary, scope=s.scope, actor=s.actor, ttl=120)
        s.pending = {'id': secrets.token_urlsafe(18), 'token': token, 'args': args, 'summary': summary, 'expires': time.time() + 115}
        s.phase = 'checkout'
        return self.view(s, '\n\n'.join(lines), ['确认下单', '查看搭配'])

    def _takeway(self, s, code):
        if not s.quote or not any(w['code'] == code for w in s.quote['takeways']):
            raise AdapterError('invalid_selection', '请从官方取餐方式中选择')
        s.takeway = code
        return self._quote(s)

    def _confirm(self, s, preview_id):
        if s.unknown:
            return self.view(s, '上一笔订单提交结果未知，已阻止重复下单。请在麦当劳官方客户端核查后处理。')
        if s.order:
            return self.view(s, '这一单已经创建，请先查看订单或使用官方付款链接。', ['订单状态'])
        if not s.pending or not preview_id or preview_id != s.pending['id'] or s.pending['expires'] <= time.time():
            s.pending = None
            return self.view(s, '当前没有有效的订单确认。请先选择餐品并查看最新官方应付金额。', ['重新算价'] if s.detail else [])
        pending = s.pending
        s.pending = None
        try:
            with self.app.lock:
                with self.ledger.connect() as db:
                    blocked = db.execute("SELECT 1 FROM confirmations WHERE status IN ('in_flight','unknown') AND scope LIKE 'agent:%' LIMIT 1").fetchone()
                if blocked:
                    s.unknown = True
                    s.phase = 'unknown'
                    return self.view(s, '有订单正在提交或结果未知，暂不能重复下单。请先在官方客户端核查。')
                result = self.ledger.execute(self.app.adapter, 'create-order', pending['args'], pending['summary'],
                                             token=pending['token'], scope=s.scope, actor=s.actor, confirmed=True)
        except Exception as exc:
            with self.ledger.connect() as db:
                row = db.execute("SELECT status FROM confirmations WHERE scope=? ORDER BY created DESC LIMIT 1", (s.scope,)).fetchone()
            if row and row[0] in ('unknown', 'in_flight'):
                s.unknown = True
                s.phase = 'unknown'
                return self.view(s, '下单请求已发出，但没有收到可确认的结果。已锁定本单，禁止重复提交；请先到麦当劳官方客户端核查。')
            if isinstance(exc, AdapterError) and exc.code in ('price_changed', 'confirmation_invalid'):
                renewed = self._quote(s)
                renewed['reply'] = '价格、搭配或确认有效期发生变化，旧确认已失效。请重新查看：\n\n' + renewed['reply']
                return renewed
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError('order_failed', '订单操作未完成，请先核查官方订单状态') from None
        data = self.app.adapter.business_payload(result)
        payload = data.get('data') if isinstance(data, dict) else None
        if not isinstance(payload, dict) or not payload.get('orderId'):
            s.unknown = True
            s.phase = 'unknown'
            with self.ledger.connect() as db:
                db.execute("UPDATE confirmations SET status='unknown' WHERE scope=? AND status='complete' AND order_id IS NULL", (s.scope,))
            return self.view(s, '官方没有返回可确认的订单号，不能重复提交。请在官方客户端核查。')
        from urllib.parse import urlparse
        link = payload.get('payH5Url')
        parsed = urlparse(link) if isinstance(link, str) else None
        host = (parsed.hostname or '') if parsed else ''
        valid = parsed and parsed.scheme == 'https' and not parsed.username and not parsed.password and any(host == domain or host.endswith('.' + domain) for domain in ('mcd.cn', 'mcdchina.net', 'mcdonalds.com.cn'))
        s.order = {'orderId': str(payload['orderId']), 'status': '待支付', 'pay_url': link if valid else None,
                   'total_cents': pending['summary']['total_cents'], 'created_at': time.time()}
        s.phase = 'order'
        reply = f"订单已创建，待支付。\n订单号：{s.order['orderId']}\n应付：{amount(s.order['total_cents'])}\n"
        reply += ('打开麦当劳官方收银台确认付款：\n' + link) if valid else '官方付款链接未通过地址校验，请在麦当劳官方客户端查看该订单。'
        return self.view(s, reply + '\n\n付款后发“订单状态”查看取餐码。', ['订单状态'])

    def _status(self, s):
        if not s.order:
            return self.view(s, '本地没有可查询的已知订单号。' + ('上一笔结果未知，请先在官方客户端核查。' if s.unknown else '可以先选餐。'))
        data = self._query(s, 'query-order', {'orderId': s.order['orderId']}, 'status', {})
        if not isinstance(data, dict):
            raise AdapterError('order_invalid', '订单详情暂时不可用')
        detail = data.get('orderDetail', data)
        status = str(detail.get('orderStatus') or detail.get('status') or '')
        labels = {'1': '待支付', '2': '已支付，配餐中', '4': '配送中', '6': '已完成', '7': '已取消', '8': '已评价', '10': '餐厅配餐中'}
        s.order['status'] = labels.get(status, '官方状态待核实')
        s.order['pickup_code'] = detail.get('pickupCode')
        if status in ('2', '4', '6', '7', '8', '10'):
            s.order['pay_url'] = None
        reply = f"订单 {s.order['orderId']}\n{s.order['status']}"
        if s.order['pickup_code']:
            reply += '\n取餐码：' + str(s.order['pickup_code'])
        if status == '1' and s.order.get('pay_url'):
            reply += '\n官方付款链接：\n' + s.order['pay_url']
        return self.view(s, reply)


class AgentConversation:
    """Adapter for the owner-bound Weixin transport; no extra state machine."""
    def __init__(self, app):
        self.app = app
        self.actor = None

    def reply(self, text):
        if not self.actor or not getattr(self.app, 'agent', None):
            return HELP
        return self.app.agent.chat(self.actor, text)['reply']
