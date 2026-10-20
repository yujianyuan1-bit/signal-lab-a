"""Run: python -m unittest discover -s tests -v"""
import sys
from datetime import date,timedelta
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'research'/'scanner'))
import scanner as S


def business(n=130):
    dt=date(2025,1,2);out=[]
    while len(out)<n:
        if dt.weekday()<5:out.append(dt.isoformat())
        dt+=timedelta(days=1)
    return out


def make_bars(n=130,base=1.2,amount=10_000_000):
    days=business(n)
    return [S.check_bar({'date':d,'open':base,'close':base,'low':base-.01,'high':base+.01,'amount':amount}) for d in days]


class ScannerTests(unittest.TestCase):
    def test_linear_q80_and_lookback(self):
        self.assertAlmostEqual(S.quantile([1,2,3,4,5,6],.8),5.0)
        self.assertAlmostEqual(S.quantile([1,2,3,4],.8),3.4)

    def test_eastmoney_amount_col_is_field_seven(self):
        payload={'rc':0,'data':{'code':'159770','klines':['2026-10-19,0.840,0.847,0.853,0.828,140000000,117000000']}}
        x=S.from_em_response(payload,'159770')
        self.assertEqual(x[0]['amount'],117000000)
        self.assertEqual(x[0]['close'],.847)

    def test_no_amount_fails_not_volume_fallback(self):
        body={'rc':0,'data':{'code':'159770','klines':['2026-10-19,0.840,0.847,0.853,0.828,140000000']}}
        with self.assertRaisesRegex(ValueError,'amount'):S.from_em_response(body,'159770')

    def test_ohlc_validation(self):
        with self.assertRaisesRegex(ValueError,'OHLC'):
            S.check_bar({'date':'2026-10-19','open':1.1,'close':1.2,'high':1.15,'low':1.05,'amount':1})

    def test_wait_day10_timeouts_without_same_day_rearm(self):
        state={'start':20}
        f={'r1_ignite':False,'r1_wait':True}
        next_state,event,age=S.step_state(state,f,'r1',30)
        self.assertEqual((event,age),('EXPIRED',10))
        self.assertIsNone(next_state['start'])
        new_state,event,age=S.step_state(next_state,f,'r1',31)
        self.assertEqual((event,age),('ENTER',0))
        self.assertEqual(new_state['start'],31)

    def test_day10_trigger_allowed(self):
        st,e,d=S.step_state({'start':20},{'r1_ignite':True,'r1_wait':True},'r1',30)
        self.assertEqual((e,d),('TRIGGERED',10))
        self.assertIsNone(st['start'])

    def test_wait_trigger_never_on_day0(self):
        f={'r1_wait':True,'r1_ignite':True}
        st,event,age=S.step_state({'start':None},f,'r1',20)
        self.assertEqual(event,'ENTER')
        st,event,age=S.step_state(st,f,'r1',21)
        self.assertEqual((event,age),('TRIGGERED',1))

    def test_r1_trigger_requires_true_amount_above_1x_not_135x(self):
        sh=make_bars(90,base=4000,amount=1e10)
        etf=make_bars(90,base=1.0,amount=1e8)
        h=[{**b,'sh_close':x['close']} for b,x in zip(etf,sh)]
        h[-1]['close']=1.031;h[-1]['high']=1.033;h[-1]['amount']=1.25e8
        f=S.features(h)
        self.assertTrue(f['r1_ignite'])
        self.assertAlmostEqual(f['amount_ratio'],1.25)

    def test_r2_requires_140_amount(self):
        sh=make_bars(90,base=4000,amount=1e10)
        etf=make_bars(90,base=1.,amount=1e8)
        h=[{**b,'sh_close':x['close']} for b,x in zip(etf,sh)]
        h[-1]['close']=1.03;h[-1]['high']=1.031;h[-1]['amount']=1.39e8
        self.assertFalse(S.features(h)['r2_ignite'])
        h[-1]['amount']=1.41e8
        self.assertTrue(S.features(h)['r2_ignite'])

    def test_65_bar_feature_window_matches_full_series(self):
        sh=make_bars(125,base=4000,amount=1e10)
        etf=make_bars(125,base=1.0,amount=1e8)
        h=[{**x,'sh_close':sh[i]['close']} for i,x in enumerate(etf)]
        h[-1]['close']=1.03
        h[-1]['amount']=1.6e8
        a=S.features(h)
        b=S.features(h[-65:])
        for key in ('r1_wait','r1_ignite','r2_wait','r2_ignite','ratio_q80','v5_q25','amount_ratio'):
            self.assertEqual(a[key],b[key],key)

    def test_missing_trading_day_reported(self):
        sh=make_bars(110,base=4000,amount=1e10)
        etf=make_bars(110,base=1,amount=1e8)
        cy=make_bars(110,base=2000,amount=1e9)
        kc=make_bars(110,base=1200,amount=1e9)
        bad=etf.pop(80)
        result=S.scanner_replay('159770',{'name':'机器人','asset_type':'etf'},sh,etf,cy,kc,sh[-1]['date'])
        self.assertEqual(result['gap_count'],1)
        self.assertIn(bad['date'],result['gap_dates'])
        self.assertTrue(any(r['r1']=='DATA_GAP' for r in result['trace']))

    def test_stale_asset_reported(self):
        b=make_bars(115);sh=make_bars(115,4000);cy=make_bars(115,2000);kc=make_bars(115,1300)
        obj=S.scanner_replay('159770',{'name':'机器人','asset_type':'etf'},sh,b[:-1],cy,kc,sh[-1]['date'])
        self.assertEqual(obj['status'],'STALE')

    def test_trigger_execution_20_sessions_and_cost(self):
        # Exactly one synthetic R1 ignition: first 70-day flat setup from very weak ratio, then +3% day.
        sh=make_bars(120,base=4000,amount=1e10)
        etf=make_bars(120,base=1,amount=1e8)
        cy=make_bars(120,base=2000,amount=1e9);kc=make_bars(120,base=1300,amount=1e9)
        # First 65 days flat, weak starts by dropping below MA20 at day 70 and ratio >q80.
        for i in range(69,77):
            etf[i]['close']=.92;etf[i]['open']=.92;etf[i]['low']=.91;etf[i]['high']=.94
        etf[77]['open']=.92;etf[77]['close']=.96;etf[77]['high']=.98;etf[77]['low']=.91;etf[77]['amount']=1.2e8
        for i in range(78,120):
            etf[i]['close']=1.;etf[i]['open']=1.;etf[i]['high']=1.01;etf[i]['low']=.99
        # feature day77: prev_high .94, +4.3%, closing may still be below MA5 depending prev history.
        result=S.scanner_replay('159770',{'name':'机器人','asset_type':'etf'},sh,etf,cy,kc,sh[-1]['date'])
        events=[x for x in result['trades'] if x['rule']=='R1' and x['hold_days']==20]
        self.assertTrue(events, 'expected a synthetic signal')
        x=events[0]
        self.assertEqual(x['entry'],sh[x['index']+1]['date'])
        self.assertEqual(x['exit'],sh[x['index']+20]['date'])
        self.assertAlmostEqual(x['net_pct'],(x['exit_close']/x['entry_open']-1)*100-.2)

    def test_json_generate_and_mobile_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            S.produce(Path(td),[],'20261019',[],{})
            h=(Path(td)/'scanner_snapshot.html').read_text(encoding='utf-8')
            self.assertIn('signallab-scanner-1.0',h)
            self.assertIn('embedded-report',h)
            self.assertTrue((Path(td)/'latest.json').exists())


if __name__=='__main__':unittest.main()
