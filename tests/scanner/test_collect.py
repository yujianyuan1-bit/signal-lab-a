import io,struct,sys,unittest,zipfile
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'research'/'scanner'))
import collect as C

def day_bytes(duplicate=False):
    rows=[]
    for i in range(80):
        dt=date(2026,1,1)+timedelta(days=i if not duplicate else 0)
        rows.append(struct.pack('<IIIIIfII',int(dt.strftime('%Y%m%d')),841,882,839,875,165196384,190946790,0))
    return b''.join(rows)

class Response(io.BytesIO):
    status=206
    def __init__(self,data,lo,hi,total,etag='v1'):
        super().__init__(data)
        self.headers={'Content-Range':f'bytes {lo}-{hi}/{total}','ETag':etag,'Last-Modified':'fixed'}

class CollectTests(unittest.TestCase):
    def test_etf_price_precision_and_real_amount(self):
        row=C.decode_day(day_bytes(),'sz159770')[-1]
        self.assertEqual(row['close'],.875)
        self.assertEqual(row['amount'],165196384)
    def test_index_scale_differs_from_etf(self):
        self.assertEqual(C.decode_day(day_bytes(),'sh000001')[-1]['close'],8.75)
    def test_corrupt_record_and_duplicate_dates_rejected(self):
        with self.assertRaises(ValueError):C.decode_day(day_bytes()+b'x','sz159770')
        with self.assertRaisesRegex(ValueError,'Duplicate'):C.decode_day(day_bytes(True),'sz159770')
    def test_http_ranges_read_real_zip_and_check_crc(self):
        b=io.BytesIO();payload=bytes(range(256))*500
        with zipfile.ZipFile(b,'w',zipfile.ZIP_STORED) as z:z.writestr('a.day',payload)
        data=b.getvalue()
        def get(req,timeout):
            spec=req.get_header('Range')[6:]
            lo,hi=(max(0,len(data)-int(spec[1:])),len(data)-1) if spec.startswith('-') else map(int,spec.split('-'))
            return Response(data[lo:hi+1],lo,hi,len(data))
        with patch('urllib.request.urlopen',side_effect=get):
            r=C.RangeFile('https://example.test/archive.zip')
            with zipfile.ZipFile(r) as z:self.assertEqual(z.read('a.day'),payload)
            self.assertGreater(r.calls,1)
    def test_ignored_range_and_wrong_offsets_rejected(self):
        r=Response(b'x',0,0,1);r.status=200
        with patch('urllib.request.urlopen',return_value=r):
            with self.assertRaises(ValueError):C.RangeFile('https://example.test/a.zip')
        with patch('urllib.request.urlopen',return_value=Response(b'x',1,1,2)):
            with self.assertRaisesRegex(ValueError,'Wrong'):C.RangeFile('https://example.test/a.zip')
    def test_archive_revision_during_read_rejected(self):
        responses=[Response(b'x'*65536,65536,131071,131072,'v1'),Response(b'x'*65536,0,65535,131072,'v2')]
        with patch('urllib.request.urlopen',side_effect=responses):
            r=C.RangeFile('https://example.test/a.zip');r.seek(0)
            with self.assertRaisesRegex(ValueError,'changed'):r.read(1)

if __name__=='__main__':unittest.main()
