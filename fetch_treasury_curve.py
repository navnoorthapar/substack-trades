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

import argparse
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

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
# A published trading year is never this short; a shorter document means the
# feed answered with an error page or a partial response.
MIN_ENTRIES_PER_YEAR = 200


def _fetch_year(year: int) -> bytes:
    url = (
        f'{SOURCE["feed_url"]}&field_tdr_date_value={year}'
    )
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
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


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--years', nargs='+', type=int, required=True,
                        help='calendar years to fetch, e.g. --years 2025 2026')
    parser.add_argument('--output', type=Path, default=Path(DATASET_NAME))
    parser.add_argument('--current-year', type=int, required=True,
                        help='the in-progress year, which may be short')
    parser.add_argument('--merge', type=Path,
                        help='existing dataset to merge into, so refreshing '
                             'recent years never drops earlier history')
    args = parser.parse_args(argv)

    try:
        observations: Dict[str, Dict[str, float]] = {}
        if args.merge is not None and args.merge.exists():
            existing = load_curve_dataset(args.merge)
            observations.update({
                day: {tenor: float(row[tenor]) for tenor in TENORS}
                for day, row in existing['observations'].items()
            })
        observations.update(fetch_years(sorted(set(args.years)), args.current_year))
        dataset = build_dataset(observations)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        print(f'Treasury curve refresh failed: {error}', file=sys.stderr)
        return 1

    args.output.write_text(
        json.dumps(dataset, ensure_ascii=False, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )
    print(
        f'Treasury curve: {dataset["observation_count"]} trading days '
        f'{dataset["first_date"]} to {dataset["last_date"]}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
