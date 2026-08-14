#!/usr/bin/env python3
"""Track and report publication-source degradation without blocking safe fallback.

The fetchers may deliberately publish a validated cached fallback during a source
outage.  A fresh timestamp alone must not make that fallback look healthy, so the
snapshot records when the current degraded streak began and how many consecutive
refreshes have observed it.  Older manifests without these additive fields remain
readable during the one-release migration and are treated as a first observation.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Tuple, cast


DEGRADED_SINCE_FIELD = 'degraded_since'
DEGRADED_CHECKS_FIELD = 'consecutive_degraded_checks'
PERSISTENT_DEGRADATION_HOURS = 48
PERSISTENT_DEGRADATION_AGE = timedelta(hours=PERSISTENT_DEGRADATION_HOURS)
VALID_STATUSES = {'ok', 'degraded'}


@dataclass(frozen=True)
class SourceHealthFinding:
    """One validated degraded source and the duration of its current streak."""

    source: str
    mode: str
    degraded_since: datetime
    consecutive_checks: int
    age: timedelta
    persistent: bool
    legacy_migration: bool


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_timestamp(value: Any, label: str) -> datetime:
    """Return one timezone-aware timestamp normalized to UTC."""
    _require(isinstance(value, str) and bool(value), f'{label} is not a timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{label} is not a timestamp') from exc
    _require(parsed.tzinfo is not None, f'{label} has no timezone')
    return parsed.astimezone(timezone.utc)


def degradation_state(
    source: str,
    item: Mapping[str, Any],
    *,
    allow_legacy: bool,
) -> Tuple[Optional[datetime], int, bool]:
    """Validate and return ``(since, checks, used_legacy_migration)``.

    A legacy source row has neither tracking field.  When it is degraded, its
    source check is the earliest degradation observation that can honestly be
    proved.  Supplying only one field is corruption rather than a legacy row.
    """
    status = item.get('status')
    _require(status in VALID_STATUSES, f'{source} has an invalid source status')
    has_since = DEGRADED_SINCE_FIELD in item
    has_checks = DEGRADED_CHECKS_FIELD in item
    _require(
        has_since == has_checks,
        f'{source} source-health tracking fields are incomplete',
    )
    checked_at = parse_timestamp(item.get('checked_at'), f'{source} checked_at')

    if not has_since:
        _require(allow_legacy, f'{source} has no source-health tracking fields')
        if status == 'degraded':
            return checked_at, 1, True
        return None, 0, True

    since_value = item.get(DEGRADED_SINCE_FIELD)
    checks = item.get(DEGRADED_CHECKS_FIELD)
    if status == 'ok':
        _require(
            since_value is None and checks == 0,
            f'{source} healthy status retains a degraded streak',
        )
        return None, 0, False

    since = parse_timestamp(since_value, f'{source} degraded_since')
    _require(
        type(checks) is int and checks >= 1,
        f'{source} consecutive degraded check count is invalid',
    )
    _require(
        since <= checked_at,
        f'{source} degraded_since is later than its source check',
    )
    return since, cast(int, checks), False


def track_source_health(
    sources: Mapping[str, Any],
    previous_manifest: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Mapping[str, Any]]:
    """Add deterministic degradation-streak fields to current source rows."""
    _require(isinstance(sources, Mapping) and bool(sources),
             'snapshot sources must be a non-empty object')
    if previous_manifest is None:
        previous_sources: Mapping[str, Any] = {}
    else:
        _require(isinstance(previous_manifest, Mapping),
                 'previous manifest must be an object')
        raw_previous_sources = previous_manifest.get('sources')
        if not isinstance(raw_previous_sources, Mapping):
            raise ValueError('previous manifest sources must be an object')
        previous_sources = raw_previous_sources

    tracked = {}
    for source, raw_item in sources.items():
        _require(isinstance(source, str) and bool(source),
                 'snapshot source name is invalid')
        _require(isinstance(raw_item, Mapping),
                 f'{source} source status must be an object')
        item = dict(raw_item)
        status = item.get('status')
        _require(status in VALID_STATUSES, f'{source} has an invalid source status')
        current_checked = parse_timestamp(item.get('checked_at'),
                                          f'{source} checked_at')

        if status == 'ok':
            item[DEGRADED_SINCE_FIELD] = None
            item[DEGRADED_CHECKS_FIELD] = 0
            tracked[source] = item
            continue

        previous_item = previous_sources.get(source)
        if isinstance(previous_item, Mapping) and previous_item.get('status') == 'degraded':
            previous_since, previous_checks, _ = degradation_state(
                source,
                previous_item,
                allow_legacy=True,
            )
            previous_checked = parse_timestamp(
                previous_item.get('checked_at'), f'previous {source} checked_at'
            )
            _require(
                previous_checked <= current_checked,
                f'{source} source check moved backwards',
            )
            if previous_since is None:
                raise ValueError(f'previous {source} degraded streak is missing')
            degraded_since = previous_since
            degraded_checks = previous_checks + 1
        else:
            if isinstance(previous_item, Mapping):
                degradation_state(source, previous_item, allow_legacy=True)
            degraded_since = current_checked
            degraded_checks = 1

        item[DEGRADED_SINCE_FIELD] = (
            degraded_since.isoformat(timespec='seconds').replace('+00:00', 'Z')
        )
        item[DEGRADED_CHECKS_FIELD] = degraded_checks
        tracked[source] = item
    return tracked


def evaluate_manifest(
    manifest: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> List[SourceHealthFinding]:
    """Return validated degraded-source findings ordered by source name."""
    _require(isinstance(manifest, Mapping), 'snapshot manifest must be an object')
    sources = manifest.get('sources')
    if not isinstance(sources, Mapping) or not sources:
        raise ValueError('snapshot manifest sources must be a non-empty object')
    reference = now or datetime.now(timezone.utc)
    _require(reference.tzinfo is not None, 'source-health clock must have a timezone')
    reference = reference.astimezone(timezone.utc)

    findings = []
    for source in sorted(sources):
        item = sources[source]
        _require(isinstance(item, Mapping),
                 f'{source} source status must be an object')
        since, checks, legacy = degradation_state(
            source,
            item,
            allow_legacy=True,
        )
        if item.get('status') != 'degraded':
            continue
        if since is None:
            raise ValueError(f'{source} degraded streak is missing')
        _require(reference >= since,
                 f'{source} degraded_since is implausibly in the future')
        age = reference - since
        findings.append(SourceHealthFinding(
            source=source,
            mode=str(item.get('mode') or 'mode unknown'),
            degraded_since=since,
            consecutive_checks=checks,
            age=age,
            persistent=age >= PERSISTENT_DEGRADATION_AGE,
            legacy_migration=legacy,
        ))
    return findings


def _age_label(age: timedelta) -> str:
    hours = max(0.0, age.total_seconds() / 3600)
    return f'{hours:.1f}h'


def _message(finding: SourceHealthFinding) -> str:
    stamp = finding.degraded_since.isoformat(timespec='seconds').replace('+00:00', 'Z')
    migration = '; legacy manifest treated as first observation' \
        if finding.legacy_migration else ''
    return (
        f'{finding.source} is degraded in {finding.mode} mode for '
        f'{_age_label(finding.age)} across {finding.consecutive_checks} consecutive '
        f'check(s), since {stamp}{migration}'
    )


def report_manifest(
    manifest: Mapping[str, Any],
    *,
    policy: str,
    now: Optional[datetime] = None,
) -> int:
    """Print source health and return the policy-specific process status."""
    _require(policy in {'publish', 'status', 'watchdog'},
             'source-health policy is invalid')
    findings = evaluate_manifest(manifest, now=now)
    if not findings:
        print('Source health: all publication sources are healthy.')
        return 0

    persistent = [finding for finding in findings if finding.persistent]
    for finding in findings:
        message = _message(finding)
        if policy == 'watchdog':
            level = 'error' if finding.persistent else 'warning'
            title = 'Persistent source degradation' \
                if finding.persistent else 'Transient source degradation'
            print(f'::{level} title={title}::{message}')
        else:
            label = 'PERSISTENT' if finding.persistent else 'DEGRADED'
            print(f'Source health {label}: {message}')

    if policy == 'publish':
        print(
            'Validated fallback data remains publishable; monitoring will fail '
            f'after {PERSISTENT_DEGRADATION_HOURS} hours of continuous degradation.'
        )
        return 0
    if policy == 'status':
        return 1
    return 1 if persistent else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Evaluate tracked publication-source degradation.',
    )
    parser.add_argument(
        '--policy',
        choices=('publish', 'status', 'watchdog'),
        required=True,
    )
    parser.add_argument('--now', help='override the UTC evaluation clock for tests')
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = json.load(sys.stdin)
        now = parse_timestamp(args.now, 'source-health clock') if args.now else None
        return report_manifest(manifest, policy=args.policy, now=now)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if args.policy == 'watchdog':
            print(f'::error title=Source health unavailable::{exc}')
        else:
            print(f'SOURCE HEALTH FAILED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
