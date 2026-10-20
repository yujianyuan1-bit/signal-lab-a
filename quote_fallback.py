"""Dated Sina A-share close quotes. Listing prices are never used as quotes."""
import concurrent.futures
import json
import math
import re
import time
import urllib.request
import subprocess
import sys
import tempfile
from pathlib import Path

BASE='http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'

def request_text(url):
    # Public market data only, no credentials/cookies. AKShare's Sina listing uses HTTP.
    request=urllib.request.Request(url,headers={'Referer':'https://finance.sina.com.cn/'})
    with urllib.request.urlopen(request,timeout=20) as response:
        return response.read().decode('gb18030')

def a_share(symbol):
    return bool(re.fullmatch(r'(?:sh(?:60|68)\d{4}|sz(?:00|30)\d{4})',symbol))

def parse_quotes(text,trade_day):
    records=[]
    for symbol,body in re.findall(r'var\s+hq_str_([a-z]{2}\d{6})="([^"]*)";',text):
        fields=body.split(',')
        if not a_share(symbol) or len(fields)<32:continue
        if fields[30]!=trade_day:continue
        # Reject incomplete intraday quotes even when their date is correct.
        if not re.fullmatch(r'\d{2}:\d{2}:\d{2}',fields[31]) or fields[31]<'15:00:00':continue
        try:
            op,close,hi,lo,shares,amount=map(float,[fields[1],fields[3],fields[4],fields[5],fields[8],fields[9]])
            if not all(math.isfinite(x) and x>0 for x in [op,close,hi,lo,shares,amount]):continue
            if not lo<=min(op,close)<=max(op,close)<=hi:continue
        except ValueError:continue
        records.append({'代码':symbol[2:],'今开':op,'最新价':close,'最高':hi,'最低':lo,
                        '成交量':shares/100,'成交额':amount,'报价日期':fields[30],'报价时间':fields[31]})
    return records

def parse_tencent(text,trade_day):
    records=[]
    for symbol,body in re.findall(r'v_([a-z]{2}\d{6})="([^"]*)";',text):
        f=body.split('~')
        if not a_share(symbol) or len(f)<38:continue
        stamp=f[30]
        if not re.fullmatch(r'\d{14}',stamp) or stamp[:8]!=trade_day.replace('-','') or stamp[8:]<'150000':continue
        try:
            op,close,hi,lo=map(float,[f[5],f[3],f[33],f[34]])
            _,raw_volume,raw_amount=f[35].split('/')
            volume,amount=float(raw_volume),float(raw_amount)
            if not all(math.isfinite(x) and x>0 for x in [op,close,hi,lo,volume,amount]):continue
            if not lo<=min(op,close)<=max(op,close)<=hi:continue
            # Tencent has returned both shares and lots for different boards.
            # Select only an unambiguous actual-volume unit whose VWAP fits OHLC.
            candidates=[shares for shares in (volume,volume*100) if lo*.999<=amount/shares<=hi*1.001]
            if len(candidates)!=1:continue
            lots=candidates[0]/100
        except (ValueError,ZeroDivisionError):continue
        records.append({'代码':symbol[2:],'今开':op,'最新价':close,'最高':hi,'最低':lo,
                        '成交量':lots,'成交额':amount,'报价日期':trade_day,'报价时间':stamp[8:],
                        '报价来源':'Tencent dated full quote'})
    return records

def baostock_symbols(trade_day):
    code='''import baostock as bs,json,sys,socket
socket.setdefaulttimeout(20)
login=bs.login()
if login.error_code!='0':raise RuntimeError(login.error_msg)
try:
 r=bs.query_all_stock(day=sys.argv[1]);rows=[]
 if r.error_code!='0':raise RuntimeError(r.error_msg)
 while r.next():rows.append(dict(zip(r.fields,r.get_row_data())))
 with open(sys.argv[2],'w') as f:json.dump([x['code'].replace('.','') for x in rows],f)
finally:bs.logout()
'''
    with tempfile.TemporaryDirectory() as td:
        output=Path(td)/'symbols.json'
        subprocess.run([sys.executable,'-c',code,trade_day,str(output)],timeout=90,check=True,capture_output=True,text=True)
        return json.loads(output.read_text())

def fetch_sina_records(trade_day,requester=None):
    requester=requester or request_text
    start=time.monotonic()
    try:
        count=int(json.loads(requester(BASE+'Market_Center.getHQNodeStockCount?node=hs_a')))
        if not 4500<=count<=15000:raise RuntimeError(f'新浪股票列表数量异常: {count}')
        pages=(count+79)//80
        def page(number):
            url=BASE+f'Market_Center.getHQNodeData?page={number}&num=80&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page'
            data=json.loads(requester(url))
            if not isinstance(data,list) or not data:raise RuntimeError(f'新浪股票列表第{number}页为空')
            return [row['symbol'] for row in data]
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            listed=[symbol for result in pool.map(page,range(1,pages+1)) for symbol in result]
        if len(listed)!=len(set(listed)) or len(listed)<count-5:
            raise RuntimeError('新浪股票列表重复或分页覆盖不足，拒绝发布')
    except Exception as exc:
        print('新浪股票名单失败，切换BaoStock当日名单:',type(exc).__name__,flush=True)
        listed=baostock_symbols(trade_day)
        if len(listed)!=len(set(listed)):raise RuntimeError('备用名单重复')
    symbols=sorted(symbol for symbol in listed if a_share(symbol))
    if len(symbols)<4500:raise RuntimeError('股票名单覆盖不足')
    Path('quote_universe.json').write_text(json.dumps({'date':trade_day,'symbols':symbols}),encoding='utf-8')
    batches=[symbols[i:i+100] for i in range(0,len(symbols),100)]
    def quote_batch(batch):
        if time.monotonic()-start>480:raise RuntimeError('新浪采集超过8分钟预算，拒绝部分发布')
        selected={symbol[2:] for symbol in batch}
        try:
            rows=parse_tencent(requester('https://qt.gtimg.cn/q='+','.join(batch)),trade_day)
            rows=[row for row in rows if row['代码'] in selected]
            if len(rows)>=len(batch)*.90:return rows
            print('腾讯批次日期/单位/覆盖校验不足，切换新浪完整报价',flush=True)
        except Exception as exc:
            print('腾讯批次失败，切换新浪:',type(exc).__name__,flush=True)
        rows=parse_quotes(requester('https://hq.sinajs.cn/list='+','.join(batch)),trade_day)
        for row in rows:row['报价来源']='Sina dated full quote'
        time.sleep(.15)
        return [row for row in rows if row['代码'] in selected]
    records=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for number,result in enumerate(pool.map(quote_batch,batches),1):
            records.extend(result)
            print(f'腾讯/新浪带日期报价批次 {number}/{len(batches)}；有效{len(records)}',flush=True)
    codes=[row['代码'] for row in records]
    if len(codes)!=len(set(codes)):raise RuntimeError('新浪报价重复，拒绝发布')
    return records
