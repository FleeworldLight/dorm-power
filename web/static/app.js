const fmt=(n,d=2)=>(n===null||n===undefined||isNaN(n))?'--':Number(n).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const q=(s)=>document.querySelector(s);
let CFG={}, UTOKEN=localStorage.getItem('dorm_utoken')||'', SID='', LEVELS=[], CHOSEN={}, POLL=null, LAST={rooms:[]};
let CHANGING=false, PREFILLED=false, AUTO_DONE=false;

async function api(path,opt={}){
  const r=await fetch(path,{cache:'no-store',...opt});
  let j=null; try{ j=await r.json(); }catch(e){ j={ok:false,error:'HTTP '+r.status}; }
  return j;
}

async function boot(){
  // 看板只认自己的 utoken：没有就是没绑定，服务端不会给任何人的数据。
  let d=await api('/api/summary?utoken='+encodeURIComponent(UTOKEN));
  if(UTOKEN && !d.bound){                     // 本地存的 utoken 已经对不上了
    UTOKEN=''; localStorage.removeItem('dorm_utoken');
    d=await api('/api/summary');
  }
  CFG=d.config||{}; LAST=d;
  const bound=!!d.bound;
  document.title=CFG.site_title||'宿舍电费看板';
  q('#sub').textContent=(CFG.site_subtitle||'')+(bound
    ? ' · 更新于 '+(d.updated||'（还没有数据）')
    : ' · 扫码添加宿舍后开始记录');
  q('#stats').style.display=bound?'grid':'none';
  q('#mineActions').style.display=bound?'flex':'none';
  renderBoard(d);
  if(bound){
    const m=await api('/api/me?prefill=1&utoken='+encodeURIComponent(UTOKEN));
    if(m.ok) LAST.prefill=m.prefill||{};
  }
  q('#btnStart').onclick=()=>startLogin(false);
  q('#btnCommit').onclick=commitRoom;
  q('#btnRefresh').onclick=refreshMine;
  q('#btnChangeRoom').onclick=()=>startLogin(true);
  q('#btnCancelChange').onclick=cancelChange;
  q('#btnForget').onclick=forgetMine;
  syncPanelMode();
  autoRefreshOnce();          // 不 await：先把缓存数据画出来，刷到新的再重画
}

// 面板文案跟着「首次添加」还是「换宿舍」变。已经绑过宿舍的人只会是后者。
function syncPanelMode(){
  const replace = CHANGING || !!LAST.bound;
  q('#panelNewTitle').textContent = replace ? '换个宿舍' : '添加宿舍';
  q('#panelNewNote').textContent = replace
    ? '扫码后重新选一次房间即可替换。姓名学号沿用原来的，不用重填。'
    : '扫码后选房间，把这个房间加进看板。';
  q('#btnStart').textContent = replace ? '开始扫码换宿舍' : '开始扫码';
  q('#btnCancelChange').style.display = CHANGING ? 'inline-block' : 'none';
}

function renderBoard(d){
  const rooms=d.rooms||[], up=CFG.unit_price||0, low=CFG.low_threshold||0;
  const box=q('#rooms');
  if(!rooms.length){
    box.innerHTML='<div class="empty">'+(d.bound
      ? '这条宿舍还没有数据，等下一次刷新。'
      : '还没有添加宿舍。<br>往下扫码添加你自己的宿舍，之后这里会记录它的电量变化。')+'</div>';
    return;
  }
  const money=rooms.reduce((s,r)=>s+(r.unit==='度'?r.remain*up:r.remain),0);
  q('#stats').innerHTML=[['合计折算','¥ '+fmt(money),'单价 '+up+' 元/度'],
    ['最近更新', d.updated?d.updated.slice(5,16):'--','']]
    .map(([k,v,s])=>'<div class="stat"><div class="k">'+k+'</div><div class="v">'+v+'</div><div class="k">'+s+'</div></div>').join('');
  box.innerHTML=rooms.map(r=>{
    const stale=r.age_hours>(CFG.stale_hours||36);
    const cls=r.remain<low?'low':(stale?'stale':'fresh');
    const tag=r.remain<low?'<span class="tag low">低电</span>':(stale?'<span class="tag stale">数据陈旧</span>':'');
    const yuan=r.unit==='度'?'约 ¥ '+fmt(r.remain*up):'';
    const where=[r.area,r.building,r.floor,r.room].filter(Boolean).join(' ')||'宿舍';
    const src=(r.source==='report')?' <span class="tag" style="background:#f1efe8;color:#5f5e5a">本地上报</span>':'';
    return '<div class="room '+cls+'">'
      +'<div class="name">'+where+tag+src+'</div>'
      +'<div class="val">'+fmt(r.remain)+'<small>'+r.unit+'</small></div><div class="meta">'+yuan+'</div>'
      +'<div class="chartwrap">'+bigChart(r.history, r.unit)+'<div class="tip" data-k=""></div></div>'
      +'<div class="meta">更新 '+r.ts+'（'+fmt(r.age_hours,1)+' 小时前）</div></div>';
  }).join('');
}

const DAYMS=86400000;
const _toMs=(ts)=>{ const t=new Date(String(ts).replace(' ','T')).getTime(); return isNaN(t)?null:t; };
const _dayStart=(ms)=>{ const d=new Date(ms); d.setHours(0,0,0,0); return d.getTime(); };
const _md=(ms)=>{ const d=new Date(ms); return ('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2); };
const _hm=(ms)=>{ const d=new Date(ms); return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2); };

// 余额曲线：横轴按天（窗口至少 5 天），纵轴整数刻度。
// 点图上任意位置，取横坐标最近的点，把它的数值和时间显示出来。
function bigChart(h, unit){
  const W=300, H=162, L=34, R=18, T=8, B=20;

  const pts=[];
  (h||[]).forEach(p=>{
    const ms=_toMs(p[0]), v=Number(p[1]);
    if(ms===null || isNaN(v)) return;
    pts.push({ms:ms, v:v});
  });
  pts.sort((a,b)=>a.ms-b.ms);

  // 横轴是「一天一格」的等距刻度。历史本来就是一天一个点，
  // 所以点要落在自己那一天上 —— 不能因为它是 22:00 就贴到第二天去。
  const today=_dayStart(Date.now());
  const endDay=Math.max(today, pts.length?_dayStart(pts[pts.length-1].ms):today);
  const startDay=Math.min(pts.length?_dayStart(pts[0].ms):endDay, endDay-4*DAYMS);
  const nDays=Math.round((endDay-startDay)/DAYMS)+1;      // 至少 5 天

  // 纵轴整数刻度；间隔不够 3 格就对称撑开，免得点少时被拉成冲天炮
  const vs=pts.map(p=>p.v);
  const vmin=vs.length?Math.min(...vs):0;
  const vmax=vs.length?Math.max(...vs):10;
  const raw=Math.max(vmax-vmin,0);
  let stp=1000;
  for(const c of [1,2,5,10,20,25,50,100,200,250,500,1000]){ if(raw/c<=5){ stp=c; break; } }
  let lo=Math.floor(vmin/stp)*stp, hi=Math.ceil(vmax/stp)*stp;
  if(hi===lo) hi=lo+stp;
  for(let g=0; (hi-lo)/stp<3 && g<12; g++){ lo-=stp; hi+=stp; }

  const daySlot=ms=>Math.round((_dayStart(ms)-startDay)/DAYMS);
  const xOfDay=i=>+(L+(W-L-R)*(nDays<2?0:i/(nDays-1))).toFixed(1);
  const xOf=ms=>xOfDay(daySlot(ms));
  const yOf=v=>+(T+(H-T-B)*(1-(v-lo)/(hi-lo))).toFixed(1);

  const hit=pts.map(p=>[xOf(p.ms), yOf(p.v), p.v, _md(p.ms)+' '+_hm(p.ms)]);

  let s='<svg class="curve" viewBox="0 0 '+W+' '+H+'" width="100%" role="img"'
    +' data-w="'+W+'" data-h="'+H+'" data-unit="'+unit+'"'
    +" data-pts='"+JSON.stringify(hit)+"'"
    +' onclick="chartHit(this,event)">';
  s+='<title>余额变化</title>';
  s+='<rect x="0" y="0" width="'+W+'" height="'+H+'" fill="transparent"/>';

  // 横轴：每天的竖线 + 日期（每条都对着它自己那一天的刻度）
  const dStep=Math.max(1, Math.ceil(nDays/6));
  const ticks=[];
  for(let i=0;i<nDays;i+=dStep) ticks.push(i);
  if((nDays-1)-ticks[ticks.length-1] >= Math.ceil(dStep*0.6)) ticks.push(nDays-1);
  ticks.forEach(i=>{
    const xx=xOfDay(i);
    s+='<line x1="'+xx+'" y1="'+T+'" x2="'+xx+'" y2="'+(H-B)+'" stroke="#efeee7" stroke-width="1"/>';
    s+='<text x="'+xx+'" y="'+(H-B+13)+'" font-size="10" fill="#8a8a80" text-anchor="middle">'+_md(startDay+i*DAYMS)+'</text>';
  });

  // 纵轴：整数刻度线 + 数值
  for(let v=lo; v<=hi+1e-9; v+=stp){
    const yy=yOf(v);
    s+='<line x1="'+L+'" y1="'+yy+'" x2="'+(W-R)+'" y2="'+yy+'" stroke="#e6e6df" stroke-width="1"/>';
    s+='<text x="'+(L-5)+'" y="'+yy+'" font-size="10" fill="#8a8a80" text-anchor="end" dominant-baseline="central">'+Math.round(v)+'</text>';
  }

  // 低电阈值（落在坐标范围内才画）
  const th=CFG.low_threshold||0;
  if(th>lo && th<hi){
    const ty=yOf(th);
    s+='<line x1="'+L+'" y1="'+ty+'" x2="'+(W-R)+'" y2="'+ty+'" stroke="#e24b4a" stroke-width="1" stroke-dasharray="5 4"/>';
  }

  if(pts.length){
    const xy=pts.map(p=>xOf(p.ms)+','+yOf(p.v));
    s+='<polygon points="'+xOf(pts[0].ms)+','+(H-B)+' '+xy.join(' ')+' '+xOf(pts[pts.length-1].ms)+','+(H-B)
      +'" fill="#e6f1fb" opacity="0.8"/>';
    s+='<polyline points="'+xy.join(' ')+'" fill="none" stroke="#185fa5" stroke-width="1.7" stroke-linejoin="round"/>';
    if(pts.length<=24){
      pts.forEach(p=>{ s+='<circle cx="'+xOf(p.ms)+'" cy="'+yOf(p.v)+'" r="2.3" fill="#185fa5"/>'; });
    }
  }

  s+='<circle class="selpt" r="4.4" fill="#fff" stroke="#185fa5" stroke-width="2" style="display:none"/>';
  s+='</svg>';
  return s;
}

// 点击/触摸曲线上的一点
function chartHit(svg, evt){
  const pts=JSON.parse(svg.dataset.pts||'[]');
  if(!pts.length) return;
  const box=svg.getBoundingClientRect();
  const W=+svg.dataset.w, H=+svg.dataset.h, unit=svg.dataset.unit||'';
  const mx=(evt.clientX-box.left)/box.width*W;
  let best=pts[0], bd=Infinity;
  for(const p of pts){ const d=Math.abs(p[0]-mx); if(d<bd){ bd=d; best=p; } }

  const wrap=svg.parentElement, tip=wrap.querySelector('.tip'), sel=svg.querySelector('.selpt');
  if(tip.dataset.k===best[3]){        // 同一个点再点一次就收起
    tip.style.display='none'; sel.style.display='none'; tip.dataset.k=''; return;
  }
  tip.dataset.k=best[3];
  tip.innerHTML='<b>'+fmt(best[2])+' '+unit+'</b><span class="t">'+best[3]+'</span>';
  tip.style.display='block';
  const px=best[0]/W*box.width, py=best[1]/H*box.height;
  const half=tip.offsetWidth/2;
  tip.style.left=Math.max(half+2, Math.min(box.width-half-2, px))+'px';   // 别跑出卡片
  tip.style.top=py+'px';
  tip.classList.toggle('below', py<40);
  sel.setAttribute('cx',best[0]); sel.setAttribute('cy',best[1]); sel.style.display='';
}

async function autoRefreshOnce(){
  if(AUTO_DONE || !UTOKEN) return;      // 没绑定就没什么可刷的
  AUTO_DONE=true;
  const sub=q('#sub'), before=sub.textContent;
  sub.textContent=before+' · 正在刷新…';
  let d=null;
  try{
    d=await api('/api/refresh',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({utoken:UTOKEN})});
  }catch(e){}
  if(d && d.ok){
    await boot();                       // 刷到新数据了才重画
  }else{
    sub.textContent=before;             // 被节流/刷不动，就别挂着"正在刷新"
  }
}

let QRSEQ=-1, POLL_T0=0;   // POLL 已在上面声明

function stopPolling(){ if(POLL){ clearInterval(POLL); POLL=null; } }

function startPolling(){ stopPolling(); POLL_T0=Date.now(); POLL=setInterval(pollState,2500); }

// 页面切到后台就停掉轮询（省流量），切回来还在等扫码就继续
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){ stopPolling(); return; }
  if(SID && q('#loginBox').style.display==='block' && q('#pickBox').style.display!=='block') startPolling();
});

async function startLogin(changing){
  if(changing===true) CHANGING=true;
  PREFILLED=false;
  syncPanelMode();
  q('#btnStart').disabled=true; q('#startHint').textContent='正在启动服务器浏览器…（首次约 10-30 秒）';
  q('#loginBox').style.display='block'; q('#pickBox').style.display='none';
  q('#qrWarn').style.display='none'; q('#qrHint').textContent='正在生成二维码…';
  q('#commitHint').textContent='';
  const d=await api('/api/session/start',{method:'POST'});
  if(!d.ok){ q('#startHint').textContent='失败：'+(d.error||'未知'); q('#btnStart').disabled=false; return; }
  SID=d.sid; QRSEQ=-1; q('#startHint').textContent='';
  applyQr(d);
  startPolling();
}

function cancelChange(){
  CHANGING=false; SID=''; stopPolling();
  q('#loginBox').style.display='none'; q('#pickBox').style.display='none';
  q('#startHint').textContent=''; q('#qrWarn').style.display='none';
  q('#btnStart').disabled=false;
  syncPanelMode();
}

// 换宿舍时把原来的姓名学号填回去，免得用户重打一遍
function prefillFromMine(){
  if(PREFILLED) return;
  const p=LAST.prefill; if(!p) return;
  if(!q('#fCustName').value) q('#fCustName').value=p.custName||'';
  if(!q('#fCustNo').value) q('#fCustNo').value=p.custNo||'';
  PREFILLED=true;
}

function applyQr(d){
  if(d.qr && d.qr_seq!==QRSEQ){
    QRSEQ=d.qr_seq;
    q('#qrImg').src='data:image/png;base64,'+d.qr;
    q('#qrHint').textContent='用手机支付宝扫码即可。二维码变化时才会重新下载；页面切到后台会自动暂停刷新，不浪费流量。';
    q('#qrWarn').style.display='none';
  } else if(!d.qr){
    q('#qrHint').textContent='正在截取二维码，稍等几秒…';
  }
}

async function pollState(){
  if(!SID){ stopPolling(); return; }
  if(Date.now()-POLL_T0 > 5*60*1000){          // 挂着不扫就别一直轮询了
    stopPolling(); q('#btnStart').disabled=false;
    q('#qrWarn').style.display='block';
    q('#qrWarn').textContent='等超过 5 分钟了，已停止刷新以免浪费流量。要登录请重新点「开始扫码登录」。';
    return;
  }
  const d=await api('/api/session/state?sid='+encodeURIComponent(SID)+'&qr_seq='+QRSEQ);
  if(!d.ok){
    stopPolling(); q('#btnStart').disabled=false;
    q('#qrWarn').style.display='block'; q('#qrWarn').textContent=d.error||'会话已失效，请重新开始';
    return;
  }
  applyQr(d);
  if(d.state==='logged_in'){
    stopPolling();
    q('#loginBox').style.display='none'; q('#pickBox').style.display='block';
    q('#pickNote').textContent = CHANGING
      ? '已登录。选新的房间（姓名学号已帮你填好）：'
      : '已登录。请填写姓名学号并选择房间：';
    prefillFromMine();
    LEVELS=d.levels||[]; CHOSEN={}; q('#levelRow').innerHTML='';
    await renderLevel(0);
  } else if(d.state==='error' || d.state==='expired'){
    stopPolling(); q('#btnStart').disabled=false;
    q('#qrWarn').style.display='block';
    q('#qrWarn').textContent=(d.error||'登录失败')+'。可以重新点「开始扫码登录」。';
  }
}

const keyOf = (type) => (type==='floor') ? 'floor' : type+'Id';

async function renderLevel(i){
  if(i>=LEVELS.length) return;
  for(let j=i;j<LEVELS.length;j++){ const e=q('#lv_'+j); if(e) e.parentElement.remove(); }
  const lv=LEVELS[i];
  const op=await api('/api/session/levels',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sid:SID,index:i,chosen:CHOSEN})});
  if(!op.ok){
    q('#levelRow').insertAdjacentHTML('beforeend',
      '<div class="note">读取「'+(lv.remark||lv.type)+'」失败：'+(op.error||'')+'</div>');
    return;
  }
  q('#levelRow').insertAdjacentHTML('beforeend',
    '<div class="fld"><label>'+(lv.remark||lv.type)+'</label><select id="lv_'+i+'">'
    +'<option value="">请选择</option>'
    +((op.options||[]).map(o=>'<option value="'+o.key+'">'+o.value+'</option>').join(''))
    +'</select></div>');
  const sel=q('#lv_'+i);
  sel.onchange=async ()=>{
    CHOSEN={};
    for(let j=0;j<=i;j++){
      const e2=q('#lv_'+j);
      if(e2 && e2.value) CHOSEN[keyOf(LEVELS[j].type)]=e2.value;
    }
    q('#commitHint').textContent='';
    await renderLevel(i+1);
  };
}

async function commitRoom(){
  const names={}; let full=true;
  LEVELS.forEach((lv,i)=>{
    const e=q('#lv_'+i);
    if(!e || e.selectedIndex<=0){ full=false; names[lv.type+'Name']=''; return; }
    names[lv.type+'Name']=e.options[e.selectedIndex].text;
  });
  if(!full){ q('#commitHint').textContent='四级都要选完'; return; }
  const body={sid:SID, utoken:UTOKEN, custName:q('#fCustName').value.trim(),
              custNo:q('#fCustNo').value.trim(), nickname:'', chosen:CHOSEN, names:names};
  await submitCommit(body);
}

async function submitCommit(body){
  if((!body.custName||!body.custNo) && !UTOKEN){
    q('#commitHint').textContent='第一次绑定要填姓名和学号'; return;
  }
  q('#btnCommit').disabled=true; q('#commitHint').textContent='正在查询…';
  const d=await api('/api/session/commit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  q('#btnCommit').disabled=false;
  if(!d.ok){ q('#commitHint').textContent='失败：'+(d.error||'未知'); return; }
  UTOKEN=d.utoken; localStorage.setItem('dorm_utoken',UTOKEN);
  q('#commitHint').textContent='已设为 '+fmt(d.remain)+' '+d.unit;
  q('#pickBox').style.display='none'; q('#loginBox').style.display='none';
  q('#btnStart').disabled=false; q('#startHint').textContent='';
  SID=''; stopPolling();
  CHANGING=false; syncPanelMode();
  await boot();
}

async function refreshMine(){
  const h=q('#mineHint');
  h.textContent='正在查询…';
  const d=await api('/api/refresh',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({utoken:UTOKEN})});
  h.textContent = d.ok ? '已更新' : (d.throttled ? '刚刚刷过' : ('失败：'+(d.error||'未知')));
  if(d.ok) await boot();
}

async function forgetMine(){
  if(!confirm('确定删除你的全部数据吗？包括服务端保存的会话凭据。删除后需要重新扫码。')) return;
  const d=await api('/api/me?utoken='+encodeURIComponent(UTOKEN),{method:'DELETE'});
  UTOKEN=''; localStorage.removeItem('dorm_utoken');
  LAST.prefill=null; AUTO_DONE=false;
  await boot();
}

boot();
