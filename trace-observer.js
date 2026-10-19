/* Observable A-share behavior, not participant identity or a calibrated entry score. */
(function(root){
'use strict';
const R1='R1_alt_bleed_reversal',R2='R2_volume_dryup_ignition';
const profiles={cy:{title:'创业板 · 混合成长承接与修复',analogy:'ETH式行为类比',description:'观察不同产业共同承接、相对弱势修复和广度扩散。类比不代表资金先后顺序，也不改变R1/R2参数。',factors:[['etf','ETF申赎背景'],['leverage','融资杠杆背景'],['breadth','修复扩散背景'],['relative','相对强弱背景']]},kc:{title:'科创50 · 集中科技点火与脉冲',analogy:'SOL / DOGE式行为类比',description:'观察相对失血后的反转、缩量压缩后的突破。科技集中不等于齐涨齐跌，放量也不等于主力建仓。',factors:[['volume','量价背景'],['trend','趋势背景'],['breadth','内部扩散背景'],['relative','相对强弱背景']]}};
const cache={cy:null,kc:null,breadth:null};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=n=>Number.isFinite(n)?`${n>=0?'+':''}${n.toFixed(2)}%`:'—';
const rules={R1:{key:R1,name:'R1 · 相对失血反转',text:'上证／目标比值处于此前60日高20%且收盘低于MA20，进入等待；随后1–10日首次跑赢上证≥1.5个百分点、收盘>MA5且>前高、成交额>此前5日均额，记录事件。'},R2:{key:R2,name:'R2 · 缩量压缩点火',text:'5日均额处于此前60日低25%且10日收盘区间≤8%，进入等待；随后1–10日首次上涨≥2%、收盘>MA20且创此前10日收盘新高、成交额≥此前5日均额1.4倍，记录事件。'}};
function unavailable(reason){return {verified:false,title:'状态待核对',reason};}
function view(kind,doc,latest,lineCount){
 if(!doc||doc.status==='failed')return unavailable('日志不可用；不把旧历史当作当前信号。');
 const version=kind==='breadth'?'cy-breadth-repair-forward-v1':`${kind}-altseason-paper-v1`,s=doc.summary;
 if(s?.version!==version||!Number.isInteger(s.observations)||s.observations<0)return unavailable('日志版本或样本量不匹配。');
 if(s.observations===0)return {verified:false,title:'等待首个有效收盘记录',reason:'前瞻样本0；历史回测未回填。'};
 if(lineCount!==s.observations||!latest||!/^\d{4}-\d{2}-\d{2}$/.test(latest.date))return unavailable('日志条数或记录日期无法核对。');
 if(!doc.spec_hash||latest.spec_hash!==doc.spec_hash||!doc.implementation_sha256||latest.implementation_sha256!==doc.implementation_sha256)return unavailable('冻结参数或程序版本与日志不一致。');
 if(!Number.isFinite(Date.parse(latest.observed_at))||!Number.isFinite(Date.parse(doc.observed_at))||Date.parse(latest.observed_at)>Date.parse(doc.observed_at))return unavailable('日志时间不一致。');
 const validState=state=>state&&Number.isInteger(state.wait_sessions)&&state.wait_sessions>=0&&state.wait_sessions<=9&&(!state.setup_date||(/^\d{4}-\d{2}-\d{2}$/.test(state.setup_date)&&state.setup_date<=latest.date));
 if(kind==='breadth'){
  if(!validState(latest.state_after)||typeof latest.event!=='boolean')return unavailable('广度状态字段不完整。');
  return {verified:true,date:latest.date,event:latest.event,waiting:!!latest.state_after.setup_date,summary:s,latest};
 }
 if(latest.features?.date!==latest.date||![R1,R2].every(k=>validState(latest.states?.[k])&&typeof latest.events?.[k]==='boolean'))return unavailable('R1/R2状态字段不完整。');
 return {verified:true,date:latest.date,events:latest.events,states:latest.states,features:latest.features,summary:s};
}
function statusText(v,key,asof){
 if(!v?.verified)return esc(v?.title||'正在读取前瞻状态…');
 const event=key==='breadth'?v.event:v.events[key],waiting=key==='breadth'?v.waiting:!!v.states[key].setup_date;
 return `${esc(v.date)} · ${event?'该日记录到事件':waiting?'处于等待状态':'该日无事件'}${v.date!==asof?' · 与页面日期不同':''}`;
}
function factorHTML(factors,id,label){
 const f=factors?.[id],valid=f&&Number.isFinite(f.score)&&f.kind!=='missing';
 const value=valid?`${f.score.toFixed(1)} /10`:'缺失';
 return `<div class="trace-factor"><span>${esc(label)}</span><strong>${value}</strong><small>${esc(f?.date||'无有效日期')} · ${f?.mode==='manual'?'手动判断':f?.proxy?'自动代理':'环境字段'}</small></div>`;
}
function table(series){
 if(!series)return '<p>历史摘要未载入。</p>';
 return `<div class="evidence-table-wrap"><table class="evidence-table"><thead><tr><th>持有</th><th>盈利／笔数</th><th>平均净收益</th><th>中位数</th><th>最差不利波动</th></tr></thead><tbody>${['10','20','40'].map(h=>{const q=series[h];if(!q)return '';return `<tr><th>${h}日</th><td>${q.wins}/${q.n} <small>${q.n?(100*q.wins/q.n).toFixed(1)+'%':'—'}</small></td><td>${pct(q.mean_net_pct)}</td><td>${pct(q.median_net_pct)}</td><td>${pct(q.worst_mae_pct)}</td></tr>`}).join('')}</tbody></table></div>`;
}
function liveStats(v,key){
 const q=v?.verified?v.summary?.rules?.[key]?.nonoverlap?.['20']:null;
 if(!q||!Number.isInteger(q.n))return '前瞻20日：尚无可核对的成熟结果。';
 return q.n?`前瞻20日：${q.wins}/${q.n}，均值${pct(q.mean_net_pct)}；只统计该规则去重成熟交易。`:'前瞻20日：成熟交易0，胜率未定义。';
}
function consensus(key,asof){
 const v=cache[key],b=key==='cy'?cache.breadth:null;
 if(v?.verified&&v.date===asof){
  const count=[R1,R2].filter(k=>v.events[k]).length;
  let text=count===2?'R1与R2同日记录到事件；联合胜率未验证。':count===1?'该日仅一条R1/R2规则记录到事件。':'该日R1/R2均未记录到事件。';
  if(key==='cy')text+=b?.verified&&b.date===asof?(b.event?' 广度修复同日记录到事件；叠加效果未验证。':' 广度修复该日无事件。'):' 广度状态未能同日核对。';
  return text;
 }
 return 'R1/R2当前事件尚无法与页面日期对齐，不计算共振，也不折算买入分。';
}
function render(key,factors,asof){
 const p=profiles[key],h=root.SIGNAL_TRACE_HISTORY?.indices?.[key];if(!p)return '';
 const v=cache[key];
 const cards=['R1','R2'].map(id=>{const def=rules[id],x=h?.[def.key],delay=x?.delay3_20;
  return `<article class="trace-rule"><h3>${def.name}</h3><p class="evidence-rule">${def.text}</p><div class="trace-status">${statusText(v,def.key,asof)}</div><p class="tiny">${esc(v?.verified?liveStats(v,def.key):v?.reason||'历史统计与前瞻状态分开读取。')}</p>${table(x?.horizons)}${delay?`<p class="tiny">同批20日买点晚2日入场：${x.horizons['20'].wins}/${x.horizons['20'].n} → ${delay.wins}/${delay.n}；均值${pct(x.horizons['20'].mean_net_pct)} → ${pct(delay.mean_net_pct)}。</p>`:''}</article>`;
 }).join('');
 let features='<p class="tiny">量价痕迹明细等待有效收盘记录，不用当前环境评分反推R1/R2事件。</p>';
 if(v?.verified){const f=v.features;features=`<p class="trace-raw">${esc(v.date)} 日量价痕迹：成交额／此前5日均额 ${Number.isFinite(f.amount_ratio)?f.amount_ratio.toFixed(2)+'倍':'—'} · 10日收盘区间 ${Number.isFinite(f.close10_range)?(100*f.close10_range).toFixed(2)+'%':'—'} · 单日跑赢上证 ${Number.isFinite(f.excess_return)?(100*f.excess_return).toFixed(2)+'个百分点':'—'}。等待条件与事件分别判断，条件符合不代表已触发。</p>`;}
 const breadth=key==='cy'?`<div class="trace-breadth"><h3>冰点修复 · 第三条独立观察</h3><p>广度分&lt;2进入等待；随后10日内首次收盘&gt;MA5且广度高于前日。20日11/17、均值+4.83%；60日5/9、均值+7.92%。两期限独立去重，属于已查看历史。</p><strong>${statusText(cache.breadth,'breadth',asof)}</strong><p class="tiny">${esc(cache.breadth?.reason||'完整历史分布和前瞻记录见下方原广度卡片。')}</p></div>`:'<p class="tiny">冰点修复沿用已冻结的创业板观察；不把其11/17历史胜率套到科创50，也不自动与科创R1/R2合成为联合策略。</p>';
 return `<div class="trace-heading"><div><div class="evidence-kicker">OBSERVABLE BEHAVIOR / A-SHARES ONLY</div><h2>${p.title}</h2><p>${p.description}</p></div><span class="badge-mini">${p.analogy}</span></div><p class="trace-boundary">“主力痕迹”仅指可观察的量价与广度行为，不识别机构身份、真实吸筹或操盘意图。类比不接入数字资产行情，不是新的胜率评分。</p><div class="trace-factors">${p.factors.map(([id,label])=>factorHTML(factors,id,label)).join('')}</div><p class="tiny">以上为环境字段镜像，保留各自日期与手动／代理性质；ETF份额和融资变化不能单独证明机构承接，也不是R1/R2的触发条件。</p>${features}<div class="trace-conclusion"><strong>事件核对</strong><p>${esc(consensus(key,asof))}</p><span class="tiny">环境综合分、行为痕迹、事件记录同时呈现；暂无经验证的联合分数或“≥8即买入”映射。</span></div><div class="trace-rule-grid">${cards}</div>${breadth}<p class="evidence-note">R1/R2：2022-08-26—2026-10-16，1000交易日，次日指数开盘入场，固定期限收盘退出，扣除0.20个百分点，各期限独立去重。全部历史已被查看、样本少，指数代理不等于ETF成交；不同规则可能重叠。画像只改变观察重点，不修改冻结参数。</p>`;
}
async function read(kind,folder){
 if(typeof root.fetch!=='function')return unavailable('无法读取前瞻日志。');
 const ctl=typeof root.AbortController==='function'?new root.AbortController():null,timer=ctl?root.setTimeout(()=>ctl.abort(),8000):null;
 try{
  const opts={cache:'no-store',...(ctl?{signal:ctl.signal}:{})};let response=await root.fetch(`${folder}/status.json`,opts);if(!response.ok)throw Error('status unavailable');const doc=await response.json();
  if(doc?.status==='failed'||!Number.isInteger(doc?.summary?.observations)||doc.summary.observations===0)return view(kind,doc,null,0);
  response=await root.fetch(`${folder}/observations.jsonl`,opts);if(!response.ok)throw Error('journal unavailable');const body=await response.text();if(body.length>4000000)throw Error('journal too large');
  const lines=body.trim().split(/\r?\n/);if(lines.length!==doc.summary.observations)throw Error('journal count mismatch');return view(kind,doc,JSON.parse(lines.at(-1)),lines.length);
 }catch(e){return unavailable('未能核对日志，不推断当前信号。');}finally{if(timer)root.clearTimeout(timer);}
}
async function load(onchange){
 await Promise.all([['cy','./research/forward-altseason-cy'],['kc','./research/forward-altseason-kc'],['breadth','./research/forward']].map(async([kind,folder])=>{cache[kind]=await read(kind,folder);if(onchange)onchange();}));
}
root.SignalTraces={render,load,view,consensus,cache};
})(typeof window==='object'?window:globalThis);
