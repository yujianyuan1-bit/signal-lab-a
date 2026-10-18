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
