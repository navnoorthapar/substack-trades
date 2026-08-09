#!/usr/bin/env python3
"""Validate and read the tracked U.S. Treasury par yield curve dataset.

The research record spans a stretch of rate history, and a reader deciding how
to express a view wants to know what the curve looked like when a comparable
position was argued. That question is only answerable from an official series,
so this module owns the contract for one: the Daily Treasury Par Yield Curve
Rates published by the U.S. Department of the Treasury.

Two rules keep the resulting labels honest. Every reported value is a published
observation, never an interpolation: a publication date that falls on a weekend
or holiday resolves to the most recent trading day at or before it, and that
as-of date travels with the value. And the bands are quantiles of the record
being shown rather than absolute regime claims, because this corpus never saw
an inverted curve -- calling any part of it "steep" in the abstract would
assert a distinction the data does not contain.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

SCHEMA_VERSION = 1
DATASET_NAME = 'treasury_curve.json'
TENORS: Tuple[str, ...] = ('m3', 'y2', 'y5', 'y10', 'y30')
# Treasury has never published a par yield outside this range, and a value
# beyond it means the feed or the parse is wrong rather than that rates moved.
MIN_YIELD = -5.0
MAX_YIELD = 25.0
DATE_LENGTH = 10

SOURCE = {
    'name': 'U.S. Treasury Daily Treasury Par Yield Curve Rates',
    'page_url': (
        'https://home.treasury.gov/resource-center/data-chart-center/'
        'interest-rates/TextView?type=daily_treasury_yield_curve'
    ),
    'feed_url': (
        'https://home.treasury.gov/resource-center/data-chart-center/'
        'interest-rates/pages/xml?data=daily_treasury_yield_curve'
    ),
}

PathInput = Union[str, Path]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_iso_day(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != DATE_LENGTH:
        return False
    if value[4] != '-' or value[7] != '-':
        return False
    year, month, day = value[:4], value[5:7], value[8:10]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return False
    return '01' <= month <= '12' and '01' <= day <= '31'


def validate_curve_dataset(dataset: Mapping[str, Any]) -> Dict[str, Any]:
    """Fail closed unless the dataset is a complete, ordered official series."""
    _require(isinstance(dataset, Mapping), 'treasury curve dataset must be an object')
    _require(dataset.get('schema_version') == SCHEMA_VERSION,
             'treasury curve dataset has an unsupported schema_version')
    source = dataset.get('source')
    if not isinstance(source, Mapping):
        raise ValueError('treasury curve dataset must name its source')
    for field in ('name', 'page_url', 'feed_url'):
        _require(source.get(field) == SOURCE[field],
                 f'treasury curve source {field} does not match the official feed')

    observations = dataset.get('observations')
    if not isinstance(observations, Mapping) or not observations:
        raise ValueError('treasury curve dataset must carry observations')
    days = list(observations.keys())
    _require(all(_is_iso_day(day) for day in days),
             'treasury curve observation keys must be ISO calendar days')
    _require(days == sorted(days),
             'treasury curve observations must be stored in calendar order')
    _require(len(set(days)) == len(days),
             'treasury curve observations must not repeat a day')

    for day in days:
        row = observations[day]
        _require(isinstance(row, Mapping), f'treasury curve row {day} must be an object')
        _require(tuple(row.keys()) == TENORS,
                 f'treasury curve row {day} must carry exactly the tracked tenors')
        for tenor in TENORS:
            value = row[tenor]
            _require(isinstance(value, (int, float)) and not isinstance(value, bool),
                     f'treasury curve {day} {tenor} must be a number')
            _require(MIN_YIELD <= float(value) <= MAX_YIELD,
                     f'treasury curve {day} {tenor} is outside the published range')

    _require(dataset.get('observation_count') == len(days),
             'treasury curve observation_count does not match the series')
    _require(dataset.get('first_date') == days[0],
             'treasury curve first_date does not match the series')
    _require(dataset.get('last_date') == days[-1],
             'treasury curve last_date does not match the series')
    return dict(dataset)


def load_curve_dataset(path: PathInput) -> Dict[str, Any]:
    """Read and validate the tracked dataset."""
    text = Path(path).read_text(encoding='utf-8')
    try:
        dataset = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError('treasury curve dataset is not valid JSON') from error
    return validate_curve_dataset(dataset)


def curve_as_of(dataset: Mapping[str, Any], day: str) -> Optional[Dict[str, Any]]:
    """Return the published curve on the latest trading day at or before day.

    Returns None when the series starts after the requested day, because
    carrying a later observation backwards would invent a reading.
    """
    _require(_is_iso_day(day), 'curve lookup needs an ISO calendar day')
    observations = dataset['observations']
    resolved: Optional[str] = None
    for candidate in observations:
        if candidate <= day:
            resolved = candidate
        else:
            break
    if resolved is None:
        return None
    row = observations[resolved]
    return {
        'as_of': resolved,
        'm3': float(row['m3']),
        'y2': float(row['y2']),
        'y5': float(row['y5']),
        'y10': float(row['y10']),
        'y30': float(row['y30']),
        'slope': round(float(row['y10']) - float(row['y2']), 2),
    }


def tercile_cuts(values: Sequence[float]) -> Tuple[float, float]:
    """Return the two value thresholds that split values into thirds."""
    _require(bool(values), 'tercile cuts need at least one value')
    ordered = sorted(float(value) for value in values)
    last = len(ordered) - 1
    return ordered[last // 3], ordered[(2 * last) // 3]


def band_for(value: float, cuts: Tuple[float, float]) -> str:
    """Classify a value against tercile thresholds of the same record."""
    lower, upper = cuts
    if value <= lower:
        return 'low'
    if value > upper:
        return 'high'
    return 'mid'


def build_rate_context(
        dataset: Mapping[str, Any],
        days: Sequence[str],
) -> Dict[str, Any]:
    """Return the curve reading for each requested day plus record-wide bands.

    Days with no published observation at or before them are omitted rather
    than filled, so a caller can tell coverage from absence.
    """
    resolved: Dict[str, Dict[str, Any]] = {}
    for day in sorted(set(days)):
        reading = curve_as_of(dataset, day)
        if reading is not None:
            resolved[day] = reading
    if not resolved:
        return {
            'schema_version': SCHEMA_VERSION,
            'source': dict(SOURCE),
            'thresholds': {},
            'days': {},
        }
    slope_cuts = tercile_cuts([row['slope'] for row in resolved.values()])
    level_cuts = tercile_cuts([row['y10'] for row in resolved.values()])
    days_out: Dict[str, List[Any]] = {}
    for day, row in resolved.items():
        days_out[day] = [
            row['as_of'], row['y2'], row['y10'], row['y30'],
            band_for(row['slope'], slope_cuts),
            band_for(row['y10'], level_cuts),
        ]
    return {
        'schema_version': SCHEMA_VERSION,
        'source': dict(SOURCE),
        'thresholds': {
            'slope': [round(slope_cuts[0], 2), round(slope_cuts[1], 2)],
            'level': [round(level_cuts[0], 2), round(level_cuts[1], 2)],
        },
        'days': days_out,
    }
