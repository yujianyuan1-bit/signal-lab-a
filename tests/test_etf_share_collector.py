import json
from pathlib import Path
import tempfile
import unittest
from research.collect_etf_shares import fetch_day, parse_page


def page(rows, number=1, total=None, size=2):
    return {'result': rows, 'pageHelp': {'data': rows, 'total': len(rows) if total is None else total,
                                       'pageSize': size, 'pageNo': number}}


def row(code='588000', day='2026-10-16', value='7019666.82'):
    return {'SEC_CODE': code, 'STAT_DATE': day, 'TOT_VOL': value}


class CollectorTest(unittest.TestCase):
    def test_unit_and_date(self):
        result = parse_page(page([row()]), '2026-10-16', 1)[3][0]
        self.assertEqual(result['shares'], '70196668200.00')
        with self.assertRaises(ValueError):
            parse_page(page([row(day='2026-10-15')]), '2026-10-16', 1)

    def test_truncation_and_invalid_values(self):
        for obj in (page([row()], total=3), page([row(), row()]),
                    page([row(value='NaN')]), page([row(value=True)]),
                    page([row(value='0')]), page([row()], total=True)):
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                parse_page(obj, '2026-10-16', 1)

    def test_paginated_complete_market(self):
        pages = [page([row('510050'), row('510300')], total=3),
                 page([row()], number=2, total=3)]
        with tempfile.TemporaryDirectory() as temp:
            result = fetch_day('2026-10-16', Path(temp),
                               fetch=lambda url: json.dumps(pages.pop(0)).encode())
            self.assertEqual(result['code'], '588000')
            self.assertEqual(len(list(Path(temp).glob('*.meta.json'))), 2)

    def test_pagination_change_and_missing_target(self):
        for pages in ([page([row('510050')])],
                      [page([row('510050'), row('510300')], total=3),
                       page([row()], number=2, total=4)],
                      [page([row('510050'), row('510300')], total=3),
                       page([row('510050')], number=2, total=3)]):
            with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                fetch_day('2026-10-16', Path(temp),
                          fetch=lambda url: json.dumps(pages.pop(0)).encode())


if __name__ == '__main__':
    unittest.main()
