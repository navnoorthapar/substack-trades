#!/usr/bin/env python3
"""Create the machine-verifiable provenance manifest for a published snapshot."""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from source_health import track_source_health


SCHEMA_VERSION = 2
CONTENT_SOURCES = ('substack', 'medium')
REGISTRY_SOURCES = ('patreon', 'fxempire')
SOURCES = CONTENT_SOURCES + REGISTRY_SOURCES
SUCCESS_STATUSES = {'ok', 'degraded'}
MEDIUM_BRIDGE_MODE = 'operator_reviewed_profile_bridge_plus_current_rss'
MEDIUM_BRIDGE_SURFACE = 'operator-reviewed-direct-public-profile-sequence'
MEDIUM_BRIDGE_PROFILE_URL = 'https://medium.com/@navnoorbawa'
MEDIUM_BRIDGE_PROVENANCE_KEYS = frozenset((
    'surface', 'profile_url', 'reviewed_at', 'expires_at', 'rss_window_ids',
    'previous_history_prefix_ids',
))
MEDIUM_BRIDGE_MAX_LIFETIME = timedelta(days=3)
MEDIUM_ID_RE = re.compile(r'^[0-9a-f]{12}$')
UTC_SECOND_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


def load_json(path, label):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except Exception as exc:
        raise ValueError(f'{label} is not valid JSON: {exc}') from exc


def data_checksum(article_bytes, observation_bytes):
    """Hash the exact deployed input bytes, separated unambiguously by NUL."""
    digest = hashlib.sha256()
    digest.update(article_bytes)
    digest.update(b'\0')
    digest.update(observation_bytes)
    return digest.hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def publication_instant(value):
    if len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('publication timestamp has no timezone')
    return parsed


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _bridge_utc_second(value, label):
    _require(
        isinstance(value, str) and UTC_SECOND_RE.fullmatch(value),
        f'{label} must be a canonical UTC-second instant',
    )
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        raise ValueError(f'{label} must be a valid UTC instant') from None


def validated_medium_bridge_provenance(value, source_checked_at):
    """Return an isolated exact copy of reviewed Medium bridge provenance."""
    _require(
        isinstance(value, dict)
        and set(value) == MEDIUM_BRIDGE_PROVENANCE_KEYS,
        'Medium reviewed-profile provenance does not match the exact schema',
    )
    _require(
        value.get('surface') == MEDIUM_BRIDGE_SURFACE
        and value.get('profile_url') == MEDIUM_BRIDGE_PROFILE_URL,
        'Medium reviewed-profile provenance has the wrong source surface',
    )
    reviewed_at = _bridge_utc_second(
        value.get('reviewed_at'),
        'Medium reviewed-profile reviewed_at',
    )
    expires_at = _bridge_utc_second(
        value.get('expires_at'),
        'Medium reviewed-profile expires_at',
    )
    _require(
        reviewed_at < expires_at
        and expires_at - reviewed_at <= MEDIUM_BRIDGE_MAX_LIFETIME,
        'Medium reviewed-profile provenance has an invalid review lifetime',
    )
    try:
        source_checked = datetime.fromisoformat(
            source_checked_at.replace('Z', '+00:00')
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError(
            'Medium reviewed-profile source checked_at is invalid'
        ) from None
    _require(
        source_checked.tzinfo is not None,
        'Medium reviewed-profile source checked_at has no timezone',
    )
    source_checked = source_checked.astimezone(timezone.utc)
    _require(
        reviewed_at <= source_checked <= expires_at,
        'Medium reviewed-profile source check is outside the review window',
    )

    rss_ids = value.get('rss_window_ids')
    history_ids = value.get('previous_history_prefix_ids')
    for ids, expected_count, label in (
            (rss_ids, 10, 'RSS window'),
            (history_ids, 2, 'history prefix')):
        _require(
            isinstance(ids, list)
            and len(ids) == expected_count
            and all(
                isinstance(post_id, str) and MEDIUM_ID_RE.fullmatch(post_id)
                for post_id in ids
            )
            and len(ids) == len(set(ids)),
            f'Medium reviewed-profile {label} IDs are invalid',
        )
    _require(
        not set(rss_ids) & set(history_ids),
        'Medium reviewed-profile RSS and history IDs overlap',
    )
    return {
        'surface': value['surface'],
        'profile_url': value['profile_url'],
        'reviewed_at': value['reviewed_at'],
        'expires_at': value['expires_at'],
        'rss_window_ids': list(rss_ids),
        'previous_history_prefix_ids': list(history_ids),
    }


def _source_manifest(source, status, included_count):
    _require(isinstance(status, dict), f'{source} fetch status must be an object')
    _require(status.get('source') == source,
             f'{source} fetch status has the wrong source identity')
    raw_status = status.get('status')
    normalized_status = {
        'fresh': 'ok',
        'cached-fallback': 'degraded',
    }.get(raw_status, raw_status)
    _require(normalized_status in SUCCESS_STATUSES,
             f'{source} fetch did not complete successfully')
    mode = status.get('mode') or {
        'fresh': 'public_metadata_api',
        'cached-fallback': 'cached_registry',
    }.get(raw_status)
    checked_at = status.get('checked_at')
    newest = status.get('newest')
    fetched_count = status.get('fetched_count', status.get('published_count'))
    published_count = status.get('published_count')
    _require(isinstance(mode, str) and mode.strip(),
             f'{source} fetch status has no mode')
    _require(isinstance(checked_at, str) and checked_at,
             f'{source} fetch status has no checked_at timestamp')
    _require(isinstance(newest, str) and newest,
             f'{source} fetch status has no newest timestamp')
    _require(type(fetched_count) is int and fetched_count >= 0,
             f'{source} fetched_count must be a non-negative integer')
    _require(type(published_count) is int and published_count >= included_count,
             f'{source} published_count is smaller than its included article count')
    item = {
        'checked_at': checked_at,
        'status': normalized_status,
        'mode': mode,
        'published_count': published_count,
        'fetched_count': fetched_count,
        'included_count': included_count,
        'newest': newest,
    }
    if mode == MEDIUM_BRIDGE_MODE:
        _require(
            source == 'medium' and normalized_status == 'ok',
            'reviewed-profile bridge mode is valid only for healthy Medium',
        )
        _require(
            'provenance' in status,
            'Medium reviewed-profile mode has no provenance',
        )
        item['provenance'] = validated_medium_bridge_provenance(
            status['provenance'], checked_at,
        )
    else:
        _require(
            'provenance' not in status,
            f'{source} fetch provenance is not valid for mode {mode}',
        )
    return item


def _registry_status(source, articles, checked_at):
    source_rows = [
        article for article in articles
        if isinstance(article, dict) and article.get('source') == source
    ]
    _require(source_rows, f'{source} registry is empty')
    newest = max(
        (str(article.get('post_date') or '') for article in source_rows),
        key=publication_instant,
    )
    return {
        'source': source,
        'checked_at': checked_at,
        'status': 'ok',
        'mode': 'manual_registry',
        'published_count': len(source_rows),
        'fetched_count': len(source_rows),
        'newest': newest,
    }


def build_manifest(
        articles,
        observations,
        statuses,
        checksum,
        checked_at=None,
        previous_manifest=None,
):
    _require(isinstance(articles, list), 'article index must be a list')
    _require(isinstance(observations, list), 'observation output must be a list')
    included = Counter(
        article.get('source') for article in articles if isinstance(article, dict)
    )
    _require(set(included).issubset(SOURCES), 'article index has an unknown source')
    resolved_checked_at = checked_at or utc_now()
    resolved_statuses = dict(statuses)
    for source in REGISTRY_SOURCES:
        if included[source] and source not in resolved_statuses:
            resolved_statuses[source] = _registry_status(
                source, articles, resolved_checked_at
            )
    raw_sources = {
        source: _source_manifest(source, resolved_statuses[source], included[source])
        for source in SOURCES if included[source]
    }
    sources = track_source_health(raw_sources, previous_manifest)
    content_articles = [
        article for article in articles
        if isinstance(article, dict)
        and article.get('content_status') != 'registry'
    ]
    registry_articles = [
        article for article in articles
        if isinstance(article, dict)
        and article.get('content_status') == 'registry'
    ]
    publication_dates = [
        article.get('post_date') for article in content_articles
        if isinstance(article.get('post_date'), str)
    ]
    catalogue_dates = [
        article.get('post_date') for article in articles
        if isinstance(article, dict) and isinstance(article.get('post_date'), str)
    ]
    latest_publication = max(
        publication_dates, key=publication_instant, default=''
    )
    catalogue_latest_publication = max(
        catalogue_dates, key=publication_instant, default=''
    )
    return {
        'schema_version': SCHEMA_VERSION,
        'checked_at': resolved_checked_at,
        'latest_publication': latest_publication,
        'catalog_latest_publication': catalogue_latest_publication,
        'article_count': len(content_articles),
        'catalog_count': len(articles),
        'registry_count': len(registry_articles),
        'observation_count': len(observations),
        'data_checksum': checksum,
        'sources': sources,
    }


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f'{path.name}.tmp'
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--articles', type=Path, required=True)
    parser.add_argument('--trades', type=Path, required=True,
                        help='extracted observations consumed by the website')
    parser.add_argument('--substack-status', type=Path, required=True)
    parser.add_argument('--medium-status', type=Path, required=True)
    parser.add_argument('--patreon-status', type=Path)
    parser.add_argument(
        '--previous-manifest',
        type=Path,
        help='prior snapshot used only to continue source-degradation streaks',
    )
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--checked-at', help='override UTC timestamp (primarily for tests)')
    args = parser.parse_args()

    try:
        article_bytes = args.articles.read_bytes()
        observation_bytes = args.trades.read_bytes()
        articles = json.loads(article_bytes)
        observations = json.loads(observation_bytes)
        statuses = {
            'substack': load_json(args.substack_status, 'Substack fetch status'),
            'medium': load_json(args.medium_status, 'Medium fetch status'),
        }
        if args.patreon_status:
            statuses['patreon'] = load_json(
                args.patreon_status, 'Patreon fetch status'
            )
        previous_manifest = (
            load_json(args.previous_manifest, 'previous snapshot manifest')
            if args.previous_manifest else None
        )
        checksum = data_checksum(article_bytes, observation_bytes)
        manifest = build_manifest(
            articles,
            observations,
            statuses,
            checksum,
            args.checked_at,
            previous_manifest,
        )
        atomic_write_json(args.output, manifest)
    except (KeyError, OSError, ValueError) as exc:
        print(f'MANIFEST FAILED: {exc}', file=sys.stderr)
        return 1

    print(
        f'Wrote snapshot manifest: {manifest["article_count"]} research articles, '
        f'{manifest["registry_count"]} registry entries, '
        f'{manifest["observation_count"]} observations, {manifest["data_checksum"]}.'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
