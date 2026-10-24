'use strict';
const $=id=>document.getElementById(id);
let busy=false,state=null;
const money=n=>'¥'+(n/100).toFixed(2);
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function button(text,fn,cls='choice-button'){const b=el('button',cls,text);b.type='button';b.onclick=fn;return b;}
function userText(text){const row=el('article','message user');row.append(el('div','bubble',text));$('chatLog').append(row);scrollChat();}
function scrollChat(){$('chatLog').scrollTop=$('chatLog').scrollHeight;}
function assistant(data){
  if(!data.reply)return;
  const row=el('article','message assistant');row.append(el('span','avatar','聚'));const bubble=el('div','bubble');bubble.append(el('p','reply-text',data.stores?.length||data.recommendations?.length?data.reply.split('\n\n')[0]:data.reply));
  if(data.stores?.length){const list=el('div','store-choices');data.stores.forEach((s,i)=>{const b=button('',()=>sendAction('select_store',{store_code:s.storeCode},'选店'+(i+1)));b.append(el('strong','',s.storeName),el('span','',s.address||'地址以门店为准'));list.append(b);});bubble.append(list);if(data.reply.includes('OpenStreetMap')){const credit=el('p','geo-attribution','定位地址数据 '),a=el('a','','© OpenStreetMap contributors');a.href='https://www.openstreetmap.org/copyright';a.target='_blank';a.rel='noopener noreferrer';credit.append(a);bubble.append(credit);}}
  if(data.recommendations?.length){const list=el('div','meal-choices');data.recommendations.forEach((c,i)=>{const b=button('',()=>sendAction('select_meal',{product_code:c.code},'套餐'+(i+1)),'meal-choice');b.append(el('span','meal-number','套餐'+(i+1)),el('strong','',c.name),el('span','meal-price',money(c.reference_cents)+' /份'));list.append(b);});bubble.append(list);}
  if(data.cart?.quote&&!data.cart.quote.takeway_code){const ways=el('div','suggestion-actions');data.cart.quote.takeways.forEach(w=>ways.append(button(w.title||w.code,()=>sendAction('takeway',{code:w.code},w.title||'取餐方式'),'text-chip')));bubble.append(ways);}
  if(data.suggestions?.length){const actions=el('div','suggestion-actions');data.suggestions.forEach(t=>{if(t==='确认下单'){const b=button('确认下单 · '+money(data.cart.quote.total_cents),()=>sendAction('confirm',{preview_id:data.cart.preview_id},'确认下单'),'primary-button');b.dataset.previewConfirm=data.cart.preview_id;actions.append(b);}else actions.append(button(t,()=>t==='使用当前位置'?locate():sendText(t),'text-chip'));});bubble.append(actions);}
  if(data.order?.pay_url){const a=el('a','payment-link','打开官方收银台');a.href=data.order.pay_url;a.target='_blank';a.rel='noopener noreferrer';bubble.append(a);}
  row.append(bubble);$('chatLog').append(row);scrollChat();
}
function render(data){
  state=data;const model=data.model;$('modelState').textContent=model?.configured?model.name+' · '+model.model:'指令模式 · 在连接设置中启用千问 / DeepSeek';
  renderTicket();
}
function renderTicket(){
  const box=$('ticketBody'),cart=state?.cart,order=state?.order;if(!cart&&!order&&!state?.unknown)return;box.replaceChildren();
  if(state.unknown){box.append(el('h2','','这单先核查'),el('p','warning','提交结果尚未确认，已阻止重复下单。请在麦当劳官方客户端核查。'));return;}
  if(order){box.append(el('span','order-state',order.status),el('h2','',order.pickup_code?'取餐码 '+order.pickup_code:'订单已创建'),el('p','order-id',order.orderId));if(order.total_cents!==undefined)box.append(el('div','ticket-total',money(order.total_cents)));if(order.pay_url){const link=el('a','primary-button block','去官方付款');link.href=order.pay_url;link.target='_blank';link.rel='noopener noreferrer';box.append(link);}box.append(button('刷新订单状态',()=>sendAction('status',{},'订单状态'),'secondary-button block'));return;}
  box.append(el('div','store-label',cart.store?.storeName||''),el('h2','meal-title',cart.name+' ×'+cart.quantity));
  const components=el('ul','components');cart.components.forEach(c=>components.append(el('li','',c)));box.append(components);
  if(cart.options?.length){const options=el('details','options-details');const sum=el('summary','','调整套餐搭配');options.append(sum);cart.options.forEach(g=>{const field=el('fieldset','option-group');field.append(el('legend','',g.title+(g.min>1?'（至少'+g.min+'项）':'')));g.choices.forEach(c=>{const b=button(c.name+(c.quantity>1?' ×'+c.quantity:''),()=>sendAction('choose_option',{round_id:g.id,code:c.code,quantity:g.max===1?1:(c.quantity?0:1)},'调整为：'+c.name),'option-chip'+(c.quantity?' selected':''));b.setAttribute('aria-pressed',String(Boolean(c.quantity)));field.append(b);});options.append(field);});box.append(options);}
  if(!cart.quote){box.append(el('p','subtle','选好必选搭配后，会查询官方应付金额。'));return;}
  const quote=cart.quote;
  if(quote.takeways?.length){const ways=el('div','takeway-group');ways.append(el('strong','','取餐方式'));quote.takeways.forEach(w=>ways.append(button(w.title||w.code,()=>sendAction('takeway',{code:w.code},w.title||'选择取餐方式'),'option-chip'+(quote.takeway_code===w.code?' selected':''))));box.append(ways);}
  const total=el('div','ticket-total');total.append(el('span','','官方应付'),el('strong','',money(quote.total_cents)));box.append(total);
  if(quote.discount_cents)box.append(el('p','saving','已优惠 '+money(quote.discount_cents)));
  if(quote.tableware_cents)box.append(el('p','subtle','含餐具费 '+money(quote.tableware_cents)));
  const confirm=button('确认下单 · '+money(quote.total_cents),()=>sendAction('confirm',{preview_id:cart.preview_id},'确认下单'),'primary-button block');confirm.id='confirmOrder';confirm.disabled=!state.can_confirm||busy||Boolean($('chatInput').value.trim());box.append(confirm);
  box.append(el('p','confirm-note','创建待支付订单后，前往麦当劳官方收银台付款。'));
  const expiry=el('p','expiry');expiry.id='quoteExpiry';box.append(expiry);
  if(!state.can_confirm&&quote.takeway_code){const b=button('重新算价',()=>sendText('重新算价'),'secondary-button block');b.id='requote';box.append(b);}
  updateExpiry();
}
function updateExpiry(){const id=state?.cart?.preview_id;document.querySelectorAll('[data-preview-confirm]').forEach(b=>b.disabled=busy||!state?.can_confirm||b.dataset.previewConfirm!==id||Date.now()/1000>=state.cart.expires_at||Boolean($('chatInput').value.trim()));if(!state?.cart?.expires_at)return;const left=Math.max(0,Math.ceil(state.cart.expires_at-Date.now()/1000));const target=$('quoteExpiry');if(target)target.textContent=left?'本次确认 '+left+' 秒内有效':'本次确认已过期，请重新算价';const b=$('confirmOrder');if(b)b.disabled=busy||!state.can_confirm||!left||Boolean($('chatInput').value.trim());if(!left&&!$('requote')&&!state.order){const refresh=button('重新算价',()=>sendText('重新算价'),'secondary-button block');refresh.id='requote';$('ticketBody').append(refresh);}}
async function request(body,label){
  if(busy)return;busy=true;document.querySelectorAll('.store-choices button,.meal-choices button,.suggestion-actions button').forEach(b=>b.disabled=true);if(label)userText(label);$('activity').textContent=body.action==='confirm'?'正在复核金额并创建订单，请勿重复提交…':'正在查看真实门店和餐品…';$('sendButton').disabled=true;updateExpiry();
  try{const r=await fetch('/api/agent/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'请求未完成');render(data);assistant(data);}
  catch(e){assistant({reply:e.message+(body.action==='confirm'?'。如订单已提交，请先查询订单或在官方客户端核查。':'')});}
  finally{busy=false;$('activity').textContent='';$('sendButton').disabled=false;updateExpiry();scrollChat();}
}
function sendAction(action,payload,label){return request({action,payload},label);}
function sendText(text){if(!text?.trim())return Promise.resolve();return request({text:text.trim()},text.trim());}
function locate(){
  if(busy)return;
  if(!navigator.geolocation){assistant({reply:'当前浏览器不支持定位，可以直接告诉我门店名。'});return;}
  $('activity').textContent='请授权本次定位；坐标将由 Photon／OpenStreetMap 解析为门店所在区域。';
  navigator.geolocation.getCurrentPosition(p=>sendAction('locate',{latitude:p.coords.latitude,longitude:p.coords.longitude},'使用我当前的位置'),e=>{$('activity').textContent='';assistant({reply:e.code===1?'定位权限未开启。可在浏览器地址栏允许定位，或直接告诉我门店名。':'暂时无法取得位置。可以重试，或直接告诉我当前门店名。'});},{enableHighAccuracy:true,timeout:20000,maximumAge:30000});
}
$('chatForm').onsubmit=e=>{e.preventDefault();if(busy)return;const text=$('chatInput').value;$('chatInput').value='';$('chatInput').style.height='auto';sendText(text);};
$('chatInput').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('chatForm').requestSubmit();}};
$('chatInput').oninput=()=>{$('chatInput').style.height='auto';$('chatInput').style.height=Math.min(150,$('chatInput').scrollHeight)+'px';updateExpiry();};
document.querySelectorAll('[data-message]').forEach(b=>b.onclick=()=>sendText(b.dataset.message));
$('locateStart').onclick=locate;$('locateButton').onclick=locate;
$('newOrder').onclick=()=>{if(state?.cart&&!state.order&&!confirm('开始新的一单？当前未提交的选餐将清空。'))return;sendText('重新点餐').then(()=>{if(state?.cart||state?.order||state?.unknown)return;$('ticketBody').replaceChildren(el('h2','','新的一单'),el('p','subtle','告诉我你在哪家店，或者授权本次定位。'));});};
$('mobileTicket').onclick=()=>$('orderPane').classList.toggle('open');$('closeTicket').onclick=()=>$('orderPane').classList.remove('open');
async function init(){try{const r=await fetch('/api/status'),status=await r.json();$('connection').textContent=status.connected?'官方门店已连接':'门店连接异常';$('connection').classList.toggle('connected',status.connected);const current=await fetch('/api/agent/current');if(!current.ok)throw Error();const data=await current.json();render(data);if(data.order)assistant({reply:'已恢复本机已知的订单号。点击“刷新订单状态”查询官方进度。'});}catch{$('connection').textContent='本地服务未连接';$('activity').textContent='请确认电脑上的麦聚桌服务正在运行。';}}
init();setInterval(updateExpiry,1000);
