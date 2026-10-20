import json, tempfile, unittest, subprocess
from pathlib import Path
from datetime import date, timedelta
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from zhupi_scan import (parse_daily, daily_quality, universe_stats, basic_health,
    score_state, days_between, build_scan, load_pit, board_of, forward_proxy, normalize_baostock_industry, fetch_spot_records)


def fake_file(dir, day, closing=10., count=5500, cy=1400, kc=600, big=0):
    """Synthetic OHLC with realistic board census. Other symbols are SH mainboard."""
    p=dir/f'stock_price_{day.replace("-","_")}.csv'
    lines=[]
    for i in range(count):
        if i<cy: s=f'sz{300000+i:06d}'
        elif i<cy+kc:s=f'sh{688000+i-cy:06d}'
        else:s=f'sh{600000+i-cy-kc:06d}'
        o=10.0;c=closing
        if big and i<big:c=7.0;o=7.1
        h=max(o,c)+.02;l=min(o,c)-.02
        lines.append(f'{s},{day},{o:.2f},{c:.2f},{h:.2f},{l:.2f},10000,100000\n')
    p.write_text(''.join(lines))
    return p


class ScanTests(unittest.TestCase):
    def test_retry_recovers(self):
        calls=[];waits=[]
        def run(args,**kwargs):
            calls.append(kwargs)
            if len(calls)==1:raise subprocess.TimeoutExpired(args,kwargs['timeout'])
            Path(args[-1]).write_text('[{"代码":"600000"}]')
        self.assertEqual(fetch_spot_records(runner=run,sleeper=waits.append),[{'代码':'600000'}])
        self.assertEqual(len(calls),2)
        self.assertEqual(waits,[5])
        self.assertEqual(calls[0]['timeout'],120)

    def test_retry_exhausted(self):
        calls=[];waits=[]
        def run(args,**kwargs):
            calls.append(1)
            raise subprocess.CalledProcessError(1,args,stderr='RemoteDisconnected')
        with self.assertRaisesRegex(RuntimeError,'不发布旧行情'):fetch_spot_records(runner=run,sleeper=waits.append)
        self.assertEqual(len(calls),3)
        self.assertEqual(waits,[5,15])

    def test_empty_rejected(self):
        def run(args,**kwargs):Path(args[-1]).write_text('[]')
        with self.assertRaisesRegex(RuntimeError,'空数据'):fetch_spot_records(attempts=1,runner=run)

    def test_sina_date_time_and_units(self):
        from quote_fallback import parse_quotes
        fields=['0']*33
        for i,v in {0:'浦发',1:'10',3:'10.5',4:'11',5:'9',8:'123400',9:'1234567',30:'2026-10-20',31:'15:00:00'}.items():fields[i]=v
        text='var hq_str_sh600000="'+','.join(fields)+'";'
        row=parse_quotes(text,'2026-10-20')[0]
        self.assertEqual(row['成交量'],1234)
        self.assertEqual(row['成交额'],1234567)
        self.assertEqual(parse_quotes(text,'2026-10-19'),[])
        self.assertEqual(parse_quotes(text.replace('15:00:00','14:59:59'),'2026-10-20'),[])
        self.assertEqual(parse_quotes(text.replace('sh600000','sh900001'),'2026-10-20'),[])

    def test_sina_bad_price_rejected(self):
        from quote_fallback import parse_quotes
        fields=['0']*33
        for i,v in {1:'10',3:'99',4:'11',5:'9',8:'100',9:'1000',30:'2026-10-20',31:'15:00:00'}.items():fields[i]=v
        self.assertEqual(parse_quotes('var hq_str_sh688001="'+','.join(fields)+'";','2026-10-20'),[])

    def test_tencent_date_and_volume_units(self):
        from quote_fallback import parse_tencent
        f=['']*38
        for i,v in {2:'600000',3:'10',5:'10',30:'20261020161440',33:'11',34:'9',35:'10/100/100000'}.items():f[i]=v
        def text():return 'v_sh600000="'+'~'.join(f)+'";'
        row=parse_tencent(text(),'2026-10-20')[0]
        self.assertEqual(row['成交量'],100)
        self.assertEqual(row['成交额'],100000)
        f[35]='10/10000/100000'
        self.assertEqual(parse_tencent(text(),'2026-10-20')[0]['成交量'],100)
        self.assertEqual(parse_tencent(text(),'2026-10-19'),[])
        f[35]='10/10000/1'
        self.assertEqual(parse_tencent(text(),'2026-10-20'),[])

    def test_live_update_retains_seeded_history(self):
        from zhupi_scan import merge_scan_history
        old_group=dict(id='CY',name='创业板',status='OK',stage='R2a',score=70,stats={},health=[],raw_healthy=True)
        new_group=dict(id='CY',name='创业板',status='OK',stage='WATCH',score=50,stats={},health=[],raw_healthy=True)
        previous=dict(schema=2,engine_version='2.0.0',daily_groups={'2026-10-16':[old_group],'2026-10-19':[old_group]},quality=[dict(date=d,passed=True) for d in ('2026-10-16','2026-10-19')],events=[])
        current=dict(schema=2,engine_version='2.0.0',asof='2026-10-20',daily_groups={'2026-10-19':[dict(old_group,status='WARMUP')],'2026-10-20':[new_group]},quality=[dict(date=d,passed=True) for d in ('2026-10-19','2026-10-20')],events=[],source={},caveats=[])
        out=merge_scan_history(previous,current,[])
        self.assertEqual(len(out['daily_groups']),3)
        self.assertEqual(out['daily_groups']['2026-10-19'][0]['status'],'OK')
        self.assertEqual(out['daily_groups']['2026-10-20'][0]['score'],50)
        self.assertEqual(len(out['series']['CY']),3)

    def test_live_update_obeys_historical_signal_lock(self):
        from zhupi_scan import merge_scan_history
        previous=dict(schema=2,engine_version='2.0.0',daily_groups={},quality=[dict(date=d,passed=True) for d in ('2026-10-16','2026-10-19')],events=[dict(id='CY',date='2026-10-16')])
        current=dict(schema=2,engine_version='2.0.0',asof='2026-10-20',daily_groups={},quality=[dict(date='2026-10-20',passed=True)],events=[dict(id='CY',date='2026-10-20')],source={},caveats=[])
        out=merge_scan_history(previous,current,[])
        self.assertEqual([e['date'] for e in out['events']],['2026-10-16'])

    def test_board_code(self):
        self.assertEqual(board_of('sz301000'),'CY')
        self.assertEqual(board_of('sz302132'),'CY')
        self.assertEqual(board_of('sh689001'),'KC')
        self.assertIsNone(board_of('sh600001'))

    def test_bad_day_cannot_pollute_median(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            a=parse_daily(fake_file(d,'2026-05-06'))
            b=parse_daily(fake_file(d,'2026-05-07',count=500,cy=10,kc=200))
            self.assertEqual(daily_quality(a,[]),[])
            self.assertTrue(daily_quality(b,[a]))

    def test_price_outlier_quarantine(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            p=parse_daily(fake_file(d,'2026-05-06'))
            q=parse_daily(fake_file(d,'2026-05-07',big=20))
            u=universe_stats(q,p,set(s for s in q['stocks'] if board_of(s)=='CY'))
            self.assertEqual(u['outlier_count'],20)
            self.assertTrue(basic_health(u,'CY'))

    def test_holidays_calendar(self):
        self.assertEqual(days_between('2026-05-08','2026-05-11',{'2026-05-08','2026-05-11'}),[])
        self.assertEqual(days_between('2026-05-07','2026-05-11'),['2026-05-08'])

    def test_future_membership_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'members.csv'
            f.write_text('symbol,group_id,group_name,effective_from,effective_to,published_on,source\n'
                         'sh688001,robot,机器人,2026-05-01,,2026-05-15,official\n')
            records,issues=load_pit(f)
            self.assertTrue(issues)
            self.assertEqual(records[0]['start'],'2026-05-15')

    def test_warmup_becomes_live(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            files=[]
            day=date(2026,5,6)
            while len(files)<26:
                if day.weekday()<5:
                    files.append(fake_file(d,day.isoformat(),closing=10.0+(len(files)%5)*.15))
                day+=timedelta(days=1)
            cal={Path(f).stem[-10:].replace('_','-') for f in files}
            out=build_scan(files,calendar=cal)
            self.assertEqual(out['groups'][0]['status'],'OK')
            self.assertIn('score',out['groups'][0])
            self.assertTrue(all(e['date']<out['asof'] or e['date']==out['asof'] for e in out['events']))

    def test_daily_industry_is_never_backdated(self):
        rows=[{'code':'sh.688001','industry':'电子','industryClassification':'申万一级行业','updateDate':'2024-01-01'},
              {'code':'sz.300001','industry':'计算机','industryClassification':'申万一级行业'}]
        got=normalize_baostock_industry(rows,'2026-10-20')
        self.assertEqual(got[0][3],'2026-10-20')
        self.assertEqual(got[0][4],'2026-10-21')
        self.assertEqual(got[0][5],'2026-10-20')

    def test_next_open_for_horizon(self):
        # An event on t uses t+1 OPEN, not t close or a future best low.
        stocks0={'sz300001':{'open':10.,'close':10.}}
        stocks1={'sz300001':{'open':11.,'close':11.}}
        stocks2={'sz300001':{'open':15.,'close':12.}}
        dates=[]
        for i,stocks in enumerate([stocks0,stocks1,stocks2]):
            dates.append({'date':f'2026-05-{6+i:02d}','global_ok':True,'stocks':stocks,
                          'groups':{'CY':{'raw_healthy':True}}})
        ev={'i':0,'id':'CY','symbols':('sz300001',)}
        self.assertAlmostEqual(forward_proxy(ev,dates,2)['gross_pct'],(12/11-1)*100,places=3)
        self.assertEqual(forward_proxy(ev,dates,3)['status'],'unmatured')
        dates[2]['stocks']['sz300001']['close']=6.
        self.assertEqual(forward_proxy(ev,dates,2)['status'],'blocked')

    def test_full_pipeline_blocks_gap(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            files=[fake_file(d,'2026-05-06'),fake_file(d,'2026-05-08')]
            cal={'2026-05-06','2026-05-07','2026-05-08'}
            out=build_scan(files,calendar=cal)
            self.assertFalse(out['quality'][1]['passed'])
            self.assertEqual(out['groups'][0]['status'],'BLOCKED')


if __name__=='__main__':unittest.main()
