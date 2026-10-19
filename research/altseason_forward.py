"""Frozen KC R1/R2 forward paper observations. No historical signal backfill."""
from __future__ import annotations
import argparse,hashlib,json,math,os,statistics,sys,urllib.request,urllib.parse
from datetime import datetime,time
from pathlib import Path
from zoneinfo import ZoneInfo
TZ=ZoneInfo('Asia/Shanghai');HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
RULES=('R1_alt_bleed_reversal','R2_volume_dryup_ignition')
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def positive(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>0

def validate_inputs(rows):
 if len(rows)<65:raise ValueError('need at least65 common sessions for prior60 V5 quantile')
 dates=[r['date'] for r in rows]
 if dates!=sorted(set(dates)):raise ValueError('duplicate or unordered dates')
 for r in rows:
  if not all(positive(r.get(f)) for f in ['open','close','high','low','amount','sh_close']):raise ValueError('invalid or missing positive OHLC/amount/benchmark')
  if not r['low']<=min(r['open'],r['close'])<=max(r['open'],r['close'])<=r['high']:raise ValueError('OHLC bounds inconsistent')
 return rows

def quantile(x,p):
 x=sorted(x);a=(len(x)-1)*p;i=int(a);return x[i]+(x[min(i+1,len(x)-1)]-x[i])*(a-i)
def feature(rows):
 validate_inputs(rows);i=len(rows)-1;c=[r['close'] for r in rows];a=[r['amount'] for r in rows];d=[r['sh_close']/r['close'] for r in rows]
 v5=[None if j<4 else statistics.mean(a[j-4:j+1]) for j in range(len(rows))]
 ma20=statistics.mean(c[-20:]);ma5=statistics.mean(c[-5:]);prev5=statistics.mean(a[-6:-1]);q80=quantile(d[-61:-1],.8);q25=quantile(v5[-61:-1],.25)
 excess=c[-1]/c[-2]-rows[-1]['sh_close']/rows[-2]['sh_close'];ret=c[-1]/c[-2]-1;span=max(c[-10:])/min(c[-10:])-1
 return dict(date=rows[-1]['date'],close=c[-1],ma20=ma20,ma5=ma5,ratio=d[-1],ratio_q80=q80,v5=v5[-1],v5_q25=q25,amount=a[-1],amount_prev5_mean=prev5,amount_ratio=a[-1]/prev5,excess_return=excess,daily_return=ret,close10_range=span,prior10_close_max=max(c[-11:-1]),
  R1_wait=d[-1]>=q80 and c[-1]<ma20,
  R1_trigger=excess>=.015 and c[-1]>ma5 and c[-1]>rows[-2]['high'] and a[-1]>prev5,
  R2_wait=v5[-1]<=q25 and span<=.08,
  R2_trigger=ret>=.02 and c[-1]>ma20 and c[-1]>max(c[-11:-1]) and a[-1]>=1.4*prev5)

def step(before,f,rule,consecutive=True):
 state=dict(before);prefix=rule[:2]
 if not consecutive:state=dict(setup_date=None,wait_sessions=0)
 if state.get('setup_date'):
  age=state['wait_sessions']+1
  if f[prefix+'_trigger']:return dict(setup_date=None,wait_sessions=0),True,dict(setup_date=state['setup_date'],wait_sessions=age)
  if age>=10:return dict(setup_date=None,wait_sessions=0),False,None
  return dict(setup_date=state['setup_date'],wait_sessions=age),False,None
 if f[prefix+'_wait']:state=dict(setup_date=f['date'],wait_sessions=0)
 return state,False,None

def request(url,params):
 req=urllib.request.Request(url+'?'+urllib.parse.urlencode(params),headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.csindex.com.cn/'})
 with urllib.request.urlopen(req,timeout=12) as response:raw=response.read(2_000_001)
 if len(raw)>2_000_000:raise ValueError('response too large')
 return json.loads(raw)

def collect(payload,asof):
 market=payload['indices']['kc']['index'];history=market['history'];dates=[r['date'] for r in history if r['date']<=asof][-85:]
 if len(dates)<65 or dates[-1]!=asof:raise ValueError('current verified KC calendar unavailable')
 target={r['date']:r['close'] for r in history};bench=payload['research']['benchmark']
 if bench.get('status')!='ok' or bench.get('date')!=asof:raise ValueError('fresh benchmark unavailable')
 sh={r['date']:r['close'] for r in bench['history']}
 raw=request('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',{'param':f'sh000688,day,,{asof},100,qfq'})
 bars=raw.get('data',{}).get('sh000688',{}).get('day',[])
 if len({str(r[0]) for r in bars})!=len(bars):raise ValueError('Tencent duplicate date')
 bydate={str(r[0]):r for r in bars}
 data=request('https://www.csindex.com.cn/csindex-home/perf/index-perf',{'indexCode':'000688','startDate':dates[0].replace('-',''),'endDate':asof.replace('-','')}).get('data',[])
 money={}
 for r in data:
  if r.get('indexCode')!='000688':raise ValueError('CSI wrong index code')
  d=str(r['tradeDate'])[:10]
  if len(d)==8:d=f'{d[:4]}-{d[4:6]}-{d[6:]}'
  if d in money:raise ValueError('CSI duplicate date')
  money[d]=r
 out=[]
 for d in dates:
  b=bydate.get(d);m=money.get(d)
  if b is None or m is None or d not in sh:raise ValueError('common input date missing: '+d)
  o,c,h,l=map(float,b[1:5]);amount=float(m['tradingValue']);official_close=float(m['close'])
  if abs(c/float(target[d])-1)>1e-4 or abs(official_close/c-1)>1e-4:raise ValueError('KC close source mismatch: '+d)
  out.append(dict(date=d,open=o,close=c,high=h,low=l,amount=amount,sh_close=float(sh[d])))
 return validate_inputs(out)

def read_journal(path,spec_hash,engine):
 records=[];prior=None
 if not path.exists():return records
 for line in path.read_text().splitlines():
  if not line:raise ValueError('blank journal line')
  row=json.loads(line);claim=row.pop('record_hash')
  if digest(row)!=claim or row.get('previous_hash')!=prior or row.get('spec_hash')!=spec_hash or row.get('implementation_sha256')!=engine:raise ValueError('journal hash/spec/implementation mismatch; use new log version')
  if records and row['date']<=records[-1]['date']:raise ValueError('journal dates out of order')
  row['record_hash']=claim;records.append(row);prior=claim
 return records

def summary(rows,spec):
 calendar=sorted({x['date'] for r in rows for x in r['inputs']});positions={d:i for i,d in enumerate(calendar)};observed={r['date']:r for r in rows}
 result={}
 for rule in RULES:
  events=[r for r in rows if r['events'][rule]];horizons={}
  for h in spec['horizons']:
   trades=[];pending=[];invalid=[];skipped=[];last_exit=-1
   for event in events:
    i=positions[event['date']];start=i+1;end=i+h
    if start<=last_exit:skipped.append(event['date']);continue
    last_exit=end
    available=calendar[start:min(end+1,len(calendar))]
    if any(d not in observed for d in available):invalid.append(event['date']);continue
    if end>=len(calendar):
     entry_row=observed.get(calendar[start]) if start<len(calendar) else None
     pending.append(dict(signal=event['date'],entry=calendar[start] if entry_row else None,entry_price=entry_row['bar']['open'] if entry_row else None,entry_observed_at=entry_row['observed_at'] if entry_row else None,state='holding_paper_proxy' if entry_row else 'awaiting_next_session_open'))
     continue
    path=[observed[d]['bar'] for d in calendar[start:end+1]];price=path[0]['open'];net=(path[-1]['close']/price-1)*100-spec['round_trip_cost_pct_points']
    trades.append(dict(signal=event['date'],entry=calendar[start],exit=calendar[end],net_pct=net,min_price_change_pct=(min(b['low'] for b in path)/price-1)*100))
   wins=sum(t['net_pct']>0 for t in trades);horizons[str(h)]=dict(n=len(trades),wins=wins,win_pct=wins/len(trades)*100 if trades else None,mean_net_pct=statistics.mean(t['net_pct'] for t in trades) if trades else None,trades=trades,pending=pending,unscorable_due_to_gaps=invalid,skipped_while_holding=skipped)
  result[rule]=dict(raw_events=len(events),nonoverlap=horizons)
 return dict(version=spec['version'],observations=len(rows),rules=result,warning='separate paper research portfolios, not actual executions or a combined portfolio; no mature results means null win rate')

def record(payload,raw_hash,folder,spec,now=None,loader=collect):
 if digest(spec.get('rules'))!='cbf124cfbe497bb1d20636a50ea13e2784138847fc380184737650230373d9e0' or spec.get('horizons')!=[10,20,40] or spec.get('round_trip_cost_pct_points')!=.2 or spec.get('wait_timeout_sessions')!=10 or spec.get('R1_amount_operator')!='>':raise ValueError('unsupported frozen specification; new rules require a new engine version')
 now=(now or datetime.now(TZ)).astimezone(TZ);folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
 engine=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();spec_hash=digest(spec);journal=folder/'observations.jsonl';rows=read_journal(journal,spec_hash,engine)
 def status(reason,detail=None):
  result=dict(status=reason,observed_at=now.isoformat(),spec_hash=spec_hash,implementation_sha256=engine,summary=summary(rows,spec))
  if detail is not None:result['reason']=detail
  temp=folder/'status.tmp';temp.write_text(canonical(result)+'\n');temp.replace(folder/'status.json');return result
 today=now.date().isoformat()
 if today<spec['activation_date'] or now.time().replace(tzinfo=None)<time(15,10):return status('not_yet_active_or_market_not_closed')
 stamp=datetime.fromisoformat(payload['generated_at'])
 if stamp.tzinfo is None:return status('payload_timestamp_missing_timezone')
 stamp=stamp.astimezone(TZ)
 if stamp.date()!=now.date() or stamp>now or stamp.time().replace(tzinfo=None)<time(15,10):return status('no_fresh_postclose_payload_no_backfill')
 if payload.get('indices',{}).get('kc',{}).get('index',{}).get('date')!=today:return status('no_fresh_today_quote_no_backfill')
 if rows and rows[-1]['date']>=today:return status('already_recorded_first_observation_preserved')
 try:inputs=loader(payload,today);validate_inputs(inputs)
 except (ValueError,OSError,KeyError,TypeError) as exc:return status('input_unavailable_no_backfill',str(exc)[:240])
 if inputs[-1]['date']!=today:return status('input_date_mismatch')
 f=feature(inputs);previous=rows[-1] if rows else None;consecutive=previous is None or previous['date']==inputs[-2]['date']
 states={};events={};details={}
 for rule in RULES:
  before=previous['states'][rule] if previous else dict(setup_date=None,wait_sessions=0)
  states[rule],events[rule],details[rule]=step(before,f,rule,consecutive)
 row=dict(date=today,observed_at=now.isoformat(),spec_hash=spec_hash,implementation_sha256=engine,snapshot_sha256=raw_hash,previous_hash=previous['record_hash'] if previous else None,gap_since_previous=not consecutive,
  features=f,states=states,events=events,event_details=details,bar={k:inputs[-1][k] for k in ['open','close','high','low']},inputs=inputs,
  sources={'OHLC':'Tencent sh000688 raw day cross-checked with quoted history','amount':'CSI000688 tradingValue native, ratios only','benchmark':'dated SH000001 close from verified research benchmark'})
 row['record_hash']=digest(row)
 with journal.open('a') as file:file.write(canonical(row)+'\n');file.flush();os.fsync(file.fileno())
 rows.append(row);return status('recorded')

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--data',type=Path,default=ROOT/'data.js');parser.add_argument('--spec',type=Path,default=HERE/'altseason-forward-spec.json');parser.add_argument('--output',type=Path,default=HERE/'forward-altseason-kc');args=parser.parse_args()
 try:
  raw=args.data.read_bytes();payload=json.loads(raw.decode().split('window.SIGNAL_LIVE = ',1)[1].strip().rstrip(';'));spec=json.loads(args.spec.read_text());result=record(payload,hashlib.sha256(raw).hexdigest(),args.output,spec)
 except Exception as exc:
  args.output.mkdir(parents=True,exist_ok=True);temp=args.output/'status.tmp';temp.write_text(canonical(dict(status='failed',observed_at=datetime.now(TZ).isoformat(),reason=str(exc)[:240],summary=None))+'\n');temp.replace(args.output/'status.json');raise
 print('KC frozen paper log: '+result['status'])
if __name__=='__main__':main()
