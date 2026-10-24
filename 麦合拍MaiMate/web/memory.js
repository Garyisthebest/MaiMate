'use strict';
const $=id=>document.getElementById(id);
let memory=null,busy=false;
const element=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const source=()=>$('memorySource').value;
function row(box,name,detail,removable=false){const article=element('article',undefined,'memory-row'),copy=element('div');copy.append(element('strong',name),element('p',detail));article.append(copy);if(removable){const b=element('button','忘记','quiet-button');b.onclick=()=>change('forget',name);b.disabled=busy;article.append(b);}box.append(article);}
function paint(data){
 memory=data;for(const id of ['explicitMemories','feedbackMemories','choiceMemories','aliasMemories'])$(id).replaceChildren();
 $('toggleMemory').hidden=$('clearMemory').hidden=!memory;
 if(!memory){$('memoryStatus').textContent='这个入口还没有可查看的记录，请先绑定微信。';return;}
 $('memoryStatus').textContent=memory.enabled?'记忆已开启 · 你随时可以纠正我':'记忆已关闭 · 已有记录保留，当前不会使用';
 $('toggleMemory').textContent=memory.enabled?'关闭记忆':'开启记忆';
 for(const [key,label] of [['likes','偏爱'],['dislikes','不喜欢']])for(const name of memory[key]){const evidence=memory.evidence?.[name];row($('explicitMemories'),name,label+' · '+(evidence?'你明确告诉我 · '+new Date(evidence.updated_at*1000).toLocaleDateString('zh-CN'):'来自已有偏好记录'),true);}
 if(!memory.likes.length&&!memory.dislikes.length)row($('explicitMemories'),'还没有明确喜好','从一句“记住，我喜欢……”开始。');
 for(const item of memory.insights||[])row($('feedbackMemories'),item.name,({positive:'下次适当优先',negative:'下次少推荐一些',mixed:'感受还在变化，继续了解'})[item.signal]+' · '+item.samples+'次评价 · '+new Date(item.updated_at*1000).toLocaleDateString('zh-CN'),true);
 if(!memory.insights?.length)row($('feedbackMemories'),'这页等你吃完再写','选过餐品后，发“这餐好吃”“这餐一般”或“这餐不喜欢”。');
 for(const item of memory.often_selected)row($('choiceMemories'),item.name,'明确选择过'+item.selections+'次',true);
 if(!memory.often_selected.length)row($('choiceMemories'),'不急着下结论','还没有多次选过的餐品。');
 for(const [alias,target] of Object.entries(memory.aliases||{})){const evidence=memory.alias_evidence?.[alias];row($('aliasMemories'),alias,'对应：'+target+' · '+(evidence?'你明确教过我 · '+new Date(evidence.updated_at*1000).toLocaleDateString('zh-CN'):'来自已有昵称记录'),true);}
 if(!Object.keys(memory.aliases||{}).length)row($('aliasMemories'),'昵称由你来教','选店后说“昵称就是菜单中的餐品名”，核实后才会保存。');
}
async function load(){const selected=source();try{const r=await fetch(selected==='web'?'/api/agent/memory':'/api/weixin/recent');const data=await r.json();if(!r.ok)throw Error(data.error||'无法读取手记');if(selected===source())paint(data.memory);}catch(e){$('memoryNotice').textContent=e.message;}}
async function change(operation,name){
 if(busy)return;if(operation==='clear'&&!confirm('清除这个入口的全部口味、选择统计和餐后评价？'))return;
 const selected=source();busy=true;document.querySelectorAll('button,select').forEach(b=>b.disabled=true);
 try{const r=await fetch(selected==='web'?'/api/agent/memory':'/api/weixin/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation,name})});const data=await r.json();if(!r.ok)throw Error(data.error||'操作未完成');paint(data.memory);$('memoryNotice').textContent=data.notice;}
 catch(e){$('memoryNotice').textContent=e.message;}finally{busy=false;document.querySelectorAll('button,select').forEach(b=>b.disabled=false);}
}
$('memorySource').onchange=()=>{$('memoryNotice').textContent='';load();};$('refreshMemory').onclick=load;$('toggleMemory').onclick=()=>change(memory?.enabled?'disable':'enable');$('clearMemory').onclick=()=>change('clear');load();
