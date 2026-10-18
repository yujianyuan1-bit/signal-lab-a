"""Auditable A-share research factors. Missing inputs never become neutral scores.

Public endpoint schemas follow AKShare's official index_cni/index_cons,
fund_scale_szse/fund_etf_sse, macro_china and stock_yjbb_em implementations.
This module adds no credentials and supplies no real aggressor-trade CVD.
"""
from __future__ import annotations

import calendar
import concurrent.futures
import io
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

IDS = ('value', 'earnings', 'liquidity', 'breadth', 'volume', 'etf', 'leverage', 'trend', 'cvd')
EM = 'https://datacenter-web.eastmoney.com/api/data/v1/get'


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(str(value).replace(',', ''))
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def score(value):
    return round(max(1, min(10, value)), 1)


def day(value):
    text = str(value or '').strip()[:10]
    if len(text) == 8 and text.isdigit():
        text = f'{text[:4]}-{text[4:6]}-{text[6:]}'
    return date.fromisoformat(text).isoformat()


def request(url, params=None, *, referer=None, binary=False, timeout=12):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': referer or 'https://finance.sina.com.cn/'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('响应超出8MB上限')
    return raw if binary else json.loads(raw)


def em_rows(report, columns='ALL', *, allow_empty=False, **params):
    obj = request(EM, dict(reportName=report, columns=columns, pageSize=500,
                           pageNumber=1, **params))
    result = obj.get('result') or {}
    if allow_empty and not result.get('data'):
        return []
    if not isinstance(result.get('data'), list) or not result['data']:
        raise ValueError(f'{report}无有效记录: {str(obj.get("message", ""))[:80]}')
    if int(result.get('pages', 1)) > 1:
        raise ValueError(f'{report}响应被分页截断，拒绝用部分记录计分')
    return result['data']


def missing(reason, *, status='unavailable', source='', **extra):
    return dict(status=status, score=None, date=None, source=source,
                reason=str(reason)[:260], **extra)


def factor(value, asof, source, rule, *, proxy=False, **extra):
    return dict(status='ok', score=score(value), date=asof, source=source,
                rule=rule, proxy=proxy, reason=None, **extra)


def attempt(fn, source=''):
    try:
        return fn()
    except Exception as exc:
        return missing(exc, source=source)


def constituents(key, asof):
    """Use dated index-provider lists first; verify exact expected sample size."""
    import pandas as pd
    expected = 100 if key == 'cy' else 50
    code = '399006' if key == 'cy' else '000688'
    if key == 'cy':
        raw = request('https://www.cnindex.com.cn/sample-detail/download-history',
                      {'indexcode': code}, binary=True)
        frame = pd.read_excel(io.BytesIO(raw), dtype=str)
        dates = [day(x) for x in frame.iloc[:, 0]]
        codes = [str(x).split('.')[0].zfill(6) for x in frame.iloc[:, 1]]
        source = '国证指数当前样本名单'
    else:
        raw = request('https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/'
                      'autofile/cons/000688cons.xls', binary=True)
        frame = pd.read_excel(io.BytesIO(raw), dtype=str)
        dates = [day(x) for x in frame.iloc[:, 0]]
        codes = [str(x).split('.')[0].zfill(6) for x in frame.iloc[:, 4]]
        source = '中证指数当前样本名单'
    codes = sorted(set(codes))
    prefixes = ('30',) if key == 'cy' else ('688', '689')
    if len(codes) != expected or any(not c.startswith(prefixes) for c in codes):
        raise ValueError(f'成分名单异常，预期{expected}只，取得{len(codes)}只')
    dated = max(dates)
    if dated > asof or (date.fromisoformat(asof) - date.fromisoformat(dated)).days > 120:
        raise ValueError(f'成分名单日期不可用: {dated}')
    return dict(codes=codes, date=dated, source=source)


def stock_closes(code, asof, old, started):
    """Fully adjusted Tencent snapshots; never append across adjustment regimes."""
    if time.monotonic() - started > 180:
        raise TimeoutError('成分股采集达到180秒预算，下次继续')
    symbol = ('sh' if code.startswith('6') else 'sz') + code
    if old.get('date') == asof and old.get('source') == '腾讯前复权':
        rows = old.get('bars', [])
    else:
        obj = request('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
                      {'param': f'{symbol},day,,{asof},90,qfq'})
        item = (obj.get('data') or {}).get(symbol) or {}
        raw = item.get('qfqday') or item.get('day') or []
        rows = [[day(x[0]), number(x[2])] for x in raw if len(x) >= 3]
    rows = sorted({d: p for d, p in rows if d <= asof and p is not None and p > 0}.items())
    if len(rows) < 65 or rows[-1][0] != asof:
        raise ValueError(f'{code}缺少65日及当日有效前复权收盘')
    return dict(date=asof, source='腾讯前复权', bars=[list(x) for x in rows[-90:]])


def breadth_factor(members, snapshots, market):
    codes, asof = members['codes'], market['date']
    calendar_days = [x['date'] for x in market['history'] if x['date'] <= asof]
    compare_day = calendar_days[-6]
    above, prior = [], []
    for code in codes:
        rows = (snapshots.get(code) or {}).get('bars', [])
        aligned = {d: p for d, p in rows}
        # A suspended/missing stock is excluded from BOTH comparison dates.
        if not all(d in aligned for d in calendar_days[-65:]):
            continue
        prices = [aligned[d] for d in calendar_days[-65:]]
        above.append(prices[-1] >= sum(prices[-60:]) / 60)
        prior.append(prices[-6] >= sum(prices[-65:-5]) / 60)
    count, total = len(above), len(codes)
    if count / total < .9:
        return missing(f'成分股同口径65日覆盖不足90%: {count}/{total}',
                       coverage=count / total, valid_count=count, total=total)
    ratio, previous = sum(above) / count, sum(prior) / count
    return factor(1 + 8 * ratio + 5 * (ratio - previous), asof,
                  members['source'] + ' / 腾讯前复权日线',
                  '1+8×站上MA60比例+5×该比例的5日变化，限制1–10',
                  ratio=round(ratio, 4), previous_ratio=round(previous, 4),
                  comparison_date=compare_day, valid_count=count, total=total,
                  coverage=round(count / total, 4), members_date=members['date'],
                  scope='当前成分股等权广度；5日比较固定同一篮子，不用于历史成分回测')


def earnings_factor(members, asof):
    codes = members['codes']
    today = date.fromisoformat(asof)
    periods = []
    for year in (today.year, today.year - 1):
        for month in (12, 9, 6, 3):
            d = date(year, month, calendar.monthrange(year, month)[1])
            if d <= today:
                periods.append(d.isoformat())
    best = None
    for period in sorted(periods, reverse=True)[:3]:
        records = {}
        for pos in range(0, len(codes), 50):
            quoted = ','.join('"' + c + '"' for c in codes[pos:pos + 50])
            rows = em_rows('RPT_LICO_FN_CPD',
                           'SECURITY_CODE,REPORTDATE,PARENT_NETPROFIT,SJLTZ,YSTZ,NOTICE_DATE,EITIME,UPDATE_DATE',
                           allow_empty=True,
                           filter=f'(REPORTDATE=\'{period}\')(SECURITY_CODE in ({quoted}))',
                           sortColumns='SECURITY_CODE', sortTypes=1)
            for r in rows:
                code = str(r.get('SECURITY_CODE', '')).zfill(6)
                if code not in codes or day(r.get('REPORTDATE')) != period:
                    continue
                notice = day(r.get('NOTICE_DATE'))
                known = max(notice, day(r.get('EITIME') or notice))
                if known > asof:
                    continue
                growth, profit = number(r.get('SJLTZ')), number(r.get('PARENT_NETPROFIT'))
                if growth is not None and profit is not None:
                    records[code] = (growth, profit, known)
        count = len(records)
        if best is None or count > best[0]:
            best = (count, period)
        if count / len(codes) < .8:
            continue
        if (today - date.fromisoformat(period)).days > 200:
            raise ValueError('最新可用统一财报期超过200天')
        median = statistics.median(max(-100, min(100, x[0])) for x in records.values())
        profitable = sum(x[1] > 0 for x in records.values()) / count
        return factor(5 + .04 * median + 2 * (profitable - .5),
                      max(x[2] for x in records.values()), '东方财富披露财报 / 当前指数成分',
                      '5+0.04×净利润同比中位数（截断±100%）+2×(盈利公司比例−0.5)，限制1–10',
                      period=period, valid_count=count, total=len(codes),
                      coverage=round(count / len(codes), 4),
                      median_profit_growth_pct=round(median, 3),
                      profitable_ratio=round(profitable, 4),
                      scope='同一报告期已披露实际业绩；不是分析师预测修正，非历史时点回测')
    raise ValueError(f'没有覆盖≥80%的统一财报期，最高覆盖{best}')


def liquidity_factor(asof):
    # Monthly observation dates are periods, not publication dates. Only use
    # a completed prior month returned by the source as of this actual fetch.
    lower = f'{int(asof[:4]) - 2}-01-01'
    money = em_rows('RPT_ECONOMY_CURRENCY_SUPPLY', filter=f'(REPORT_DATE>=\'{lower}\')',
                    sortColumns='REPORT_DATE', sortTypes=-1)
    lpr = em_rows('RPTA_WEB_RATE', filter=f'(TRADE_DATE>=\'{lower}\')',
                  sortColumns='TRADE_DATE', sortTypes=-1)
    current_month = asof[:7]
    money = sorted([r for r in money if day(r['REPORT_DATE'])[:7] < current_month],
                   key=lambda r: day(r['REPORT_DATE']))
    lpr = sorted([r for r in lpr if day(r['TRADE_DATE']) <= asof], key=lambda r: day(r['TRADE_DATE']))
    if len(money) < 4 or len(lpr) < 4:
        raise ValueError('M2或LPR不足4期')
    m, previous, r, old = money[-1], money[-4], lpr[-1], lpr[-4]
    period = day(m['REPORT_DATE'])[:7]
    month_end = date(int(period[:4]), int(period[5:7]), calendar.monthrange(int(period[:4]), int(period[5:7]))[1])
    if (date.fromisoformat(asof) - month_end).days > 75 or (date.fromisoformat(asof) - date.fromisoformat(day(r['TRADE_DATE']))).days > 60:
        raise ValueError('M2或LPR已过期')
    m2, m2_old, rate, rate_old = [number(v) for v in
        (m['BASIC_CURRENCY_SAME'], previous['BASIC_CURRENCY_SAME'], r['LPR1Y'], old['LPR1Y'])]
    if None in (m2, m2_old, rate, rate_old):
        raise ValueError('M2/LPR字段缺失')
    return factor(5 + .7 * (m2 - m2_old) + 4 * (rate_old - rate), asof,
                  '东方财富宏观数据 / 人民银行M2与LPR',
                  '5+0.7×M2同比较3期前变化(百分点)+4×1年LPR较3期前下降(百分点)，限制1–10',
                  period=period, lpr_date=day(r['TRADE_DATE']), m2_yoy=m2,
                  m2_change_pp=round(m2 - m2_old, 3), lpr_1y=rate,
                  scope='M2/LPR流动性代理；未覆盖社融、信用脉冲；仅当前采集可见值，不用于历史回测',
                  proxy=True)


def valuation_factor(key, market):
    import pandas as pd
    asof = market['date']
    if key == 'cy':
        obj = request('https://www.cnindex.com.cn/index/indexList',
                      {'channelCode': -1, 'rows': 2000, 'pageNum': 1})
        rows = [r for r in obj['data']['rows'] if r.get('indexcode') == '399006']
        if len(rows) != 1:
            raise ValueError('国证指数未返回唯一399006估值')
        r = rows[0]
        if abs(float(r['closeingPoint']) / market['close'] - 1) > .0001:
            raise ValueError('国证估值快照收盘与已核实行情不一致')
        pe, source = number(r['peDynamic']), '国证指数399006滚动PE快照'
        source_day = asof
        date_quality = '来源未单列估值日期；以同一指数收盘一致校验，采集时点快照'
    else:
        raw = request('https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/'
                      'autofile/indicator/000688indicator.xls', binary=True)
        frame = pd.read_excel(io.BytesIO(raw))
        rows = [(day(r.iloc[0]), number(r.iloc[6])) for _, r in frame.iterrows()]
        rows = sorted((d, p) for d, p in rows if d <= asof and p is not None)
        if not rows or rows[-1][0] != asof:
            raise ValueError('中证估值没有最新交易日记录')
        source_day, pe = rows[-1]
        source, date_quality = '中证指数000688市盈率1（总股本）', '来源直接披露估值日期'
    if pe is None or pe <= 0:
        raise ValueError('PE非正数，绝对PE规则不适用')
    return factor(5 + 2 * math.log2(30 / pe), source_day, source,
                  '5+2×log2(30/PE)，限制1–10；绝对PE研究标尺，非历史分位',
                  pe=round(pe, 4), metric='absolute_pe', date_quality=date_quality,
                  scope='指数来源各自定义的PE；缺少足够同口径历史，不计算历史百分位，不与原等权PS拼接')


def shares_factor(key, market, previous):
    import pandas as pd
    code = '159915' if key == 'cy' else '588000'
    calendar_days = [r['date'] for r in market['history']]
    dates = calendar_days[-8:]
    records = {r['date']: r for r in previous.get('series', [])
               if r.get('date') and r.get('date') <= market['date'] and number(r.get('shares')) is not None}
    if key == 'cy':
        raw = request('https://www.szse.cn/api/report/ShowReport',
                      dict(SHOWTYPE='xlsx', CATALOGID='scsj_fund_jjgm', TABKEY='tab1',
                           txtStart=dates[0], txtEnd=dates[-1], jjlb='ETF'),
                      binary=True, referer='https://www.szse.cn/market/fund/volume/etf/index.html')
        frame = pd.read_excel(io.BytesIO(raw), dtype={'基金代码': str})
        for _, r in frame.iterrows():
            if str(r['基金代码']).zfill(6) != code:
                continue
            d, value = day(r['日期']), number(r.get('基金规模(份)', r.get('基金份额')))
            if d in dates and value is not None and value > 0:
                records[d] = dict(date=d, shares=value)
        source = '深交所ETF每日基金规模（份）'
    else:
        source = '上交所ETF每日规模（万份转份）'
        for d in dates:
            if d in records and d not in dates[-2:]:
                continue
            try:
                obj = request('https://query.sse.com.cn/commonQuery.do',
                              {'isPagination': 'true', 'pageHelp.pageSize': 10000,
                               'pageHelp.pageNo': 1, 'sqlId': 'COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L',
                               'STAT_DATE': d}, referer='https://www.sse.com.cn/')
                rows = [r for r in obj.get('result', []) if r.get('SEC_CODE') == code and day(r.get('STAT_DATE')) == d]
                if len(rows) != 1 or number(rows[0].get('TOT_VOL')) is None:
                    continue
                value = number(rows[0]['TOT_VOL']) * 10000
                if value > 0:
                    records[d] = dict(date=d, shares=round(value, 2))
            except (OSError, ValueError):
                continue  # retain each independently verified date
    series = [records[d] for d in sorted(records)][-65:]
    available = [d for d in dates if d in records]
    last = available[-1] if available else None
    pos = calendar_days.index(last) if last else -1
    window = calendar_days[max(0, pos - 5):pos + 1] if last else []
    lag = len(calendar_days) - 1 - pos
    if len(window) != 6 or lag > 2 or any(d not in records for d in window):
        return missing('缺少首尾相隔5个指数交易日的连续份额', source=source, series=series)
    change = (records[window[-1]]['shares'] / records[window[0]]['shares'] - 1) * 100
    return factor(5 + .5 * change, last, source,
                  '5+0.5×单只观察ETF的5交易日份额变化百分比，限制1–10',
                  etf=code, series=series, five_session_change_pct=round(change, 4),
                  shares=records[last]['shares'], proxy=True, lag_trading_days=lag,
                  scope=f'仅{code}单只ETF份额代理，不是该指数全部ETF申赎，也不是净流入金额')


def volume_factor(doc):
    market = doc['index']
    candidate, scope = market, '指数成交额'
    if number(candidate.get('amount_ratio_20d')) is None:
        candidate, scope = doc.get('etf', {}), '单只观察ETF成交额代理'
    ratio, change = number(candidate.get('amount_ratio_20d')), number(candidate.get('daily_change_pct'))
    if ratio is None or ratio <= 0 or change is None or candidate.get('date') != market['date']:
        return missing('指数及ETF均缺少同日真实成交额量比；停用旧主观分')
    return factor(5 + 2 * (ratio - 1) + (1 if change >= 0 else -1) * min(abs(change), 3) * .4,
                  candidate['date'], candidate.get('source', '') + ' / ' + scope,
                  '5+2×(成交额比此前20日均值−1)+涨跌方向×min(|涨跌幅|,3)×0.4，限制1–10',
                  ratio=ratio, daily_change_pct=change, scope=scope, proxy=candidate is not market)


def leverage_factor(margin, market):
    rows = {r['date']: r for r in margin.get('series', []) if r.get('date')}
    dates = [r['date'] for r in market['history']]
    available = [d for d in dates if d in rows]
    if len(available) < 6:
        return missing('至少需要连续6个交易日融资余额')
    last = available[-1]
    pos = dates.index(last)
    window = dates[pos - 5:pos + 1]
    if len(window) != 6 or any(d not in rows for d in window) or len(dates) - 1 - pos > 3:
        return missing('融资余额过期或最近6日不连续；不使用跨缺口增量')
    first, final = rows[window[0]], rows[last]
    balances = [number(x.get('financing_balance')) for x in (first, final)]
    prices = [number(x.get('price')) for x in (first, final)]
    if None in balances + prices or min(balances + prices) <= 0:
        return missing('融资余额或价格无效')
    change, price_change = (balances[1] / balances[0] - 1) * 100, (prices[1] / prices[0] - 1) * 100
    # Deleveraging alone is not a bullish signal. A simultaneous price fall
    # offsets the reduced leverage score; no fabricated liquidation levels.
    return factor(5 - .3 * change + .3 * max(-5, min(5, price_change)), last,
                  margin.get('source', ''),
                  '5−0.3×融资余额5日变化%+0.3×指数同期涨跌%（截断±5%），限制1–10',
                  balance_change_pct=round(change, 4), price_change_pct=round(price_change, 4),
                  lag_trading_days=len(dates) - 1 - pos, proxy=True, scope=margin.get('scope'),
                  warning='板块融资代理，不是指数成分股杠杆，更不是账户强平')


def trend_factor(market):
    values = [number(market.get(k)) for k in ('close', 'ma20', 'ma60', 'five_day_change_pct')]
    if None in values:
        return missing('价格趋势字段缺失')
    close, ma20, ma60, change = values
    return factor(5.5 + (1 if close >= ma20 else -1) + (1 if ma20 >= ma60 else -.7)
                  + max(-2, min(2, change / 4)), market['date'], market.get('source', '') + '指数日线',
                  '5.5+价格相对MA20(±1)+MA20相对MA60(+1/−0.7)+5日涨幅÷4（截断±2），限制1–10')


def collect_research(result, previous=None):
    """Bounded parallel public reads; factor failure cannot erase index history."""
    previous = previous or {}
    old_cache = previous.get('cache', {}) if isinstance(previous.get('cache'), dict) else {}
    old_indices = previous.get('indices', {})
    out = dict(version=1, generated_at=result['generated_at'], indices={}, cache={})
    members = {}
    for key in ('cy', 'kc'):
        market = result['indices'][key]['index']
        try:
            members[key] = constituents(key, market['date'])
        except Exception as exc:
            members[key] = dict(error=str(exc)[:240])
    all_codes = sorted({c for m in members.values() for c in m.get('codes', [])})
    latest = max(result['indices'][k]['index']['date'] for k in ('cy', 'kc'))
    started = time.monotonic()
    snapshots, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(stock_closes, c, latest, old_cache.get(c, {}), started): c for c in all_codes}
        for future in concurrent.futures.as_completed(futures):
            code = futures[future]
            try:
                snapshots[code] = future.result()
            except Exception as exc:
                errors.append(f'{code}: {str(exc)[:100]}')
                if code in old_cache:
                    snapshots[code] = old_cache[code]
            completed = len(snapshots) + sum(x.split(':', 1)[0] not in snapshots for x in errors)
            if completed % 25 == 0:
                print(f'成分日线进度 {completed}/{len(all_codes)}', flush=True)
    out['cache'] = snapshots
    liquidity = attempt(lambda: liquidity_factor(latest), 'M2/LPR')
    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for key in ('cy', 'kc'):
            doc = result['indices'][key]
            market = doc['index']
            m = members[key]
            facts = dict(trend=trend_factor(market), volume=volume_factor(doc),
                         leverage=leverage_factor(result.get('margin', {}).get(key, {}), market),
                         liquidity=dict(liquidity),
                         cvd=missing('没有授权真实逐笔主动买卖数据；分钟PVD独立展示且不替代CVD计分',
                                     status='not_connected'))
            out['indices'][key] = facts
            for name, fn in [('value', lambda k=key, d=market: valuation_factor(k, d)),
                             ('etf', lambda k=key, d=market: shares_factor(k, d, old_indices.get(k, {}).get('etf', {})))]:
                jobs[pool.submit(attempt, fn)] = (key, name)
            if m.get('error'):
                facts['breadth'] = missing(m['error'])
                facts['earnings'] = missing(m['error'])
            else:
                facts['breadth'] = attempt(lambda: breadth_factor(m, snapshots, market))
                jobs[pool.submit(attempt, lambda m=m, d=market: earnings_factor(m, d['date']))] = (key, 'earnings')
        for future in concurrent.futures.as_completed(jobs):
            key, name = jobs[future]
            out['indices'][key][name] = future.result()
    for key, facts in out['indices'].items():
        for name in IDS:
            current = facts.get(name, missing('采集器未返回此维度'))
            # Keep old observations for audit, but NEVER silently score them
            # after a failed refresh. Distinct asof/date make cache age visible.
            old = old_indices.get(key, {}).get(name, {})
            if current['status'] != 'ok' and old.get('status') == 'ok':
                current['last_good'] = {k: v for k, v in old.items() if k != 'last_good'}
            elif current['status'] != 'ok' and old.get('last_good'):
                current['last_good'] = old['last_good']
            if name == 'etf' and not current.get('series') and old.get('series'):
                current['series'] = old['series']
            current['checked_at'] = result['generated_at']
            facts[name] = current
        print('九维研究', key, ' '.join(f'{name}={facts[name]["status"]}' for name in IDS), flush=True)
    out['collection_errors'] = errors[:20]
    out['health'] = {key: dict(ok=sum(f['status'] == 'ok' for f in facts.values()), total=9,
                              missing=[name for name, f in facts.items() if f['status'] != 'ok'])
                     for key, facts in out['indices'].items()}
    return out
