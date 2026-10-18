#!/usr/bin/env python3
"""SignalLab A end-of-day data generator.

Public data only; does NOT claim licensed Level-2, constituent breadth,
valuation, or CVD. Outputs browser-readable data.js. API calls require network.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Shanghai')
CONFIG = {
    'cy': {'index': 'sz399006', 'etf': '159915', 'etf_symbol': 'sz159915', 'tushare': '159915.SZ'},
    'kc': {'index': 'sh000688', 'etf': '588000', 'etf_symbol': 'sh588000', 'tushare': '588000.SH'},
}


def valid(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def pct_change(new, old):
    return (new / old - 1) * 100 if new is not None and old not in (None, 0) else None


def record_from_history(df, date_col, price_col, amount_col):
    """Use column names documented by AKShare. Most recent LAST official daily bar."""
    if df is None or df.empty:
        raise ValueError('数据源返回空行情')
    work = df.copy()
    work['_day'] = work[date_col].astype(str).str.slice(0, 10)
    work = work.sort_values('_day').drop_duplicates('_day', keep='last')
    rows = []
    for _, row in work.iterrows():
        price = valid(row[price_col])
        amount = valid(row[amount_col]) if amount_col is not None else None
        if price is not None and price > 0:
            rows.append({'date': row['_day'], 'close': price, 'amount': amount})
    if len(rows) < 61:
        raise ValueError(f'有效交易日不足61天，得到{len(rows)}天')
    prices = [x['close'] for x in rows]
    amounts = [x['amount'] for x in rows]
    amount20 = [x for x in amounts[-20:] if x is not None]
    if len(amount20) != 20:
        amount_ratio = None
    else:
        # Compare today with the 20 PRECEDING sessions, not an average including today.
        prev_amounts = [x for x in amounts[-21:-1] if x is not None]
        amount_ratio = amounts[-1] / (sum(prev_amounts)/len(prev_amounts)) if len(prev_amounts)==20 and sum(prev_amounts)>0 else None
    return {
        'date': rows[-1]['date'],
        'close': round(prices[-1], 4),
        'prev_close': round(prices[-2], 4),
        'daily_change_pct': round(pct_change(prices[-1], prices[-2]), 3),
        'five_day_change_pct': round(pct_change(prices[-1], prices[-6]), 3),
        'ma20': round(sum(prices[-20:])/20, 4),
        'ma60': round(sum(prices[-60:])/60, 4),
        'amount': round(amounts[-1], 2) if amounts[-1] is not None else None,
        'amount_ratio_20d': round(amount_ratio, 3) if amount_ratio is not None else None,
        'twenty_day_low': round(min(prices[-20:]), 4),
        'twenty_day_high': round(max(prices[-20:]), 4),
        'history': [{'date': r['date'], 'close': round(r['close'], 4)} for r in rows[-65:]],
    }




def load_index_with_fallback(ak, symbol, start, end):
    """Try independent public daily sources; never mistake volume for CNY turnover."""
    strategies = [
        ('东方财富', lambda: (ak.stock_zh_index_daily_em(symbol=symbol, start_date=start, end_date=end), 'date', 'close', 'amount')),
        ('腾讯', lambda: (ak.stock_zh_index_daily_tx(symbol=symbol, start_date=start, end_date=end), 'date', 'close', None)),
        ('新浪', lambda: (ak.stock_zh_index_daily(symbol=symbol), 'date', 'close', None)),
    ]
    errors = []
    for source, get_data in strategies:
        try:
            df, date_col, close_col, amount_col = get_data()
            # Provider dates can be stale. Record date in payload and reject grossly stale data.
            df = df.copy()
            df['_trade_day'] = df[date_col].astype(str).str.slice(0, 10)
            df = df[(df['_trade_day'] >= start[:4]+'-'+start[4:6]+'-'+start[6:]) &
                    (df['_trade_day'] <= end[:4]+'-'+end[4:6]+'-'+end[6:])]
            index = record_from_history(df, date_col, close_col, amount_col)
            last_day = datetime.strptime(index['date'], '%Y-%m-%d').date()
            if (datetime.now(TZ).date() - last_day).days > 16:
                raise ValueError(f'行情过期: {index["date"]}')
            index['source'] = source
            index['amount_unit'] = 'CNY' if source == '东方财富' else None
            index['amount_status'] = 'ok' if source == '东方财富' else 'unavailable_成交量不是成交额'
            print(f'指数 {symbol} 使用{source}，数据日 {index["date"]}，成交额 {index["amount"]}', flush=True)
            return index
        except Exception as exc:
            desc = f'{source}: {str(exc)[:160]}'
            errors.append(desc)
            print(f'指数 {symbol} 备用源尝试失败: {desc}', file=sys.stderr, flush=True)
    raise RuntimeError('全部行情来源失败: ' + ' | '.join(errors))

def load_etf_with_fallback(ak, cfg, start, end):
    """Eastmoney ETF turnover when available; Sina historical close only otherwise."""
    errors = []
    for source, method in [
        ('东方财富', lambda: record_from_history(
            ak.fund_etf_hist_em(symbol=cfg['etf'],period='daily',start_date=start,end_date=end,adjust=''),
            '日期','收盘','成交额')),
        ('新浪', lambda: record_from_history(
            ak.fund_etf_hist_sina(symbol=cfg['etf_symbol']),
            'date','close',None)),
    ]:
        try:
            etf = method()
            if (datetime.now(TZ).date()-datetime.strptime(etf['date'],'%Y-%m-%d').date()).days > 16:
                raise ValueError(f'ETF行情过期: {etf["date"]}')
            etf['source'] = source
            etf['amount_unit'] = 'CNY' if source == '东方财富' else None
            return etf
        except Exception as exc:
            errors.append(f'{source}:{str(exc)[:130]}')
    return {'status':'unavailable','reason':' | '.join(errors)[:300]}

def optional_etf_shares(ts_code):
    """Tushare Pro optional; official interface fund_share needs access points."""
    token = os.getenv('TUSHARE_TOKEN', '').strip()
    if not token:
        return {'status': 'not_connected', 'reason': '未配置TUSHARE_TOKEN；ETF份额不计分'}
    try:
        import tushare as ts
        pro = ts.pro_api(token)
        df = pro.fund_share(ts_code=ts_code, start_date=(datetime.now(TZ)-timedelta(days=60)).strftime('%Y%m%d'))
        if df is None or df.empty:
            return {'status':'unavailable','reason':'ETF份额未返回记录'}
        df = df.sort_values('trade_date').drop_duplicates('trade_date',keep='last')
        values=[]
        for _, row in df.iterrows():
            x=valid(row['fd_share'])
            if x is not None:
                values.append({'date':str(row['trade_date']), 'ten_thousand_shares':x})
        if not values:
            return {'status':'unavailable','reason':'ETF份额未返回有效数据'}
        last=values[-1]
        change5 = pct_change(last['ten_thousand_shares'],values[-6]['ten_thousand_shares']) if len(values)>=6 else None
        return {'status':'ok','source':'Tushare fund_share','date':f"{last['date'][:4]}-{last['date'][4:6]}-{last['date'][6:8]}",
                'shares_ten_thousand':round(last['ten_thousand_shares'],2),
                'five_session_change_pct':round(change5,3) if change5 is not None else None}
    except Exception as exc:
        return {'status':'unavailable','reason':f'Tushare未返回可用数据：{str(exc)[:110]}'}


def optional_breadth(ak, asof):
    """All-A snapshot, NOT 399006/000688 index-constituent breadth."""
    try:
        df = ak.stock_zh_a_spot_em()
        change = [valid(x) for x in df['涨跌幅']]
        change = [x for x in change if x is not None]
        if len(change)<1000:
            raise ValueError('可用证券数量明显不足')
        return {'status':'ok','scope':'全A股（并非创业板指/科创50成分股）',
                'date':asof, 'up':sum(x>0 for x in change), 'down':sum(x<0 for x in change),
                'flat':sum(x==0 for x in change), 'total':len(change),
                'up_ratio':round(sum(x>0 for x in change)/len(change),4)}
    except Exception as exc:
        return {'status':'unavailable','reason':f'未能获取全A广度：{str(exc)[:110]}'}



# ─── Free board-level financing proxy (NOT index-constituent financing). ───
def _load_previous_margin():
    """Preserve already verified trading days across scheduled runs."""
    try:
        content=(ROOT/'data.js').read_text(encoding='utf-8')
        prefix='window.SIGNAL_LIVE = '
        pos=content.find(prefix)
        if pos < 0:
            return {}
        obj=json.loads(content[pos+len(prefix):].strip().rstrip(';'))
        return obj.get('margin',{}) if isinstance(obj.get('margin',{}),dict) else {}
    except (OSError,ValueError,TypeError) as exc:
        print('旧两融记录不可用，将尝试重新获取:',str(exc)[:100],flush=True)
        return {}


def _norm_code(value):
    """SSE sometimes produces integer securities codes; restore the 6 digits."""
    raw=str(value).strip().split('.')[0]
    raw=''.join(ch for ch in raw if ch.isdigit())
    return raw.zfill(6) if 1<=len(raw)<=6 else ''


def _margin_snapshot(df, exchange, date, price):
    """Aggregate board securities, not ETFs/other A-shares. All balances in CNY."""
    if df is None or df.empty:
        raise ValueError('交易所未返回个股融资明细')
    df=df.copy()
    code_col=next((k for k in ['标的证券代码','证券代码','证券代码及简称','证券代码(6位)','证券代码（6位）'] if k in df.columns),None)
    bal_col=next((k for k in ['融资余额','融资余额(元)','融资余额（元）'] if k in df.columns),None)
    if code_col is None or bal_col is None:
        raise ValueError(f'字段改变，未识别代码/余额列: {list(df.columns)[:12]}')
    if '万' in bal_col:
        raise ValueError('当前余额字段以万元计价，拒绝在未确认单位时自动聚合')
    df['_code']=df[code_col].map(_norm_code)
    prefixes=('300','301') if exchange=='SZSE' else ('688','689')
    df=df[df['_code'].str.startswith(prefixes)]
    df=df.drop_duplicates('_code',keep='last')
    bals=[]
    for item in df[bal_col]:
        if isinstance(item,str):
            item=item.replace(',','').replace('，','').replace('元','').strip()
        v=valid(item)
        if v is not None and v>=0:
            bals.append(v)
    total=sum(bals)
    if len(bals)<30 or not (1e9 < total < 2e13):
        raise ValueError(f'股票数或单位异常：{len(bals)}只，余额合计 {total:g}')
    return {'date':date,'price':float(price),'financing_balance':round(total,2),
            'stock_count':len(bals),'exchange':exchange}


def _one_margin_date(ak,key,date,price):
    import signal
    # AKShare's exchange calls can hang on an unreachable host. Linux GitHub
    # runners support a short, per-request wall-clock watchdog.
    def alarm_handler(_signum,_frame):
        raise TimeoutError('该交易所融资接口响应超时')
    if key=='cy':
        method=ak.stock_margin_detail_szse
        exchange='SZSE'
    else:
        method=ak.stock_margin_detail_sse
        exchange='SSE'
    previous=None
    use_alarm=hasattr(signal,'SIGALRM')
    if use_alarm:
        previous=signal.signal(signal.SIGALRM,alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, 16)
    try:
        df=method(date=date.replace('-',''))
        return _margin_snapshot(df,exchange,date,price)
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL,0)
            signal.signal(signal.SIGALRM,previous)


def optional_board_margin(ak,indices):
    """Fetch verified dates incrementally; avoid inventing missing exchange data.

    The source is per-security daily financing from SSE/SZSE. The scope is ALL
    financing-eligible GEM / STAR stocks and does NOT equal index membership.
    """
    previous=_load_previous_margin()
    result={}
    for key in ('cy','kc'):
        item=indices.get(key,{})
        index=item.get('index') if isinstance(item,dict) else None
        history=(index or {}).get('history',[])
        old=previous.get(key,{}) if isinstance(previous.get(key,{}),dict) else {}
        old_rows=old.get('series',[])
        records={r['date']:r for r in old_rows if isinstance(r,dict) and r.get('date') and
                 valid(r.get('price')) is not None and valid(r.get('financing_balance')) is not None}
        source={'cy':'深交所个股融资融券明细 / AKShare','kc':'上交所个股融资融券明细 / AKShare'}[key]
        scope={'cy':'创业板融资标的股票(300/301)，非创业板指成分股','kc':'科创板融资标的股票(688/689)，非科创50成分股'}[key]
        if not history:
            result[key]={'status':'unavailable','scope':scope,'source':source,
                         'series':sorted(records.values(),key=lambda x:x['date'])[-90:],
                         'reason':'指数没有交易日期，无法确认两融数据日期'}
            continue
        recent=history[-40:]
        needs=[d for d in reversed(recent) if d.get('date') not in records]
        # Max 12 dates per board per run; oldest holes will be filled incrementally.
        # Prefer last reported sessions; data may lag close by one exchange day.
        attempts=0
        messages=[]
        for day in needs[:12]:
            attempts+=1
            try:
                row=_one_margin_date(ak,key,day['date'],day['close'])
                records[day['date']]=row
                print(f'两融 {key} {day["date"]} {row["stock_count"]} 只, {row["financing_balance"]:.0f} 元',flush=True)
            except Exception as exc:
                msg=f'{day["date"]} {str(exc)[:100]}'
                messages.append(msg)
                print('两融接口暂不可用：'+key+' '+msg,file=sys.stderr,flush=True)
                # If last two calls fail (e.g. entire exchange down), stop trying
                # older dates to avoid GitHub Actions rate-limits and long hangs.
                if len(messages)>=3 and not records:
                    break
        rows=sorted(records.values(),key=lambda x:x['date'])[-90:]
        latest_day=rows[-1]['date'] if rows else None
        last_index_day=index.get('date')
        trading_dates=[x['date'] for x in recent]
        lag=sum(1 for d in trading_dates if latest_day and latest_day<d<=last_index_day) if latest_day else None
        result[key]={'status':('ok' if rows and lag is not None and lag<=3 else 'stale' if rows else 'unavailable'),
                     'source':source,'scope':scope,'balance_unit':'CNY','series':rows,
                     'date':latest_day,'lag_trading_days':lag,
                     'coverage_trading_days':len(rows),'attempted':attempts,
                     'reason':(messages[0] if messages else None),
                     'warning':'融资标的板块代理指标；不等于指数成分股或实际账户强平数据'}
        print(f'融资数据 {key} 状态 {result[key]["status"]}，已有 {len(rows)}日，最后 {latest_day}',flush=True)
    return result


# ---- Historical index bars for reproducible MA/EMA backtests --------------
# A separate top-level payload prevents interfering with live quotes, ETF,
# financing, and the existing 65-session history expected by Flow.

def _previous_backtest_series():
    try:
        raw = (ROOT / 'data.js').read_text(encoding='utf-8')
        marker = 'window.SIGNAL_LIVE = '
        at = raw.find(marker)
        if at < 0:
            return {}
        obj = json.loads(raw[at + len(marker):].strip().rstrip(';'))
        return obj.get('backtests', {}) if isinstance(obj.get('backtests'), dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _normalize_backtest_bars(df, date_col='date', close_col='close'):
    if df is None or df.empty:
        raise ValueError('历史序列为空')
    copy = df.copy()
    if date_col not in copy or close_col not in copy:
        raise ValueError('历史数据缺少日期或收盘字段')
    # Dates in AKShare are datetime.date or YYYY-MM-DD strings.
    copy['_day'] = copy[date_col].astype(str).str.slice(0, 10)
    copy = copy.sort_values('_day').drop_duplicates('_day', keep='last')
    rows = []
    for _, rec in copy.iterrows():
        day = rec['_day']
        val = valid(rec[close_col])
        try:
            datetime.strptime(day, '%Y-%m-%d')
        except ValueError:
            continue
        if val is not None and val > 0:
            rows.append([day, round(val, 4)])
    if len(rows) < 450:
        raise ValueError(f'历史日线样本过少（{len(rows)}条），拒绝提供回测胜率')
    return rows


def _merge_backtest_bars(long_bars, recent_bars):
    """Join identical index-price series; refuse inconsistent source series."""
    historic = {x[0]: x[1] for x in long_bars}
    recent = {x['date']: float(x['close']) for x in recent_bars
              if x.get('date') and valid(x.get('close')) is not None}
    common = sorted(set(historic) & set(recent))
    if len(common) < 10:
        raise ValueError(f'历史数据与现用日线重叠不足10天（仅{len(common)}天），拒绝拼接')
    mismatches = [abs(historic[d] / recent[d] - 1) for d in common]
    mismatches.sort()
    if mismatches[len(mismatches) // 2] > .003 or mismatches[-1] > .015:
        raise ValueError('不同数据源指数点位差异过大，拒绝拼接')
    historic.update(recent)
    # Only trust backtest bars through the latest verified current quote.
    latest = max(recent)
    out = [[d, historic[d]] for d in sorted(historic) if d <= latest]
    return out[-4500:]


def _build_backtest_history(ak, key, config, quote, previous):
    current = quote.get('index', {}) if isinstance(quote, dict) else {}
    recent = current.get('history', [])
    latest = current.get('date')
    if not latest or len(recent) < 12:
        return {'status': 'unavailable', 'reason': '缺少已核实的当前指数日线', 'bars': []}
    old = previous.get(key, {}) if isinstance(previous.get(key), dict) else {}
    old_bars = old.get('bars', [])
    prior_source = old.get('source', '旧版历史缓存')
    candidates = []
    if len(old_bars) >= 550:
        candidates.append((prior_source, old_bars))
    else:
        print(f'{key} 缺少足量历史回测数据，首次向公开来源回补', flush=True)
        # Sina offers a complete history in one request; Tencent is segmented
        # by year; Eastmoney is used only as the last public fallback.
        from datetime import datetime as _dt
        from signal import SIGALRM, ITIMER_REAL, signal, setitimer
        def timeout_handler(_sig, _frame):
            raise TimeoutError('历史指数接口超过50秒无响应')
        start = '20100101' if key == 'cy' else '20200101'
        end = datetime.now(TZ).strftime('%Y%m%d')
        providers = [
            ('新浪', lambda: ak.stock_zh_index_daily(symbol=config['index'])),
            ('腾讯', lambda: ak.stock_zh_index_daily_tx(symbol=config['index'], start_date=start, end_date=end)),
            ('东方财富', lambda: ak.stock_zh_index_daily_em(symbol=config['index'], start_date=start, end_date=end)),
        ]
        for provider, fetch in providers:
            try:
                old_handler = signal(SIGALRM, timeout_handler)
                setitimer(ITIMER_REAL, 50)
                try:
                    raw = fetch()
                finally:
                    setitimer(ITIMER_REAL, 0)
                    signal(SIGALRM, old_handler)
                loaded = _normalize_backtest_bars(raw)
                if loaded[-1][0] < recent[0]['date']:
                    raise ValueError('历史数据与当前日线之间存在过大空档')
                combined = _merge_backtest_bars(loaded, recent)
                if combined[-1][0] != latest:
                    raise ValueError('历史末端与最新指数数据不匹配')
                return {'status': 'ok' if len(combined) >= 550 else 'insufficient',
                        'code': config['index'], 'source': provider, 'as_of': latest,
                        'first_date': combined[0][0], 'count': len(combined),
                        'bars': combined,
                        'note': '指数历史日收盘行情，非ETF或Level-2成交。'}
            except Exception as exc:
                print(f'{key} 历史源 {provider} 获取或校验失败：{str(exc)[:130]}', file=sys.stderr, flush=True)
    if not candidates and len(old_bars) >= 450:
        candidates.append((prior_source, old_bars))
    for source, bars in candidates:
        try:
            combined = _merge_backtest_bars(bars, recent)
            if combined[-1][0] != latest:
                raise ValueError('历史末端与最新指数数据不匹配')
            return {'status': 'ok' if len(combined) >= 550 else 'insufficient',
                    'code': config['index'], 'source': source,
                    'as_of': latest, 'first_date': combined[0][0],
                    'count': len(combined), 'bars': combined,
                    'note': '交易所指数价格（日收盘）；非ETF、非逐笔成交数据。'}
        except Exception as exc:
            print(f'{key} 历史序列未采用：{str(exc)[:140]}', file=sys.stderr, flush=True)
    if len(old_bars) >= 450:
        return {'status': 'stale', 'source': prior_source, 'as_of':old.get('as_of'),
                'count':len(old_bars),'bars':old_bars,
                'reason': '新旧序列校验未通过，保留旧历史数据（请核对日期）'}
    return {'status': 'unavailable', 'bars': [],
            'reason': '公开指数历史数据源不可用或未通过一致性校验；不会生成虚构胜率'}


def build(ak):
    now = datetime.now(TZ)
    start = (now-timedelta(days=390)).strftime('%Y%m%d')
    end = now.strftime('%Y%m%d')
    result = {'schema_version': 1, 'generated_at':now.isoformat(timespec='seconds'),
              'source': 'AKShare/东方财富、腾讯、新浪公开日线（非授权Level-2逐笔）',
              'indices': {}, 'breadth': {'status':'not_collected'},
              'notes': ['公开行情接口可能延迟、中断或调整；逐项保留数据日期',
                        '自动评分仅为透明模型计算，不代表回测胜率或收益预测',
                        '没有真实Level-2逐笔主动买卖数据，无法自动计算真实CVD']}
    success_dates=[]
    for key, cfg in CONFIG.items():
        print('获取指数', cfg['index'], flush=True)
        try:
            index = load_index_with_fallback(ak,cfg['index'],start,end)
            success_dates.append(index['date'])
        except Exception as exc:
            print(f'{key}指数下载失败: {exc}', file=sys.stderr)
            result['indices'][key]={'status':'unavailable','reason':str(exc)[:140]}
            continue
        try:
            etf=load_etf_with_fallback(ak,cfg,start,end)
        except Exception as exc:
            etf={'status':'unavailable','reason':str(exc)[:140]}
        share=optional_etf_shares(cfg['tushare'])
        result['indices'][key]={'status':'ok','index':index,'etf':etf,'etf_shares':share}
    if len(success_dates) != len(CONFIG):
        raise RuntimeError('至少一个指数的所有行情来源均失败，拒绝覆盖旧快照')
    # Run full-market query only once. Its scope must be explicit.
    result['breadth']=optional_breadth(ak,max(success_dates))
    result['margin']=optional_board_margin(ak,result['indices'])
    previous_backtests = _previous_backtest_series()
    result['backtests'] = {}
    for key, cfg in CONFIG.items():
        result['backtests'][key] = _build_backtest_history(
            ak, key, cfg, result['indices'].get(key, {}), previous_backtests)
        result_item = result['backtests'][key]
        print(f'回测日线 {key}: {result_item.get("status")}，{result_item.get("count",0)}天，'
              f'截至 {result_item.get("as_of","N/A")}',flush=True)
    return result


def main():
    import akshare as ak
    output=build(ak)
    outfile=ROOT/'data.js'
    content='// Auto-generated by SignalLab A update_data.py; do not hand-edit.\n'
    content+='window.SIGNAL_LIVE = '+json.dumps(output,ensure_ascii=False,allow_nan=False,separators=(',',':'))+';\n'
    tmp=outfile.with_suffix('.tmp')
    tmp.write_text(content,encoding='utf-8')
    tmp.replace(outfile)
    print(f'写入 {outfile}，数据日期: '+ ', '.join(f"{k}={v.get('index',{}).get('date','missing')}" for k,v in output['indices'].items()))

if __name__=='__main__':
    main()
