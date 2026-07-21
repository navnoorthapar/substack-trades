#!/usr/bin/env python3
"""Validate metadata-only publication registries and cross-link twins.

The Patreon and FX Empire feeds represented here are deliberately sparse.
They contain public catalogue metadata, never article bodies, excerpts,
engagement counts, subscriber information, or payment data.
"""

import copy
import difflib
import html
import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from article_briefs import build_article_brief
from research_taxonomy import classify_family


REGISTRY_SOURCES: Tuple[str, ...] = ('patreon', 'fxempire')
SOURCE_PRIORITY = {
    'substack': 0,
    'medium': 1,
    'patreon': 2,
    'fxempire': 3,
}
PATREON_KEYS: Set[str] = {
    'source_id', 'title', 'url', 'post_date', 'access',
}
FXEMPIRE_KEYS: Set[str] = {
    'source_id', 'title', 'url', 'post_date',
}
OVERRIDE_KEYS: Set[str] = {
    'source', 'source_id', 'target_source', 'target_slug', 'decision', 'reason',
}
NUMBER_WORDS = {
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
    'fourteen': '14', 'fifteen': '15', 'sixteen': '16',
    'seventeen': '17', 'eighteen': '18', 'nineteen': '19', 'twenty': '20',
}


def normalize_title(value: object) -> str:
    """Normalize harmless typography without erasing meaningful words."""
    text = html.unescape(str(value or '')).replace('\u00a0', ' ')
    text = text.replace('&', ' and ')
    text = unicodedata.normalize('NFKD', text.casefold())
    text = ''.join(
        character for character in text
        if not unicodedata.combining(character)
    )
    tokens = re.sub(r'[^a-z0-9]+', ' ', text).split()
    return ' '.join(NUMBER_WORDS.get(token, token) for token in tokens)


def _publication_day(value: object) -> Optional[date]:
    text = str(value or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T.*)?', text):
        return None
    try:
        return datetime.strptime(text[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _date_distance(left: object, right: object) -> int:
    left_day = _publication_day(left)
    right_day = _publication_day(right)
    if left_day is None or right_day is None:
        return 10 ** 9
    return abs((left_day - right_day).days)


def _clean_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string')
    cleaned = value.strip()
    if not cleaned or any(ord(character) < 32 for character in cleaned):
        raise ValueError(f'{label} must be non-empty and contain no controls')
    return cleaned


def _validate_https_url(value: object, source: str, source_id: str) -> str:
    url = _clean_text(value, f'{source} url')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.query or parsed.fragment:
        raise ValueError(f'{source} url must be a canonical HTTPS URL')

    escaped_id = re.escape(source_id)
    if source == 'patreon':
        path_pattern = rf'/NavnoorBawa/posts/[a-z0-9-]+-{escaped_id}'
        valid = (
            parsed.hostname == 'www.patreon.com'
            and parsed.port is None
            and re.fullmatch(path_pattern, parsed.path) is not None
        )
    elif source == 'fxempire':
        path_pattern = (
            rf'/(?:forecasts|news|education)/article/[a-z0-9-]+-{escaped_id}'
        )
        valid = (
            parsed.hostname == 'www.fxempire.com'
            and parsed.port is None
            and re.fullmatch(path_pattern, parsed.path) is not None
        )
    else:
        raise ValueError(f'unsupported registry source: {source!r}')
    if not valid:
        raise ValueError(f'{source} url does not match source_id {source_id!r}')
    return url


def validate_registry(
        records: object, source: str,
) -> List[Dict[str, object]]:
    """Return canonical copies after strict, privacy-preserving validation."""
    if source not in REGISTRY_SOURCES:
        raise ValueError(f'unsupported registry source: {source!r}')
    if not isinstance(records, list):
        raise ValueError(f'{source} registry must be a JSON array')
    required = PATREON_KEYS if source == 'patreon' else FXEMPIRE_KEYS
    seen_ids: Set[str] = set()
    seen_urls: Set[str] = set()
    result: List[Dict[str, object]] = []

    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise ValueError(f'{source} registry row {index} must be an object')
        if set(raw) != required:
            missing = sorted(required - set(raw))
            extra = sorted(set(raw) - required)
            raise ValueError(
                f'{source} registry row {index} has invalid keys; '
                f'missing={missing}, extra={extra}'
            )
        source_id = _clean_text(raw.get('source_id'), 'source_id')
        if not source_id.isdigit():
            raise ValueError(f'{source} source_id must contain only digits')
        title = _clean_text(raw.get('title'), f'{source} title')
        post_date = _clean_text(raw.get('post_date'), f'{source} post_date')
        if _publication_day(post_date) is None:
            raise ValueError(f'{source} post_date must begin with a valid ISO date')
        url = _validate_https_url(raw.get('url'), source, source_id)
        if source_id in seen_ids or url in seen_urls:
            raise ValueError(f'{source} registry contains a duplicate identity')
        seen_ids.add(source_id)
        seen_urls.add(url)

        item: Dict[str, object] = {
            'source_id': source_id,
            'title': title,
            'url': url,
            'post_date': post_date,
        }
        if source == 'patreon':
            access = raw.get('access')
            if access not in ('public', 'paid'):
                raise ValueError('patreon access must be "public" or "paid"')
            item['access'] = access
        result.append(item)

    # Stable newest-first order keeps diffs reviewable and rejects silent feed
    # truncation from being hidden among older entries.
    return sorted(
        result,
        key=lambda row: (str(row['post_date']), str(row['source_id'])),
        reverse=True,
    )


def load_registry(path: Path, source: str) -> List[Dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'{source} registry is not valid JSON: {exc}') from exc
    return validate_registry(payload, source)


def load_overrides(path: Path) -> List[Dict[str, str]]:
    """Load reviewed match/distinct decisions from a versioned JSON object."""
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'registry overrides are not valid JSON: {exc}') from exc
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('registry overrides require schema_version 1')
    entries = payload.get('overrides')
    if not isinstance(entries, list):
        raise ValueError('registry overrides require an overrides array')

    result: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict) or set(raw) != OVERRIDE_KEYS:
            raise ValueError(f'registry override {index} has invalid keys')
        item = {key: _clean_text(raw.get(key), key) for key in OVERRIDE_KEYS}
        if item['source'] not in REGISTRY_SOURCES:
            raise ValueError(f'registry override {index} has an invalid source')
        if item['decision'] not in ('match', 'distinct'):
            raise ValueError(f'registry override {index} has an invalid decision')
        if item['target_source'] not in SOURCE_PRIORITY:
            raise ValueError(f'registry override {index} has an invalid target source')
        identity = (item['source'], item['source_id'])
        if identity in seen:
            raise ValueError(f'registry override {index} duplicates an identity')
        seen.add(identity)
        result.append(item)
    return result


def registry_slug(record: Mapping[str, object], source: str) -> str:
    """Return the stable, URL-derived ASCII slug (including source ID)."""
    source_id = str(record.get('source_id') or '')
    url = _validate_https_url(record.get('url'), source, source_id)
    slug = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
    if not re.fullmatch(r'[a-z0-9-]+', slug):
        raise ValueError(f'{source} registry URL has an unsafe slug')
    return slug


def registry_to_post(
        record: Mapping[str, object], source: str,
) -> Dict[str, object]:
    """Convert a sparse registry row to an index-compatible empty-body post."""
    rows = validate_registry([dict(record)], source)
    item = rows[0]
    post: Dict[str, object] = {
        'source': source,
        'source_id': item['source_id'],
        'slug': registry_slug(item, source),
        'title': item['title'],
        'subtitle': '',
        'post_date': item['post_date'],
        'url': item['url'],
        'audience': item['access'] if source == 'patreon' else 'public',
        'wordcount': 0,
        'content_status': 'registry',
        'body_text': '',
    }
    if source == 'patreon':
        post['access'] = item['access']
    post['brief'] = build_article_brief(post)
    post['family'] = classify_family(post)
    return post


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _candidate_matches(
        record: Mapping[str, object], candidates: Sequence[Dict[str, object]],
) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    title = normalize_title(record.get('title'))
    dated = [
        candidate for candidate in candidates
        if _date_distance(record.get('post_date'), candidate.get('post_date')) <= 7
    ]
    exact = [
        candidate for candidate in dated
        if normalize_title(candidate.get('title')) == title
    ]
    if len(exact) == 1:
        return exact[0], 'normalized-title-and-date'
    if len(exact) > 1:
        return None, None

    scored: List[Tuple[float, Dict[str, object]]] = []
    for candidate in dated:
        candidate_title = normalize_title(candidate.get('title'))
        ratio = difflib.SequenceMatcher(None, title, candidate_title).ratio()
        jaccard = _token_jaccard(title, candidate_title)
        if ratio >= 0.90 and jaccard >= 0.80:
            scored.append((ratio + jaccard, candidate))
    scored.sort(key=lambda row: row[0], reverse=True)
    if not scored:
        return None, None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.12:
        return None, None
    return scored[0][1], 'strict-title-similarity-and-date'


def _override_target(
        record: Mapping[str, object], source: str,
        candidates: Sequence[Dict[str, object]],
        overrides: Sequence[Mapping[str, str]],
) -> Tuple[Optional[Dict[str, object]], Optional[str], bool]:
    source_id = str(record.get('source_id') or '')
    relevant = [
        item for item in overrides
        if item.get('source') == source and item.get('source_id') == source_id
    ]
    if not relevant:
        return None, None, False
    if len(relevant) != 1:
        raise ValueError(f'multiple overrides for {source}:{source_id}')
    override = relevant[0]
    if override.get('decision') == 'distinct':
        return None, 'reviewed-distinct', True
    matches = [
        candidate for candidate in candidates
        if candidate.get('source') == override.get('target_source')
        and candidate.get('slug') == override.get('target_slug')
    ]
    if len(matches) != 1:
        raise ValueError(
            f'override for {source}:{source_id} resolves to {len(matches)} targets'
        )
    return matches[0], 'reviewed-override', True


def _prefer_incoming_alternate(
        source: str, incoming: Mapping[str, object],
        incumbent_record: Optional[Mapping[str, object]],
) -> bool:
    if incumbent_record is None:
        return True
    if source == 'patreon':
        incoming_public = incoming.get('access') == 'public'
        incumbent_public = incumbent_record.get('access') == 'public'
        if incoming_public != incumbent_public:
            return incoming_public
    return str(incoming.get('post_date') or '') > str(
        incumbent_record.get('post_date') or ''
    )


def crosslink_registry(
        base_posts: Sequence[Mapping[str, object]],
        registry_records: Sequence[Mapping[str, object]],
        source: str,
        overrides: Optional[Sequence[Mapping[str, str]]] = None,
) -> Tuple[List[Dict[str, object]], List[Dict[str, str]]]:
    """Cross-link registry twins while retaining every ambiguous record.

    The returned report is public provenance only: source identities, target
    identities, and the deterministic matching rule. No private data enters it.
    """
    records = validate_registry([dict(record) for record in registry_records], source)
    decisions = list(overrides or [])
    result = [copy.deepcopy(dict(post)) for post in base_posts]
    # Earlier/higher-priority sources may be canonical targets.  This permits
    # Patreon-to-FX Empire links when no Substack or Medium copy exists while
    # keeping output stable regardless of title similarity among peers.
    incoming_priority = SOURCE_PRIORITY[source]
    candidates = [
        post for post in result
        if SOURCE_PRIORITY.get(str(post.get('source') or ''), 99)
        < incoming_priority
    ]
    alternates: Dict[Tuple[int, str], Mapping[str, object]] = {}
    report: List[Dict[str, str]] = []

    for record in records:
        target, reason, reviewed = _override_target(
            record, source, candidates, decisions,
        )
        if not reviewed:
            target, reason = _candidate_matches(record, candidates)
        if target is None:
            post = registry_to_post(record, source)
            result.append(post)
            report.append({
                'source': source,
                'source_id': str(record['source_id']),
                'decision': reason or 'distinct',
                'target': '',
            })
            continue

        target_index = result.index(target)
        alternate_urls = target.setdefault('alternate_urls', {})
        if not isinstance(alternate_urls, dict):
            raise ValueError('target alternate_urls must be an object')
        incumbent = alternates.get((target_index, source))
        if _prefer_incoming_alternate(source, record, incumbent):
            displaced = incumbent
            alternate_urls[source] = record['url']
            alternates[(target_index, source)] = record
            if displaced is not None:
                retained = registry_to_post(displaced, source)
                retained['alternate_urls'] = {
                    str(target.get('source')): str(target.get('url') or ''),
                }
                result.append(retained)
        else:
            retained = registry_to_post(record, source)
            retained['alternate_urls'] = {
                str(target.get('source')): str(target.get('url') or ''),
            }
            result.append(retained)

        report.append({
            'source': source,
            'source_id': str(record['source_id']),
            'decision': str(reason or 'matched'),
            'target': f"{target.get('source')}:{target.get('slug')}",
        })

    result.sort(
        key=lambda post: (
            str(post.get('post_date') or ''),
            -SOURCE_PRIORITY.get(str(post.get('source') or ''), 99),
            str(post.get('slug') or ''),
        ),
        reverse=True,
    )
    return result, report


def registry_article_metadata(post: Mapping[str, object]) -> Dict[str, object]:
    """Strip the implementation-only empty body from a registry post."""
    keys = (
        'source', 'source_id', 'slug', 'title', 'subtitle', 'post_date', 'url',
        'audience', 'wordcount', 'content_status', 'brief', 'family',
        'alternate_urls', 'access',
    )
    return {key: copy.deepcopy(post[key]) for key in keys if key in post}
