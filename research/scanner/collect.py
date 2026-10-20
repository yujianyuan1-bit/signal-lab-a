"""Collect genuine daily turnover for the original six-asset R1/R2 radar.

The frozen scanner stays unchanged. TDX official ZIP ranges are primary;
Eastmoney f57 is a second source. Cached bars remain auditable on failure.
"""
from __future__ import annotations
import concurrent.futures as cf
import csv, hashlib, io, json, re, struct, sys, urllib.request, zipfile
from datetime import datetime
from pathlib import Path
import scanner as S

ROOT=Path(__file__).resolve().parent
ASSETS={'sh000001':'index','sz399006':'index','sh000688':'index'}
ASSETS.update({m['market']+c:m['asset_type'] for c,m in S.CONFIG.items()})
URL='https://www.tdx.com.cn/products/data/data/vipdoc/{}lday.zip'

class RangeFile(io.RawIOBase):
    """Seekable ZIP input; reject ignored ranges and mid-read revisions."""
    def __init__(self,url):
        self.url=url;self.pos=0;self.size=None;self.identity=None;self.calls=0
        self._get('bytes=-65536')
    def _get(self,byte_range):
        req=urllib.request.Request(self.url,headers={'Range':byte_range,'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req,timeout=35) as r:
            match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',r.headers.get('Content-Range',''))
            if r.status!=206 or not match:raise ValueError('Official server did not honor ZIP range')
            lo,hi,total=map(int,match.groups())
            if byte_range.startswith('bytes=-'):
                expected=(max(0,total-int(byte_range[7:])),total-1)
            else:expected=tuple(map(int,byte_range[6:].split('-')))
            if (lo,hi)!=expected:raise ValueError('Wrong ZIP byte range')
            identity=(r.headers.get('ETag'),r.headers.get('Last-Modified'),total)
            if self.identity is not None and identity!=self.identity:raise ValueError('Archive changed during read')
            data=r.read(hi-lo+2)
            if len(data)!=hi-lo+1:raise ValueError('Truncated ZIP range')
        self.identity=identity;self.size=total;self.cache=(lo,data);self.calls+=1
    def seekable(self):return True
    def readable(self):return True
    def tell(self):return self.pos
    def seek(self,offset,whence=0):
        self.pos=offset if whence==0 else self.pos+offset if whence==1 else self.size+offset
        if self.pos<0:raise ValueError('negative seek')
        return self.pos
    def read(self,n=-1):
        n=self.size-self.pos if n<0 else min(n,self.size-self.pos)
        if n<=0:return b''
        lo,data=self.cache
        if not (lo<=self.pos and self.pos+n<=lo+len(data)):
            hi=min(self.size-1,self.pos+max(n,65536)-1)
            self._get(f'bytes={self.pos}-{hi}');lo,data=self.cache
        start=self.pos-lo;out=data[start:start+n];self.pos+=len(out);return out

def decode_day(blob,key,start='2021-10-01',end='9999-12-31'):
    if len(blob)%32:raise ValueError('TDX record length is not 32 bytes')
    divisor=1000 if ASSETS[key]=='etf' else 100
    rows=[];seen=set()
    for dt,op,hi,lo,cl,amount,volume,reserved in struct.iter_unpack('<IIIIIfII',blob):
        text=str(dt);day=f'{text[:4]}-{text[4:6]}-{text[6:]}'
        if not start<=day<=end:continue
        if day in seen:raise ValueError('Duplicate daily date')
        seen.add(day)
        rows.append(S.check_bar(dict(date=day,open=op/divisor,high=hi/divisor,
                                    low=lo/divisor,close=cl/divisor,amount=amount)))
    if len(rows)<80:raise ValueError('Fewer than 80 verified daily bars')
    return sorted(rows,key=lambda b:b['date'])

def official_market(market,end):
    reader=RangeFile(URL.format(market));out={};meta={}
    with zipfile.ZipFile(reader) as z:
        for key in ASSETS:
            if not key.startswith(market):continue
            blob=z.read(key+'.day')
            out[key]=decode_day(blob,key,end=end)
            meta[key]={'source':'TDX official unadjusted daily archive; turnover yuan',
                       'url':reader.url,'archive_identity':reader.identity,
                       'member_sha256':hashlib.sha256(blob).hexdigest(),
                       'price_divisor':1000 if ASSETS[key]=='etf' else 100}
    print('Official ZIP ranges:',market,reader.calls,flush=True)
    return out,meta

def write_cache(key,rows):
    cache=ROOT/'cache';cache.mkdir(exist_ok=True)
    p=cache/f'{key}.csv';tmp=p.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=S.REQUIRED);w.writeheader();w.writerows(rows)
    tmp.replace(p)

def publish(loaded,meta,end,errors):
    results=[]
    for code,m in S.CONFIG.items():
        key=m['market']+code
        if not all(k in loaded for k in ('sh000001','sz399006','sh000688',key)):
            errors.append({'code':code,'error':'Asset or benchmark missing; signal not computed'});continue
        item=S.scanner_replay(code,{**m,'source':meta.get(key,{}).get('source','cached verified daily bars')},
             loaded['sh000001'],loaded[key],loaded['sz399006'],loaded['sh000688'],end)
        if item['asset_last_date']<end or item['session']<end:item['status']='STALE'
        results.append(item)
    sources={k:v.get('source','unknown') for k,v in meta.items()}
    report=S.produce(ROOT,results,end.replace('-',''),errors,sources)
    for asset,item in zip(report['assets'],results):asset['trace']=item['trace']
    report['collection_provenance']=meta
    report['notice']='原版六标的R1/R2日线雷达；当日状态与历史回溯分开。ETF未复权，拆分/分红可能影响历史收益。'
    (ROOT/'latest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    template=ROOT.parents[1]/'scanner.html'
    if template.exists():
        blob=json.dumps(report,ensure_ascii=False,allow_nan=False).replace('<','\\u003c')
        page=template.read_text().replace('<script id="embedded-report" type="application/json">null</script>',
             '<script id="embedded-report" type="application/json">'+blob+'</script>')
        (ROOT/'scanner_snapshot.html').write_text(page)
    (ROOT/'provenance.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    print('Published:',len(results),'assets,',len(errors),'errors',flush=True)
    return report

def main():
    now=datetime.now(S.TZ)
    import exchange_calendars as xc
    cal=xc.get_calendar('XSHG')
    # Never take a forming daily bar. On holidays retain the last exchange close.
    cutoff=now.date().isoformat()
    if (now.hour,now.minute)<(17,0):
        from datetime import timedelta
        cutoff=(now.date()-timedelta(days=1)).isoformat()
    end=cal.sessions_in_range('2021-10-01',cutoff)[-1].date().isoformat()
    loaded={};meta={};errors=[]
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        jobs={pool.submit(official_market,m,end):m for m in ('sh','sz')}
        for job in cf.as_completed(jobs):
            try:
                bars,info=job.result();loaded.update(bars);meta.update(info)
            except Exception as exc:print('TDX unavailable:',jobs[job],str(exc),flush=True)
    for key,kind in ASSETS.items():
        if key not in loaded or loaded[key][-1]['date']<end:
            try:
                rows,src=S.fetch_bars(key[2:],key[:2],kind,'20211001',end.replace('-',''),retries=2,allow_akshare=False)
                if not rows or rows[-1]['date']!=end:raise ValueError('Secondary source is stale')
                loaded[key]=rows;meta[key]={'source':src,'retrieved_at':now.isoformat()}
            except Exception as exc:
                errors.append({'code':key,'error':str(exc)[:350]})
                p=ROOT/'cache'/f'{key}.csv'
                if key not in loaded and p.exists():loaded[key]=S._read_csv(p);meta[key]={'source':'last verified CSV cache; collection failed'}
        if key in loaded:
            meta.setdefault(key,{})['last_date']=loaded[key][-1]['date']
            if loaded[key][-1]['date']==end:write_cache(key,loaded[key])
    publish(loaded,meta,end,errors)
    return 0 if len(loaded)==len(ASSETS) and not errors else 2

if __name__=='__main__':sys.exit(main())
