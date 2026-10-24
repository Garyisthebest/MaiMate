'use strict';
const $=id=>document.getElementById(id);
let busy=false,lastQrState='',failures=0;
const labels={disconnected:'未连接',waiting_scan:'等待扫码',scanned:'请在手机确认',need_verifycode:'等待配对',connected:'已开启',paused:'已暂停',expired:'二维码过期',session_expired:'需重新绑定',error:'连接异常'};
function paint(s){
  $('wxBadge').textContent=labels[s.state]||'读取状态中';
  $('wxBadge').className='channel-badge '+(s.state==='connected'?'connected':'');
  $('wxNotice').textContent=s.notice||'';
  $('qrPanel').hidden=!s.qr_available;
  if(s.qr_available&&lastQrState!==s.state){$('wxQr').src='/api/weixin/qr.svg?v='+Date.now();lastQrState=s.state;}
  if(!s.qr_available)lastQrState='';
  $('verifyForm').hidden=s.state!=='need_verifycode';
  $('wxLogin').hidden=s.bound||['waiting_scan','scanned','need_verifycode'].includes(s.state);
  $('wxLogin').textContent=['expired','error'].includes(s.state)?'重新获取二维码':'获取微信连接二维码';
  $('wxResume').hidden=!s.bound||['connected','session_expired'].includes(s.state);
  $('wxPause').hidden=s.state!=='connected';
  $('wxForget').hidden=!s.bound;
  $('wxActivity').textContent=s.last_reply?'已回复 '+s.reply_count+' 条消息，最近一次 '+new Date(s.last_reply*1000).toLocaleTimeString('zh-CN'):(s.bound?'已绑定。去微信里的机器人对话发“帮助”试试。':'二维码将在本机生成；绑定凭证只保存在项目目录。');
}
async function poll(){
  if(busy)return;
  try{const r=await fetch('/api/weixin/status');if(!r.ok)throw new Error();paint(await r.json());failures=0;}
  catch{if(++failures>=2)$('wxNotice').textContent='本地服务未响应，请确认电脑上的服务仍在运行。';}
}
async function action(name,body={}){
  if(busy)return;busy=true;
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  $('wxNotice').textContent=name==='login'?'正在向微信获取连接二维码…':'正在处理…';
  try{const r=await fetch('/api/weixin/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const s=await r.json();if(!r.ok)throw new Error(s.error||'操作未完成');paint(s);}
  catch(e){$('wxNotice').textContent=e.message;}
  finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
$('wxLogin').addEventListener('click',()=>action('login'));
$('wxResume').addEventListener('click',()=>action('resume'));
$('wxPause').addEventListener('click',()=>action('pause'));
$('wxForget').addEventListener('click',()=>{if(confirm('删除本机保存的微信绑定？将停止接收与回复，重新使用需再扫码。'))action('forget');});
$('verifyForm').addEventListener('submit',e=>{e.preventDefault();const code=$('verifyCode').value.trim();$('verifyCode').value='';action('verify',{code});});
poll();setInterval(()=>{if(!document.hidden)poll();},2000);

let memoryEnabled=true;
function paintMemory(memory){
  $('toggleMemory').hidden=!memory;$('clearMemory').hidden=!memory;
  if(!memory){$('memorySummary').textContent='先绑定微信，再查看偏好。';return;}
  memoryEnabled=memory.enabled;
  $('toggleMemory').textContent=memory.enabled?'关闭记忆':'开启记忆';
  const lines=[memory.enabled?'偏好记忆已开启':'偏好记忆已关闭',
    '偏爱：'+(memory.likes.join('、')||'尚未记录'),
    '不喜欢：'+(memory.dislikes.join('、')||'尚未记录')];
  if(memory.often_selected.length)lines.push('多次选择：'+memory.often_selected.map(x=>x.name+'（'+x.selections+'次）').join('、')+'；不代表已付款');
  if(memory.insights?.length)lines.push('餐后反馈：'+memory.insights.map(x=>x.name+'（'+({positive:'好评',negative:'少推荐',mixed:'继续了解'})[x.signal]+'）').join('、'));
  lines.push('你教过的昵称：'+(Object.entries(memory.aliases||{}).map(([name,target])=>name+'＝'+target).join('、')||'尚未记录')+'；完整记录见口味手记');
  $('memorySummary').textContent=lines.join('\n');
}
async function loadRecent(showTurns=false){
  try{
    const r=await fetch('/api/weixin/recent');const data=await r.json();
    if(!r.ok)throw Error(data.error||'暂时无法读取');
    paintMemory(data.memory);
    if(showTurns){
      const box=$('recentTurns');box.replaceChildren();
      if(!data.turns.length){const p=document.createElement('p');p.className='settings-note';p.textContent='还没有可显示的新对话。去微信发一条消息后刷新。';box.append(p);}
      for(const turn of data.turns){
        const item=document.createElement('article');item.className='recent-turn';
        const time=document.createElement('small');time.textContent=new Date(turn.at*1000).toLocaleTimeString('zh-CN');item.append(time);
        for(const [label,value] of [['你',turn.user],['麦合拍MaiMate',turn.assistant]]){const p=document.createElement('p');const strong=document.createElement('strong');strong.textContent=label+'：';p.append(strong,document.createTextNode(value));item.append(p);}
        box.append(item);
      }
    }
  }catch(e){$('memoryNotice').textContent=e.message;}
}
async function memoryAction(operation){
  if(busy)return;busy=true;
  $('toggleMemory').disabled=$('clearMemory').disabled=true;
  try{const r=await fetch('/api/weixin/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation})});const data=await r.json();if(!r.ok)throw Error(data.error||'操作未完成');paintMemory(data.memory);$('memoryNotice').textContent=data.notice;}
  catch(e){$('memoryNotice').textContent=e.message;}
  finally{busy=false;$('toggleMemory').disabled=$('clearMemory').disabled=false;}
}
$('refreshMemory').addEventListener('click',()=>loadRecent());
$('toggleMemory').addEventListener('click',()=>memoryAction(memoryEnabled?'disable':'enable'));
$('clearMemory').addEventListener('click',()=>{if(confirm('清除本机保存的微信饮食偏好和选择统计？'))memoryAction('clear');});
$('refreshRecent').addEventListener('click',()=>loadRecent(true));
$('recentDetails').addEventListener('toggle',()=>{if($('recentDetails').open)loadRecent(true);});
loadRecent();
