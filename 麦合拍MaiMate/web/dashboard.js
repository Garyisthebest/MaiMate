'use strict';
const $=id=>document.getElementById(id);
const money=n=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(n/100);
const node=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
let busy=false;
function render(d){
 $('mcp').textContent='麦当劳：'+(d.mcp_connected?'已连接':'未连接');
 $('model').textContent='模型：'+(d.model.configured?d.model.name:'未配置');
 $('weixin').textContent='微信：'+({connected:'接收中',paused:'已暂停',unavailable:'未启用'}[d.weixin.state]||d.weixin.state);
 const h=d.history,has=!!h.imported_at;
 $('orders').textContent=has?h.orders:'—';$('spend').textContent=has?money(h.total_cents):'—';$('period').textContent=h.periods[0]?.period||'—';
 $('historyNotice').textContent=has?`纳入 ${h.orders} 单；跳过 ${h.skipped} 条取消、重复、较早或待核实记录。`:'导入近期完成订单，看看常点什么。';
 $('importedAt').textContent=has?'上次导入 '+new Date(h.imported_at*1000).toLocaleString('zh-CN'):'尚未导入';
 $('foods').replaceChildren();if(!h.foods.length)$('foods').append(node('li',has?'本次没有可分析的餐品。':'还没有历史订单分析。常点是线索，未必全是你爱吃的。','empty'));
 for(const f of h.foods){const li=node('li','');li.append(node('span',f.name,'food-name'),node('small',f.orders+' 单'));$('foods').append(li);}
 $('months').replaceChildren();const max=Math.max(1,...h.months.map(m=>m.total_cents));
 if(!h.months.length)$('months').append(node('p',has?'暂无可汇总的完成订单。':'导入后显示按月的订单金额。','empty'));
 for(const m of h.months){const row=node('div',''),label=node('div','','month-label'),track=node('div','','bar-track'),fill=node('div','','bar-fill');label.append(node('span',m.month+' / '+m.orders+' 单'),node('span',money(m.total_cents)));fill.style.width=(m.total_cents/max*100)+'%';track.append(fill);row.append(label,track);$('months').append(row);}
 const mem=d.memory;$('evidence').replaceChildren();
 if(!mem.enabled)$('evidence').append(node('p','记忆已关闭，推荐不使用长期口味或历史推断。'));
 else{const rows=[];if(mem.likes.length)rows.push('你亲口说过：'+mem.likes.join('、'));if(mem.dislikes.length)rows.push('你不喜欢：'+mem.dislikes.join('、'));if(mem.insights.length)rows.push('有 '+mem.insights.length+' 项餐后反馈可参考');const count=h.foods.filter(f=>f.orders>=2).length;if(count)rows.push('历史中有 '+count+' 项重复餐品，仅作弱偏好，导入30天后需刷新才继续参考。');for(const t of rows.length?rows:['还在了解你。在微信告诉它喜欢什么，或者吃完评价这一餐。'])$('evidence').append(node('p',t));}
}
async function request(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const d=await r.json();if(!r.ok)throw Error(d.error||'查询失败，请重试');return d;}
async function load(){if(busy)return;busy=true;$('pointReply').hidden=true;$('pointReply').textContent='';try{render(await request('/api/dashboard?source='+encodeURIComponent($('source').value)));$('notice').textContent='';}catch(e){$('notice').textContent=e.message;}finally{busy=false;}}
async function operate(op,button){if(busy)return;busy=true;const source=$('source').value;button.disabled=true;$('source').disabled=true;$('notice').textContent=op==='refresh'?'正在查询并分析近期订单…':'正在处理…';try{const d=await request('/api/insights',{source,operation:op});if(op==='points'){$('pointReply').textContent=d.reply;$('pointReply').hidden=false;}else render(d);$('notice').textContent=op==='clear'?'已删除此入口的历史分析。':op==='refresh'?'分析已更新。原始订单未保存。':'积分查询完成。';}catch(e){$('notice').textContent=e.message;}finally{busy=false;button.disabled=false;$('source').disabled=false;}}
$('source').addEventListener('change',load);
$('importHistory').addEventListener('click',()=>operate('refresh',$('importHistory')));
$('clearHistory').addEventListener('click',()=>{if(confirm('删除此入口的历史订单分析？明确喜好和昵称会保留。'))operate('clear',$('clearHistory'));});
$('points').addEventListener('click',()=>operate('points',$('points')));
async function init(){try{const wx=await request('/api/weixin/status');if(!wx.bound)$('source').value='web';}catch{}await load();}
init();
