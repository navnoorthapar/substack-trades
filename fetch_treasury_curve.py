#!/usr/bin/env python3
"""Fetch the official Daily Treasury Par Yield Curve Rates into a tracked file.

The build must be reproducible, so the curve is fetched here and committed
rather than read from the network while rendering. Treasury publishes one XML
document per calendar year, keyed by the tenor field names it documents, and
requires no API key.

The write is transactional against the existing dataset: a year that returns a
short or unparseable document leaves the tracked series untouched instead of
truncating it, because a silently shortened rate history would mislabel the
research record rather than fail.
"""

from datetime import datetime, timezone
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from treasury_curve import (
    DATASET_NAME,
    SCHEMA_VERSION,
    SOURCE,
    TENORS,
    load_curve_dataset,
    validate_curve_dataset,
)

ATOM = '{http://www.w3.org/2005/Atom}'
META = '{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}'
DATA = '{http://schemas.microsoft.com/ado/2007/08/dataservices}'

# Treasury's documented field names for the tenors this project tracks.
TENOR_FIELDS: Tuple[Tuple[str, str], ...] = (
    ('m3', 'BC_3MONTH'),
    ('y2', 'BC_2YEAR'),
    ('y5', 'BC_5YEAR'),
    ('y10', 'BC_10YEAR'),
    ('y30', 'BC_30YEAR'),
)
REQUEST_TIMEOUT = 60
USER_AGENT = 'navnoor-research-terminal/1.0 (+treasury par yield curve)'
TRACKED_DATASET_PATH = Path(__file__).resolve().parent / DATASET_NAME
OFFICIAL_FEED_URL = (
    'https://home.treasury.gov/resource-center/data-chart-center/'
    'interest-rates/pages/xml?data=daily_treasury_yield_curve'
)
# A published trading year is never this short; a shorter document means the
# feed answered with an error page or a partial response.
MIN_ENTRIES_PER_YEAR = 200


if SOURCE['feed_url'] != OFFICIAL_FEED_URL:
    raise RuntimeError('Treasury source contract does not match the fetch allowlist')


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects before urllib can issue a second network request."""

    def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str) -> Optional[urllib.request.Request]:
        del req, fp, msg, headers, newurl
        raise ValueError(
            f'Treasury feed refused HTTP {code} redirect; expected the exact official URL'
        )


TREASURY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _RejectRedirects(),
)


def _feed_url_for_year(year: int) -> str:
    """Return the one allowlisted official URL for an internally chosen year."""
    if type(year) is not int or not 1000 <= year <= 9999:
        raise ValueError('Treasury feed year must be a four-digit integer')
    year_text = str(year)
    if len(year_text) != 4 or not year_text.isdecimal():
        raise ValueError('Treasury feed year must contain exactly four decimal digits')
    return f'{OFFICIAL_FEED_URL}&field_tdr_date_value={year_text}'


def _fetch_year(year: int) -> bytes:
    url = _feed_url_for_year(year)
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with TREASURY_OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
        if response.geturl() != url:
            raise ValueError(
                f'treasury feed response URL did not match the official URL for {year}'
            )
        if response.status != 200:
            raise ValueError(f'treasury feed returned HTTP {response.status} for {year}')
        return bytes(response.read())


def parse_year(document: bytes, year: int, *, partial_year: bool = False) -> Dict[str, Dict[str, float]]:
    """Parse one Treasury year document into ordered daily observations."""
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as error:
        raise ValueError(f'treasury feed for {year} is not parseable XML') from error

    rows: Dict[str, Dict[str, float]] = {}
    for entry in root.iter(f'{ATOM}entry'):
        properties = entry.find(f'.//{META}properties')
        if properties is None:
            continue
        date_node = properties.find(f'{DATA}NEW_DATE')
        raw_date = (date_node.text or '').strip() if date_node is not None else ''
        if not raw_date:
            continue
        day = raw_date[:10]
        if not day.startswith(str(year)):
            continue
        row: Dict[str, float] = {}
        for key, field in TENOR_FIELDS:
            node = properties.find(f'{DATA}{field}')
            text = (node.text or '').strip() if node is not None else ''
            if not text or text == 'N/A':
                break
            try:
                row[key] = float(text)
            except ValueError:
                break
        # A day missing any tracked tenor is dropped whole: a partial curve
        # would silently change what a slope means.
        if len(row) == len(TENOR_FIELDS):
            rows[day] = {key: row[key] for key in TENORS}
    if not partial_year and len(rows) < MIN_ENTRIES_PER_YEAR:
        raise ValueError(
            f'treasury feed for {year} returned {len(rows)} usable days, '
            f'fewer than the {MIN_ENTRIES_PER_YEAR} a published year carries'
        )
    if not rows:
        raise ValueError(f'treasury feed for {year} returned no usable days')
    return rows


def build_dataset(observations: Mapping[str, Mapping[str, float]]) -> Dict[str, object]:
    ordered = {day: dict(observations[day]) for day in sorted(observations)}
    days: List[str] = list(ordered.keys())
    dataset = {
        'schema_version': SCHEMA_VERSION,
        'source': dict(SOURCE),
        'observation_count': len(days),
        'first_date': days[0],
        'last_date': days[-1],
        'observations': ordered,
    }
    return validate_curve_dataset(dataset)


def fetch_years(years: Sequence[int], current_year: int) -> Dict[str, Dict[str, float]]:
    observations: Dict[str, Dict[str, float]] = {}
    for year in years:
        document = _fetch_year(year)
        observations.update(
            parse_year(document, year, partial_year=year >= current_year)
        )
    return observations


def main() -> int:
    try:
        current_year = datetime.now(timezone.utc).year
        existing = load_curve_dataset(TRACKED_DATASET_PATH)
        observations: Dict[str, Dict[str, float]] = {
            day: {tenor: float(row[tenor]) for tenor in TENORS}
            for day, row in existing['observations'].items()
        }
        observations.update(fetch_years(
            (current_year - 1, current_year),
            current_year,
        ))
        dataset = build_dataset(observations)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        print(f'Treasury curve refresh failed: {error}', file=sys.stderr)
        return 1

    sys.stdout.write(
        json.dumps(dataset, ensure_ascii=False, separators=(',', ':')) + '\n'
    )
    print(
        f'Treasury curve: {dataset["observation_count"]} trading days '
        f'{dataset["first_date"]} to {dataset["last_date"]}',
        file=sys.stderr,
    )
    return 0


def cli(argv: Sequence[str]) -> int:
    """Expose no data, path, or network controls to the command line."""
    if argv:
        print('fetch_treasury_curve.py accepts no arguments', file=sys.stderr)
        return 2
    return main()


if __name__ == '__main__':
    raise SystemExit(cli(sys.argv[1:]))
