#!/usr/bin/env python3
"""Validate one complete, immutable Navnoor Research Terminal release.

This module is the shared artifact policy for local release checks and GitHub
Actions.  It validates a freshly built site against the exact tracked article,
observation, and snapshot inputs, then returns the six fingerprints used by the
post-deploy smoke test.
"""
from __future__ import annotations

import argparse
import binascii
import gzip
import hashlib
import html as html_lib
import json
import re
import struct
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

from article_briefs import is_boilerplate_text
from client_article_contract import (
    ARTICLE_WIRE_SCHEMA_VERSION,
    hydrate_client_article,
)
from data_contract import (
    DATA_ENDPOINT_NAMES,
    data_bundle_checksum,
    validate_data_layer,
)
from extract_trades import has_negated_trade_signal
from share_cards import render_article_stub, render_share_card
from smoke_test_site import (
    share_proof_bundle_checksum,
    snapshot_checksum,
    support_bundle_checksum,
    validate_html,
)
from validate_inline_scripts import validate_inline_scripts


CORE_ASSETS = frozenset((
    'index.html',
    'article_briefs.json',
    'observations.json',
    'robots.txt',
    'sitemap.xml',
    'site.webmanifest',
    'favicon.svg',
    'og.jpg',
))
EXPECTED_DIRECTORIES = frozenset(('a', 'cards', 'data'))
FINGERPRINT_KEYS = (
    'html_sha256',
    'brief_sha256',
    'observation_sha256',
    'support_sha256',
    'data_sha256',
    'share_sha256',
)
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
PNG_HEADER = (1200, 630, 8, 3, 0, 0, 0)
ARTICLES_RE = re.compile(r'const ARTICLES = (.*?);\n')
ARTICLE_WIRE_SCHEMA_RE = re.compile(
    r'(?m)^\s*const\s+ARTICLE_WIRE_SCHEMA_VERSION\s*=\s*([0-9]+)\s*;\s*$',
)
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
SITE_URL = 'https://navnoorthapar.github.io/substack-trades/'
LIGHT_THEME_BG = '#f2e8dd'
DARK_THEME_BG = '#050607'
INLINE_BRIEF_COUNT = 12
MANAGER_ALIAS_LABELS = {
    'citadel': 'Citadel / Ken Griffin',
    'griffin / citadel': 'Citadel / Ken Griffin',
    'griffin': 'Citadel / Ken Griffin',
    'bridgewater': 'Bridgewater / Ray Dalio',
    'dalio / bridgewater': 'Bridgewater / Ray Dalio',
    'dalio': 'Bridgewater / Ray Dalio',
    'ackman': 'Pershing Square / Bill Ackman',
    'ackman / pershing': 'Pershing Square / Bill Ackman',
    'druckenmiller': 'Duquesne / Stanley Druckenmiller',
    'duquesne': 'Duquesne / Stanley Druckenmiller',
    'point72': 'Point72 / Steve Cohen',
    'cohen / point72': 'Point72 / Steve Cohen',
    'tiger': 'Tiger Management / Julian Robertson',
    'robertson / tiger': 'Tiger Management / Julian Robertson',
    'third point': 'Third Point / Dan Loeb',
    'loeb / third point': 'Third Point / Dan Loeb',
    'brevan howard': 'Brevan Howard / Alan Howard',
    'howard': 'Brevan Howard / Alan Howard',
    'einhorn / greenlight': 'Greenlight / David Einhorn',
    'einhorn': 'Greenlight / David Einhorn',
}
REFERENCE_LINE_RE = re.compile(
    r"^(?:https?://|[^.!?\n]{0,120}(?:—|–|:)[ \t]*https?://)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReleasePolicy:
    """Reviewed production byte limits for one generated Pages artifact."""

    index_min_bytes: int = 100_000
    index_max_bytes: int = 900_000
    index_gzip_max_bytes: int = 250_000
    brief_min_bytes: int = 100_000
    brief_max_bytes: int = 800_000
    observation_min_bytes: int = 500_000
    observation_max_bytes: int = 1_500_000
    data_max_bytes: int = 4_000_000
    search_max_bytes_exclusive: int = 500_000
    cards_max_bytes: int = 10_000_000
    card_max_bytes: int = 100_000
    stubs_max_bytes: int = 5_000_000
    total_max_bytes: int = 20_000_000


PRODUCTION_POLICY = ReleasePolicy()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f'non-standard JSON constant {value!r}')


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def _decode_json(payload: bytes, label: str) -> Any:
    try:
        text = payload.decode('utf-8')
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f'{label} is not strict UTF-8 JSON: {exc}') from exc


def _read_bytes(path: Path, label: str) -> bytes:
    _require(path.is_file() and not path.is_symlink(),
             f'{label} is missing or not a regular file')
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f'{label} could not be read: {exc}') from exc


def _read_json(path: Path, label: str) -> Any:
    return _decode_json(_read_bytes(path, label), label)


def _canonical_url_identity(value: Any) -> str:
    raw = str(value or '').strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.casefold()
    host = (parts.hostname or '').casefold()
    port = parts.port
    if port and not (
        (scheme == 'https' and port == 443)
        or (scheme == 'http' and port == 80)
    ):
        host = f'{host}:{port}'
    path = parts.path.rstrip('/') or '/'
    return urlunsplit((scheme, host, path, parts.query, ''))


def _display_url(value: Any) -> str:
    return str(value or '').strip().rstrip('/')


def _clean_date(value: Any) -> str:
    """Mirror the builder's sortable calendar-date projection."""
    date = str(value or '')[:10]
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return '1970-01-01'
    return date


def _clean_publication_time(value: Any) -> Tuple[str, str]:
    """Mirror the builder's exact publication value and precision."""
    raw = str(value or '').strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        datetime.strptime(raw, '%Y-%m-%d')
        return raw, 'day'
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'invalid source publication timestamp: {raw!r}') from exc
    _require(parsed.tzinfo is not None,
             f'source publication timestamp has no timezone: {raw!r}')
    parsed.astimezone(timezone.utc)
    return raw, 'instant'


def _clean_source(value: Any, url: str) -> str:
    """Mirror the builder's content-source normalization."""
    source = str(value or '').strip().casefold()
    if source in {'substack', 'medium'}:
        return source
    return 'medium' if 'medium.com/' in url.casefold() else 'substack'


def _stable_article_id(url: Any) -> str:
    identity = _canonical_url_identity(url)
    return f'a_{hashlib.sha256(identity.encode("utf-8")).hexdigest()[:14]}'


def _source_client_metadata(
        source: Mapping[str, Any],
        first_observation: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return the exact source-backed metadata projected by build_site.py."""
    url = _display_url(source.get('url'))
    publication_value = (
        source.get('post_date') or first_observation.get('article_date')
    )
    published_at, publication_precision = _clean_publication_time(
        publication_value,
    )
    subtitle = str(source.get('subtitle') or '').strip()
    if is_boilerplate_text(subtitle):
        subtitle = ''
    try:
        wordcount = max(0, int(source.get('wordcount') or 0))
    except (TypeError, ValueError):
        wordcount = 0
    read_minutes = max(1, round(wordcount / 220)) if wordcount else 0
    return {
        'title': (
            source.get('title')
            or first_observation.get('article_title')
            or url
        ),
        'subtitle': subtitle,
        'date': _clean_date(publication_value),
        'published_at': published_at,
        'publication_precision': publication_precision,
        'source': _clean_source(source.get('source'), url),
        'alternate_urls': source.get('alternate_urls') or {},
        'wordcount': wordcount,
        'read_minutes': read_minutes,
        'content_status': source.get('content_status') or 'full',
        'body_revision_status': (
            source.get('body_revision_status') or 'current'
        ),
        'source_updated_at': (
            source.get('source_updated_at')
            if isinstance(source.get('source_updated_at'), str)
            else ''
        ),
        'observed_source_updated_at': (
            source.get('observed_source_updated_at')
            if isinstance(source.get('observed_source_updated_at'), str)
            else ''
        ),
    }


def _normalize_identity_text(value: Any) -> str:
    normalized = unicodedata.normalize('NFKC', str(value or ''))
    return ' '.join(normalized.split()).casefold()


def _canonical_manager_label(value: Any) -> Tuple[str, str]:
    raw = ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())
    if not raw:
        return '', ''
    return raw, MANAGER_ALIAS_LABELS.get(_normalize_identity_text(raw), raw)


def _source_manager_labels(
        source_observations: Sequence[Mapping[str, Any]],
) -> Dict[str, str]:
    manager_variants: Dict[str, Counter[str]] = {}
    for observation in source_observations:
        _, canonical_manager = _canonical_manager_label(
            observation.get('fund_name_if_mentioned'),
        )
        if canonical_manager:
            key = _normalize_identity_text(canonical_manager)
            manager_variants.setdefault(key, Counter())[canonical_manager] += 1
    return {
        key: sorted(
            variants,
            key=lambda label: (
                -variants[label],
                label.islower(),
                label.casefold(),
                label,
            ),
        )[0]
        for key, variants in manager_variants.items()
    }


def _source_publication_sort_key(
        url: str,
        metadata: Mapping[str, Any],
) -> Tuple[datetime, str]:
    value = str(metadata['published_at'])
    if metadata['publication_precision'] == 'day':
        instant = datetime.strptime(value, '%Y-%m-%d').replace(
            tzinfo=timezone.utc,
        )
    else:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        instant = instant.astimezone(timezone.utc)
    return instant, _canonical_url_identity(url)


def _source_observation_metadata(
        description: Any,
        direction: str,
        instruments: Sequence[str],
        underlying: Any,
        thesis: Any,
        quant: Any,
        description_truncated: Any = False,
) -> Dict[str, Any]:
    """Mirror the builder's documentation coverage and review flags."""
    text = str(description or '').strip()
    fields = {
        'market': any(
            value and value != 'unspecified' for value in instruments
        ),
        'stance': bool(direction and direction != 'unspecified'),
        'underlying': bool(str(underlying or '').strip()),
        'thesis': bool(str(thesis or '').strip()),
        'numeric': bool(str(quant or '').strip()),
    }
    return {
        'documentation_fields': fields,
        'documentation_score': sum(fields.values()),
        'reference_line': bool(REFERENCE_LINE_RE.search(text)),
        'negation_risk': has_negated_trade_signal(text),
        'description_truncated': bool(description_truncated),
    }


def _client_span(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping) or not value.get('text'):
        return None
    return {
        'text': value['text'],
        'truncated': bool(value.get('truncated')),
        'start': int(value.get('start') or 0),
        'end': int(value.get('end') or 0),
        'sha256': str(value.get('sha256') or ''),
    }


def _client_brief(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            'lead': None,
            'sections': [],
            'fallback_evidence': None,
            'checkpoints': [],
        }
    sections = []
    for section in value.get('sections') or []:
        if not isinstance(section, Mapping) or not section.get('text'):
            continue
        sections.append({
            'kind': section.get('kind') or '',
            'heading': section.get('heading') or '',
            'text': section['text'],
            'truncated': bool(section.get('truncated')),
            'source_order': int(section.get('source_order') or 0),
            'start': int(section.get('start') or 0),
            'end': int(section.get('end') or 0),
            'sha256': str(section.get('sha256') or ''),
        })
    checkpoints = []
    for checkpoint in value.get('checkpoints') or []:
        if not isinstance(checkpoint, Mapping) or not checkpoint.get('text'):
            continue
        checkpoints.append({
            'date': checkpoint.get('date') or '',
            'date_label': checkpoint.get('date_label') or '',
            'text': checkpoint['text'],
            'context_kind': checkpoint.get('context_kind') or '',
            'truncated': bool(checkpoint.get('truncated')),
            'start': int(checkpoint.get('start') or 0),
            'end': int(checkpoint.get('end') or 0),
            'sha256': str(checkpoint.get('sha256') or ''),
        })
    return {
        'lead': _client_span(value.get('lead')),
        'sections': sections,
        'fallback_evidence': _client_span(value.get('fallback_evidence')),
        'checkpoints': checkpoints,
    }


def _source_derived_projection(
        source_by_url: Mapping[str, Mapping[str, Any]],
        source_observations: Sequence[Mapping[str, Any]],
) -> Tuple[
    List[str],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
]:
    """Project exact builder order, article aggregates, and observation rows."""
    observations_by_url: Dict[str, List[Mapping[str, Any]]] = {}
    for observation in source_observations:
        url = _display_url(observation.get('article_url'))
        if url:
            observations_by_url.setdefault(url, []).append(observation)

    ordered_sources: List[Tuple[str, Dict[str, Any]]] = []
    for url, source in source_by_url.items():
        article_observations = observations_by_url.get(url, [])
        first_observation = (
            article_observations[0] if article_observations else {}
        )
        ordered_sources.append((
            url,
            _source_client_metadata(source, first_observation),
        ))
    ordered_sources.sort(
        key=lambda row: _source_publication_sort_key(row[0], row[1]),
        reverse=True,
    )

    manager_labels = _source_manager_labels(source_observations)
    article_order: List[str] = []
    article_derived: Dict[str, Dict[str, Any]] = {}
    expected_observations: Dict[str, Dict[str, Any]] = {}

    for url, _ in ordered_sources:
        source = source_by_url[url]
        article_id = _stable_article_id(url)
        article_order.append(article_id)
        idea_ids: List[str] = []
        directions: Set[str] = set()
        instruments: Set[str] = set()
        underlyings: Dict[str, str] = {}
        managers: Set[str] = set()
        manager_keys: Set[str] = set()
        article_observations = observations_by_url.get(url, [])

        for observation in article_observations:
            description = str(
                observation.get('trade_description') or '',
            ).strip()
            identity_description = (
                description[:-1]
                if len(description) >= 790 and description.endswith('…')
                else description
            )
            idea_identity = (
                _canonical_url_identity(url)
                + '\0'
                + _normalize_identity_text(identity_description)
            )
            idea_id = (
                f'i_{hashlib.sha256(idea_identity.encode("utf-8")).hexdigest()[:14]}'
            )
            _require(
                idea_id not in expected_observations,
                f'source observation stable ID collision: {idea_id}',
            )
            idea_ids.append(idea_id)

            direction = str(observation.get('direction') or 'unspecified')
            idea_instruments = [
                str(value)
                for value in (
                    observation.get('instruments') or ['unspecified']
                )
                if value
            ] or ['unspecified']
            manager_raw, canonical_manager = _canonical_manager_label(
                observation.get('fund_name_if_mentioned'),
            )
            manager_key = _normalize_identity_text(canonical_manager)
            manager = manager_labels.get(manager_key, '')
            thesis = observation.get('edge_or_thesis') or ''
            quant = observation.get('any_quant_detail') or ''
            underlying = observation.get('underlying') or ''

            directions.add(direction)
            instruments.update(idea_instruments)
            for underlying_value in re.split(
                    r'\s*;\s*', str(underlying or '')):
                underlying_value = underlying_value.strip()
                if underlying_value and underlying_value not in {'—', '-'}:
                    underlying_key = _normalize_identity_text(underlying_value)
                    underlyings.setdefault(underlying_key, underlying_value)
            if manager:
                managers.add(manager)
                manager_keys.add(manager_key)

            expected_observations[idea_id] = {
                'id': idea_id,
                'article_id': article_id,
                'description': description,
                'direction': direction,
                'instruments': idea_instruments,
                'underlying': underlying,
                'thesis': thesis,
                'quant': quant,
                'outcome': observation.get('outcome_if_mentioned') or '',
                'manager': manager,
                'manager_key': manager_key,
                'manager_raw': manager_raw,
                **_source_observation_metadata(
                    description,
                    direction,
                    idea_instruments,
                    underlying,
                    thesis,
                    quant,
                    observation.get('description_truncated', False),
                ),
            }

        brief_value = _client_brief(source.get('brief'))
        brief_kinds = {
            section.get('kind')
            for section in brief_value.get('sections', [])
        }
        article_derived[article_id] = {
            'idea_ids': idea_ids,
            'trade_count': len(idea_ids),
            'directions': sorted(directions),
            'instruments': sorted(instruments),
            'underlyings': sorted(
                underlyings.values(),
                key=lambda value: (value.casefold(), value),
            ),
            'managers': sorted(
                managers,
                key=lambda value: (value.casefold(), value),
            ),
            'manager_keys': sorted(manager_keys),
            'brief_features': {
                'lead': bool(brief_value.get('lead')),
                'evidence': bool(
                    'evidence' in brief_kinds
                    or brief_value.get('fallback_evidence')
                ),
                'countercase': 'countercase' in brief_kinds,
                'falsifier': 'falsifier' in brief_kinds,
                'implementation': 'implementation' in brief_kinds,
                'mechanism': 'mechanism' in brief_kinds,
                'checkpoint_count': len(
                    brief_value.get('checkpoints') or [],
                ),
            },
            'has_quant': any(
                bool(observation.get('any_quant_detail'))
                for observation in article_observations
            ),
            'has_thesis': any(
                bool(observation.get('edge_or_thesis'))
                for observation in article_observations
            ),
            'has_outcome': any(
                bool(observation.get('outcome_if_mentioned'))
                for observation in article_observations
            ),
        }

    return article_order, article_derived, expected_observations


def _expected_assets(articles: Sequence[Mapping[str, Any]]) -> Set[str]:
    slugs: List[str] = []
    for article in articles:
        slug = article.get('slug')
        if not isinstance(slug, str) or not slug:
            raise ValueError(
                'master catalogue contains an article without a slug',
            )
        slugs.append(slug)
    _require(len(slugs) == len(set(slugs)),
             'master catalogue slugs are duplicated')
    assets = set(CORE_ASSETS)
    assets.update(f'data/{name}' for name in DATA_ENDPOINT_NAMES)
    assets.update(f'cards/{slug}.png' for slug in slugs)
    assets.update(f'a/{slug}.html' for slug in slugs)
    return assets


def _validate_artifact_tree(
        site: Path,
        articles: Sequence[Mapping[str, Any]],
) -> List[Path]:
    _require(site.is_dir() and not site.is_symlink(),
             'site artifact root is missing, not a directory, or a symlink')
    entries = list(site.rglob('*'))
    symlinks = [
        path.relative_to(site).as_posix()
        for path in entries if path.is_symlink()
    ]
    if symlinks:
        raise ValueError(
            f'site artifact contains a symbolic link: {symlinks[0]}',
        )
    temporary = [
        path.relative_to(site).as_posix()
        for path in entries if path.name.endswith('.tmp')
    ]
    if temporary:
        raise ValueError(
            f'site artifact contains a temporary path: {temporary[0]}',
        )

    files = [path for path in entries if path.is_file()]
    actual_assets = {
        path.relative_to(site).as_posix()
        for path in files
    }
    expected_assets = _expected_assets(articles)
    if actual_assets != expected_assets:
        missing = sorted(expected_assets - actual_assets)[:5]
        extra = sorted(actual_assets - expected_assets)[:5]
        raise ValueError(
            f'artifact allowlist mismatch; missing={missing}, extra={extra}',
        )
    actual_directories = {
        path.relative_to(site).as_posix()
        for path in entries if path.is_dir()
    }
    _require(
        actual_directories == set(EXPECTED_DIRECTORIES),
        'artifact directory set does not match a/, cards/, and data/',
    )
    _require(
        len(files) == 14 + 2 * len(articles),
        'artifact file count does not match its catalogue',
    )
    return files


def _validate_sizes(
        site: Path,
        files: Sequence[Path],
        policy: ReleasePolicy,
) -> Dict[str, int]:
    index_bytes = _read_bytes(site / 'index.html', 'index.html')
    brief_bytes = _read_bytes(
        site / 'article_briefs.json', 'article_briefs.json',
    )
    observation_bytes = _read_bytes(
        site / 'observations.json', 'observations.json',
    )
    gzip_bytes = len(gzip.compress(index_bytes, compresslevel=9, mtime=0))
    data_bytes = sum(
        path.stat().st_size for path in (site / 'data').iterdir()
        if path.is_file()
    )
    search_bytes = (site / 'data' / 'search_index.json').stat().st_size
    card_paths = [path for path in (site / 'cards').iterdir() if path.is_file()]
    cards_bytes = sum(path.stat().st_size for path in card_paths)
    max_card_bytes = max(
        (path.stat().st_size for path in card_paths),
        default=0,
    )
    stubs_bytes = sum(
        path.stat().st_size for path in (site / 'a').iterdir()
        if path.is_file()
    )
    total_bytes = sum(path.stat().st_size for path in files)

    _require(
        policy.index_min_bytes <= len(index_bytes) <= policy.index_max_bytes,
        f'index.html size {len(index_bytes)} is outside the '
        f'{policy.index_min_bytes}–{policy.index_max_bytes} byte policy',
    )
    _require(
        gzip_bytes <= policy.index_gzip_max_bytes,
        f'compressed index size {gzip_bytes} exceeds '
        f'{policy.index_gzip_max_bytes} bytes',
    )
    _require(
        policy.brief_min_bytes <= len(brief_bytes) <= policy.brief_max_bytes,
        f'deferred dossier size {len(brief_bytes)} is outside the '
        f'{policy.brief_min_bytes}–{policy.brief_max_bytes} byte policy',
    )
    _require(
        policy.observation_min_bytes
        <= len(observation_bytes)
        <= policy.observation_max_bytes,
        f'deferred observation size {len(observation_bytes)} is outside the '
        f'{policy.observation_min_bytes}–'
        f'{policy.observation_max_bytes} byte policy',
    )
    _require(
        data_bytes <= policy.data_max_bytes,
        f'machine-readable data size {data_bytes} exceeds '
        f'{policy.data_max_bytes} bytes',
    )
    _require(
        search_bytes < policy.search_max_bytes_exclusive,
        f'search index size {search_bytes} must remain below '
        f'{policy.search_max_bytes_exclusive} bytes',
    )
    _require(
        cards_bytes <= policy.cards_max_bytes
        and max_card_bytes <= policy.card_max_bytes,
        'share cards exceed their aggregate or per-card size policy',
    )
    _require(
        stubs_bytes <= policy.stubs_max_bytes,
        f'article stubs size {stubs_bytes} exceeds '
        f'{policy.stubs_max_bytes} bytes',
    )
    _require(
        total_bytes <= policy.total_max_bytes,
        f'site artifact size {total_bytes} exceeds '
        f'{policy.total_max_bytes} bytes',
    )
    return {
        'index': len(index_bytes),
        'index_gzip': gzip_bytes,
        'briefs': len(brief_bytes),
        'observations': len(observation_bytes),
        'data': data_bytes,
        'cards': cards_bytes,
        'max_card': max_card_bytes,
        'stubs': stubs_bytes,
        'total': total_bytes,
    }


def _validate_share_card(path: Path) -> None:
    header = _read_bytes(path, path.name)[:33]
    valid = (
        len(header) == 33
        and header[:8] == PNG_SIGNATURE
        and header[8:12] == struct.pack('>I', 13)
        and header[12:16] == b'IHDR'
        and struct.unpack('>IIBBBBB', header[16:29]) == PNG_HEADER
        and struct.unpack('>I', header[29:33])[0]
        == binascii.crc32(header[12:29]) & 0xffffffff
    )
    _require(valid, f'share card {path.name} lacks the exact 1200x630 '
             'indexed-PNG header')


@lru_cache(maxsize=2048)
def _expected_share_assets(
        article_json: str,
        article_id: str,
) -> Tuple[bytes, bytes]:
    article = json.loads(article_json)
    article['id'] = article_id
    card = render_share_card(
        article.get('title'),
        article.get('source'),
        article.get('post_date'),
    )
    stub = render_article_stub(article, article_id, SITE_URL).encode('utf-8')
    return card, stub


def _validate_share_assets(
        site: Path,
        source_articles: Sequence[Mapping[str, Any]],
) -> None:
    """Require every generated card and stub to equal its source rendering."""
    for article in source_articles:
        slug = article.get('slug')
        _require(isinstance(slug, str) and bool(slug),
                 'source article has no slug for its share assets')
        article_id = _stable_article_id(article.get('url'))
        article_json = json.dumps(
            dict(article),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        expected_card, expected_stub = _expected_share_assets(
            article_json,
            article_id,
        )
        card_path = site / 'cards' / f'{slug}.png'
        stub_path = site / 'a' / f'{slug}.html'
        _require(
            _read_bytes(card_path, card_path.name) == expected_card,
            f'share card differs from source rendering: {slug}',
        )
        _require(
            _read_bytes(stub_path, stub_path.name) == expected_stub,
            f'article stub differs from source rendering: {slug}',
        )


def _sitemap_date(value: Any) -> str:
    """Mirror the builder's deterministic ISO-date fallback."""
    date = str(value or '')[:10]
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return '1970-01-01'
    return date


def _expected_text_support_assets(
        source_articles: Sequence[Mapping[str, Any]],
        snapshot: Mapping[str, Any],
) -> Mapping[str, bytes]:
    """Render the exact builder-owned support files from tracked inputs."""
    last_modified = _sitemap_date(
        str(
            snapshot.get('checked_at')
            or snapshot.get('latest_publication')
            or '',
        )[:10],
    )
    robots_text = (
        'User-agent: *\n'
        'Allow: /\n'
        f'Sitemap: {SITE_URL}sitemap.xml\n'
    )
    sitemap_rows = [
        '  <url>\n'
        f'    <loc>{SITE_URL}</loc>\n'
        f'    <lastmod>{last_modified}</lastmod>\n'
        '  </url>',
    ]
    for article in source_articles:
        slug = quote(str(article.get('slug') or ''), safe='-')
        location = html_lib.escape(
            f'{SITE_URL}a/{slug}.html',
            quote=False,
        )
        publication_date = _sitemap_date(article.get('post_date'))
        sitemap_rows.append(
            '  <url>\n'
            f'    <loc>{location}</loc>\n'
            f'    <lastmod>{publication_date}</lastmod>\n'
            '  </url>',
        )
    sitemap_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + '\n'.join(sitemap_rows)
        + '\n</urlset>\n'
    )
    web_manifest = json.dumps(
        {
            'name': 'Navnoor Research Terminal',
            'short_name': 'Navnoor Research',
            'description': (
                'Source-backed institutional research dossiers with exact '
                'passages, evidence ledgers, checkpoints, and decision '
                'boundaries.'
            ),
            'start_url': './',
            'scope': './',
            'display': 'standalone',
            'background_color': LIGHT_THEME_BG,
            'theme_color': LIGHT_THEME_BG,
            'icons': [{
                'src': 'favicon.svg',
                'sizes': 'any',
                'type': 'image/svg+xml',
                'purpose': 'any',
            }],
        },
        ensure_ascii=False,
        indent=2,
    ) + '\n'
    favicon_svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="6" fill="{DARK_THEME_BG}"/>
<rect x="2" y="2" width="60" height="60" rx="4" fill="none" stroke="#ffb000" stroke-width="2"/>
<text x="32" y="39" fill="#f4f6f7" font-family="Arial,sans-serif" font-size="19" font-weight="700" text-anchor="middle">N/R</text>
</svg>
'''
    return {
        'robots.txt': robots_text.encode('utf-8'),
        'sitemap.xml': sitemap_xml.encode('utf-8'),
        'site.webmanifest': web_manifest.encode('utf-8'),
        'favicon.svg': favicon_svg.encode('utf-8'),
    }


def _jpeg_dimensions(payload: bytes) -> Tuple[int, int]:
    """Read JPEG dimensions without adding an image-library dependency."""
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset:offset + 2], 'big')
        if segment_length < 2 or offset + segment_length > len(payload):
            break
        if marker in sof_markers and segment_length >= 7:
            height = int.from_bytes(
                payload[offset + 3:offset + 5],
                'big',
            )
            width = int.from_bytes(
                payload[offset + 5:offset + 7],
                'big',
            )
            return width, height
        offset += segment_length
    raise ValueError('og.jpg JPEG dimensions could not be read')


def _validate_support_assets(
        site: Path,
        source_articles: Sequence[Mapping[str, Any]],
        snapshot: Mapping[str, Any],
        social_image_source: Path,
) -> None:
    """Bind every support/SEO asset to its deterministic source projection."""
    expected_assets = _expected_text_support_assets(
        source_articles,
        snapshot,
    )
    for asset_name, expected in expected_assets.items():
        actual = _read_bytes(site / asset_name, asset_name)
        _require(
            actual == expected,
            f'{asset_name} differs from its deterministic source rendering',
        )

    actual_og = _read_bytes(site / 'og.jpg', 'og.jpg')
    valid_jpeg = (
        10_000 <= len(actual_og) <= 500_000
        and actual_og.startswith(b'\xff\xd8')
        and actual_og.rstrip().endswith(b'\xff\xd9')
    )
    _require(
        valid_jpeg,
        'og.jpg must be a valid, optimized 10-500 KB JPEG',
    )
    _require(
        _jpeg_dimensions(actual_og) == (1200, 630),
        'og.jpg JPEG must be exactly 1200x630 pixels',
    )
    tracked_og = _read_bytes(
        social_image_source,
        'tracked assets/og.jpg',
    )
    _require(
        actual_og == tracked_og,
        'og.jpg differs from tracked assets/og.jpg',
    )


def _extract_embedded_articles(html: str) -> List[Dict[str, Any]]:
    matches = ARTICLES_RE.findall(html)
    _require(len(matches) == 1,
             'generated HTML must contain exactly one article wire payload')
    payload = _decode_json(matches[0].encode('utf-8'), 'embedded article payload')
    _require(isinstance(payload, list),
             'embedded article payload must be a list')
    hydrated = []
    for position, article in enumerate(payload):
        _require(isinstance(article, Mapping),
                 f'embedded article {position} must be an object')
        try:
            hydrated.append(hydrate_client_article(article))
        except ValueError as exc:
            raise ValueError(
                f'embedded article {position} violates the wire contract: {exc}',
            ) from exc
    return hydrated


def _validate_article_bijection(
        source_articles: Sequence[Mapping[str, Any]],
        generated_articles: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, str], Dict[str, Mapping[str, Any]]]:
    content_articles = [
        article for article in source_articles
        if article.get('content_status') != 'registry'
    ]
    _require(bool(content_articles),
             'master catalogue has no body-backed articles')

    expected_by_id: Dict[str, str] = {}
    source_by_url: Dict[str, Mapping[str, Any]] = {}
    for article in content_articles:
        url = _display_url(article.get('url'))
        _require(bool(url), 'body-backed source article has no URL')
        article_id = _stable_article_id(url)
        _require(article_id not in expected_by_id,
                 f'body-backed source article ID collision: {article_id}')
        _require(url not in source_by_url,
                 f'body-backed source article URL is duplicated: {url}')
        expected_by_id[article_id] = url
        source_by_url[url] = article

    actual_by_id: Dict[str, str] = {}
    for article in generated_articles:
        generated_id = article.get('id')
        url = _display_url(article.get('url'))
        if not isinstance(generated_id, str) or not generated_id:
            raise ValueError('generated article has no stable ID')
        _require(bool(url), f'generated article {generated_id} has no URL')
        _require(generated_id not in actual_by_id,
                 f'generated article ID is duplicated: {generated_id}')
        _require(url not in actual_by_id.values(),
                 f'generated article URL is duplicated: {url}')
        actual_by_id[generated_id] = url

    if actual_by_id != expected_by_id:
        missing = sorted(set(expected_by_id.items()) - set(actual_by_id.items()))[:3]
        extra = sorted(set(actual_by_id.items()) - set(expected_by_id.items()))[:3]
        raise ValueError(
            'generated article URL/ID bijection does not match body-backed '
            f'source articles; missing={missing}, extra={extra}',
        )
    return actual_by_id, source_by_url


def _validate_article_metadata_projection(
        generated_articles: Sequence[Mapping[str, Any]],
        article_url_by_id: Mapping[str, str],
        source_by_url: Mapping[str, Mapping[str, Any]],
        source_observations: Sequence[Mapping[str, Any]],
) -> None:
    """Bind every hydrated client metadata field to its exact source projection."""
    first_observation_by_url: Dict[str, Mapping[str, Any]] = {}
    for observation in source_observations:
        url = _display_url(observation.get('article_url'))
        if url and url not in first_observation_by_url:
            first_observation_by_url[url] = observation

    for article in generated_articles:
        article_id = str(article['id'])
        url = article_url_by_id[article_id]
        expected = _source_client_metadata(
            source_by_url[url],
            first_observation_by_url.get(url, {}),
        )
        actual = {field: article.get(field) for field in expected}
        if actual != expected:
            differing = sorted(
                field for field in expected
                if actual[field] != expected[field]
            )
            raise ValueError(
                'generated article metadata differs from source/build '
                f'projection: {article_id}; fields={differing}',
            )


def _validate_article_derived_projection(
        generated_articles: Sequence[Mapping[str, Any]],
        expected_order: Sequence[str],
        expected_derived: Mapping[str, Mapping[str, Any]],
) -> None:
    """Bind article chronology and every aggregate to the source observations."""
    actual_order = [str(article['id']) for article in generated_articles]
    _require(
        actual_order == list(expected_order),
        'generated article chronology/order differs from source/build projection',
    )
    for article in generated_articles:
        article_id = str(article['id'])
        expected = expected_derived[article_id]
        actual = {field: article.get(field) for field in expected}
        if actual != expected:
            differing = sorted(
                field for field in expected
                if actual[field] != expected[field]
            )
            raise ValueError(
                'generated article derived fields differ from source/build '
                f'projection: {article_id}; fields={differing}',
            )


def _validate_deferred_briefs(
        payload: Any,
        expected_checksum: str,
        generated_articles: Sequence[Mapping[str, Any]],
        article_url_by_id: Mapping[str, str],
        source_by_url: Mapping[str, Mapping[str, Any]],
) -> None:
    _require(isinstance(payload, Mapping),
             'deferred article dossier must be an object')
    _require(
        set(payload) == {'schema_version', 'data_checksum', 'briefs'},
        'deferred article dossier has the wrong field set',
    )
    _require(
        type(payload.get('schema_version')) is int
        and payload.get('schema_version') == 1,
        'deferred article dossier schema_version must be 1',
    )
    _require(
        payload.get('data_checksum') == expected_checksum,
        'deferred article dossiers do not match the source snapshot',
    )
    briefs = payload.get('briefs')
    _require(isinstance(briefs, Mapping) and bool(briefs),
             'deferred article dossier payload is empty')

    expected_deferred: List[str] = []
    for position, article in enumerate(generated_articles):
        article_id = str(article['id'])
        if position < INLINE_BRIEF_COUNT:
            _require(
                article.get('brief') is not None,
                'article dossier inline/deferred placement differs from '
                f'builder policy: {article_id}',
            )
        else:
            _require(
                article.get('brief') is None,
                'article dossier inline/deferred placement differs from '
                f'builder policy: {article_id}',
            )
            expected_deferred.append(article_id)
    if list(briefs) != expected_deferred:
        missing = sorted(set(expected_deferred) - set(briefs))[:3]
        extra = sorted(set(briefs) - set(expected_deferred))[:3]
        raise ValueError(
            'deferred dossier ownership/order does not match builder policy; '
            f'missing={missing}, extra={extra}',
        )

    for article in generated_articles:
        article_id = str(article['id'])
        source = source_by_url[article_url_by_id[article_id]]
        expected_brief = _client_brief(source.get('brief'))
        actual_brief = (
            briefs[article_id]
            if article.get('brief') is None
            else article.get('brief')
        )
        _require(
            actual_brief == expected_brief,
            f'article dossier differs from source brief: {article_id}',
        )


def _validate_observations(
        payload: Any,
        source_observations: Sequence[Mapping[str, Any]],
        expected_checksum: str,
        generated_articles: Sequence[Mapping[str, Any]],
        expected_observations: Mapping[str, Mapping[str, Any]],
) -> None:
    _require(isinstance(payload, Mapping),
             'deferred observation archive must be an object')
    _require(
        set(payload) == {'schema_version', 'data_checksum', 'observations'},
        'deferred observation archive has the wrong field set',
    )
    _require(
        type(payload.get('schema_version')) is int
        and payload.get('schema_version') == 1,
        'deferred observation schema_version must be 1',
    )
    _require(
        payload.get('data_checksum') == expected_checksum,
        'deferred observations do not match the source snapshot',
    )
    rows = payload.get('observations')
    if not isinstance(rows, list):
        raise ValueError('deferred observation payload is invalid')
    _require(
        len(rows) == len(source_observations),
        'deferred observation count does not match the source snapshot',
    )

    expected_owner_by_id: Dict[str, str] = {}
    for article in generated_articles:
        article_id = str(article['id'])
        idea_ids = article.get('idea_ids')
        if not isinstance(idea_ids, list):
            raise ValueError(
                'hydrated article observation references are invalid',
            )
        for idea_id in idea_ids:
            _require(
                isinstance(idea_id, str)
                and bool(idea_id)
                and idea_id not in expected_owner_by_id,
                'generated article observation references are missing or '
                'duplicated',
            )
            expected_owner_by_id[idea_id] = article_id

    asset_ids: List[str] = []
    for row in rows:
        _require(isinstance(row, Mapping),
                 'deferred observation row must be an object')
        idea_id = row.get('id')
        _require(isinstance(idea_id, str) and bool(idea_id),
                 'deferred observation identity is missing')
        asset_ids.append(idea_id)
    _require(
        len(asset_ids) == len(set(asset_ids)),
        'deferred observation identities are duplicated',
    )
    _require(
        set(asset_ids) == set(expected_owner_by_id),
        'deferred observations do not match generated article references',
    )
    _require(
        set(asset_ids) == set(expected_observations),
        'deferred observations do not match the source/build projection',
    )
    _require(
        asset_ids == list(expected_observations),
        'deferred observation order does not match the source/build projection',
    )
    asset_by_id = {
        str(row['id']): row
        for row in rows
    }
    for row in rows:
        idea_id = str(row['id'])
        _require(
            row.get('article_id') == expected_owner_by_id[idea_id],
            f'deferred observation article ownership is invalid: {idea_id}',
        )
    for idea_id, expected in expected_observations.items():
        asset_row = asset_by_id[idea_id]
        _require(
            set(asset_row) == set(expected),
            f'deferred observation {idea_id} has the wrong field set',
        )
        _require(
            dict(asset_row) == dict(expected),
            'deferred observation content differs from source/build '
            f'projection: {idea_id}',
        )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_github_output(path: Path, fingerprints: Mapping[str, str]) -> None:
    _require(
        set(fingerprints) == set(FINGERPRINT_KEYS),
        'release fingerprint set is incomplete',
    )
    for key in FINGERPRINT_KEYS:
        _require(
            SHA256_RE.fullmatch(str(fingerprints[key])) is not None,
            f'release fingerprint {key} is invalid',
        )
    try:
        with path.open('a', encoding='utf-8') as handle:
            for key in FINGERPRINT_KEYS:
                handle.write(f'{key}={fingerprints[key]}\n')
    except OSError as exc:
        raise ValueError(f'GitHub output could not be written: {exc}') from exc


def validate_release(
        site_dir: Path,
        articles_path: Path,
        observations_path: Path,
        manifest_path: Path,
        expected_revision: str,
        *,
        github_output: Optional[Path] = None,
        policy: ReleasePolicy = PRODUCTION_POLICY,
) -> Dict[str, str]:
    """Validate a built artifact and return its six trusted fingerprints."""
    site = Path(site_dir)
    article_source = Path(articles_path)
    observation_source = Path(observations_path)
    manifest_source = Path(manifest_path)
    _require(
        isinstance(expected_revision, str)
        and bool(expected_revision)
        and '\n' not in expected_revision
        and '\r' not in expected_revision,
        'expected revision must be a non-empty single-line string',
    )

    source_articles = _read_json(article_source, 'source article index')
    source_observations = _read_json(
        observation_source, 'source observations',
    )
    snapshot = _read_json(manifest_source, 'snapshot manifest')
    _require(
        isinstance(source_articles, list)
        and bool(source_articles)
        and all(isinstance(row, Mapping) for row in source_articles),
        'source article index must be a non-empty list of objects',
    )
    _require(
        isinstance(source_observations, list)
        and bool(source_observations)
        and all(isinstance(row, Mapping) for row in source_observations),
        'source observations must be a non-empty list of objects',
    )
    _require(isinstance(snapshot, Mapping),
             'snapshot manifest must be an object')

    files = _validate_artifact_tree(site, source_articles)
    _validate_support_assets(
        site,
        source_articles,
        snapshot,
        article_source.parent / 'assets' / 'og.jpg',
    )
    sizes = _validate_sizes(site, files, policy)
    for path in sorted((site / 'cards').iterdir()):
        _validate_share_card(path)
    _validate_share_assets(site, source_articles)

    expected_checksum = snapshot_checksum(
        article_source, observation_source,
    )
    _require(
        snapshot.get('data_checksum') == expected_checksum,
        'snapshot manifest checksum does not match exact source bytes',
    )
    data_summary = validate_data_layer(
        site,
        article_source,
        manifest_source,
    )
    _require(
        data_summary.get('article_count') == len(source_articles),
        'validated data-layer count does not match the master catalogue',
    )

    index_path = site / 'index.html'
    html_bytes = _read_bytes(index_path, 'index.html')
    try:
        html = html_bytes.decode('utf-8')
    except UnicodeError as exc:
        raise ValueError(f'index.html is not UTF-8: {exc}') from exc
    wire_versions = ARTICLE_WIRE_SCHEMA_RE.findall(html)
    _require(
        len(wire_versions) == 1,
        'generated HTML must declare exactly one article wire schema version',
    )
    _require(
        int(wire_versions[0]) == ARTICLE_WIRE_SCHEMA_VERSION,
        'generated HTML article wire schema version does not match '
        'the current client contract',
    )
    validate_inline_scripts(index_path)
    content_count = sum(
        article.get('content_status') != 'registry'
        for article in source_articles
    )
    embedded_digests = validate_html(
        html,
        expected_revision,
        content_count,
        len(source_observations),
        expected_checksum,
    )

    brief_path = site / 'article_briefs.json'
    brief_bytes = _read_bytes(brief_path, 'article_briefs.json')
    observation_path = site / 'observations.json'
    observation_bytes = _read_bytes(
        observation_path, 'observations.json',
    )
    _require(
        embedded_digests.get('article_briefs.json') == _sha256(brief_bytes),
        'embedded deferred dossier digest does not match exact asset bytes',
    )
    _require(
        embedded_digests.get('observations.json')
        == _sha256(observation_bytes),
        'embedded observation digest does not match exact asset bytes',
    )

    generated_articles = _extract_embedded_articles(html)
    article_url_by_id, source_by_url = _validate_article_bijection(
        source_articles, generated_articles,
    )
    _validate_article_metadata_projection(
        generated_articles,
        article_url_by_id,
        source_by_url,
        source_observations,
    )
    (
        expected_article_order,
        expected_article_derived,
        expected_observations,
    ) = _source_derived_projection(source_by_url, source_observations)
    _validate_article_derived_projection(
        generated_articles,
        expected_article_order,
        expected_article_derived,
    )
    _validate_deferred_briefs(
        _decode_json(brief_bytes, 'deferred article dossiers'),
        expected_checksum,
        generated_articles,
        article_url_by_id,
        source_by_url,
    )
    _validate_observations(
        _decode_json(observation_bytes, 'deferred observations'),
        source_observations,
        expected_checksum,
        generated_articles,
        expected_observations,
    )

    fingerprints = {
        'html_sha256': _sha256(html_bytes),
        'brief_sha256': _sha256(brief_bytes),
        'observation_sha256': _sha256(observation_bytes),
        'support_sha256': support_bundle_checksum(site),
        'data_sha256': data_bundle_checksum(site),
        'share_sha256': share_proof_bundle_checksum(site, article_source),
    }
    _require(
        all(SHA256_RE.fullmatch(value) for value in fingerprints.values()),
        'release fingerprint generation failed',
    )
    if github_output is not None:
        _write_github_output(Path(github_output), fingerprints)
    print(
        'Release artifact passed: '
        + ', '.join(f'{key}={value}' for key, value in sizes.items())
        + ' bytes.',
    )
    return fingerprints


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Validate an exact built Navnoor Research Terminal release.',
    )
    parser.add_argument('--site', required=True, type=Path)
    parser.add_argument('--articles', required=True, type=Path)
    parser.add_argument('--trades', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--github-output', type=Path)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        validate_release(
            args.site,
            args.articles,
            args.trades,
            args.manifest,
            args.expected_revision,
            github_output=args.github_output,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f'RELEASE VALIDATION FAILED: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
