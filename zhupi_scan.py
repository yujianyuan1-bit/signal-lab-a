#!/usr/bin/env python3
"""猪皮信号 2.0: A-share breadth/fire scanner, research-only.

Inputs: daily per-stock OHLCV files, optional dated PIT sector membership.
All outputs are end-of-day, strictly past-looking; forward study uses next open.
Not real CVD, net institutional flow, Level-2, or verified industry breadth by default.
"""
from __future__ import annotations
import argparse, csv, io, json, math, statistics, sys, urllib.error, urllib.request
import subprocess, tempfile, time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

VERSION = '2.0.0'
SOURCE_URL = ('https://raw.githubusercontent.com/guidebee/china-stock-data/main/'
              'data/price/{year}/{month}/stock_price_{year}_{month}_{day}.csv')
MIN_TOTAL = 4500
MIN_BOARD = {'CY': 1100, 'KC': 450}
MAX_COUNT_DROP = .06
PRICE_OUTLIER = .23  # larger than usual 20% limits; not always erroneous (e.g. ex-rights)
BOARD_OUTLIER_SHARE = .0075
MIN_HISTORY = 22
EXCHANGE_TZ_LABEL = 'Asia/Shanghai'


def avg(values):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


def rounded(value, places=3):
    return round(value, places) if value is not None and math.isfinite(value) else None


def pct(x, y):
    return (x / y - 1) * 100 if y and y > 0 else None


def quantile(values, q):
    a = sorted(values)
    if not a: return None
    pos = (len(a) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return a[lo] * (hi - pos) + a[hi] * (pos - lo)


def board_of(symbol):
    if symbol.startswith(('sz300', 'sz301', 'sz302')): return 'CY'
    if symbol.startswith(('sh688', 'sh689')): return 'KC'
    return None


def valid_price(p):
    return (all(math.isfinite(p[k]) for k in ('open','close','high','low','volume','amount'))
            and p['open'] > 0 and p['close'] > 0 and p['low'] > 0
            and p['high'] >= max(p['open'], p['close'], p['low'])
            and p['low'] <= min(p['open'], p['close'], p['high'])
            and p['volume'] >= 0 and p['amount'] >= 0)


def parse_daily(path):
    day = path.stem.removeprefix('stock_price_').replace('_','-')
    date.fromisoformat(day)
    out, invalid, duplicates = {}, 0, 0
    with path.open(encoding='utf-8-sig', newline='') as f:
        for arr in csv.reader(f):
            if not arr: continue
            if len(arr) != 8 or arr[1] != day:
                invalid += 1; continue
            try:
                symbol = arr[0].strip().lower()
                if len(symbol) != 8 or symbol[:2] not in ('sh','sz','bj') or not symbol[2:].isdigit():
                    invalid += 1; continue
                point = dict(zip(('open','close','high','low','volume','amount'), map(float, arr[2:])))
            except (ValueError, TypeError):
                invalid += 1; continue
            if not valid_price(point):
                invalid += 1; continue
            if symbol in out:
                duplicates += 1; continue
            out[symbol] = point
    return dict(date=day, stocks=out, count=len(out), invalid=invalid,
                duplicates=duplicates, board_counts=dict(Counter(board_of(s) for s in out if board_of(s))))


def load_pit(path):
    """Read a PIT CSV file or a directory of dated PIT snapshot CSV files."""
    if not path: return [], []
    path=Path(path)
    files=sorted(path.glob('*.csv')) if path.is_dir() else [path]
    records, issues = [], []
    for path in files:
        with path.open(encoding='utf-8-sig',newline='') as f:
            for n,r in enumerate(csv.DictReader(f),2):
                try:
                    sym = r['symbol'].strip().lower()
                    sec = r['group_id'].strip()
                    name = r['group_name'].strip()
                    effective = r['effective_from'].strip()
                    published = r['published_on'].strip()
                    end = r.get('effective_to','').strip() or '9999-12-31'
                    for v in (effective, published): date.fromisoformat(v)
                    if end!='9999-12-31': date.fromisoformat(end)
                    if not sym or not sec or not name or not r['source'].strip() or len(sym)!=8: raise ValueError('字段缺失')
                    if published > effective:
                        issues.append(f'{path.name}第{n}行公告晚于生效：此区间生效日延至公告日')
                    records.append(dict(symbol=sym,id='IND:'+sec,name=name,start=max(effective,published),
                                        end=end,source=r['source'].strip(),published=published))
                except (ValueError,KeyError) as e:
                    issues.append(f'{path.name}第{n}行被拒绝: {e}')
    return records, issues


def make_calendar(path):
    if not path: return None
    with Path(path).open(encoding='utf-8-sig') as f:
        sessions={s.strip().split(',')[0] for s in f if s.strip() and not s.startswith('#') and not s.lower().startswith('date')}
    for s in sessions: date.fromisoformat(s)
    return sessions


def autodetect_calendar(files):
    """Use XSHG exchange sessions when exchange_calendars is installed."""
    try:
        import exchange_calendars
        lo=min(Path(p).stem[-10:].replace('_','-') for p in files)
        hi=max(Path(p).stem[-10:].replace('_','-') for p in files)
        cal=exchange_calendars.get_calendar('XSHG')
        return {d.date().isoformat() for d in cal.sessions_in_range(lo,hi)}, 'exchange_calendars/XSHG'
    except Exception as exc:
        print('无法使用XSHG交易日历，回退工作日检查（节假日可能导致误阻断）:',str(exc)[:150],file=sys.stderr)
        return None, 'weekday_fallback_unverified'


def days_between(a,b,calendar=None):
    d=date.fromisoformat(a)+timedelta(days=1);stop=date.fromisoformat(b)
    missing=[]
    while d<stop:
        s=d.isoformat()
        if (s in calendar) if calendar is not None else d.weekday()<5:
            missing.append(s)
        d+=timedelta(days=1)
    return missing


def daily_quality(raw, prev_good):
    reasons=[]
    if raw['count']<MIN_TOTAL: reasons.append('全市场有效股票数不足')
    if raw['invalid'] or raw['duplicates']: reasons.append('行情字段非法或股票代码重复')
    for g,n in MIN_BOARD.items():
        if raw['board_counts'].get(g,0)<n: reasons.append(f'{g}板覆盖严重不足')
    if len(prev_good)>=3:
        med=statistics.median(x['count'] for x in prev_good[-5:])
        if raw['count'] < med*(1-MAX_COUNT_DROP): reasons.append('相比近期有效样本大幅缺失')
    return reasons


def groups_for_day(raw, pit):
    groups={'CY': {'name':'创业板全板','kind':'风格池','symbols':set()},
            'KC': {'name':'科创板全板','kind':'风格池','symbols':set()}}
    for s in raw['stocks']:
        b=board_of(s)
        if b: groups[b]['symbols'].add(s)
    # PIT universe: only when the membership was already public and effective on the day
    active = defaultdict(dict)
    for r in pit:
        if r['start']<=raw['date']<r['end']:
            active[r['id']][r['symbol']] = r
    for gid,m in active.items():
        if len(m)>=15:
            one=next(iter(m.values()))
            groups[gid]={'name':one['name'],'kind':'行业PIT','symbols':set(m),
                         'membership_source':one['source'],'members_total':len(m)}
    return groups


def universe_stats(today, yesterday, members):
    valid, anomalies=[],[]
    for symbol in sorted(members):
        now=today['stocks'].get(symbol); old=yesterday['stocks'].get(symbol)
        if not now or not old: continue
        r=pct(now['close'],old['close'])
        if r is None: continue
        if abs(r)>PRICE_OUTLIER*100:
            anomalies.append(symbol);continue
        valid.append((symbol,r,now['amount'],now['close']))
    if not valid: return None
    returns=[x[1] for x in valid]
    amount=sum(x[2] for x in valid)
    leaders=[x for x in valid if x[1]>=7]
    up2=sum(x[1]>=2 for x in valid)
    return {'n':len(valid),'members_n':len(members),'matched_raw':len(valid)+len(anomalies),
            'outlier_count':len(anomalies),'outlier_symbols':anomalies[:40],
            'outlier_share':len(anomalies)/max(1,len(valid)+len(anomalies)),
            'breadth2':up2/len(valid), 'breadth_up':sum(x[1]>0 for x in valid)/len(valid),
            'leader7':len(leaders)/len(valid),'leader_count':len(leaders),
            'leader_amount_share':sum(x[2] for x in leaders)/amount if amount else None,
            'amount':amount,'eq_return':avg(returns),'median_return':quantile(returns,.5),
            'p90_return':quantile(returns,.9),'p10_return':quantile(returns,.1),
            'coverage':len(valid)/max(1,len(members))}


def basic_health(stats, group):
    reasons=[]
    min_n=MIN_BOARD.get(group,20)
    if stats is None or stats['n']<min_n: reasons.append('可比较个股数量不足')
    if stats and stats['outlier_share']>BOARD_OUTLIER_SHARE:
        reasons.append('异常大幅价格跳变过密（疑似除权/价格口径问题）')
    if stats and stats['coverage']<.95:
        reasons.append('前后日可匹配股票覆盖低于95%')
    return reasons


def score_state(cur, prev, trail, market):
    """Fixed exploratory rules; never optimized on a return sample."""
    breadth=cur['breadth2'];lead=cur['leader7'];up=cur['breadth_up']
    ratio=cur.get('amount_ratio5');prev_b=prev['breadth2']
    delta=breadth-prev_b
    relative=cur['eq_return']-(market['eq_return'] if market else 0)
    leader_share=cur['leader_amount_share']
    heat=(breadth>=.56 and lead>=.075)
    exhaustion= (prev_b>=.34 and delta<=-.16)
    concentration = leader_share is not None and leader_share>=.58 and breadth<.30
    # Two-day continuation after an earlier broad push is NOT R2 first ignition.
    if heat: stage='R2c';title='疑似高潮·严防追高'
    elif exhaustion: stage='R2c';title='扩散退潮·风险'
    elif concentration: stage='R2c';title='龙头吸血·未扩散'
    elif breadth>=.28 and prev_b>=.22 and lead>=.035 and ratio is not None and ratio>=1.05:
        stage='R2b';title='扩散接力·待验证'
    elif breadth>=.30 and lead>=.05 and ratio is not None and ratio>=1.15:
        stage='R2a';title='首轮点火·待确认'
    elif prev_b<.18 and breadth>=.23 and delta>=.10:
        stage='R1b';title='冰点回暖·未确认'
    elif breadth>=.26 and delta>=.07 and lead>=.025:
        stage='R1c';title='回暖扩散·观察'
    elif breadth<.12 and up<.35:
        stage='R1a';title='冰点观察·不接飞刀'
    else: stage='WATCH';title='尚无明确结构'
    reasons=[]
    base=0
    comp={
        '上涨扩散':min(25,25*breadth/.45),
        '领涨活跃':min(17,17*lead/.08),
        '成交额变化':min(15,15*max(0,(ratio or 0)-.8)/.65) if ratio is not None else 0,
        '持续扩散':min(15,15*max(0, min(breadth,prev_b))/.36),
        '相对强度':min(16,16*max(0,relative+1)/3.2),
        '市场参与':min(12,12*up/.75),
    }
    points=sum(comp.values())
    penalty=(16 if heat else 0)+(12 if exhaustion else 0)+(12 if concentration else 0)
    score=round(max(0,min(100,points-penalty)))
    reasons.append(f'涨幅≥2%股票{breadth:.1%}，前日{prev_b:.1%}（变化{delta:+.1%}）')
    reasons.append(f'领涨≥7%股票{lead:.1%}，领涨成交额占比{leader_share:.1%}' if leader_share is not None else '领涨成交额不可用')
    reasons.append(f'成交额相对之前5日：{ratio:.2f}倍' if ratio is not None else '成交额5日基准不足')
    reasons.append(f'相对全A等权当日涨幅：{relative:+.2f}个百分点')
    if heat: reasons.append('高广度且领涨高度同步，可能已进入短线高潮')
    if exhaustion: reasons.append('昨日扩散后今日明显收缩')
    if concentration: reasons.append('领涨股占据过高成交额，但多数股票未跟随')
    return dict(stage=stage,title=title,score=score,components={k:rounded(v,1) for k,v in comp.items()},
                penalty=penalty,reason=reasons,warning=(heat or exhaustion or concentration))


def forward_proxy(event, days, h):
    """Next day's open to the h-th following session close for signal-day members."""
    t=event['i']
    if t+h>=len(days):return {'status':'unmatured'}
    seq=days[t:t+h+1]
    if any(not d['global_ok'] for d in seq):return {'status':'blocked','why':'未来窗口包含数据质量失败日期'}
    # Enforce same dates, quality of the same group for every day in holding period
    g=event['id']; symbols=event['symbols']
    if any(g not in d['groups'] or not d['groups'][g].get('raw_healthy') for d in seq[1:]):
        return {'status':'blocked','why':'持仓期间行业/板块质量不合格'}
    start=seq[1]['stocks'];end=seq[h]['stocks']
    values=[]; price_unverified=0
    for sym in symbols:
        a=start.get(sym);b=end.get(sym)
        if not a or not b or a['open']<=0:continue
        # Check every intermediate adjusted-event jump; never mistake ex-rights for alpha.
        price_ok=True
        for j in range(1,h+1):
            previous=seq[j-1]['stocks'].get(sym)
            current=seq[j]['stocks'].get(sym)
            if not previous or not current or previous['close']<=0:
                price_ok=False;break
            if abs(pct(current['close'],previous['close']))>PRICE_OUTLIER*100:
                price_ok=False;break
            if j==1 and abs(pct(current['open'],previous['close']))>PRICE_OUTLIER*100:
                price_ok=False;break
        if not price_ok:
            price_unverified+=1
            continue
        values.append(pct(b['close'],a['open']))
    if len(values)/max(1,len(symbols))<.95:
        return {'status':'blocked','why':'开盘/到期价格留存不足95%'}
    return {'status':'ok','gross_pct':rounded(avg(values)), 'net_pct':rounded(avg(values)-.20),
            'matched':len(values),'excluded_unverified_price':price_unverified,'entry_date':seq[1]['date'],'exit_date':seq[h]['date']}


def build_scan(files,pit=None,calendar=None,window=60):
    pit=pit or []
    raw=[parse_daily(Path(p)) for p in files]
    raw.sort(key=lambda d:d['date'])
    days=[]; accepted=[]; last=None
    for one in raw:
        quality=daily_quality(one,accepted)
        if last:
            holes=days_between(last['date'],one['date'],calendar)
            if holes: quality.append('交易日缺档/未校验的工作日间隔：'+','.join(holes[:4]))
        one['global_ok']=not quality
        one['quality_reasons']=quality
        one['groups']={}
        if one['global_ok']:
            accepted.append(one)
        days.append(one)
        last=one
    # Match to immediately preceding calendar-valid consecutive file only
    for i,one in enumerate(days):
        group_defs=groups_for_day(one,pit)
        market=None
        if i and one['global_ok'] and days[i-1]['global_ok']:
            common=set(one['stocks']) & set(days[i-1]['stocks'])
            market=universe_stats(one,days[i-1],common)
        for group_id,g in group_defs.items():
            base={'id':group_id,'name':g['name'],'kind':g['kind'],
                  'members_total':g.get('members_total'), 'membership_source':g.get('membership_source')}
            if not one['global_ok']:
                base.update(health=one['quality_reasons'],status='BLOCKED')
            elif i==0 or not days[i-1]['global_ok']:
                base.update(health=['无连续合格前收盘价'],status='BLOCKED')
            else:
                stats=universe_stats(one,days[i-1],g['symbols'])
                health=basic_health(stats,group_id)
                if g['kind']=='行业PIT' and stats and stats['coverage']<.95:
                    health.append('历史成分股票可用率不足')
                base.update(health=health,stats=stats,status='BLOCKED' if health else 'OK', raw_healthy=not health)
                if not health:
                    trailing=[days[j]['groups'].get(group_id) for j in range(max(0,i-5),i)]
                    if len(trailing)==5 and all(x and x.get('raw_healthy') for x in trailing):
                        base['stats']['amount_ratio5']=(stats['amount']/avg(x['stats']['amount'] for x in trailing)
                        if avg(x['stats']['amount'] for x in trailing) else None)
                    else:base['stats']['amount_ratio5']=None
                    prev=days[i-1]['groups'].get(group_id)
                    if prev and prev.get('raw_healthy') and i>=MIN_HISTORY:
                        # Require 20 immediately consecutive healthy observations, not only two valid days
                        back=[days[j]['groups'].get(group_id) for j in range(i-20,i)]
                        if all(x and x.get('raw_healthy') for x in back) and base['stats']['amount_ratio5'] is not None:
                            base.update(score_state(stats,prev['stats'],back,market))
                        else:
                            base.update(status='WARMUP',health=['20个连续有效历史观测不足'])
                    else:
                        base.update(status='WARMUP',health=['至少需要20个完整历史比较日'])
            one['groups'][group_id]=base
    # Paper-trade research, separate event categories & no overlapping opportunities
    events=[];last_signal={}
    for i,d in enumerate(days):
        for gid,g in d['groups'].items():
            if g['status']!='OK' or g.get('stage') not in ('R1c','R2a','R2b'):continue
            if i-last_signal.get(gid,-100)<20:continue
            last_signal[gid]=i
            event={'i':i,'id':gid,'name':g['name'],'date':d['date'],'stage':g['stage'],
                   'score':g.get('score'), 'symbols':tuple(groups_for_day(d,pit)[gid]['symbols'])}
            for h in (5,10,20):event[f'hold{h}']=forward_proxy(event,days,h)
            events.append(event)
    def public_group(g):
        s=g.copy()
        if s.get('stats'):
            s['stats']={k: (rounded(v,5) if isinstance(v,float) else v) for k,v in s['stats'].items()}
        return s
    series={}
    for d in days:
        for gid,g in d['groups'].items():
            series.setdefault(gid,[]).append({'date':d['date'],'status':g['status'],'stage':g.get('stage'),
                'score':g.get('score'),'breadth2':rounded((g.get('stats') or {}).get('breadth2'),5),
                'leader7':rounded((g.get('stats') or {}).get('leader7'),5),
                'amount_ratio5':rounded((g.get('stats') or {}).get('amount_ratio5'),4),
                'eq_return':rounded((g.get('stats') or {}).get('eq_return'),4)})
    latest=days[-1] if days else None
    public_events=[{k:v for k,v in e.items() if k not in ('symbols','i')} for e in events]
    return {'schema':2,'engine_version':VERSION,'generated_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'asof':latest['date'] if latest else None,
            'source':{'name':'guidebee/china-stock-data or user-supplied equivalent daily OHLCV',
                      'url':'https://github.com/guidebee/china-stock-data', 'calendar':bool(calendar),
                      'pit_loaded':bool(pit),'scope_note':'无PIT时仅创业板全板/科创板全板；不等于创业板指/科创50/申万行业'},
            'quality': [{'date':d['date'],'passed':d['global_ok'],'n':d['count'],
                         'board_counts':d['board_counts'],'reasons':d['quality_reasons']} for d in days],
            'groups':[public_group(g) for g in (latest['groups'].values() if latest else [])],
            'daily_groups':{d['date']:[public_group(g) for g in d['groups'].values()] for d in days},
            'series':series,'events':public_events,
            'rules':{'warmup_days':MIN_HISTORY,'outlier_abs_change_pct':PRICE_OUTLIER*100,
                     'board_outlier_threshold':BOARD_OUTLIER_SHARE,'event_lock_days':20,
                     'next_open_proxy':True,'research_cost_pct_points':.20,
                     'r1r2_note':'2.0全新候选状态机，不替代原网站冻结R1/R2'},
            'caveats':['非真实CVD/主力买卖/清算地图','股票未复权，遇除权必须另行核验','指数与ETF不可直接等同可交易等权篮子',
                       '信号仅收盘后成立；回测须下一开盘买入','尚未经独立样本外证明胜率','缺失/过期数据不可报当前可交易信号']}


def fetch_range(directory,start,end):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    d=date.fromisoformat(start);last=date.fromisoformat(end)
    ok, missing, errors=0,[],[]
    while d<=last:
        if d.weekday()<5:
            dst=directory/f'stock_price_{d:%Y_%m_%d}.csv'
            if dst.exists() and dst.stat().st_size>100_000:
                ok+=1
            else:
                link=SOURCE_URL.format(year=f'{d.year}', month=f'{d.month:02d}',day=f'{d.day:02d}')
                try:
                    req=urllib.request.Request(link,headers={'User-Agent':'ZhupiSignalResearch/2.0'})
                    with urllib.request.urlopen(req,timeout=25) as resp:
                        blob=resp.read(2_500_000)
                    if len(blob)<100_000: raise ValueError('文件过小，拒绝')
                    tmp=dst.with_suffix('.tmp');tmp.write_bytes(blob);tmp.replace(dst)
                    ok+=1
                except urllib.error.HTTPError as exc:
                    if exc.code==404: missing.append(d.isoformat())
                    else:errors.append(f'{d}: HTTP {exc.code}')
                except (TimeoutError,ValueError,OSError,urllib.error.URLError) as exc:
                    errors.append(f'{d}: {str(exc)[:80]}')
        d+=timedelta(days=1)
    return {'available_files':ok,'not_found_weekdays':missing,'errors':errors}


def convert_spot_rows(records, trade_day):
    """Convert AKShare current all-A snapshot, use amount in yuan; does not guess CVD."""
    output=[]
    for row in records:
        try:
            code=str(row['代码']).strip().zfill(6)
            if len(code)!=6 or not code.isdigit(): continue
            if code.startswith(('60','68')):sym='sh'+code
            elif code.startswith(('00','30','301')):sym='sz'+code
            else:continue  # exclude BSE, B-share, ETF or unknown instrument
            price=float(row['最新价']);op=float(row['今开']);hi=float(row['最高']);lo=float(row['最低'])
            volume=float(row['成交量']);amount=float(row['成交额'])
            point={'open':op,'close':price,'high':hi,'low':lo,'volume':volume,'amount':amount}
            if not valid_price(point): continue
            if amount<=0 or volume<=0:continue
            output.append([sym,trade_day,op,price,hi,lo,volume,amount])
        except (KeyError,ValueError,TypeError):continue
    return output


def fetch_spot_records(attempts=3, timeout=120, runner=None, sleeper=None):
    """Bound vendor calls in child processes and retry transient failures."""
    runner=runner or subprocess.run
    sleeper=sleeper or time.sleep
    errors=[]
    code="import akshare as ak,sys; ak.stock_zh_a_spot_em().to_json(sys.argv[1],orient='records',force_ascii=False)"
    for attempt in range(1,attempts+1):
        print(f'东财请求 {attempt}/{attempts}，单次上限{timeout}秒',flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix='zhupi-spot-') as td:
                output=Path(td)/'snapshot.json'
                runner([sys.executable,'-c',code,str(output)],timeout=timeout,check=True,capture_output=True,text=True)
                records=json.loads(output.read_text(encoding='utf-8'))
                if not isinstance(records,list) or not records:raise ValueError('行情接口返回空数据或非列表')
                return records
        except (subprocess.TimeoutExpired,subprocess.CalledProcessError,OSError,ValueError) as exc:
            detail=(exc.stderr or '')[-1500:] if isinstance(exc,subprocess.CalledProcessError) else str(exc)
            message=f'{type(exc).__name__}: {detail}'
            errors.append(message)
            print(f'行情请求失败 {attempt}/{attempts}: {message}',file=sys.stderr,flush=True)
            if attempt<attempts:sleeper((5,15)[min(attempt-1,1)])
    raise RuntimeError('行情源有限重试仍失败；不发布旧行情。'+' | '.join(errors))


def collect_spot(directory):
    """Accumulate CLOSE snapshots over trading days; no fake backfilling of missing history."""
    now=datetime.now(ZoneInfo(EXCHANGE_TZ_LABEL))
    today=now.date().isoformat()
    if now.hour<17:raise RuntimeError('本地收盘后17:00才准存档收盘行情')
    calendar,source=autodetect_calendar([f'stock_price_{today.replace("-","_")}.csv'])
    if calendar is None or today not in calendar:
        raise RuntimeError('无法由XSHG日历确认今天是交易日；拒绝用上次报价假装今日行情')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    dst=directory/f'stock_price_{today.replace("-","_")}.csv'
    if dst.exists():
        print('今日行情文件已存在，跳过抓取:',dst)
        return
    try:
        import akshare as ak
    except ImportError as e:
        raise RuntimeError('请先安装 requirements_scan.txt 中的 AKShare') from e
    quote_source='AKShare stock_zh_a_spot_em / Eastmoney quote snapshot'
    try:
        records=fetch_spot_records()
    except RuntimeError as exc:
        print('东财不可用，切换腾讯/新浪逐只日期验证报价:',exc,file=sys.stderr,flush=True)
        from quote_fallback import fetch_sina_records
        records=fetch_sina_records(today)
        quote_source='Tencent / Sina dated full quotes; per-stock close-time and volume-unit validated'
    rows=convert_spot_rows(records,today)
    if len(rows)<MIN_TOTAL:raise RuntimeError(f'快照覆盖不足，仅{len(rows)}只，拒绝存档')
    shares=Counter(board_of(r[0]) for r in rows if board_of(r[0]))
    if any(shares[g]<minimum for g,minimum in MIN_BOARD.items()):
        raise RuntimeError(f'创业/科创覆盖不足：{dict(shares)}')
    tmp=dst.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:csv.writer(f).writerows(rows)
    tmp.replace(dst)
    dst.with_suffix('.provenance.json').write_text(json.dumps({
        'source':quote_source,
        'quote_sources':dict(Counter(r.get('报价来源','Eastmoney') for r in records)),
        'trade_date_assumption':today,'retrieved_local':now.isoformat(),
        'note':'第三方盘后截面快照，不是交易所官方逐只已复核的历史日线。'},ensure_ascii=False,indent=2),encoding='utf-8')
    print('快照归档完成:',today,len(rows),'股票；不保证API永远稳定')


def normalize_baostock_industry(rows, trade_day):
    """Capture what BaoStock classified TODAY; never backdate to updateDate."""
    end=(date.fromisoformat(trade_day)+timedelta(days=1)).isoformat()
    out=[]
    for r in rows:
        classification=str(r.get('industryClassification','')).strip()
        if '申万' not in classification or '一级' not in classification:continue
        industry=str(r.get('industry','')).strip()
        code=str(r.get('code','')).strip().lower().replace('.','')
        if not industry or len(code)!=8 or code[:2] not in ('sh','sz') or not code[2:].isdigit():continue
        out.append([code,'SW1_'+industry,industry,trade_day,end,trade_day,
                    'BaoStock当日采集的申万一级分类；非官方成分历史'])
    return out


def collect_industry_snapshot(directory):
    """Non-backdated present-day sector membership. Missing days remain missing."""
    now=datetime.now(ZoneInfo(EXCHANGE_TZ_LABEL))
    today=now.date().isoformat()
    if now.hour<17:raise RuntimeError('收盘前不能把行业成员存成当日收盘快照')
    calendar,_=autodetect_calendar([f'stock_price_{today.replace("-","_")}.csv'])
    if calendar is None or today not in calendar:raise RuntimeError('行业抓取日未通过交易所日历验证')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    dest=directory/f'industry_{today}.csv'
    if dest.exists():return dest
    import baostock as bs
    login=bs.login()
    if login.error_code!='0':raise RuntimeError('BaoStock登录失败: '+login.error_msg)
    try:
        rs=bs.query_stock_industry(date=today)
        if rs.error_code!='0':raise RuntimeError('BaoStock行业查询失败: '+rs.error_msg)
        raw=[]
        while rs.next():raw.append(dict(zip(rs.fields,rs.get_row_data())))
    finally:
        bs.logout()
    rows=normalize_baostock_industry(raw,today)
    if len(rows)<1500:raise RuntimeError(f'申万一级分类快照覆盖仅{len(rows)}行，拒绝伪造行业雷达')
    tmp=dest.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f);w.writerow(['symbol','group_id','group_name','effective_from','effective_to','published_on','source']);w.writerows(rows)
    tmp.replace(dest)
    print('PIT当日行业快照:',today,len(rows),'股票',len(set(x[1] for x in rows)),'行业')
    return dest


def main(argv=None):
    ap=argparse.ArgumentParser(description='猪皮信号2.0 收盘后扫描')
    ap.add_argument('--data-dir',default='daily_archive')
    ap.add_argument('--pit',help='PIT行业成分CSV，不能用现今名单回填历史')
    ap.add_argument('--calendar',help='交易日CSV 一行 YYYY-MM-DD；建议使用官方交易日历')
    ap.add_argument('--output',default='scan_data.json')
    ap.add_argument('--download',action='store_true')
    ap.add_argument('--spot',action='store_true',help='在17:00后采集当日A股盘后快照，逐日积累')
    ap.add_argument('--industries',action='store_true',help='采集当日BaoStock申万一级行业快照；需与--spot同用')
    ap.add_argument('--pit-dir',default='research/scan2/pit_daily',help='自动行业快照保存目录')
    ap.add_argument('--start',help='下载起日，YYYY-MM-DD')
    ap.add_argument('--end',help='下载止日，YYYY-MM-DD')
    a=ap.parse_args(argv)
    if a.spot:
        try:collect_spot(a.data_dir)
        except Exception as e:
            print('盘后采集失败，保留原文件，禁止伪造更新:',str(e),file=sys.stderr)
            return 2
    if a.industries:
        if not a.spot:
            print('--industries 必须与 --spot 同时使用，不能倒填历史成员',file=sys.stderr)
            return 2
        try:
            collect_industry_snapshot(a.pit_dir)
            a.pit=a.pit_dir
        except Exception as e:
            print('当日行业分类失败：仅保留风格池扫描；不冒充行业可用：',str(e),file=sys.stderr)
            a.pit=a.pit_dir if Path(a.pit_dir).is_dir() else None
    if a.download:
        end=a.end or date.today().isoformat()
        start=a.start or (date.fromisoformat(end)-timedelta(days=95)).isoformat()
        info=fetch_range(a.data_dir,start,end)
        print('REMOTE:',json.dumps(info,ensure_ascii=False))
    files=list(Path(a.data_dir).glob('stock_price_????_??_??.csv'))
    if not files:
        print('没有日线CSV。先 --download，或放入本地日线文件。未生成虚假信号。',file=sys.stderr)
        return 2
    pit,issues=load_pit(a.pit)
    for issue in issues:print('PIT:',issue,file=sys.stderr)
    trade_calendar,cal_label=(make_calendar(a.calendar),'用户提供交易日历') if a.calendar else autodetect_calendar(files)
    out=build_scan(files,pit=pit,calendar=trade_calendar)
    out['source']['calendar_source']=cal_label
    if a.spot:out['source']['name']='东财 / 腾讯 / 新浪日期验证备用源盘后快照 + 逐日归档（详见provenance；非官方历史库）'
    if a.industries:out['source']['industry_note']='BaoStock当日申万一级分类快照；不回填，不作为官方成分版本'
    Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    groups=out['groups']
    print('完成:',out['asof'],'日期:',len(out['quality']),'分组:',len(groups),'最新有效:',sum(x['status']=='OK' for x in groups))
    for g in groups:print(g['name'],g['status'],g.get('stage','—'),g.get('score','—'),'/'.join(g.get('health',[])))
    return 0


if __name__=='__main__':raise SystemExit(main())
