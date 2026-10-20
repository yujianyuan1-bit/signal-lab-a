#!/usr/bin/env python3
"""SignalLab A-share R1/R2 scanner (independent historical research).

Only public daily OHLC + genuine turnover amount (yuan); no L2, no intraday trades.
Requires standard Python 3.11+ for Eastmoney JSON. AKShare is OPTIONAL fallback.
Never modifies the pre-existing frozen R1/R2 specifications or paper forward logs.
"""
from __future__ import annotations
import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from io import StringIO
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
CONFIG={
    '159770': {'name':'机器人ETF','market':'sz','asset_type':'etf'},
    '515880': {'name':'通信ETF','market':'sh','asset_type':'etf'},
    '399363': {'name':'国证算力指数','market':'sz','asset_type':'index'},
    '159732': {'name':'消费电子ETF','market':'sz','asset_type':'etf'},
    '512480': {'name':'半导体ETF','market':'sh','asset_type':'etf'},
    '516510': {'name':'云计算50ETF','market':'sh','asset_type':'etf'},
}
BENCHMARK={'sh':'sh000001', 'cy':'sz399006','kc':'sh000688'}
REQUIRED=('date','open','high','low','close','amount')
MIN_WARM=65
HOLD_DAYS=(20,40)
COST_PCTPOINTS=0.2
DAYS_WAIT=10
EASTMONEY='https://push2his.eastmoney.com/api/qt/stock/kline/get'
FIELDS='f51,f52,f53,f54,f55,f56,f57'


def quantile(vals: list[float], q: float) -> float:
    if not vals: raise ValueError('quantile empty list')
    s=sorted(vals); pos=(len(s)-1)*q; lo=math.floor(pos); hi=math.ceil(pos)
    return s[lo]+(s[hi]-s[lo])*(pos-lo)


def check_bar(b: dict) -> dict:
    d=str(b['date'])[:10]
    date.fromisoformat(d)
    v={k:float(b[k]) for k in ('open','high','low','close','amount')}
    if not all(math.isfinite(n) and n>0 for n in v.values()):
        raise ValueError(f'{d}: invalid OHLC/turnover amount')
    if not v['low']<=min(v['open'],v['close'])<=max(v['open'],v['close'])<=v['high']:
        raise ValueError(f'{d}: OHLC bounds incorrect')
    return {'date':d,**v}


def _get_json(url:str, timeout=17) -> dict:
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (SignalLab-A/research)',
                     'Referer':'https://quote.eastmoney.com/','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as f:
        raw=f.read(8_000_001)
    if len(raw)>8_000_000: raise ValueError('response too large')
    return json.loads(raw.decode('utf-8-sig'))


def from_em_response(body:dict, expected_code:str) -> list[dict]:
    node=body.get('data')
    if body.get('rc') not in (0,None) or not isinstance(node,dict) or not node.get('klines'):
        raise ValueError(f'{expected_code}: no Eastmoney klines')
    if str(node.get('code','')).zfill(6)!=expected_code:
        raise ValueError(f'{expected_code}: returned code mismatch: {node.get("code")}')
    out=[]
    for line in node['klines']:
        z=line.split(',') if isinstance(line,str) else line
        if len(z)<7: raise ValueError(f'{expected_code}: missing amount field f57')
        out.append(check_bar(dict(date=z[0],open=z[1],close=z[2],high=z[3],low=z[4],amount=z[6])))
    if len(set(x['date'] for x in out))!=len(out):raise ValueError(f'{expected_code}: duplicate daily bars')
    return sorted(out,key=lambda x:x['date'])


def _akshare_fallback(code:str,market:str,kind:str,beg:str,end:str) -> list[dict]:
    import akshare as ak
    if kind=='etf':
        df=ak.fund_etf_hist_em(symbol=code,period='daily',start_date=beg,end_date=end,adjust='')
    else:
        df=ak.stock_zh_index_daily_em(symbol=f'{market}{code}')
    if df is None or df.empty:raise ValueError('AKShare returned empty')
    df.columns=[str(z) for z in df.columns]
    en={'date':'日期','open':'开盘','close':'收盘','high':'最高','low':'最低','amount':'成交额'}
    if all(v in df.columns for v in en.values()):
        df=df.rename(columns={v:k for k,v in en.items()})
    if not all(k in df.columns for k in en):
        raise ValueError('AKShare index response has no verified turnover amount column')
    output=[check_bar({k:str(row[k]) for k in en}) for _,row in df.iterrows() if beg<=str(row['date'])[:10].replace('-','')<=end]
    return sorted(output,key=lambda x:x['date'])


def fetch_bars(code:str,market:str,kind:str,beg:str,end:str,retries=2,allow_akshare=True)->tuple[list[dict],str]:
    secid=f'{1 if market=="sh" else 0}.{code}'
    args={'secid':secid,'klt':'101','fqt':'0','fields1':'f1,f2,f3,f4,f5,f6',
          'fields2':FIELDS,'beg':beg,'end':end,'lmt':'2500'}
    url=EASTMONEY+'?'+urllib.parse.urlencode(args)
    errors=[]
    for attempt in range(retries):
        try:
            rows=from_em_response(_get_json(url),code)
            return rows,'Eastmoney f57成交额 / 不复权'
        except (OSError,ValueError,KeyError,json.JSONDecodeError) as e:
            errors.append(f'EM:{str(e)[:140]}')
            if attempt+1<retries:time.sleep(0.45*(attempt+1))
    if allow_akshare:
        try:return _akshare_fallback(code,market,kind,beg,end),'AKShare: 东方财富实际成交额 / 不复权'
        except (Exception,) as e:errors.append(f'AKShare:{str(e)[:140]}')
    raise RuntimeError('; '.join(errors))


def features(history:list[dict])->dict | None:
    # Same frozen feature definitions as research/altseason_forward.py
    if len(history)<65:return None
    rows=history; now=rows[-1]; prev=rows[-2]; price=[z['close'] for z in rows]; amt=[z['amount'] for z in rows]
    ratios=[z['sh_close']/z['close'] for z in rows]
    v5=[statistics.mean(amt[j-4:j+1]) for j in range(4,len(rows))]
    q80=quantile(ratios[-61:-1],.8)
    q25=quantile(v5[-61:-1],.25)
    ma5=statistics.mean(price[-5:]);ma20=statistics.mean(price[-20:]);prev5=statistics.mean(amt[-6:-1])
    return {'date':now['date'],'close':now['close'], 'ma5':ma5,'ma20':ma20,
        'ratio':ratios[-1],'ratio_q80':q80,'v5':v5[-1],'v5_q25':q25,'amount':amt[-1],
        'amount_ratio':amt[-1]/prev5,'excess':now['close']/prev['close']-now['sh_close']/prev['sh_close'],
        'daily_return':now['close']/prev['close']-1,
        'span10':max(price[-10:])/min(price[-10:])-1,
        'r1_wait':ratios[-1]>=q80 and price[-1]<ma20,
        'r1_ignite':(now['close']/prev['close']-now['sh_close']/prev['sh_close'])>=.015
          and now['close']>ma5 and now['close']>prev['high'] and now['amount']>prev5,
        'r2_wait':v5[-1]<=q25 and max(price[-10:])/min(price[-10:])-1<=.08,
        'r2_ignite':(now['close']/prev['close']-1)>=.02 and now['close']>ma20
          and now['close']>max(price[-11:-1]) and now['amount']>=1.4*prev5}


def step_state(state:dict,f:dict,rule:str,global_session_index:int)->tuple[dict,str,int | None]:
    # Independent state per rule. Trigger can fire day10; timeout day10 prevents same-day rearm.
    state=dict(state)
    if state['start'] is not None:
        age=global_session_index-state['start']
        if age<=10 and f[f'{rule}_ignite']:
            state['start']=None
            return state,'TRIGGERED',age
        if age>=10:
            state['start']=None
            return state,'EXPIRED',10
        return state,'WAIT',age
    if f[f'{rule}_wait']:
        state['start']=global_session_index
        return state,'ENTER',0
    return state,'IDLE',None


def _round(value:Any,nd=5):
    return round(value,nd) if isinstance(value,float) and math.isfinite(value) else value


def scanner_replay(code:str,meta:dict,benchmark:list[dict],asset:list[dict],cy:list[dict],kc:list[dict],end_date:str,eval_sessions:int=1000)->dict:
    # Benchmark Shanghai calendar authoritative; never compress a suspension or provider gap.
    benchmark=[z for z in benchmark if z['date']<=end_date]
    cmap={z['date']:z for z in cy};kmap={z['date']:z for z in kc}
    smap={z['date']:z for z in asset if z['date']<=end_date}
    if not benchmark:raise ValueError('benchmark empty')
    first=min(smap) if smap else None
    sh_dates=[x['date'] for x in benchmark]
    sh_map={x['date']:x for x in benchmark}
    cutoff=sh_dates[-eval_sessions] if len(sh_dates)>=eval_sessions else sh_dates[0]
    states={rule:{'start':None} for rule in ('r1','r2')}
    trace=[];events=[];segment=[];missing=[];ever=False
    for gi,dt in enumerate(sh_dates):
        if first is None or dt<first: continue
        bar=smap.get(dt)
        if bar is None:
            missing.append(dt)
            if segment: segment=[]
            states={rule:{'start':None} for rule in ('r1','r2')}
            trace.append({'date':dt,'r1':'DATA_GAP','r2':'DATA_GAP','reason':'ETF/index missing Shanghai trading session'})
            continue
        row={**bar,'sh_close':sh_map[dt]['close']}
        segment.append(row);ever=True
        if len(segment)<MIN_WARM:continue
        # Frozen R1/R2 features only need the most recent 65 aligned bars.
        # Using fixed window avoids quadratic full-history recomputation.
        f=features(segment[-65:])
        if f is None:continue
        log={'date':dt,'double_ma5': bool(dt in cmap and dt in kmap and _is_above_ma5(cy,dt) and _is_above_ma5(kc,dt)), 'close':row['close'], 'ma5':_round(f['ma5']), 'ma20':_round(f['ma20']),
             'amount':_round(row['amount']), 'amount_ratio':_round(f['amount_ratio'],3),
             'r1_weak':f['r1_wait'],'r1_ignite':f['r1_ignite'],
             'r2_weak':f['r2_wait'],'r2_ignite':f['r2_ignite'],
             'excess_pct':_round(f['excess']*100,3),'ratio':_round(f['ratio']),
             'ratio_q80':_round(f['ratio_q80']),'v5_q25':_round(f['v5_q25'])}
        for rule in ('r1','r2'):
            old=states[rule]['start']
            states[rule],event,age=step_state(states[rule],f,rule,gi)
            log[rule]=event;log[f'{rule}_day']=age
            log[f'{rule}_start']=(sh_dates[states[rule]['start']] if states[rule]['start'] is not None else (sh_dates[old] if event=='TRIGGERED' and old is not None else None))
            if event=='TRIGGERED':events.append({'date':dt,'index':gi,'rule':rule.upper(),'wait_day':age,'wait_start':sh_dates[old], 'r1_double_ma5': (dt in cmap and dt in kmap and _is_above_ma5(cy,dt) and _is_above_ma5(kc,dt)) if rule=='r1' else None})
        trace.append(log)
    trading=[r for r in trace if 'close' in r]
    last=trading[-1] if trading else None
    # Freshness compares to benchmark's latest bar, not wall clock. After midnight previous close is fine.
    aligned_date=sh_dates[-1]
    fresh=bool(last and last['date']==aligned_date and not missing[-1:] == [aligned_date])
    if not last:status='INSUFFICIENT_DATA'
    elif not fresh:status='STALE'
    elif last['r1']=='TRIGGERED' or last['r2']=='TRIGGERED':status='TRIGGERED'
    elif last['r1']=='EXPIRED' or last['r2']=='EXPIRED':status='EXPIRED'
    elif last['r1'] in ('WAIT','ENTER') or last['r2'] in ('WAIT','ENTER'):status='WAITING'
    else:status='IDLE'
    stats={}
    trades=[]
    for rule in ('R1','R2'):
        for days in HOLD_DAYS:
            last_exit_index=-1;selected=[]
            signals=[e for e in events if e['rule']==rule and e['date']>=cutoff]
            for e in signals:
                si=e['index'];ei=si+1;xi=ei+days-1
                if ei<=last_exit_index:continue
                if xi>=len(sh_dates):continue
                hd=sh_dates[ei:xi+1]
                if any(d not in smap for d in hd):continue
                er=smap[hd[0]];xr=smap[hd[-1]]
                # raw ETF prices; no guaranteed net returns after corporate actions.
                net=(xr['close']/er['open']-1)*100-COST_PCTPOINTS
                lows=[smap[x]['low'] for x in hd];highs=[smap[x]['high'] for x in hd]
                closes=[smap[x]['close'] for x in hd]
                peak=er['open'];mdd=0
                for px in closes:
                    peak=max(peak,px);mdd=min(mdd,(px/peak-1)*100)
                entry={**e,'entry':hd[0],'exit':hd[-1],'entry_open':er['open'],'exit_close':xr['close'],
                      'hold_days':days,'net_pct':round(net,4),
                      'mae_pct':round((min(lows)/er['open']-1)*100,4),
                      'mfe_pct':round((max(highs)/er['open']-1)*100,4),
                      'close_path_mdd_pct':round(mdd,4),
                      'proxy_only':meta['asset_type']=='index'}
                selected.append(entry);trades.append(entry);last_exit_index=xi
            n=len(selected);wins=sum(1 for x in selected if x['net_pct']>0)
            stats[f'{rule}_{days}']={'n':n,'wins':wins,'win_rate':round(100*wins/n,2) if n else None,
                                   'avg_net_pct':round(statistics.mean(x['net_pct'] for x in selected),3) if n else None,
                                   'median_net_pct':round(statistics.median(x['net_pct'] for x in selected),3) if n else None,
                                   'worst_net_pct':round(min(x['net_pct'] for x in selected),3) if n else None,
                                   'worst_mae_pct':round(min(x['mae_pct'] for x in selected),3) if n else None}
    active_aftershock=None
    # Aftershock is forward observation only; no retrospective exit/winrate claim.
    r1_events=[e for e in events if e['rule']=='R1']
    recent_e=r1_events[-1] if r1_events else None
    if recent_e and fresh:
        i=recent_e['index'];j=len(sh_dates)-1
        if j in (i+1,i+2) and sh_dates[i] in cmap and sh_dates[i] in kmap:
            v1=cmap[sh_dates[i]]['close'];v2=kmap[sh_dates[i]]['close']
            red_dates=[]
            for q in range(i+1,j+1):
                d=sh_dates[q]
                red_dates.append(d in cmap and d in kmap and cmap[d]['close']<v1 and kmap[d]['close']<v2)
            active_aftershock='RED' if j==i+2 and len(red_dates)==2 and all(red_dates) else ('OBSERVING' if j==i+1 else 'CLEAR')
    return {'code':code,'name':meta['name'],'type':meta['asset_type'],'source':meta.get('source',''),
            'session':aligned_date,'asset_last_date':max(smap) if smap else None,
            'status':status,'latest':last,'aftershock':active_aftershock,
            'gap_count':len(missing),'gap_dates':missing[-20:],'available_sessions':len(smap),
            'eval_start':cutoff,'historical_events':len(events),
            'latest_signal':events[-1] if events else None,
            'latest_r1_signal':r1_events[-1] if r1_events else None,
            'latest_r2_signal':next((e for e in reversed(events) if e['rule']=='R2'),None),
            'stats':stats,'trades':trades,
            'trace':trace[-40:]}


def _is_above_ma5(bars:list[dict],dt:str)->bool:
    # Use date-specific trading days without future leakage.
    ix=next((i for i,z in enumerate(bars) if z['date']==dt),None)
    if ix is None or ix<4:return False
    return bars[ix]['close']>statistics.mean(z['close'] for z in bars[ix-4:ix+1])


def _dump_csv(path:Path,rows:list[dict]):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not rows:
        path.write_text('',encoding='utf-8-sig');return
    names=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=names,extrasaction='ignore');w.writeheader()
        for row in rows:w.writerow({k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v) for k,v in row.items()})


def produce(out_dir:Path,results:list[dict],asof:str,errors:list[dict],sources:dict) -> dict:
    out_dir.mkdir(parents=True,exist_ok=True)
    now=datetime.now(TZ).isoformat(timespec='seconds')
    trimmed=[];trades=[];trace=[]
    for item in results:
        obj={k:v for k,v in item.items() if k not in ('trace','trades')};trimmed.append(obj)
        trades.extend({'code':item['code'],'name':item['name'],**r} for r in item['trades'])
        trace.extend({'code':item['code'],'name':item['name'],**r} for r in item['trace'])
    report={'schema':'signallab-scanner-1.0','generated_at':now,'requested_end':asof,
       'notice':'A股研究/历史重建。非实时逐笔行情、非交易指令。未经独立成交记录验证不称机构级。',
       'rule_basis':'research/altseason-forward-spec.json on main (R1/R2 untouched)',
       'evaluation_sessions':1000,'round_trip_cost_pct_points':COST_PCTPOINTS,
       'price_basis':'unadjusted daily OHLC (corporate actions may affect returns)',
       'benchmark':'上证指数 sh000001','sources':sources,'assets':trimmed,'errors':errors}
    (out_dir/'latest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    _dump_csv(out_dir/'trades.csv',trades)
    _dump_csv(out_dir/'recent_trace.csv',trace)
    # Self-contained iPhone-ready HTML: real JSON injected only after successful scan.
    template=Path(__file__).resolve().parents[2]/'scanner.html'
    if template.exists():
        content=template.read_text(encoding='utf-8')
        blob=json.dumps(report,ensure_ascii=False,allow_nan=False).replace('<','\\u003c')
        content=content.replace('<script id="embedded-report" type="application/json">null</script>',
                                '<script id="embedded-report" type="application/json">'+blob+'</script>')
        (out_dir/'scanner_snapshot.html').write_text(content,encoding='utf-8')
    return report


def _read_csv(path:Path)->list[dict]:
    with path.open(encoding='utf-8-sig',newline='') as f:
        return [check_bar(row) for row in csv.DictReader(f)]


def main(argv=None):
    p=argparse.ArgumentParser(description='SignalLab A股 R1/R2 严格独立多资产扫描器')
    p.add_argument('--end',default=datetime.now(TZ).strftime('%Y%m%d'),help='YYYYMMDD; use 20261019 for frozen historical EOD replay')
    p.add_argument('--start',default='20211001',help='YYYYMMDD')
    p.add_argument('--out',default=str(Path(__file__).resolve().parent),help='output latest.json/trades.csv/recent_trace.csv')
    p.add_argument('--data-dir',help='offline CSV folder, named sh000001.csv / sz399006.csv / sh000688.csv / sz159770.csv etc')
    p.add_argument('--no-akshare',action='store_true')
    args=p.parse_args(argv)
    # Reject a partially formed current-day bar before the SSE/SZSE daily close gate.
    clock=datetime.now(TZ)
    if args.end == clock.strftime('%Y%m%d') and (clock.hour,clock.minute)<(15,10):
        new_end=(clock.date()-timedelta(days=1)).strftime('%Y%m%d')
        print(f'当前尚未完成收盘，自动截断至 {new_end}（不使用盘中未收盘K线）',flush=True)
        args.end=new_end
    if not (len(args.end)==8 and args.end.isdigit() and len(args.start)==8 and args.start.isdigit()):p.error('dates must be YYYYMMDD')
    allcodes={'sh000001':('000001','sh','index'),'sz399006':('399006','sz','index'),'sh000688':('000688','sh','index')}
    for code,m in CONFIG.items():allcodes[m['market']+code]=(code,m['market'],m['asset_type'])
    loaded={};sources={};errors=[]
    for key,(code,market,kind) in allcodes.items():
        try:
            if args.data_dir:
                fn=Path(args.data_dir)/f'{key}.csv'
                rows=_read_csv(fn);src='CSV: '+str(fn)
            else:
                rows,src=fetch_bars(code,market,kind,args.start,args.end,retries=2,allow_akshare=not args.no_akshare)
            rows=[x for x in rows if args.start<=x['date'].replace('-','')<=args.end]
            if len(rows)<80:raise ValueError(f'only {len(rows)} bars (<80)')
            loaded[key]=rows;sources[key]=src
            print(f'OK {key} {len(rows)}  {rows[0]["date"]} → {rows[-1]["date"]}',flush=True)
        except Exception as e:
            errors.append({'code':key,'error':str(e)[:450]});print('ERROR',key,str(e),file=sys.stderr,flush=True)
    results=[]
    for code,meta in CONFIG.items():
        key=meta['market']+code
        if any(k not in loaded for k in ('sh000001','sz399006','sh000688',key)):
            errors.append({'code':code,'error':'benchmark or asset unavailable; no invented signal'});continue
        try:
            results.append(scanner_replay(code,{**meta,'source':sources[key]},loaded['sh000001'],loaded[key],loaded['sz399006'],loaded['sh000688'],args.end[:4]+'-'+args.end[4:6]+'-'+args.end[6:]))
        except Exception as e:
            errors.append({'code':code,'error':str(e)[:450]})
    report=produce(Path(args.out),results,args.end,errors,sources)
    print('\nCODE     R1           DAY  R2           DAY  R1_20(n/win/avg)   DATA')
    for item in results:
        f=item['latest'] or {};q=item['stats']['R1_20'];day=str(f.get('r1_day') if f.get('r1_day') is not None else '-')
        print(f"{item['code']:<8} {f.get('r1','NO_DATA'):<12} {day:<4} {f.get('r2','NO_DATA'):<12} "
              f"{str(f.get('r2_day', '-')):<4} {q['n']}/{q['win_rate']}/{q['avg_net_pct']}  {item['status']}")
    print('Output:',Path(args.out).resolve(), 'errors:',len(errors))
    return 0 if len(results)==len(CONFIG) and not errors else 2


if __name__=='__main__':sys.exit(main())
