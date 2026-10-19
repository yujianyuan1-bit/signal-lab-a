import copy,importlib.util,json,sys,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('altseason_forward',ROOT/'research/altseason_forward.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
SPEC=json.loads((ROOT/'research/altseason-forward-spec.json').read_text())
def now(d,hour=17):return datetime.fromisoformat(d).replace(hour=hour,tzinfo=m.TZ)
def payload(d,hour=17):return dict(generated_at=now(d,hour).isoformat(),indices={'kc':{'index':{'date':d}}})
def inputs(d,jump=False):
 day=datetime.fromisoformat(d).date();rows=[]
 for i in range(65):
  today=i==64;c=103. if today and jump else 100.
  rows.append(dict(date=(day-timedelta(days=64-i)).isoformat(),open=100.,close=c,high=max(101.,c),low=99.,amount=200. if today and jump else 100.,sh_close=300.))
 return rows
class ForwardPaperTests(unittest.TestCase):
 def test_first_record_arms_only_then_trigger_then_next_open(self):
  with tempfile.TemporaryDirectory() as d:
   m.record(payload('2026-10-19'),'a',d,SPEC,now('2026-10-19'),lambda p,t:inputs(t))
   z=m.record(payload('2026-10-20'),'b',d,SPEC,now('2026-10-20'),lambda p,t:inputs(t,True))
   self.assertEqual(z['summary']['rules'][m.RULES[1]]['raw_events'],1)
   p=z['summary']['rules'][m.RULES[1]]['nonoverlap']['20']['pending'][0];self.assertIsNone(p['entry_price'])
   z=m.record(payload('2026-10-21'),'c',d,SPEC,now('2026-10-21'),lambda p,t:inputs(t))
   q=z['summary']['rules'][m.RULES[1]]['nonoverlap']['20'];self.assertEqual(q['n'],0);self.assertIsNone(q['win_pct']);self.assertEqual(q['pending'][0]['entry'],'2026-10-21');self.assertEqual(q['pending'][0]['entry_price'],100.)
 def test_preclose_and_stale_payloads_never_load_or_backfill(self):
  def fail(*a):raise AssertionError('network must not be called')
  with tempfile.TemporaryDirectory() as d:
   for p,n in [(payload('2026-10-19'),now('2026-10-19',14)),(payload('2026-10-19',11),now('2026-10-19')),(payload('2026-10-18'),now('2026-10-19'))]:
    self.assertEqual(m.record(p,'x',d,SPEC,n,fail)['summary']['observations'],0)
   self.assertFalse((Path(d)/'observations.jsonl').exists())
 def test_duplicate_preserved_and_tamper_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   m.record(payload('2026-10-19'),'a',d,SPEC,now('2026-10-19'),lambda p,t:inputs(t))
   path=Path(d)/'observations.jsonl';before=path.read_bytes();m.record(payload('2026-10-19'),'changed',d,SPEC,now('2026-10-19'),lambda p,t:inputs(t,True));self.assertEqual(before,path.read_bytes())
   row=json.loads(path.read_text());row['date']='2026-10-18';path.write_text(json.dumps(row)+'\n')
   self.assertRaises(ValueError,m.record,payload('2026-10-20'),'x',d,SPEC,now('2026-10-20'),lambda p,t:inputs(t))
 def test_missing_day_cancels_wait_and_does_not_backfill(self):
  with tempfile.TemporaryDirectory() as d:
   m.record(payload('2026-10-19'),'a',d,SPEC,now('2026-10-19'),lambda p,t:inputs(t))
   z=m.record(payload('2026-10-21'),'c',d,SPEC,now('2026-10-21'),lambda p,t:inputs(t,True))
   self.assertEqual(z['summary']['rules'][m.RULES[1]]['raw_events'],0);self.assertEqual(z['summary']['observations'],2)
 def test_invalid_input_visible_without_journal_append(self):
  with tempfile.TemporaryDirectory() as d:
   def loader(p,t):r=inputs(t);r[-1]['amount']=None;return r
   z=m.record(payload('2026-10-19'),'a',d,SPEC,now('2026-10-19'),loader)
   self.assertEqual(z['status'],'input_unavailable_no_backfill');self.assertIn('reason',json.loads((Path(d)/'status.json').read_text()));self.assertFalse((Path(d)/'observations.jsonl').exists())
 def test_day10_no_refresh_no_same_day_setup_and_trigger(self):
  f=dict(date='new',R2_wait=True,R2_trigger=True)
  a,e,_=m.step(dict(setup_date=None,wait_sessions=0),f,m.RULES[1]);self.assertFalse(e);self.assertEqual(a['setup_date'],'new')
  a,e,detail=m.step(dict(setup_date='old',wait_sessions=9),f,m.RULES[1]);self.assertTrue(e);self.assertEqual(detail['wait_sessions'],10)
  f['R2_trigger']=False;a,e,_=m.step(dict(setup_date='old',wait_sessions=9),f,m.RULES[1]);self.assertIsNone(a['setup_date'])
 def test_portfolio_nonoverlap_maturity_and_cost(self):
  rows=[]
  for i in range(25):
   d=f'2026-11-{i+1:02}';rows.append(dict(date=d,observed_at=d+'T17:00:00+08:00',inputs=[dict(date=d)],bar=dict(open=100.,close=110.,low=95.),events={r:i in [0,3] for r in m.RULES}))
  q=m.summary(rows,SPEC)['rules'][m.RULES[1]]['nonoverlap']['20'];self.assertEqual(q['n'],1);self.assertEqual(q['skipped_while_holding'],['2026-11-04']);self.assertAlmostEqual(q['mean_net_pct'],9.8)
  rows.pop(2);q=m.summary(rows,SPEC)['rules'][m.RULES[1]]['nonoverlap']['20']
  # Preserve missing date in the snapshot calendar, while daily journal lacks it.
  rows[-1]['inputs'].append(dict(date='2026-11-03'));q=m.summary(rows,SPEC)['rules'][m.RULES[1]]['nonoverlap']['20'];self.assertEqual(q['n'],0);self.assertEqual(q['unscorable_due_to_gaps'],['2026-11-01'])
 def test_changed_frozen_parameters_rejected(self):
  bad=copy.deepcopy(SPEC);bad['rules'][m.RULES[0]]['wait_conditions']['ratio_quantile_min']=.7
  with tempfile.TemporaryDirectory() as d:self.assertRaises(ValueError,m.record,payload('2026-10-19'),'x',d,bad,now('2026-10-19'),lambda p,t:inputs(t))
 def test_prior_quantile_excludes_current(self):
  a=inputs('2026-10-19');b=copy.deepcopy(a);b[-1]['amount']=1e12
  self.assertEqual(m.feature(a)['v5_q25'],m.feature(b)['v5_q25'])
if __name__=='__main__':unittest.main()
