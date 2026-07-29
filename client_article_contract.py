#!/usr/bin/env python3
"""Encode and validate the compact client-article wire contract.

The static terminal omits derived fields and empty defaults from each embedded
article.  This module keeps the Python encoder and release validator on one
versioned contract while the browser restores the same runtime shape before its
first consumer.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Mapping, Tuple
from urllib.parse import urlsplit


ARTICLE_WIRE_SCHEMA_VERSION = 2
MAX_CHECKPOINT_COUNT = 3

BRIEF_FEATURE_BITS: Tuple[Tuple[str, int], ...] = (
    ('lead', 1),
    ('evidence', 2),
    ('countercase', 4),
    ('falsifier', 8),
    ('implementation', 16),
    ('mechanism', 32),
)
COVERAGE_FEATURE_BITS: Tuple[Tuple[str, int], ...] = (
    ('has_quant', 1),
    ('has_thesis', 2),
    ('has_outcome', 4),
)

_BRIEF_FEATURE_MASK_MAX = sum(bit for _, bit in BRIEF_FEATURE_BITS)
_COVERAGE_MASK_MAX = sum(bit for _, bit in COVERAGE_FEATURE_BITS)
_DERIVED_RUNTIME_KEYS = frozenset((
    'date',
    'publication_precision',
    'read_minutes',
    'trade_count',
    'brief_features',
    *(name for name, _ in COVERAGE_FEATURE_BITS),
))
_OBJECT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    'alternate_urls': {},
}
_NULLABLE_OBJECT_DEFAULTS: Dict[str, None] = {
    'brief': None,
}
_LIST_DEFAULT_KEYS = (
    'idea_ids',
    'directions',
    'instruments',
    'underlyings',
    'managers',
    'manager_keys',
)
_DATE_ONLY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_BODY_TIMESTAMP_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'
    r'(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$'
)
_ARTICLE_ID_RE = re.compile(r'^a_[0-9a-f]{14}$')
_IDEA_ID_RE = re.compile(r'^i_[0-9a-f]{14}$')
_WIRE_REQUIRED_KEYS = frozenset((
    'id',
    'title',
    'subtitle',
    'published_at',
    'url',
    'source',
    'wordcount',
    'content_status',
    'body_revision_status',
    'source_updated_at',
    'observed_source_updated_at',
))
_WIRE_OPTIONAL_KEYS = frozenset((
    *_OBJECT_DEFAULTS,
    *_NULLABLE_OBJECT_DEFAULTS,
    *_LIST_DEFAULT_KEYS,
    '_b',
    '_q',
))
_FULL_ARTICLE_KEYS = (
    _WIRE_REQUIRED_KEYS
    | frozenset(_OBJECT_DEFAULTS)
    | frozenset(_NULLABLE_OBJECT_DEFAULTS)
    | frozenset(_LIST_DEFAULT_KEYS)
    | _DERIVED_RUNTIME_KEYS
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _publication_metadata(value: Any) -> Tuple[str, str]:
    _require(isinstance(value, str), 'client article published_at must be a string')
    _require(bool(value) and value == value.strip(),
             'client article published_at must be a non-empty trimmed string')
    if _DATE_ONLY_RE.fullmatch(value):
        try:
            datetime.strptime(value, '%Y-%m-%d')
        except ValueError as exc:
            raise ValueError('client article published_at is not a real date') from exc
        return value, 'day'
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(
            'client article published_at must be a date or ISO timestamp'
        ) from exc
    _require(parsed.tzinfo is not None,
             'client article publication timestamp must include a timezone')
    return value[:10], 'instant'


def _wordcount(value: Any) -> int:
    _require(type(value) is int, 'client article wordcount must be an integer')
    _require(value >= 0, 'client article wordcount must not be negative')
    return value


def _validate_base_fields(article: Mapping[str, Any], *, compact: bool) -> None:
    expected_keys = (
        _WIRE_REQUIRED_KEYS | _WIRE_OPTIONAL_KEYS
        if compact
        else _FULL_ARTICLE_KEYS
    )
    missing = sorted(_WIRE_REQUIRED_KEYS - set(article))
    unexpected = sorted(set(article) - expected_keys)
    _require(not missing, 'client article is missing fields: ' + ', '.join(missing))
    _require(
        not unexpected,
        'client article contains unexpected fields: ' + ', '.join(unexpected),
    )

    article_id = article.get('id')
    _require(
        isinstance(article_id, str)
        and _ARTICLE_ID_RE.fullmatch(article_id) is not None,
        'client article id must be a stable a_ identifier',
    )
    title = article.get('title')
    _require(
        isinstance(title, str) and bool(title.strip()),
        'client article title must be a non-empty string',
    )
    subtitle = article.get('subtitle')
    _require(
        isinstance(subtitle, str) and subtitle == subtitle.strip(),
        'client article subtitle must be a trimmed string',
    )
    source = article.get('source')
    _require(
        source in {'substack', 'medium'},
        'client article source must be substack or medium',
    )
    content_status = article.get('content_status')
    _require(
        content_status in {'full', 'excerpt'},
        'client article content_status must be full or excerpt',
    )
    revision_status = article.get('body_revision_status')
    _require(
        revision_status in {'current', 'prior', 'unverified'},
        'client article body_revision_status is invalid',
    )
    source_updated_at = article.get('source_updated_at')
    observed_updated_at = article.get('observed_source_updated_at')
    _require(
        isinstance(source_updated_at, str)
        and isinstance(observed_updated_at, str),
        'client article body revision timestamps must be strings',
    )
    for label, value in (
        ('source_updated_at', source_updated_at),
        ('observed_source_updated_at', observed_updated_at),
    ):
        if value:
            _require(
                _BODY_TIMESTAMP_RE.fullmatch(value) is not None,
                f'client article {label} must be a timezone-qualified timestamp',
            )
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError as exc:
                raise ValueError(
                    f'client article {label} must be an ISO timestamp'
                ) from exc
            _require(
                parsed.tzinfo is not None,
                f'client article {label} must include a timezone',
            )
    if revision_status == 'current':
        _require(
            source_updated_at == observed_updated_at,
            'client article current body revision timestamps must match',
        )
    elif revision_status == 'prior':
        _require(
            content_status == 'excerpt'
            and bool(source_updated_at)
            and bool(observed_updated_at)
            and source_updated_at != observed_updated_at,
            'client article prior body revision is inconsistent',
        )
    else:
        _require(
            content_status == 'excerpt',
            'client article unverified body revision must be an excerpt',
        )
    if content_status == 'full':
        _require(
            revision_status == 'current'
            and bool(source_updated_at)
            and source_updated_at == observed_updated_at,
            'client article full content requires a timestamp-bound '
            'current body revision',
        )
    url = article.get('url')
    _require(
        isinstance(url, str) and bool(url) and url == url.strip(),
        'client article url must be a non-empty trimmed string',
    )
    parts = urlsplit(url)
    _require(
        parts.scheme == 'https'
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.fragment,
        'client article url must be a canonical HTTPS URL',
    )


def _read_minutes(wordcount: int) -> int:
    if not wordcount:
        return 0
    whole_minutes, remainder = divmod(wordcount, 220)
    rounded_minutes = whole_minutes + int(
        remainder > 110 or (remainder == 110 and whole_minutes % 2 == 1)
    )
    return max(1, rounded_minutes)


def _validate_default_fields(article: Mapping[str, Any], *, required: bool) -> None:
    for key in _OBJECT_DEFAULTS:
        if required:
            _require(key in article, f'client article is missing {key}')
        if key in article:
            _require(type(article[key]) is dict,
                     f'client article {key} must be an object')
            for name, value in article[key].items():
                _require(
                    isinstance(name, str)
                    and bool(name)
                    and isinstance(value, str)
                    and value.startswith('https://'),
                    f'client article {key} must map strings to HTTPS URLs',
                )

    for key in _NULLABLE_OBJECT_DEFAULTS:
        if required:
            _require(key in article, f'client article is missing {key}')
        if key in article:
            value = article[key]
            _require(value is None or type(value) is dict,
                     f'client article {key} must be an object or null')

    for key in _LIST_DEFAULT_KEYS:
        if required:
            _require(key in article, f'client article is missing {key}')
        if key in article:
            _require(type(article[key]) is list,
                     f'client article {key} must be a list')
            values = article[key]
            _require(
                all(
                    isinstance(value, str)
                    and bool(value)
                    and value == value.strip()
                    for value in values
                ),
                f'client article {key} must contain non-empty trimmed strings',
            )
            _require(
                len(values) == len(set(values)),
                f'client article {key} must not contain duplicates',
            )
            if key == 'idea_ids':
                _require(
                    all(_IDEA_ID_RE.fullmatch(value) is not None for value in values),
                    'client article idea_ids must contain stable i_ identifiers',
                )


def _brief_code(value: Any) -> Tuple[int, int]:
    _require(type(value) is list and len(value) == 2,
             'client article _b must be a two-integer list')
    mask, checkpoint_count = value
    _require(type(mask) is int,
             'client article brief feature mask must be an integer')
    _require(0 <= mask <= _BRIEF_FEATURE_MASK_MAX,
             'client article brief feature mask is outside 0..63')
    _require(type(checkpoint_count) is int,
             'client article checkpoint count must be an integer')
    _require(0 <= checkpoint_count <= MAX_CHECKPOINT_COUNT,
             f'client article checkpoint count is outside 0..{MAX_CHECKPOINT_COUNT}')
    return mask, checkpoint_count


def _coverage_mask(value: Any) -> int:
    _require(type(value) is int,
             'client article coverage feature mask must be an integer')
    _require(0 <= value <= _COVERAGE_MASK_MAX,
             'client article coverage feature mask is outside 0..7')
    return value


def compact_client_article(article: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the canonical compact wire row for one full client article."""
    _require(isinstance(article, Mapping), 'client article must be an object')
    _require('_b' not in article and '_q' not in article,
             'full client article contains reserved compact wire keys')
    _validate_base_fields(article, compact=False)
    _validate_default_fields(article, required=True)

    published_date, publication_precision = _publication_metadata(
        article.get('published_at')
    )
    wordcount = _wordcount(article.get('wordcount'))
    expected_read_minutes = _read_minutes(wordcount)

    for key in _DERIVED_RUNTIME_KEYS:
        _require(key in article, f'full client article is missing {key}')
    _require(type(article['date']) is str and article['date'] == published_date,
             'full client article date does not match published_at')
    _require(
        type(article['publication_precision']) is str
        and article['publication_precision'] == publication_precision,
        'full client article publication_precision does not match published_at',
    )
    _require(
        type(article['read_minutes']) is int
        and article['read_minutes'] == expected_read_minutes,
        'full client article read_minutes does not match wordcount',
    )
    _require(
        type(article['trade_count']) is int
        and article['trade_count'] == len(article['idea_ids']),
        'full client article trade_count does not match idea_ids',
    )

    features = article['brief_features']
    _require(type(features) is dict,
             'full client article brief_features must be an object')
    expected_feature_keys = {
        name for name, _ in BRIEF_FEATURE_BITS
    } | {'checkpoint_count'}
    _require(set(features) == expected_feature_keys,
             'full client article brief_features has the wrong field set')
    for name, _ in BRIEF_FEATURE_BITS:
        _require(type(features[name]) is bool,
                 f'full client article brief feature {name} must be boolean')
    checkpoint_count = features['checkpoint_count']
    _require(type(checkpoint_count) is int,
             'full client article checkpoint_count must be an integer')
    _require(0 <= checkpoint_count <= MAX_CHECKPOINT_COUNT,
             f'full client article checkpoint_count is outside 0..{MAX_CHECKPOINT_COUNT}')

    for name, _ in COVERAGE_FEATURE_BITS:
        _require(type(article[name]) is bool,
                 f'full client article {name} must be boolean')

    compact = dict(article)
    for key in _DERIVED_RUNTIME_KEYS:
        compact.pop(key)

    brief_mask = sum(
        bit for name, bit in BRIEF_FEATURE_BITS if features[name]
    )
    if brief_mask or checkpoint_count:
        compact['_b'] = [brief_mask, checkpoint_count]

    coverage_mask = sum(
        bit for name, bit in COVERAGE_FEATURE_BITS if article[name]
    )
    if coverage_mask:
        compact['_q'] = coverage_mask

    for key, object_default in _OBJECT_DEFAULTS.items():
        if compact[key] == object_default:
            compact.pop(key)
    for key, null_default in _NULLABLE_OBJECT_DEFAULTS.items():
        if compact[key] is null_default:
            compact.pop(key)
    for key in _LIST_DEFAULT_KEYS:
        if not compact[key]:
            compact.pop(key)
    return compact


def hydrate_client_article(article: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly validate and expand one compact wire row without mutating it."""
    _require(isinstance(article, Mapping), 'compact client article must be an object')
    unexpected_runtime_keys = sorted(_DERIVED_RUNTIME_KEYS.intersection(article))
    _require(
        not unexpected_runtime_keys,
        'compact client article contains runtime fields: '
        + ', '.join(unexpected_runtime_keys),
    )
    _validate_base_fields(article, compact=True)
    _validate_default_fields(article, required=False)

    published_date, publication_precision = _publication_metadata(
        article.get('published_at')
    )
    wordcount = _wordcount(article.get('wordcount'))
    brief_mask, checkpoint_count = (
        _brief_code(article['_b']) if '_b' in article else (0, 0)
    )
    coverage_mask = (
        _coverage_mask(article['_q']) if '_q' in article else 0
    )

    hydrated = dict(article)
    hydrated.pop('_b', None)
    hydrated.pop('_q', None)
    hydrated['date'] = published_date
    hydrated['publication_precision'] = publication_precision
    hydrated['read_minutes'] = _read_minutes(wordcount)
    hydrated['alternate_urls'] = (
        dict(article['alternate_urls']) if 'alternate_urls' in article else {}
    )
    hydrated['brief'] = (
        dict(article['brief']) if article.get('brief') is not None else None
    )
    for key in _LIST_DEFAULT_KEYS:
        hydrated[key] = list(article[key]) if key in article else []
    hydrated['trade_count'] = len(hydrated['idea_ids'])
    hydrated['brief_features'] = {
        name: bool(brief_mask & bit) for name, bit in BRIEF_FEATURE_BITS
    }
    hydrated['brief_features']['checkpoint_count'] = checkpoint_count
    for name, bit in COVERAGE_FEATURE_BITS:
        hydrated[name] = bool(coverage_mask & bit)
    return hydrated
