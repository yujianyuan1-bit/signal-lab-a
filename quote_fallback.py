"""Dated Sina A-share close quotes. Listing prices are never used as quotes."""
import concurrent.futures
import json
import math
import re
import time
import urllib.request

BASE='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'

def request_text(url):
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

def fetch_sina_records(trade_day,requester=None):
    requester=requester or request_text
    start=time.monotonic()
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
    symbols=sorted(symbol for symbol in listed if a_share(symbol))
    records=[]
    for offset in range(0,len(symbols),100):
        if time.monotonic()-start>480:raise RuntimeError('新浪采集超过8分钟预算，拒绝部分发布')
        batch=symbols[offset:offset+100]
        text=requester('https://hq.sinajs.cn/list='+','.join(batch))
        records.extend(parse_quotes(text,trade_day))
        print(f'新浪带日期报价 {min(offset+100,len(symbols))}/{len(symbols)}；有效{len(records)}',flush=True)
        time.sleep(.15)
    codes=[row['代码'] for row in records]
    if len(codes)!=len(set(codes)):raise RuntimeError('新浪报价重复，拒绝发布')
    return records
