#!/usr/bin/env python3
"""Build and validate the public, machine-readable research data contract."""

import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from research_graph import article_feature_terms


SCHEMA_VERSION = 1
LATEST_LIMIT = 20
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=10)
MAX_GENERATED_AGE = timedelta(hours=16)
MAX_SEARCH_INDEX_BYTES = 500_000

DATA_ENDPOINT_NAMES: Tuple[str, ...] = tuple(sorted((
    'articles_index.json',
    'latest.json',
    'manifest.json',
    'search_index.json',
    'related.json',
    'families.json',
)))
DATA_ENDPOINTS: Tuple[str, ...] = tuple(
    f'data/{name}' for name in DATA_ENDPOINT_NAMES
)
SOURCES: Tuple[str, ...] = ('substack', 'medium', 'patreon', 'fxempire')
FAMILIES: Tuple[str, ...] = (
    'firm-mechanics',
    'career-structure',
    'model-critique',
    'scandal-enforcement',
    'event-reaction',
    'market-structure',
    'other',
)

ARTICLE_REQUIRED_KEYS = frozenset((
    'source', 'source_id', 'slug', 'title', 'subtitle', 'post_date', 'url',
    'audience', 'wordcount', 'content_status', 'brief', 'family',
))
LATEST_KEYS = (
    'source', 'slug', 'title', 'subtitle', 'post_date', 'url', 'alternate_urls',
)
SEARCH_ARTICLE_KEYS = frozenset((
    'slug', 'source', 'title', 'post_date', 'url', 'entities',
))
RELATED_ROW_KEYS = frozenset((
    'slug', 'source', 'title', 'url', 'score', 'why',
))
MANIFEST_KEYS = frozenset((
    'schema_version', 'dataset_version', 'generated_at', 'article_count',
    'source_counts', 'family_counts', 'endpoints',
))
BRIEF_REQUIRED_KEYS = frozenset((
    'schema_version', 'body_sha256', 'lead', 'sections', 'fallback_evidence',
    'checkpoints',
))

SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
EMPTY_BODY_SHA256 = hashlib.sha256(b'').hexdigest()
NORMALIZED_TERM_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
DATE_ONLY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TIMESTAMP_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'
    r'(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$'
)
FORBIDDEN_PRIVACY_KEYS = frozenset((
    'openrate',
    'emailopenrate',
    'subscriber',
    'subscribers',
    'subscribercount',
    'paidsubscribers',
    'paidsubscribercount',
    'revenue',
    'revenues',
    'monthlyrevenue',
    'annualrevenue',
    'subscriptionrevenue',
    'creatorrevenue',
    'patreonrevenue',
    'pledge',
    'pledges',
    'pledgecount',
))

PathInput = Union[str, Path]
SnapshotInput = Union[PathInput, Mapping[str, Any]]
Summary = Dict[str, Any]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f'non-standard JSON constant {value!r}')


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON object key {key!r}')
        value[key] = item
    return value


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


def _read_json(path: Path, label: str) -> Any:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f'{label} could not be read: {exc}') from exc
    return _decode_json(payload, label)


def _json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f'data endpoint could not be serialized: {exc}') from exc
    return (text + '\n').encode('utf-8')


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.tmp')
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_snapshot(value: SnapshotInput) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    path = Path(value)
    snapshot = _read_json(path, 'snapshot manifest')
    _require(isinstance(snapshot, dict), 'snapshot manifest must be a JSON object')
    return snapshot


def _publication_instant(value: Any, label: str) -> datetime:
    _require(isinstance(value, str), f'{label} must be a string')
    try:
        if DATE_ONLY_RE.fullmatch(value):
            parsed_date = Date.fromisoformat(value)
            return datetime.combine(parsed_date, datetime.min.time(), tzinfo=timezone.utc)
        _require(TIMESTAMP_RE.fullmatch(value) is not None,
                 f'{label} must be an ISO date or timezone-qualified timestamp')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        _require(parsed.tzinfo is not None, f'{label} must include a timezone')
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f'{label} is not a real ISO date') from None


def _generated_instant(value: Any) -> datetime:
    _require(isinstance(value, str) and value.endswith('Z'),
             'data manifest generated_at must be a UTC timestamp ending in Z')
    _require(TIMESTAMP_RE.fullmatch(value) is not None,
             'data manifest generated_at must be a timezone-qualified timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError):
        raise ValueError('data manifest generated_at is not a real timestamp') from None
    return parsed.astimezone(timezone.utc)


def _article_sort_key(article: Mapping[str, Any]) -> Tuple[datetime, str, str]:
    return (
        _publication_instant(article.get('post_date'), 'article post_date'),
        str(article.get('source') or ''),
        str(article.get('slug') or ''),
    )


def _latest_projection(article: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        'source': article.get('source'),
        'slug': article.get('slug'),
        'title': article.get('title'),
        'subtitle': article.get('subtitle'),
        'post_date': article.get('post_date'),
        'url': article.get('url'),
        'alternate_urls': article.get('alternate_urls') or {},
    }


def _latest_articles(articles: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    newest = sorted(articles, key=_article_sort_key, reverse=True)[:LATEST_LIMIT]
    return [_latest_projection(article) for article in newest]


def _https_url(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()),
             f'{label} must be a non-empty string')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f'{label} is not a valid URL') from None
    _require(
        parsed.scheme == 'https' and bool(parsed.hostname)
        and parsed.username is None and parsed.password is None and port is None,
        f'{label} must be a canonical HTTPS URL without credentials or a port',
    )
    _require(not parsed.fragment, f'{label} must not contain a fragment')
    return value


def _validate_brief(value: Any, label: str) -> None:
    _require(isinstance(value, dict), f'{label} must be an object')
    missing = sorted(BRIEF_REQUIRED_KEYS - value.keys())
    _require(not missing, f'{label} is missing fields: {", ".join(missing)}')
    _require(type(value.get('schema_version')) is int and value['schema_version'] >= 1,
             f'{label} schema_version must be a positive integer')
    checksum = value.get('body_sha256')
    _require(isinstance(checksum, str) and SHA256_RE.fullmatch(checksum) is not None,
             f'{label} body_sha256 must be a lowercase SHA-256 digest')
    for field in ('lead', 'fallback_evidence'):
        _require(value.get(field) is None or isinstance(value[field], dict),
                 f'{label} {field} must be an object or null')
    for field in ('sections', 'checkpoints'):
        _require(isinstance(value.get(field), list),
                 f'{label} {field} must be an array')


def _validate_registry_brief(value: Any, label: str) -> None:
    """Require the exact brief emitted for a metadata-only, empty-body row."""
    _require(isinstance(value, dict), f'{label} must be an object')
    _require(set(value) == BRIEF_REQUIRED_KEYS,
             f'{label} does not match the exact empty-body registry contract')
    _require(type(value.get('schema_version')) is int
             and value['schema_version'] == 1,
             f'{label} does not match the exact empty-body registry contract')
    _require(value.get('body_sha256') == EMPTY_BODY_SHA256,
             f'{label} does not match the exact empty-body registry contract')
    _require(value.get('lead') is None and value.get('fallback_evidence') is None,
             f'{label} does not match the exact empty-body registry contract')
    _require(value.get('sections') == [] and value.get('checkpoints') == [],
             f'{label} does not match the exact empty-body registry contract')


def _validate_articles(value: Any) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, int]]:
    _require(isinstance(value, list) and bool(value),
             'public articles_index.json must be a non-empty array')
    articles: List[Dict[str, Any]] = []
    slugs: set = set()
    identities: set = set()
    urls: set = set()
    source_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    for index, raw in enumerate(value):
        label = f'article {index}'
        _require(isinstance(raw, dict), f'{label} must be an object')
        missing = sorted(ARTICLE_REQUIRED_KEYS - raw.keys())
        _require(not missing, f'{label} is missing fields: {", ".join(missing)}')
        article = raw
        source = article.get('source')
        _require(source in SOURCES, f'{label} has an invalid source')
        source_id = article.get('source_id')
        slug = article.get('slug')
        title = article.get('title')
        _require(isinstance(source_id, str) and bool(source_id.strip()),
                 f'{label} source_id must be a non-empty string')
        _require(isinstance(slug, str) and bool(slug.strip()),
                 f'{label} slug must be a non-empty string')
        _require(isinstance(title, str) and bool(title.strip()),
                 f'{label} title must be a non-empty string')
        for field in ('subtitle', 'audience'):
            _require(isinstance(article.get(field), str),
                     f'{label} {field} must be a string')
        wordcount = article.get('wordcount')
        _require(type(wordcount) is int and wordcount >= 0,
                 f'{label} wordcount must be a non-negative integer')
        _publication_instant(article.get('post_date'), f'{label} post_date')
        url = _https_url(article.get('url'), f'{label} url')
        content_status = article.get('content_status')
        if source in {'patreon', 'fxempire'}:
            _require(content_status == 'registry',
                     f'{label} registry source must have content_status registry')
            _require(wordcount == 0,
                     f'{label} registry source must have an empty-body wordcount')
            allowed_keys = set(ARTICLE_REQUIRED_KEYS) | {'alternate_urls'}
            if source == 'patreon':
                allowed_keys.add('access')
            _require(set(article) <= allowed_keys,
                     f'{label} has fields outside the metadata-only registry contract')
            if source == 'patreon':
                _require(article.get('access') in {'public', 'paid'}
                         and article.get('access') == article.get('audience'),
                         f'{label} Patreon access is missing or inconsistent')
            else:
                _require(article.get('audience') == 'public',
                         f'{label} FX Empire metadata must be public')
        else:
            _require(content_status in {'full', 'excerpt'},
                     f'{label} publication source has an invalid content_status')
        if content_status == 'registry':
            _validate_registry_brief(article.get('brief'), f'{label} brief')
        else:
            _validate_brief(article.get('brief'), f'{label} brief')
        family = article.get('family')
        _require(family in FAMILIES, f'{label} has an invalid family')
        alternate_urls = article.get('alternate_urls')
        if alternate_urls is not None:
            _require(isinstance(alternate_urls, dict),
                     f'{label} alternate_urls must be an object')
            unknown_alternates = set(alternate_urls) - set(SOURCES)
            _require(not unknown_alternates,
                     f'{label} alternate_urls has unknown sources: {sorted(unknown_alternates)}')
            for alternate_source, alternate_url in alternate_urls.items():
                _require(alternate_source != source,
                         f'{label} alternate_urls must not repeat its canonical source')
                _https_url(alternate_url, f'{label} alternate_urls.{alternate_source}')
        identity = (source, source_id.casefold())
        _require(identity not in identities, f'{label} duplicates a source identity')
        _require(slug not in slugs, f'{label} duplicates slug {slug!r}')
        _require(url not in urls, f'{label} duplicates canonical URL')
        identities.add(identity)
        slugs.add(slug)
        urls.add(url)
        source_counts[source] += 1
        family_counts[family] += 1
        articles.append(article)

    exact_source_counts = {source: source_counts[source] for source in SOURCES}
    _require(all(count > 0 for count in exact_source_counts.values()),
             'public article index must contain at least one article from all four sources')
    exact_family_counts = {family: family_counts[family] for family in FAMILIES}
    return articles, exact_source_counts, exact_family_counts


def _validate_latest(value: Any, articles: Sequence[Mapping[str, Any]]) -> None:
    _require(isinstance(value, list), 'latest.json must be an array')
    expected = _latest_articles(articles)
    _require(len(value) == len(expected),
             f'latest.json must contain exactly {len(expected)} records')
    expected_keys = set(LATEST_KEYS)
    for index, row in enumerate(value):
        _require(isinstance(row, dict), f'latest record {index} must be an object')
        _require(set(row) == expected_keys,
                 f'latest record {index} must contain exactly the minimal field set')
        _require(isinstance(row.get('alternate_urls'), dict),
                 f'latest record {index} alternate_urls must be an object')
    _require(value == expected,
             'latest.json is not the exact deterministic newest-20 projection')


def _validate_families(
    value: Any,
    articles: Sequence[Mapping[str, Any]],
) -> Dict[str, int]:
    _require(isinstance(value, dict), 'families.json must be an object')
    _require(set(value) == set(FAMILIES),
             'families.json must contain exactly the seven allowed families')
    expected: Dict[str, List[str]] = {family: [] for family in FAMILIES}
    for article in articles:
        expected[str(article['family'])].append(str(article['slug']))
    seen: set = set()
    for family in FAMILIES:
        slugs = value.get(family)
        _require(isinstance(slugs, list), f'families.{family} must be an array')
        _require(all(isinstance(slug, str) and slug for slug in slugs),
                 f'families.{family} contains an invalid slug')
        _require(len(slugs) == len(set(slugs)),
                 f'families.{family} contains duplicate slugs')
        _require(not seen.intersection(slugs),
                 f'families.{family} repeats a slug assigned to another family')
        seen.update(slugs)
        _require(slugs == expected[family],
                 f'families.{family} does not match master-index order and assignment')
    _require(len(seen) == len(articles),
             'families.json does not assign every article exactly once')
    return {family: len(expected[family]) for family in FAMILIES}


def _validate_normalized_terms(value: Any, label: str) -> List[str]:
    _require(isinstance(value, list), f'{label} must be an array')
    _require(all(
        isinstance(term, str) and NORMALIZED_TERM_RE.fullmatch(term) is not None
        for term in value
    ), f'{label} contains an invalid normalized term')
    _require(value == sorted(set(value)),
             f'{label} terms must be unique and lexicographically sorted')
    return value


def _validate_search_index(
    value: Any,
    articles: Sequence[Mapping[str, Any]],
    byte_count: int,
) -> List[set]:
    _require(byte_count < MAX_SEARCH_INDEX_BYTES,
             f'search_index.json must be smaller than {MAX_SEARCH_INDEX_BYTES} bytes')
    _require(isinstance(value, dict) and set(value) == {'entities', 'articles'},
             'search_index.json must contain exactly entities and articles')
    entities = value.get('entities')
    search_articles = value.get('articles')
    _require(isinstance(entities, dict), 'search_index entities must be an object')
    _require(isinstance(search_articles, list), 'search_index articles must be an array')
    _require(len(search_articles) == len(articles),
             'search_index must cover every article exactly once')
    _require(list(entities) == sorted(entities),
             'search_index entity keys must be lexicographically sorted')

    expected_inverted: Dict[str, List[int]] = defaultdict(list)
    entity_sets: List[set] = []
    for index, (row, article) in enumerate(zip(search_articles, articles)):
        _require(isinstance(row, dict), f'search article {index} must be an object')
        _require(set(row) == SEARCH_ARTICLE_KEYS,
                 f'search article {index} has the wrong field set')
        for field in ('slug', 'source', 'title', 'post_date', 'url'):
            _require(row.get(field) == article.get(field),
                     f'search article {index} {field} does not match the master index')
        terms = _validate_normalized_terms(row.get('entities'),
                                           f'search article {index} entities')
        entity_sets.append(set(terms))
        for term in terms:
            expected_inverted[term].append(index)

    _require(set(entities) == set(expected_inverted),
             'search_index inverted entity keys do not match article entity lists')
    for term, indices in entities.items():
        _require(NORMALIZED_TERM_RE.fullmatch(term) is not None,
                 f'search_index has an invalid normalized entity {term!r}')
        _require(isinstance(indices, list),
                 f'search_index entity {term!r} must map to an array')
        _require(all(type(index) is int for index in indices),
                 f'search_index entity {term!r} has a non-integer article index')
        _require(indices == sorted(set(indices)),
                 f'search_index entity {term!r} indices must be unique and ascending')
        _require(indices == expected_inverted[term],
                 f'search_index entity {term!r} is inconsistent with article rows')
    return entity_sets


def _collect_article_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {'url', 'alternate_urls', 'body_sha256', 'sha256'}:
                yield from _collect_article_text(item)
    elif isinstance(value, list):
        for item in value:
            yield from _collect_article_text(item)


def _article_words(article: Mapping[str, Any]) -> Tuple[str, ...]:
    text = ' '.join(_collect_article_text({
        'title': article.get('title'),
        'subtitle': article.get('subtitle'),
        'brief': article.get('brief'),
    })).casefold()
    return tuple(re.findall(r'[a-z0-9]+', text))


def _contains_term(words: Sequence[str], term: str) -> bool:
    parts = tuple(term.split('-'))
    width = len(parts)
    return any(tuple(words[index:index + width]) == parts
               for index in range(len(words) - width + 1))


def _validate_related(
    value: Any,
    articles: Sequence[Mapping[str, Any]],
    entity_sets: Sequence[set],
) -> None:
    _require(isinstance(value, dict), 'related.json must be an object')
    article_keys = [f'{article["source"]}:{article["slug"]}' for article in articles]
    _require(list(value) == article_keys,
             'related.json must cover every article in master-index order')
    article_by_identity = {
        (str(article['source']), str(article['slug'])): (index, article)
        for index, article in enumerate(articles)
    }
    words = [_article_words(article) for article in articles]
    feature_sets = [article_feature_terms(article) for article in articles]
    for source_index, (source_key, rows) in enumerate(value.items()):
        _require(isinstance(rows, list) and len(rows) == 5,
                 f'related list {source_key!r} must contain exactly five rows')
        seen = set()
        for row_index, row in enumerate(rows):
            label = f'related {source_key!r} row {row_index}'
            _require(isinstance(row, dict), f'{label} must be an object')
            _require(set(row) == RELATED_ROW_KEYS, f'{label} has the wrong field set')
            identity = (row.get('source'), row.get('slug'))
            _require(identity in article_by_identity, f'{label} points to an unknown article')
            target_index, target = article_by_identity[identity]
            _require(target_index != source_index, f'{label} points to itself')
            _require(identity not in seen, f'{label} duplicates a related article')
            seen.add(identity)
            for field in ('slug', 'source', 'title', 'url'):
                _require(row.get(field) == target.get(field),
                         f'{label} {field} does not match the master index')
            score = row.get('score')
            _require(type(score) is float and math.isfinite(score) and 0 < score <= 1,
                     f'{label} score must be a finite float in (0, 1]')
            _require(float(f'{score:.6f}') == score,
                     f'{label} score must be rounded to at most six decimals')
            reasons = row.get('why')
            _require(isinstance(reasons, list) and 1 <= len(reasons) <= 3,
                     f'{label} why must contain one to three reasons')
            _require(len(reasons) == len(set(reasons)),
                     f'{label} why contains duplicate reasons')
            for reason in reasons:
                _require(isinstance(reason, str) and reason.startswith('shared: '),
                         f'{label} has an invalid why reason')
                term = reason[len('shared: '):]
                _require(NORMALIZED_TERM_RE.fullmatch(term) is not None,
                         f'{label} why has a non-normalized shared term')
                shared_entity = (
                    term in entity_sets[source_index]
                    and term in entity_sets[target_index]
                )
                shared_text = (
                    (
                        term in feature_sets[source_index]
                        and term in feature_sets[target_index]
                    )
                    or (
                        _contains_term(words[source_index], term)
                        and _contains_term(words[target_index], term)
                    )
                )
                _require(shared_entity or shared_text,
                         f'{label} why term {term!r} is not shared by both articles')

        expected_order = sorted(
            rows,
            key=lambda item: (
                -float(item['score']),
                f'{item["source"]}:{item["slug"]}',
            ),
        )
        _require(rows == expected_order,
                 f'related list {source_key!r} is not in deterministic score order')


def _normalized_privacy_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', value.casefold())


def _validate_privacy_keys(value: Any, label: str, path: str = '$') -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _normalized_privacy_key(str(key))
            _require(normalized not in FORBIDDEN_PRIVACY_KEYS,
                     f'{label} contains forbidden private-analytics key at {path}.{key}')
            _validate_privacy_keys(item, label, f'{path}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_privacy_keys(item, label, f'{path}[{index}]')


def _manifest_for(
    articles: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    families: Mapping[str, Any],
) -> Dict[str, Any]:
    source_counts = Counter(str(article.get('source') or '') for article in articles)
    family_counts = {
        family: len(families.get(family, []))
        if isinstance(families.get(family), list) else 0
        for family in FAMILIES
    }
    return {
        'schema_version': SCHEMA_VERSION,
        'dataset_version': snapshot.get('data_checksum'),
        'generated_at': snapshot.get('checked_at'),
        'article_count': len(articles),
        'source_counts': {source: source_counts[source] for source in SOURCES},
        'family_counts': family_counts,
        'endpoints': list(DATA_ENDPOINTS),
    }


def write_data_layer(
    site_dir: PathInput,
    source_articles_path: PathInput,
    snapshot_manifest: Mapping[str, Any],
    search_index: Mapping[str, Any],
    related: Mapping[str, Any],
    families: Mapping[str, Any],
) -> Dict[str, Any]:
    """Write the six deterministic ``data/`` endpoints into a site artifact."""
    source_path = Path(source_articles_path)
    try:
        article_bytes = source_path.read_bytes()
    except OSError as exc:
        raise ValueError(f'source article index could not be read: {exc}') from exc
    articles_value = _decode_json(article_bytes, 'source article index')
    _require(isinstance(articles_value, list), 'source article index must be an array')
    _require(isinstance(snapshot_manifest, Mapping), 'snapshot manifest must be an object')
    _require(isinstance(search_index, Mapping), 'search index must be an object')
    _require(isinstance(related, Mapping), 'related graph must be an object')
    _require(isinstance(families, Mapping), 'family index must be an object')

    articles: List[Mapping[str, Any]] = articles_value
    snapshot = dict(snapshot_manifest)
    family_value = dict(families)
    manifest = _manifest_for(articles, snapshot, family_value)
    output = Path(site_dir) / 'data'
    outputs = {
        'articles_index.json': article_bytes,
        'latest.json': _json_bytes(_latest_articles(articles)),
        'manifest.json': _json_bytes(manifest),
        'search_index.json': _json_bytes(dict(search_index)),
        'related.json': _json_bytes(dict(related)),
        'families.json': _json_bytes(family_value),
    }
    for name in DATA_ENDPOINT_NAMES:
        _atomic_write(output / name, outputs[name])
    return manifest


def data_bundle_checksum(site_dir: PathInput) -> str:
    """Hash each sorted endpoint path plus NUL plus its exact deployed bytes."""
    root = Path(site_dir)
    digest = hashlib.sha256()
    for name in DATA_ENDPOINT_NAMES:
        relative = Path('data') / name
        path = root / relative
        _require(path.is_file() and not path.is_symlink(),
                 f'data endpoint {relative.as_posix()} is missing or not a regular file')
        digest.update(relative.as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_data_layer(
    site_dir: PathInput,
    source_articles_path: PathInput,
    snapshot_manifest_path_or_dict: SnapshotInput,
    now: Optional[datetime] = None,
) -> Summary:
    """Fail closed unless all public endpoints form one current, coherent release."""
    root = Path(site_dir)
    data_dir = root / 'data'
    _require(data_dir.is_dir() and not data_dir.is_symlink(),
             'site artifact has no regular data directory')
    actual_files = {
        path.relative_to(data_dir).as_posix()
        for path in data_dir.rglob('*')
        if path.is_file() or path.is_symlink()
    }
    _require(actual_files == set(DATA_ENDPOINT_NAMES),
             'data directory endpoint set does not match the public contract')

    payloads: Dict[str, Any] = {}
    endpoint_bytes: Dict[str, bytes] = {}
    for name in DATA_ENDPOINT_NAMES:
        path = data_dir / name
        _require(path.is_file() and not path.is_symlink(),
                 f'data endpoint {name} is missing or not a regular file')
        try:
            endpoint_bytes[name] = path.read_bytes()
        except OSError as exc:
            raise ValueError(f'data endpoint {name} could not be read: {exc}') from exc
        payloads[name] = _decode_json(endpoint_bytes[name], f'data endpoint {name}')

    source_path = Path(source_articles_path)
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise ValueError(f'source article index could not be read: {exc}') from exc
    _require(endpoint_bytes['articles_index.json'] == source_bytes,
             'public articles_index.json is not byte-for-byte equal to its source')
    source_value = _decode_json(source_bytes, 'source article index')
    _require(payloads['articles_index.json'] == source_value,
             'public article index does not equal its parsed source')
    articles, source_counts, family_counts = _validate_articles(source_value)

    snapshot = _load_snapshot(snapshot_manifest_path_or_dict)
    manifest = payloads['manifest.json']
    _require(isinstance(manifest, dict) and set(manifest) == MANIFEST_KEYS,
             'data manifest has the wrong field set')
    _require(type(manifest.get('schema_version')) is int
             and manifest['schema_version'] == SCHEMA_VERSION,
             'data manifest has an unsupported schema_version')
    dataset_version = manifest.get('dataset_version')
    _require(isinstance(dataset_version, str)
             and SHA256_RE.fullmatch(dataset_version) is not None,
             'data manifest dataset_version must be a lowercase SHA-256 digest')
    _require(dataset_version == snapshot.get('data_checksum'),
             'data manifest dataset_version does not match the snapshot checksum')
    generated_at = manifest.get('generated_at')
    _require(generated_at == snapshot.get('checked_at'),
             'data manifest generated_at does not match the snapshot check time')
    generated = _generated_instant(generated_at)
    validation_now = now or datetime.now(timezone.utc)
    _require(isinstance(validation_now, datetime) and validation_now.tzinfo is not None,
             'data-layer validation clock must be timezone-aware')
    validation_now = validation_now.astimezone(timezone.utc)
    _require(generated <= validation_now + MAX_FUTURE_CLOCK_SKEW,
             'data manifest generated_at is implausibly far in the future')
    _require(validation_now - generated <= MAX_GENERATED_AGE,
             'data manifest generated_at is older than 16 hours')
    _require(type(manifest.get('article_count')) is int
             and manifest['article_count'] == len(articles),
             'data manifest article_count does not match the public index')
    snapshot_count = (
        snapshot.get('catalog_count')
        if snapshot.get('schema_version') == 2
        else snapshot.get('article_count')
    )
    _require(type(snapshot_count) is int and snapshot_count == len(articles),
             'snapshot manifest catalogue count does not match the public index')
    _require(manifest.get('source_counts') == source_counts,
             'data manifest source_counts do not match the public index')
    _require(all(count > 0 for count in source_counts.values()),
             'data manifest source counts must all be greater than zero')
    _require(manifest.get('family_counts') == family_counts,
             'data manifest family_counts do not match the public index')
    _require(manifest.get('endpoints') == list(DATA_ENDPOINTS),
             'data manifest endpoints do not match the complete sorted endpoint set')

    _validate_latest(payloads['latest.json'], articles)
    validated_family_counts = _validate_families(payloads['families.json'], articles)
    _require(validated_family_counts == family_counts,
             'families.json counts do not match article family assignments')
    entity_sets = _validate_search_index(
        payloads['search_index.json'],
        articles,
        len(endpoint_bytes['search_index.json']),
    )
    _validate_related(payloads['related.json'], articles, entity_sets)
    for name, value in payloads.items():
        _validate_privacy_keys(value, f'data endpoint {name}')

    return {
        'schema_version': SCHEMA_VERSION,
        'dataset_version': dataset_version,
        'generated_at': generated_at,
        'article_count': len(articles),
        'source_counts': source_counts,
        'family_counts': family_counts,
        'endpoint_count': len(DATA_ENDPOINT_NAMES),
        'data_bundle_sha256': data_bundle_checksum(root),
    }
