import copy,json,sys,tempfile,unittest,subprocess
from datetime import date,datetime,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from forward_log import TZ,digest,read_journal,record,summarize,step
SPEC=json.loads((Path(__file__).resolve().parents[1]/'forward_spec.json').read_text())

def payload(d,score=1.5,close=105):
    day=date.fromisoformat(d)
    hist=[dict(date=(day-timedelta(days=i)).isoformat(),close=100.) for i in range(4,0,-1)]+[dict(date=d,close=close)]
    return dict(indices={'cy':{'index':dict(date=d,close=close,history=hist)}},research={'indices':{'cy':{'breadth':dict(status='ok',date=d,score=score)}}})
def clock(d,hour=17):return datetime.fromisoformat(d).replace(hour=hour,tzinfo=TZ)
def bar(d,c):return dict(date=d,open=c,close=c,high=c+1,low=c-1,source='test fixture')

class ForwardLogTests(unittest.TestCase):
    def test_stale_and_preclose_snapshots_never_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            for p,n in [(payload('2026-10-16'),clock('2026-10-19')),(payload('2026-10-19'),clock('2026-10-19',14))]:
                z=record(p,'x',tmp,SPEC,n,bar);self.assertEqual(z['summary']['observations'],0)
            self.assertFalse((Path(tmp)/'observations.jsonl').exists())
    def test_first_observation_then_trigger_and_duplicate_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=record(payload('2026-10-19'),'a',tmp,SPEC,clock('2026-10-19'),bar)
            self.assertEqual(a['summary']['raw_events'],0)
            b=record(payload('2026-10-20',1.8),'b',tmp,SPEC,clock('2026-10-20'),bar)
            self.assertEqual(b['summary']['raw_events'],1)
            path=Path(tmp)/'observations.jsonl';before=path.read_bytes()
            record(payload('2026-10-20',9),'changed',tmp,SPEC,clock('2026-10-20'),bar)
            self.assertEqual(path.read_bytes(),before)
            rows=read_journal(path,digest(SPEC));self.assertTrue(rows[-1]['event'])
    def test_gap_cancels_waiting_state_instead_of_inventing_missing_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            record(payload('2026-10-19'),'a',tmp,SPEC,clock('2026-10-19'),bar)
            record(payload('2026-10-22',1.8),'b',tmp,SPEC,clock('2026-10-22'),bar)
            rows=read_journal(Path(tmp)/'observations.jsonl',digest(SPEC))
            self.assertTrue(rows[-1]['gap_since_previous']);self.assertFalse(rows[-1]['event'])
            self.assertEqual(rows[-1]['state_after']['setup_date'],'2026-10-22')
    def test_weekend_is_not_a_trading_observation_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            record(payload('2026-10-23'),'a',tmp,SPEC,clock('2026-10-23'),bar)
            p=payload('2026-10-26',1.8)
            p['indices']['cy']['index']['history']=[dict(date=d,close=100 if d!='2026-10-26' else 105) for d in ('2026-10-20','2026-10-21','2026-10-22','2026-10-23','2026-10-26')]
            record(p,'b',tmp,SPEC,clock('2026-10-26'),bar)
            rows=read_journal(Path(tmp)/'observations.jsonl',digest(SPEC))
            self.assertFalse(rows[-1]['gap_since_previous']);self.assertTrue(rows[-1]['event'])
    def test_journal_or_spec_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            record(payload('2026-10-19'),'a',tmp,SPEC,clock('2026-10-19'),bar)
            p=Path(tmp)/'observations.jsonl';self.assertRaises(ValueError,read_journal,p,digest(dict(SPEC,version='new')))
            r=json.loads(p.read_text());r['close']=1;p.write_text(json.dumps(r)+'\n')
            self.assertRaises(ValueError,read_journal,p,digest(SPEC))
    def test_maturity_pending_null_and_missing_price_cannot_be_win(self):
        rows=[dict(date='a',event=True,gap_since_previous=False,ohlc=None),dict(date='b',event=False,gap_since_previous=False,ohlc=dict(open=100,close=101,low=99))]
        spec=dict(SPEC,horizons=[2]);z=summarize(rows,spec)['horizons']['2']
        self.assertEqual(z['pending'],1);self.assertIsNone(z['all_events']['win_pct'])
        rows.append(dict(date='c',event=False,gap_since_previous=False,ohlc=dict(open=101,close=110,low=100)))
        z=summarize(rows,spec)['horizons']['2'];self.assertEqual(z['all_events']['n'],1);self.assertAlmostEqual(z['all_events']['mean_net_pct'],9.8)
        rows[-1]['ohlc']=None;z=summarize(rows,spec)['horizons']['2'];self.assertEqual(z['unscorable_due_to_gaps'],1);self.assertEqual(z['all_events']['n'],0)
    def test_wait_expiry_and_missing_breadth_cancel(self):
        before={'setup_date':'old','wait_sessions':10};current=dict(date='today',breadth_score=3,close=110,ma5=100)
        after,event,reason=step(before,current,dict(breadth_score=1));self.assertFalse(event);self.assertIsNone(after['setup_date']);self.assertEqual(reason,'expired')
        current['breadth_score']=None;self.assertIsNone(step(before,current,dict(breadth_score=1))[0]['setup_date'])
    def test_cli_failure_is_visible_and_preserves_damaged_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=root/'data.js';data.write_text('window.SIGNAL_LIVE = '+json.dumps(payload('2026-10-19'))+';')
            out=root/'log';out.mkdir();journal=out/'observations.jsonl';journal.write_text('broken\n')
            p=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'forward_log.py'),'--data',str(data),'--output',str(out)],capture_output=True,text=True)
            self.assertEqual(p.returncode,1);self.assertEqual(journal.read_text(),'broken\n')
            self.assertEqual(json.loads((out/'status.json').read_text())['status'],'failed')

if __name__=='__main__':unittest.main()
