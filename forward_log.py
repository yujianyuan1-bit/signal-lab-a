"""Append-only, first-observed forward record. No historical signal imports."""
from __future__ import annotations
import argparse,hashlib,json,math,os,statistics,sys
from datetime import datetime,time
from pathlib import Path
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
ROOT=Path(__file__).resolve().parent

def canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(obj):return hashlib.sha256(canonical(obj).encode()).hexdigest()
def positive(v):
    try:
        x=float(v)
        return x if math.isfinite(x) and x>0 else None
    except (ValueError,TypeError):return None

def read_journal(path,spec_hash,implementation_hash=None):
    rows=[];previous=None
    if not path.exists():return rows
    for line in path.read_text().splitlines():
        if not line:raise ValueError('blank or damaged journal line')
        r=json.loads(line);claimed=r.pop('record_hash')
        if digest(r)!=claimed or r['previous_hash']!=previous or r['spec_hash']!=spec_hash:raise ValueError('journal hash/spec mismatch; preserve original file for review')
        if implementation_hash is not None and r.get('implementation_sha256')!=implementation_hash:raise ValueError('implementation changed; use a new frozen log version')
        if rows and r['date']<=rows[-1]['date']:raise ValueError('journal date order invalid')
        r['record_hash']=claimed;rows.append(r);previous=claimed
    return rows

def step(before,current,previous,consecutive=True):
    state=dict(before);event=False;reason=None
    if not consecutive or current.get('breadth_score') is None or previous is not None and previous.get('breadth_score') is None:
        state={'setup_date':None,'wait_sessions':0};reason='observation_gap_or_missing_breadth'
    if current.get('breadth_score') is None:return state,False,reason
    if state.get('setup_date') is not None:
        state['wait_sessions']+=1
        if state['wait_sessions']>10:state={'setup_date':None,'wait_sessions':0};reason='expired'
        elif previous is not None and current['close']>current['ma5'] and current['breadth_score']>previous['breadth_score']:
            event=True;state={'setup_date':None,'wait_sessions':0};return state,event,'repair_trigger'
    if state.get('setup_date') is None and current['breadth_score']<2:
        state={'setup_date':current['date'],'wait_sessions':0}
    return state,event,reason

def fetch_bar(asof,close):
    from research_factors import request
    obj=request('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',{'param':f'sz399006,day,,{asof},90,qfq'},timeout=12)
    rows=(obj.get('data',{}).get('sz399006',{}).get('day') or [])
    matches=[r for r in rows if str(r[0])==asof]
    if len(matches)!=1:raise ValueError('no unique raw index OHLC for asof')
    r=matches[0];o,c,h,l=[positive(v) for v in r[1:5]]
    if None in (o,c,h,l) or l>min(o,c) or h<max(o,c) or abs(c/close-1)>1e-4:raise ValueError('OHLC inconsistent with verified quote')
    return dict(date=asof,open=o,close=c,high=h,low=l,source='腾讯原始指数day；不可直接交易的研究价格代理')

def summarize(rows,spec):
    events=[(i,r) for i,r in enumerate(rows) if r['event']];out={}
    for h in spec['horizons']:
        complete=[];pending=bad=0
        for i,r in events:
            later=rows[i+1:i+h+1]
            if any(x['gap_since_previous'] or x.get('ohlc') is None for x in later):bad+=1;continue
            if len(later)<h:pending+=1;continue
            price=later[0]['ohlc']['open'];net=(later[-1]['ohlc']['close']/price-1)*100-spec['round_trip_cost_pct_points']
            complete.append(dict(signal=r['date'],entry=later[0]['date'],exit=later[-1]['date'],net_pct=net,
                min_price_change_pct=(min(x['ohlc']['low'] for x in later)/price-1)*100,signal_i=i,exit_i=i+h))
        def stats(items):
            return dict(n=len(items),wins=sum(t['net_pct']>0 for t in items),win_pct=sum(t['net_pct']>0 for t in items)/len(items)*100 if items else None,
                mean_net_pct=statistics.mean(t['net_pct'] for t in items) if items else None,trades=items)
        out[str(h)]=dict(all_events=stats(complete),pending=pending,unscorable_due_to_gaps=bad)
    return dict(version=spec['version'],observations=len(rows),raw_events=len(events),horizons=out,
        warning='前瞻原始事件观察；没有成熟事件时胜率为null；指数开盘是研究代理；重叠事件非独立样本，策略去重另行离线核验')

def record(payload,raw_hash,folder,spec,now=None,bar_loader=fetch_bar):
    now=(now or datetime.now(TZ)).astimezone(TZ);folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    spec_hash=digest(spec);implementation_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    journal=folder/'observations.jsonl';rows=read_journal(journal,spec_hash,implementation_hash)
    def status(reason):
        doc=dict(observed_at=now.isoformat(),status=reason,spec_hash=spec_hash,implementation_sha256=implementation_hash,summary=summarize(rows,spec))
        tmp=folder/'status.tmp';tmp.write_text(canonical(doc)+'\n');tmp.replace(folder/'status.json');return doc
    if now.date().isoformat()<spec['activation_date'] or now.time().replace(tzinfo=None)<time(15,10):return status('not_yet_active_or_market_not_closed')
    market=payload.get('indices',{}).get('cy',{}).get('index',{});d=market.get('date')
    if d!=now.date().isoformat():return status('no_fresh_today_quote_no_backfill')
    if rows and rows[-1]['date']>=d:return status('already_recorded_first_observation_preserved')
    hist=[r for r in market.get('history',[]) if r.get('date','')<=d];close=positive(market.get('close'))
    if len(hist)<5 or hist[-1]['date']!=d or close is None:return status('invalid_current_price_history')
    if any(positive(r.get('close')) is None for r in hist[-5:]):return status('invalid_ma5_history')
    calendar=[r['date'] for r in hist]
    if len(calendar)!=len(set(calendar)) or calendar!=sorted(calendar):return status('invalid_trading_calendar')
    factors=payload.get('research',{}).get('indices',{}).get('cy',{});b=factors.get('breadth',{})
    score=b.get('score') if b.get('status')=='ok' and b.get('date')==d else None
    if score is not None and (not isinstance(score,(int,float)) or not math.isfinite(score) or not 1<=score<=10):score=None
    previous=rows[-1] if rows else None
    consecutive=previous is None or previous['date'] in calendar and calendar.index(d)-calendar.index(previous['date'])==1
    current=dict(date=d,close=close,ma5=statistics.mean(float(r['close']) for r in hist[-5:]),breadth_score=score)
    before=previous['state_after'] if previous else {'setup_date':None,'wait_sessions':0}
    after,event,reason=step(before,current,previous,consecutive)
    try:bar=bar_loader(d,close);bar_error=None
    except (OSError,ValueError,KeyError,TypeError) as e:bar=None;bar_error=str(e)[:200]
    fields=('status','score','date','source','rule','proxy','reason','coverage','ratio','previous_ratio','members_date')
    row=dict(current,observed_at=now.isoformat(),spec_hash=spec_hash,implementation_sha256=implementation_hash,snapshot_sha256=raw_hash,previous_hash=previous['record_hash'] if previous else None,
        gap_since_previous=not consecutive,state_before=before,state_after=after,event=event,state_reason=reason,ohlc=bar,ohlc_error=bar_error,
        factors={k:{f:v[f] for f in fields if f in v} for k,v in factors.items() if isinstance(v,dict)})
    row['record_hash']=digest(row)
    with journal.open('a',encoding='utf-8') as f:f.write(canonical(row)+'\n');f.flush();os.fsync(f.fileno())
    rows.append(row);return status('recorded' if bar is not None else 'recorded_price_unavailable')

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,default=ROOT/'data.js');p.add_argument('--output',type=Path,default=ROOT/'research/forward');p.add_argument('--spec',type=Path,default=ROOT/'forward_spec.json');a=p.parse_args()
    try:
        raw=a.data.read_bytes();text=raw.decode();payload=json.loads(text.split('window.SIGNAL_LIVE = ',1)[1].strip().rstrip(';'))
        spec=json.loads(a.spec.read_text());result=record(payload,hashlib.sha256(raw).hexdigest(),a.output,spec)
    except Exception as exc:
        # Preserve the journal. Expose failure separately while the daily market
        # update can still be committed by the enclosing workflow.
        try:
            a.output.mkdir(parents=True,exist_ok=True)
            marker=dict(status='failed',observed_at=datetime.now(TZ).isoformat(),reason=str(exc)[:240],summary=None)
            tmp=a.output/'status.tmp';tmp.write_text(canonical(marker)+'\n');tmp.replace(a.output/'status.json')
        except OSError:pass
        print('前瞻日志失败，保留原始日志：'+str(exc)[:240],file=sys.stderr)
        raise SystemExit(1)
    print('前瞻日志：'+result['status'])

if __name__=='__main__':main()
