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
    'cy': {'index': 'sz399006', 'etf': '159915', 'tushare': '159915.SZ'},
    'kc': {'index': 'sh000688', 'etf': '588000', 'tushare': '588000.SH'},
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
        amount = valid(row[amount_col])
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
              'source': 'AKShare/东方财富公开行情（非交易所授权逐笔）',
              'indices': {}, 'breadth': {'status':'not_collected'},
              'notes': ['公开行情接口可能延迟、中断或调整；逐项保留数据日期',
                        '自动评分仅为透明模型计算，不代表回测胜率或收益预测',
                        '没有真实Level-2逐笔主动买卖数据，无法自动计算真实CVD']}
    success_dates=[]
    for key, cfg in CONFIG.items():
        print('获取指数', cfg['index'], flush=True)
        try:
            index_df = ak.stock_zh_index_daily_em(symbol=cfg['index'],start_date=start,end_date=end)
            index = record_from_history(index_df,'date','close','amount')
            success_dates.append(index['date'])
        except Exception as exc:
            print(f'{key}指数下载失败: {exc}', file=sys.stderr)
            result['indices'][key]={'status':'unavailable','reason':str(exc)[:140]}
            continue
        try:
            etf_df = ak.fund_etf_hist_em(symbol=cfg['etf'],period='daily', start_date=start,end_date=end,adjust='')
            etf=record_from_history(etf_df,'日期','收盘','成交额')
        except Exception as exc:
            etf={'status':'unavailable','reason':str(exc)[:140]}
        share=optional_etf_shares(cfg['tushare'])
        result['indices'][key]={'status':'ok','index':index,'etf':etf,'etf_shares':share}
    if not success_dates:
        raise RuntimeError('两个指数均失败，拒绝覆盖旧快照')
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
