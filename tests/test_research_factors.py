"""Run with python -m unittest discover -s tests from the repository root."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_factors as r


def market():
    dates = [(dt.date(2026, 6, 1) + dt.timedelta(days=i)).isoformat() for i in range(90)]
    return dict(date=dates[-1], close=100, ma20=101, ma60=103, five_day_change_pct=-2,
                history=[dict(date=d, close=100) for d in dates], source='fixture')


class ResearchTests(unittest.TestCase):
    def test_missing_never_has_a_neutral_score(self):
        self.assertIsNone(r.missing('unavailable')['score'])
        self.assertIsNone(r.attempt(lambda: 1 / 0)['score'])

    def test_volume_falls_back_to_etf_and_rejects_stale(self):
        m = market()
        doc = dict(index=m, etf=dict(date=m['date'], amount_ratio_20d=1.5, daily_change_pct=-1))
        self.assertTrue(r.volume_factor(doc)['proxy'])
        doc['etf']['date'] = m['history'][-2]['date']
        self.assertIsNone(r.volume_factor(doc)['score'])

    def test_native_index_turnover_fallback_uses_money_not_volume(self):
        m = dict(market(), code='399006', daily_change_pct=-1)
        dates = [x['date'] for x in m['history']][-21:]
        obj = {'data':{'indexCode':'399006','item':['timestamp','close','amount','volume'],
                       'data':[[d,100,100+i,999999] for i,d in enumerate(dates)]}}
        with patch.object(r, 'request', return_value=obj):
            got = r.volume_factor(dict(index=m, etf={}))
        self.assertEqual(got['status'], 'ok')
        self.assertFalse(got['proxy'])
        self.assertAlmostEqual(got['ratio'], 120 / 109.5, places=4)
        # A complete but inconsistent index series must not fill the score.
        obj['data']['data'][-1][1] = 200
        with patch.object(r, 'request', return_value=obj):
            self.assertIsNone(r.volume_factor(dict(index=m, etf={}))['score'])

    def test_breadth_requires_same_dates_and_90_percent(self):
        m = market()
        codes = [str(i) for i in range(10)]
        members = dict(codes=codes, source='fixture', date=m['date'])
        cache = {c: dict(bars=[[d['date'], 100 + i] for i, d in enumerate(m['history'])]) for c in codes[:9]}
        self.assertEqual(r.breadth_factor(members, cache, m)['valid_count'], 9)
        del cache[codes[8]]
        self.assertIsNone(r.breadth_factor(members, cache, m)['score'])

    def test_breadth_missing_comparison_date_not_counted(self):
        m = market()
        members = dict(codes=['x'], source='fixture', date=m['date'])
        cache = {'x': dict(bars=[[x['date'], 100] for x in m['history'] if x['date'] != m['history'][-6]['date']])}
        self.assertIsNone(r.breadth_factor(members, cache, m)['score'])

    def test_earnings_excludes_not_yet_announced_and_falls_back_period(self):
        def rows(report, columns, **params):
            period = '2026-09-30' if '2026-09-30' in params['filter'] else '2026-06-30'
            return [dict(SECURITY_CODE='300001', REPORTDATE=period, NOTICE_DATE='2026-10-30' if period.endswith('09-30') else '2026-08-20',
                         EITIME='2026-10-30' if period.endswith('09-30') else '2026-08-20', SJLTZ=10, PARENT_NETPROFIT=100)]
        with patch.object(r, 'em_rows', side_effect=rows):
            got = r.earnings_factor(dict(codes=['300001']), '2026-10-16')
        self.assertEqual(got['period'], '2026-06-30')
        self.assertEqual(got['date'], '2026-08-20')

    def test_earnings_requires_80_percent(self):
        with patch.object(r, 'em_rows', return_value=[]):
            with self.assertRaises(ValueError):
                r.earnings_factor(dict(codes=['300001']), '2026-10-16')

    def test_leverage_rejects_missing_previous_session(self):
        m = market()
        rows = [dict(date=x['date'], financing_balance=1000 + i, price=100) for i, x in enumerate(m['history'])]
        self.assertEqual(r.leverage_factor(dict(series=rows), m)['status'], 'ok')
        del rows[-2]
        self.assertIsNone(r.leverage_factor(dict(series=rows), m)['score'])

    def test_future_stock_rows_are_not_used(self):
        m = market()
        rows = [[x['date'], 100] for x in m['history']]
        rows.append(['2099-01-01', 999])
        got = r.stock_closes('300001', m['date'], dict(date=m['date'], source='腾讯前复权', bars=rows), r.time.monotonic())
        self.assertEqual(got['bars'][-1][0], m['date'])

    def test_sse_share_units_and_disclosure_lag(self):
        m = market()
        dates = [x['date'] for x in m['history']]
        def endpoint(url, params, **kw):
            d = params['STAT_DATE']
            if d == dates[-1]:
                return {'result': []}  # latest day's shares not yet disclosed
            return {'result':[dict(SEC_CODE='588000', STAT_DATE=d, TOT_VOL=100 + dates.index(d))]}
        with patch.object(r, 'request', side_effect=endpoint):
            got = r.shares_factor('kc', m, {})
        self.assertEqual(got['date'], dates[-2])
        self.assertEqual(got['lag_trading_days'], 1)
        self.assertEqual(got['shares'], (100 + 88) * 10000)

    def test_integration_keeps_backtest_and_minute_history_on_research_error(self):
        import update_data as u
        m = market()
        bars = dict(status='ok', bars=[[x['date'], x['close']] for x in m['history']])
        minutes = dict(days=[dict(date=m['date'], close=100)])
        with patch.object(u, 'load_index_with_fallback', return_value=m), \
             patch.object(u, 'load_etf_with_fallback', return_value={}), \
             patch.object(u, 'optional_board_margin', return_value={}), \
             patch.object(u, '_build_backtest_history', return_value=bars), \
             patch.object(u, 'build_all_cvd_proxy', return_value={k:minutes for k in ('cy','kc')}), \
             patch.object(r, 'collect_research', side_effect=ValueError('offline')):
            got = u.build(None)
        self.assertEqual(got['backtests']['cy'], bars)
        self.assertEqual(got['cvd_proxy']['cy'], minutes)
        self.assertEqual(got['research']['health']['cy']['ok'], 0)

    def test_failed_refresh_keeps_evidence_but_does_not_score(self):
        m = market()
        doc = dict(generated_at='2026-08-29T17:00:00+08:00',
                   indices={k: dict(index=m, etf={}) for k in ('cy', 'kc')})
        good = r.factor(7, m['date'], 'old', 'old')
        previous = dict(indices={k: {'value':good} for k in ('cy', 'kc')})
        with patch.object(r, 'constituents', side_effect=ValueError('offline')), \
             patch.object(r, 'index_turnover_ratio', side_effect=ValueError('offline')), \
             patch.object(r, 'liquidity_factor', side_effect=ValueError('offline')), \
             patch.object(r, 'valuation_factor', side_effect=ValueError('offline')), \
             patch.object(r, 'shares_factor', side_effect=ValueError('offline')):
            got = r.collect_research(doc, previous)
        self.assertIsNone(got['indices']['cy']['value']['score'])
        self.assertEqual(got['indices']['cy']['value']['last_good']['score'], 7)
        self.assertEqual(got['indices']['cy']['cvd']['status'], 'not_connected')


if __name__ == '__main__':
    unittest.main()
