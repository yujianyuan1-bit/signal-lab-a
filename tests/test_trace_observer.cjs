// Run node tests/test_trace_observer.cjs. Test state parsing, data provenance and DOM integration.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'..');
function moduleContext(extra={}){const w={...extra};const c={window:w,console};vm.createContext(c);for(const f of ['trace-evidence.js','trace-observer.js'])vm.runInContext(fs.readFileSync(path.join(root,f),'utf8'),c);return w;}
const w=moduleContext(),m=w.SignalTraces,R1='R1_alt_bleed_reversal',R2='R2_volume_dryup_ignition';
function journal(k){const date='2026-10-19',summary={version:`${k}-altseason-paper-v1`,observations:1,rules:{}};const doc={status:'recorded',observed_at:date+'T17:00:00+08:00',spec_hash:'frozen',implementation_sha256:'engine',summary};const row={date,observed_at:doc.observed_at,spec_hash:'frozen',implementation_sha256:'engine',features:{date,amount_ratio:1.5,close10_range:.04,excess_return:.02},states:{},events:{}};for(const k of [R1,R2]){row.states[k]={setup_date:null,wait_sessions:0};row.events[k]=false;summary.rules[k]={nonoverlap:{'20':{n:0,wins:0,win_pct:null}}};}return {doc,row};}
{
 assert.equal(m.view('cy',{summary:{version:'cy-altseason-paper-v1',observations:0}},null,0).verified,false);
 const {doc,row}=journal('kc');assert.equal(m.view('kc',doc,row,1).verified,true);
 for(const mutate of [d=>d.summary.version='cy-altseason-paper-v1',d=>d.status='failed',d=>d.spec_hash='changed']){const d=structuredClone(doc);mutate(d);assert.equal(m.view('kc',d,row,1).verified,false);}
 assert.equal(m.view('kc',doc,row,2).verified,false);
 const bad=structuredClone(row);bad.states[R1].wait_sessions=11;assert.equal(m.view('kc',doc,bad,1).verified,false);
 row.events[R1]=true;row.events[R2]=true;m.cache.kc=m.view('kc',doc,row,1);
 assert.match(m.consensus('kc',row.date),/同日.*联合胜率未验证/);assert.match(m.consensus('kc','2026-10-16'),/无法.*对齐/);
 const html=m.render('kc',{volume:{score:9,kind:'judgment',mode:'manual',date:'2026-10-16'}},row.date);
 assert.match(html,/SOL \/ DOGE/);assert.match(html,/手动判断/);assert.match(html,/4\/5/);assert.match(html,/成熟交易0，胜率未定义/);assert.doesNotMatch(html,/前瞻20日：4\/5/);
 const cy=m.render('cy',{},'2026-10-16');assert.match(cy,/ETH式行为类比/);assert.match(cy,/11\/17/);assert.match(cy,/9\/15/);assert.match(cy,/缺失/);assert.doesNotMatch(cy,/SOL \/ DOGE/);
 const xss=m.render('cy',{etf:{score:3,kind:'verified',date:'<img src=x onerror=alert(1)>'}},'2026-10-16');assert.doesNotMatch(xss,/<img/);assert.match(xss,/&lt;img/);
}
(async()=>{
 const broken=moduleContext({fetch:async()=>{throw Error('offline')}});await broken.SignalTraces.load();assert.equal(broken.SignalTraces.cache.cy.verified,false);assert.match(broken.SignalTraces.render('cy',{},'2026-10-16'),/未能核对日志/);
 const {doc,row}=journal('cy');const fetch=async url=>({ok:true,json:async()=>doc,text:async()=>JSON.stringify(row)+'\n'});const loaded=moduleContext({fetch});await loaded.SignalTraces.load();assert.equal(loaded.SignalTraces.cache.cy.verified,true);assert.equal(loaded.SignalTraces.cache.kc.verified,false);assert.equal(loaded.SignalTraces.cache.breadth.verified,false);
 // Execute the real dashboard's existing fixtures with both new modules loaded.
 let source=fs.readFileSync(path.join(root,'tests/test_dashboard.cjs'),'utf8');source=source.replace("vm.runInContext(script,context);", "vm.runInContext(fs.readFileSync(path.join(root,'trace-evidence.js'),'utf8'),context);vm.runInContext(fs.readFileSync(path.join(root,'trace-observer.js'),'utf8'),context);vm.runInContext(script,context);");
 source+=`\n{const b=browser(fixture());const before=b.run("scoreOf('cy').score");assert.match(b.get('#tracePanel').innerHTML,/ETH式行为类比/);assert.match(b.get('#tracePanel').innerHTML,/9\\/15/);b.tabs[1].click();assert.match(b.get('#tracePanel').innerHTML,/SOL \\/ DOGE/);assert.match(b.get('#tracePanel').innerHTML,/4\\/5/);assert.equal(b.run("scoreOf('cy').score"),before);assert.equal(b.run("db.cy.factors.cvd.score"),null);}`;
 const context={require,console,URL,Blob,__dirname:path.join(root,'tests'),process:{argv:[]},structuredClone};vm.runInNewContext(source,context);
 console.log('PASS trace module: per-board profiles, frozen cohorts, zero results, invalid/stale/version-mismatched logs, offline fetch, XSS escaping, tab switching and unchanged scores/CVD.');
})().catch(e=>{console.error(e);process.exitCode=1});
