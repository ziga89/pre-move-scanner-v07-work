const cards=document.getElementById("cards");
const dot=document.getElementById("dot");
const connection=document.getElementById("connection");
const flows=document.getElementById("flows");
const onchainStatus=document.getElementById("onchain-status");

function money(v){
  if(v==null||!isFinite(v)) return "—";
  const a=Math.abs(v);
  if(a>=1e9)return "$"+(v/1e9).toFixed(2)+"B";
  if(a>=1e6)return "$"+(v/1e6).toFixed(2)+"M";
  if(a>=1e3)return "$"+(v/1e3).toFixed(1)+"K";
  return "$"+v.toFixed(0);
}
function pct(v,d=1){return v==null?"—":(100*v).toFixed(d)+"%";}
function col(s){
  if(s==null)return "var(--muted)";
  if(s>=80)return "var(--extreme)";
  if(s>=70)return "var(--hot)";
  if(s>=55)return "var(--warn)";
  return "var(--good)";
}
function venueRows(m){
  if(!m.venues||!m.venues.length)return `<div class="muted">Waiting for venue data…</div>`;
  return `<div class="venue-table">
    <div class="vr vh"><span>Venue</span><span>Ask ±1%</span><span>vs base</span><span>Buy 60s</span><span>Vol 60s</span><span>Conf.</span></div>
    ${m.venues.map(v=>`<div class="vr">
      <span><b>${v.venue}</b><small>${v.symbol} · 24h ${money(v.volume_24h_discovery_usd||0)}</small></span>
      <span>${money(v.ask_depth_1)}</span>
      <span>${pct(v.ask_depth_ratio)}</span>
      <span>${pct(v.buy_ratio_60s)}</span>
      <span>${money(v.volume_60s)}</span>
      <span class="${v.confirmed?"up":"muted"}">${v.confirmed?"YES":"no"} (${v.flags}/4)</span>
    </div>`).join("")}
  </div>`;
}
function card(m){
  if(m.score==null){
    return `<article class="card"><div class="symbol">${m.asset}</div><div class="muted">${m.status||"waiting"} · coverage ${m.coverage||0}/${m.coverage_total||5}</div></article>`;
  }
  const comps=Object.entries(m.components||{}).map(([k,v])=>`<div class="chip"><b>${v}</b>${k.replaceAll("_"," ")}</div>`).join("");
  return `<article class="card composite" data-asset="${m.asset}" onclick="selectHistoryAsset('${m.asset}')">
    <div class="card-head">
      <div>
        <div class="symbol">${m.asset}<span class="venue">AUTO TOP‑5 BY VOLUME</span></div>
        <div class="price">$${Number(m.price||0).toLocaleString(undefined,{maximumFractionDigits:7})} · 5m ${m.price_change_5m_pct>=0?"+":""}${m.price_change_5m_pct}%</div>
      </div>
      <div class="score" style="color:${col(m.score)}">${m.score}<small>/100</small></div>
    </div>
    <div class="meter"><div style="width:${Math.min(100,m.score)}%;background:${col(m.score)}"></div></div>
    <div class="coverage"><b>${m.coverage}/${m.coverage_total}</b> venues live · <b>${m.confirmed_venues}</b> venue confirmations</div>
    <div class="grid">
      <div class="metric"><label>Combined ask depth ±1%</label><b>${money(m.ask_depth_1)}</b></div>
      <div class="metric"><label>Combined bid depth ±1%</label><b>${money(m.bid_depth_1)}</b></div>
      <div class="metric"><label>Ask depth vs own baselines</label><b>${pct(m.ask_depth_ratio_vs_baseline)}</b></div>
      <div class="metric"><label>Aggressive buys · all venues</label><b>${pct(m.buy_ratio_60s)}</b><small>${m.trade_count_60s} trades</small></div>
      <div class="metric"><label>Combined volume · 60s</label><b>${money(m.volume_60s)}</b><small>vs baseline ${m.volume_ratio_vs_baseline}×</small></div>
      <div class="metric"><label>Book imbalance ±1%</label><b>${pct((m.book_imbalance_1+1)/2)}</b><small>50% = balanced</small></div>
      <div class="metric"><label>Ask replenishment</label><b>${m.ask_replenishment==null?"n/a":m.ask_replenishment+"×"}</b></div>
      <div class="metric"><label>Liquidity-weighted spread</label><b>${m.spread_bps} bps</b></div>
    </div>
    <div class="components">${comps}</div>
    <details>
      <summary>Exchange breakdown</summary>
      ${venueRows(m)}
    </details>
  </article>`;
}
function render(data){
  cards.innerHTML=Object.values(data.assets||{}).sort((a,b)=>(b.score??-1)-(a.score??-1)).map(card).join("");
  const oc=data.onchain||{};
  if(!oc.enabled){
    onchainStatus.textContent="Disabled. Enable Etherscan in config.json for known MM-wallet tracking.";
    flows.innerHTML="";
  }else if(!oc.active){
    onchainStatus.textContent=oc.last_error||"Waiting for Etherscan API key…";
  }else{
    onchainStatus.textContent="Live known-wallet transfers. Transfers are not automatically buys/sells.";
    flows.innerHTML=(oc.events||[]).slice(0,20).map(e=>`<div class="flow">
      <span>${new Date(e.ts*1000).toLocaleTimeString()}</span>
      <b class="${e.direction==="IN"?"up":"down"}">${e.direction}</b>
      <span>${Number(e.amount).toLocaleString()} ${e.asset} · ${e.wallet_label}</span>
      <span class="tx">${String(e.tx||"").slice(0,10)}…</span>
    </div>`).join("");
  }
}
function connect(){
  const proto=location.protocol==="https:"?"wss:":"ws:";
  const ws=new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen=()=>{dot.classList.add("live");connection.textContent="live";ws.send("hello");};
  ws.onmessage=e=>{try{render(JSON.parse(e.data));}catch(err){console.error(err);}};
  ws.onclose=()=>{dot.classList.remove("live");connection.textContent="reconnecting…";setTimeout(connect,1500);};
  ws.onerror=()=>ws.close();
  setInterval(()=>{if(ws.readyState===1)ws.send("ping")},15000);
}
connect();

// ---------------- Historical microstructure charts ----------------

let historyAsset = null;
let historyHours = 6;
let historyTimer = null;
const HISTORY_REFRESH_MS = 15000;

function selectHistoryAsset(asset){
  historyAsset = asset;
  document.querySelectorAll(".card.composite").forEach(el=>{
    el.classList.toggle("selected", el.dataset.asset===asset);
  });
  loadHistory();
}
window.selectHistoryAsset = selectHistoryAsset;

document.querySelectorAll(".range-buttons button").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    document.querySelectorAll(".range-buttons button").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    historyHours = Number(btn.dataset.hours);
    loadHistory();
  });
});

function resizeCanvas(canvas){
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(320, rect.width);
  const h = Math.max(160, rect.height || 200);
  canvas.width = Math.floor(w*dpr);
  canvas.height = Math.floor(h*dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  return {ctx,w,h};
}
function finite(v){ return Number.isFinite(Number(v)); }

function niceTime(ts){
  const d = new Date(ts*1000);
  if(historyHours<=24) return d.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  return d.toLocaleDateString([], {month:"short",day:"numeric"})+" "+d.toLocaleTimeString([], {hour:"2-digit"});
}

function drawSeries(canvasId, rows, series, opts={}){
  const canvas=document.getElementById(canvasId);
  if(!canvas) return;
  const {ctx,w,h}=resizeCanvas(canvas);
  ctx.clearRect(0,0,w,h);

  const pad={l:52,r:15,t:12,b:28};
  const pw=w-pad.l-pad.r, ph=h-pad.t-pad.b;

  if(!rows || rows.length<2){
    ctx.fillStyle="#94a3b8";
    ctx.font="12px system-ui";
    ctx.fillText("Collecting history…",pad.l,pad.t+20);
    return;
  }

  const xs=rows.map(r=>Number(r.ts)).filter(Number.isFinite);
  const xmin=Math.min(...xs), xmax=Math.max(...xs);

  let vals=[];
  for(const s of series){
    for(const r of rows){
      const v=s.get(r);
      if(finite(v)) vals.push(Number(v));
    }
  }
  if(!vals.length) return;

  let ymin=opts.ymin!=null?opts.ymin:Math.min(...vals);
  let ymax=opts.ymax!=null?opts.ymax:Math.max(...vals);
  if(ymax===ymin){ ymax=ymin+1; }
  const margin=(ymax-ymin)*0.06;
  if(opts.ymin==null) ymin-=margin;
  if(opts.ymax==null) ymax+=margin;

  const X=x=>pad.l+(x-xmin)/(xmax-xmin||1)*pw;
  const Y=y=>pad.t+(1-(y-ymin)/(ymax-ymin||1))*ph;

  // grid
  ctx.strokeStyle="#263241";
  ctx.lineWidth=1;
  ctx.fillStyle="#94a3b8";
  ctx.font="10px system-ui";
  for(let i=0;i<=4;i++){
    const y=pad.t+ph*i/4;
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();
    const val=ymax-(ymax-ymin)*i/4;
    const label=opts.formatY?opts.formatY(val):String(Number(val.toFixed(2)));
    ctx.fillText(label,4,y+3);
  }
  for(let i=0;i<=4;i++){
    const x=pad.l+pw*i/4;
    ctx.beginPath();ctx.moveTo(x,pad.t);ctx.lineTo(x,pad.t+ph);ctx.stroke();
    const ts=xmin+(xmax-xmin)*i/4;
    const label=niceTime(ts);
    const tw=ctx.measureText(label).width;
    ctx.fillText(label,Math.max(pad.l,Math.min(w-pad.r-tw,x-tw/2)),h-7);
  }

  const defaultColors=["#5eead4","#fbbf24","#fb7185","#60a5fa","#c084fc","#34d399","#f97316"];
  series.forEach((s,idx)=>{
    ctx.strokeStyle=s.color||defaultColors[idx%defaultColors.length];
    ctx.lineWidth=s.width||1.8;
    ctx.beginPath();
    let started=false;
    for(const r of rows){
      const v=s.get(r);
      if(!finite(v)) continue;
      const x=X(Number(r.ts)), y=Y(Number(v));
      if(!started){ctx.moveTo(x,y);started=true;} else ctx.lineTo(x,y);
    }
    ctx.stroke();
  });

  // event markers supplied in opts.events
  if(opts.events){
    for(const e of opts.events){
      const x=X(Number(e.ts));
      ctx.strokeStyle=e.event_type==="score_cross_up"?"rgba(251,113,133,.65)":"rgba(148,163,184,.45)";
      ctx.setLineDash([4,4]);
      ctx.beginPath();ctx.moveTo(x,pad.t);ctx.lineTo(x,pad.t+ph);ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // legend
  let lx=pad.l+4;
  ctx.font="10px system-ui";
  series.forEach((s,idx)=>{
    ctx.fillStyle=s.color||defaultColors[idx%defaultColors.length];
    ctx.fillRect(lx,pad.t+3,10,2);
    ctx.fillStyle="#cbd5e1";
    ctx.fillText(s.label,lx+14,pad.t+6);
    lx+=18+ctx.measureText(s.label).width+15;
  });
}

function drawVolume(canvasId, rows, events){
  const canvas=document.getElementById(canvasId);
  if(!canvas) return;
  const {ctx,w,h}=resizeCanvas(canvas);
  ctx.clearRect(0,0,w,h);
  const pad={l:52,r:15,t:12,b:28};
  const pw=w-pad.l-pad.r, ph=h-pad.t-pad.b;
  if(!rows||rows.length<2){ctx.fillStyle="#94a3b8";ctx.fillText("Collecting history…",pad.l,25);return;}

  const xmin=rows[0].ts,xmax=rows[rows.length-1].ts;
  const vols=rows.map(r=>Number(r.volume_60s)||0);
  const maxv=Math.max(...vols,1);
  const X=x=>pad.l+(x-xmin)/(xmax-xmin||1)*pw;
  const Y=y=>pad.t+(1-y/maxv)*ph;

  ctx.strokeStyle="#263241";ctx.fillStyle="#94a3b8";ctx.font="10px system-ui";
  for(let i=0;i<=4;i++){
    const y=pad.t+ph*i/4;
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();
    ctx.fillText(money(maxv*(1-i/4)),4,y+3);
  }

  const barw=Math.max(1,pw/rows.length);
  ctx.fillStyle="rgba(96,165,250,.42)";
  rows.forEach(r=>{
    const x=X(r.ts), y=Y(Number(r.volume_60s)||0);
    ctx.fillRect(x,y,barw,Math.max(1,pad.t+ph-y));
  });

  // confirmed venues line, scaled to top-right-ish overlay.
  const maxConf=Math.max(...rows.map(r=>Number(r.confirmed_venues)||0),1);
  ctx.strokeStyle="#fbbf24";ctx.lineWidth=1.7;ctx.beginPath();
  let started=false;
  rows.forEach(r=>{
    const x=X(r.ts);
    const y=pad.t+(1-(Number(r.confirmed_venues)||0)/maxConf)*ph;
    if(!started){ctx.moveTo(x,y);started=true}else ctx.lineTo(x,y);
  });ctx.stroke();

  ctx.fillStyle="#cbd5e1";
  ctx.fillText("Volume",pad.l+4,pad.t+7);
  ctx.fillStyle="#fbbf24";
  ctx.fillText("Confirmed venues",pad.l+60,pad.t+7);
}

function drawVenueHistory(data){
  const canvas=document.getElementById("chart-venues");
  if(!canvas) return;
  const all=[];
  const series=[];
  let idx=0;
  const palette=["#5eead4","#fbbf24","#fb7185","#60a5fa","#c084fc","#34d399","#f97316","#a3e635"];

  // Build a unified timestamp array by taking all points and using each venue as sparse series.
  const timestampMap=new Map();
  for(const [venue,rows] of Object.entries(data.venues||{})){
    rows.forEach(r=>{
      if(!timestampMap.has(r.ts)) timestampMap.set(r.ts,{ts:r.ts});
      timestampMap.get(r.ts)[venue]=r.ask_depth_ratio;
    });
    series.push({label:venue,color:palette[idx++%palette.length],get:r=>r[venue]});
  }
  const merged=Array.from(timestampMap.values()).sort((a,b)=>a.ts-b.ts);
  drawSeries("chart-venues",merged,series,{
    ymin:0,
    formatY:v=>(v*100).toFixed(0)+"%"
  });
  const legend=document.getElementById("venue-history-legend");
  legend.textContent=series.length
    ? "Ask depth vs each venue's own baseline. 100% = normal; falling lines = sell-side liquidity thinning."
    : "No venue history yet.";
}

async function loadHistory(){
  if(!historyAsset) return;
  const subtitle=document.getElementById("history-subtitle");
  subtitle.textContent=`${historyAsset} · last ${historyHours===168?"7 days":historyHours+"h"} · refreshing every 15s`;

  try{
    const r=await fetch(`/api/history/${encodeURIComponent(historyAsset)}?hours=${historyHours}&max_points=1800`);
    const d=await r.json();
    const rows=d.composite||[];
    const events=d.events||[];

    drawSeries("chart-price",rows,[
      {label:"Price",get:r=>r.price,color:"#5eead4"}
    ],{events,formatY:v=>"$"+Number(v).toLocaleString(undefined,{maximumFractionDigits:6})});

    drawSeries("chart-score",rows,[
      {label:"Score",get:r=>r.score,color:"#fb7185"}
    ],{ymin:0,ymax:100,events,formatY:v=>Math.round(v)});

    drawSeries("chart-flow",rows,[
      {label:"Ask depth vs baseline",get:r=>r.ask_depth_ratio,color:"#60a5fa"},
      {label:"Aggressive buy ratio",get:r=>r.buy_ratio_60s,color:"#34d399"},
      {label:"Volume vs baseline",get:r=>Math.min(Number(r.volume_ratio)||0,4)/4,color:"#fbbf24"}
    ],{ymin:0,ymax:1.25,events,formatY:v=>(v*100).toFixed(0)+"%"});

    drawVolume("chart-volume",rows,events);
    drawVenueHistory(d);

    const eventBox=document.getElementById("history-events");
    if(!events.length){
      eventBox.innerHTML='<span class="muted">No threshold/confirmation events in this range.</span>';
    }else{
      eventBox.innerHTML=events.slice().reverse().slice(0,60).map(e=>
        `<div class="hist-event"><span>${new Date(e.ts*1000).toLocaleString()}</span><b>${e.asset}</b><span>${e.message}</span></div>`
      ).join("");
    }
  }catch(err){
    subtitle.textContent=`History error: ${err}`;
  }
}

setInterval(()=>{ if(historyAsset) loadHistory(); },HISTORY_REFRESH_MS);
window.addEventListener("resize",()=>{ if(historyAsset) loadHistory(); });

// Auto-select the first live coin once cards arrive.
const _oldRender = render;
render = function(data){
  _oldRender(data);
  if(!historyAsset){
    const first=Object.values(data.assets||{}).sort((a,b)=>(b.score??-1)-(a.score??-1))[0];
    if(first && first.asset) setTimeout(()=>selectHistoryAsset(first.asset),100);
  }else{
    document.querySelectorAll(".card.composite").forEach(el=>{
      el.classList.toggle("selected", el.dataset.asset===historyAsset);
    });
  }
};
