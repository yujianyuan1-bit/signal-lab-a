// Run: node tests/test_dashboard.cjs (no third-party test dependencies).
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const script=[...html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');
const now='2026-10-19T09:00:00+08:00',asof='2026-10-16';
const node=()=>({value:'',textContent:'',innerHTML:'',checked:false,hidden:true,style:{},dataset:{},events:{},classList:{toggle(){},add(){},remove(){}},addEventListener(t,fn){(this.events[t]??=[]).push(fn)},setAttribute(){},click(){(this.events.click||[]).forEach(fn=>fn({target:this}))}});
function fixture(){const data={schema_version:1,generated_at:now,indices:{},research:{version:1,indices:{}}};for(const k of ['cy','kc']){data.indices[k]={status:'ok',index:{date:asof,close:100,ma20:110,ma60:120,five_day_change_pct:-2}};data.research.indices[k]={};for(const id of ['value','earnings','liquidity','breadth','volume','etf','leverage','trend','relative'])data.research.indices[k][id]={status:'ok',score:6,date:asof,source:'fixture',rule:'fixture',pe:30};data.research.indices[k].cvd={status:'not_connected',score:null,reason:'no real ticks'};}return data;}
function browser(feed){const nodes={},tabs=['cy','kc'].map(k=>Object.assign(node(),{dataset:{index:k}}));const get=id=>nodes[id]??(nodes[id]=node());const storage=new Map();class Clock extends Date{constructor(...args){super(...(args.length?args:[now]))}static now(){return +new Date(now)}}
const context={console,Date:Clock,URL,Blob,setTimeout(){},clearTimeout(){},confirm:()=>true,alert(){},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},location:{hash:'',origin:'http://localhost'},document:{querySelector:s=>get(s),getElementById:id=>get('#'+id),querySelectorAll:s=>s==='.tab[data-index]'?tabs:[],createElement:()=>node(),body:{append(){},appendChild(){}}},window:{SIGNAL_LIVE:feed,scrollTo(){}}};vm.createContext(context);vm.runInContext(script,context);return{context,nodes,tabs,get,run:s=>vm.runInContext(s,context)};}
{
 const b=browser(fixture());
 for(const k of ['cy','kc']){assert.equal(b.run(`scoreOf('${k}').count`),8);assert.equal(b.run(`db.${k}.factors.cvd.score`),null);assert.equal(b.run(`db.${k}.factors.relative.weight??FACTORS.find(f=>f.id==='relative').weight`),0);}
 assert.match(b.nodes['#feedBadge'].textContent,/9\/10/);
 // Refresh failure must clear an old automatic score, even with cached data.
 const legacy=b.run('deep(db)');delete legacy.cy.factors.relative;delete legacy.kc.factors.relative;b.context.legacyInput=legacy;
 assert.equal(b.run('validateInputJson(legacyInput)'),true);
 b.run("window.SIGNAL_LIVE.research.indices.cy.volume={status:'unavailable',reason:'offline'};applyFeed()");
 assert.equal(b.run('db.cy.factors.volume.score'),null);assert.equal(b.run("scoreOf('cy').count"),7);
 // Manual edits survive automatic refresh but are visibly separate.
 b.run("db.cy.factors.volume={mode:'manual',kind:'judgment',score:8,date:'2026-10-16',note:'manual'};applyFeed()");
 assert.equal(b.run('db.cy.factors.volume.score'),8);assert.match(b.nodes['#feedBadge'].textContent,/8\/10/);
 // Neither changing the index nor reloading can reuse unpersisted CVD trust.
 b.run("db.cy.factors.cvd={mode:'manual',kind:'judgment',score:9,date:'2026-10-16'}");
 b.tabs[1].click();assert.equal(b.run('db.cy.factors.cvd.score'),null);
 b.run("window.SIGNAL_LIVE.generated_at='2026-09-01T17:00:00+08:00';db.cy.factors.volume.mode='auto';applyFeed()");
 assert.equal(b.run("scoreOf('cy').count"),0);
}
{
 const b=browser(undefined);assert.equal(b.run("scoreOf('cy').count"),0);assert.equal(b.run("scoreOf('kc').count"),0);
 assert.equal(b.nodes['#indexPrice'].textContent,'—');
}
{
 const d=fixture();delete d.research;d.indices.cy.etf={date:asof,amount_ratio_20d:1.1,daily_change_pct:-1,source:'fixture'};
 const b=browser(d);assert.equal(b.run("scoreOf('cy').count"),2);assert.equal(b.run('db.cy.factors.volume.proxy'),true);
}
console.log('PASS dashboard: 9/10 observations, 8 weighted factors, failed refresh clearing, manual override, missing/stale feeds, legacy data and CVD index isolation');
if(process.argv[2]){
 const captured={window:{}};vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),captured);
 const b=browser(captured.window.SIGNAL_LIVE);
 for(const k of ['cy','kc'])assert.equal(b.run(`scoreOf('${k}').count`),8);
 console.log('PASS both indices use 8 weighted real factors, with the tenth observation isolated in the dashboard');
}
