"""Read-only SSE outstanding-share collector; no scoring or production writes."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

URL = 'https://query.sse.com.cn/commonQuery.do'
SQL = 'COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L'


def integer(value):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('invalid pagination integer')
    return int(value)


def parse_page(obj, requested_date, page_no):
    """Validate the whole returned market page before selecting 588000."""
    rows = obj.get('result')
    meta = obj.get('pageHelp')
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise ValueError('missing market rows or pagination metadata')
    total, page_size = integer(meta.get('total')), integer(meta.get('pageSize'))
    if total <= 0 or page_size <= 0 or integer(meta.get('pageNo')) != page_no:
        raise ValueError('empty date or mismatched pagination')
    if meta.get('data') != rows:
        raise ValueError('result and pageHelp.data disagree')
    expected = max(0, min(page_size, total - (page_no - 1) * page_size))
    if len(rows) != expected:
        raise ValueError('truncated or inconsistent page')
    seen, target = set(), []
    for row in rows:
        if not isinstance(row, dict) or row.get('STAT_DATE') != requested_date:
            raise ValueError('source ignored requested date')
        code = row.get('SEC_CODE')
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or code in seen:
            raise ValueError('invalid or duplicate security code')
        seen.add(code)
        if code == '588000':
            value = row.get('TOT_VOL')
            if isinstance(value, bool):
                raise ValueError('boolean share quantity')
            try:
                shares = Decimal(str(value)) * 10000
            except InvalidOperation as exc:
                raise ValueError('invalid share quantity') from exc
            if not shares.is_finite() or shares <= 0:
                raise ValueError('nonpositive or nonfinite shares')
            target.append({'date': requested_date, 'code': code,
                           'shares': str(shares), 'source_unit': '万份',
                           'source_value': str(value), 'source_name': row.get('SEC_NAME')})
    return total, page_size, seen, target


def fetch_day(day, raw_dir, fetch=None):
    date.fromisoformat(day)
    raw_dir.mkdir(parents=True, exist_ok=True)
    page, all_codes, target, expected_total, expected_size = 1, set(), [], None, None
    while True:
        params = {'isPagination': 'true', 'pageHelp.pageSize': 2000,
                  'pageHelp.pageNo': page, 'sqlId': SQL, 'STAT_DATE': day}
        url = URL + '?' + urllib.parse.urlencode(params)
        if fetch is None:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                       'Referer': 'https://www.sse.com.cn/'})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read(8_000_001)
        else:
            raw = fetch(url)
        if len(raw) > 8_000_000:
            raise ValueError('oversized response')
        # Store evidence even when the source schema is invalid.
        path = raw_dir / f'{day}-page{page}.json'
        path.write_bytes(raw)
        (raw_dir / f'{day}-page{page}.meta.json').write_text(json.dumps({
            'url': url, 'retrieved_at': datetime.now(timezone.utc).isoformat(),
            'sha256': hashlib.sha256(raw).hexdigest()}))
        total, size, codes, found = parse_page(json.loads(raw), day, page)
        if expected_total is None:
            expected_total, expected_size = total, size
        if (total, size) != (expected_total, expected_size) or all_codes & codes:
            raise ValueError('market pagination changed or duplicate code across pages')
        all_codes.update(codes)
        target.extend(found)
        if len(all_codes) >= total:
            break
        page += 1
        time.sleep(0.25)
    if len(all_codes) != expected_total or len(target) != 1:
        raise ValueError('incomplete market or missing/duplicate target')
    return target[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--calendar', default='research/etf-share-calendar.json')
    parser.add_argument('--mode', choices=('probe', 'full'), default='probe')
    parser.add_argument('--output', default='etf-share-output')
    args = parser.parse_args()
    calendar = json.loads(Path(args.calendar).read_text())
    dates = calendar['collection_dates']
    if dates != sorted(set(dates)) or len(calendar['target_dates']) != 1000:
        raise ValueError('invalid frozen calendar')
    if not set(calendar['target_dates']).issubset(dates):
        raise ValueError('target dates outside collection calendar')
    selected = calendar['probe_dates'] if args.mode == 'probe' else dates
    if not set(selected).issubset(dates):
        raise ValueError('probe dates outside calendar')
    output = Path(args.output)
    # Each run uses a fresh directory. Never accept stale successes as fresh data.
    output.mkdir(parents=True, exist_ok=False)
    rows, errors = [], []
    started = datetime.now(timezone.utc).isoformat()
    for day in selected:
        try:
            rows.append(fetch_day(day, output / 'raw'))
            print(day, 'ok', flush=True)
        except Exception as exc:
            errors.append({'date': day, 'error': f'{type(exc).__name__}: {exc}'})
            print(day, errors[-1]['error'], flush=True)
        missing = [d for d in selected if d not in {r['date'] for r in rows}]
        status = {'code': '588000', 'mode': args.mode, 'started_at': started,
                  'calendar_sha256': hashlib.sha256(Path(args.calendar).read_bytes()).hexdigest(),
                  'expected_days': len(selected), 'verified_days': len(rows),
                  'missing_dates': missing, 'errors': errors,
                  'complete': not missing, 'rows': rows,
                  'point_in_time_publication': 'unknown; retrieval timestamp is not first publication',
                  'note': 'No interpolation, no strategy tests, no production data writes.'}
        (output / 'shares.json').write_text(json.dumps(status, ensure_ascii=False, indent=2))
        # A failed source probe is a failed run, not an empty successful data set.
        if errors:
            return 1
        time.sleep(0.35)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
