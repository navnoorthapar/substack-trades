#!/usr/bin/env python3
"""Create the machine-verifiable provenance manifest for a published snapshot."""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2
CONTENT_SOURCES = ('substack', 'medium')
REGISTRY_SOURCES = ('patreon', 'fxempire')
SOURCES = CONTENT_SOURCES + REGISTRY_SOURCES
SUCCESS_STATUSES = {'ok', 'degraded'}


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
    return {
        'checked_at': checked_at,
        'status': normalized_status,
        'mode': mode,
        'published_count': published_count,
        'fetched_count': fetched_count,
        'included_count': included_count,
        'newest': newest,
    }


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


def build_manifest(articles, observations, statuses, checksum, checked_at=None):
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
    sources = {
        source: _source_manifest(source, resolved_statuses[source], included[source])
        for source in SOURCES if included[source]
    }
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
        checksum = data_checksum(article_bytes, observation_bytes)
        manifest = build_manifest(
            articles, observations, statuses, checksum, args.checked_at
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
