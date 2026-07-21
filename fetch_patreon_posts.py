#!/usr/bin/env python3
"""Fetch the author's sparse, public Patreon catalogue metadata.

Only title, canonical URL, publication date, source ID, and anonymous access
state are persisted.  No body, teaser, engagement, subscriber, revenue, or
pledge fields are requested or written.
"""

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from fetch_all_posts import SSL_CONTEXT, atomic_write_json, utc_now
from registry_sources import validate_registry


ROOT = Path(__file__).parent
OUTPUT_PATH = Path(os.environ.get(
    'PATREON_OUTPUT', ROOT / 'patreon_registry.json',
)).expanduser()
PREVIOUS_PATH = Path(os.environ.get(
    'PREVIOUS_PATREON', ROOT / 'patreon_registry.json',
)).expanduser()
_status_output = os.environ.get('PATREON_STATUS_OUTPUT')
STATUS_PATH = Path(_status_output).expanduser() if _status_output else None

CAMPAIGN_ID = '14781816'
API_ORIGIN = 'https://www.patreon.com'
API_PATH = '/api/posts'
SPARSE_FIELDS = (
    'title,published_at,url,current_user_can_view,'
    'was_posted_by_campaign_owner'
)
INITIAL_URL = API_ORIGIN + API_PATH + '?' + urllib.parse.urlencode({
    'filter[campaign_id]': CAMPAIGN_ID,
    'sort': '-published_at',
    'page[count]': '100',
    'fields[post]': SPARSE_FIELDS,
})
HEADERS = {
    'Accept': 'application/vnd.api+json',
    'User-Agent': 'NavnoorResearchRegistry/1.0 (+public metadata only)',
}
MAX_RESPONSE_BYTES = 500_000
MAX_PAGES = 10
MAX_POSTS = 500


def _canonical_api_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('Patreon pagination link must be a string')
    parsed = urllib.parse.urlsplit(value)
    if (
            parsed.scheme != 'https'
            or parsed.hostname != 'www.patreon.com'
            or parsed.port is not None
            or parsed.path != API_PATH
            or parsed.fragment
    ):
        raise ValueError('Patreon pagination left the approved API origin')
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if query.get('filter[campaign_id]') != [CAMPAIGN_ID]:
        raise ValueError('Patreon pagination changed the approved campaign')
    return value


def request_json(url: str, attempts: int = 3) -> Mapping[str, Any]:
    """Download one bounded JSON:API page with verified TLS and origin."""
    _canonical_api_url(url)
    if attempts < 1:
        raise ValueError('Patreon fetch attempts must be at least one')
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(
                    request, timeout=30, context=SSL_CONTEXT,
            ) as response:
                final_url = response.geturl()
                _canonical_api_url(final_url)
                content_type = response.headers.get_content_type()
                if content_type not in ('application/vnd.api+json', 'application/json'):
                    raise ValueError('Patreon returned a non-JSON content type')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('Patreon response exceeded the byte limit')
            payload = json.loads(raw.decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('Patreon returned a non-object JSON document')
            return payload
        except (
                OSError, TimeoutError, UnicodeDecodeError,
                urllib.error.URLError, json.JSONDecodeError, ValueError,
        ) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    if last_error is None:
        raise RuntimeError('Patreon request failed without an error')
    raise last_error


def _publication_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('Patreon published_at must be a string')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Patreon published_at must be an ISO timestamp') from exc
    return parsed.date().isoformat()


def _post_row(resource: object) -> Dict[str, object]:
    if not isinstance(resource, dict) or resource.get('type') != 'post':
        raise ValueError('Patreon data must contain post resources')
    source_id = resource.get('id')
    attributes = resource.get('attributes')
    if not isinstance(source_id, str) or not source_id.isdigit():
        raise ValueError('Patreon post id must contain only digits')
    if not isinstance(attributes, dict):
        raise ValueError('Patreon post attributes must be an object')
    required = {
        'title', 'published_at', 'url', 'current_user_can_view',
        'was_posted_by_campaign_owner',
    }
    if not required.issubset(attributes):
        raise ValueError('Patreon sparse response omitted a requested field')
    if type(attributes['current_user_can_view']) is not bool:
        raise ValueError('Patreon anonymous access flag must be boolean')
    if attributes['was_posted_by_campaign_owner'] is not True:
        raise ValueError('Patreon resource is not owned by the target campaign')
    title = attributes['title']
    if not isinstance(title, str) or not title.strip():
        raise ValueError('Patreon title must be non-empty')

    return {
        'source_id': source_id,
        'title': title.strip(),
        'url': attributes['url'],
        'post_date': _publication_date(attributes['published_at']),
        # This is only the anonymous viewer's capability; no payment or
        # private creator-dashboard field is requested.
        'access': (
            'public' if attributes['current_user_can_view'] else 'paid'
        ),
    }


PageFetcher = Callable[[str], Mapping[str, Any]]


def fetch_registry(fetch_page: PageFetcher = request_json) -> List[Dict[str, object]]:
    """Follow the bounded JSON:API cursor chain and return validated rows."""
    next_url: Optional[str] = INITIAL_URL
    visited: set = set()
    rows: List[Dict[str, object]] = []
    while next_url is not None:
        if len(visited) >= MAX_PAGES:
            raise ValueError('Patreon pagination exceeded the page limit')
        next_url = _canonical_api_url(next_url)
        if next_url in visited:
            raise ValueError('Patreon pagination contains a cursor loop')
        visited.add(next_url)
        payload = fetch_page(next_url)
        data = payload.get('data')
        links = payload.get('links', {})
        if not isinstance(data, list) or not isinstance(links, dict):
            raise ValueError('Patreon returned an unexpected JSON:API shape')
        rows.extend(_post_row(resource) for resource in data)
        if len(rows) > MAX_POSTS:
            raise ValueError('Patreon registry exceeded the post limit')
        candidate = links.get('next')
        if candidate is not None and not isinstance(candidate, str):
            raise ValueError('Patreon next link must be a string or null')
        next_url = candidate
    if not rows:
        raise ValueError('Patreon registry is unexpectedly empty')
    return validate_registry(rows, 'patreon')


def _read_previous(path: Path) -> List[Dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'cached Patreon registry is unavailable: {exc}') from exc
    rows = validate_registry(payload, 'patreon')
    if not rows:
        raise ValueError('cached Patreon registry is empty')
    return rows


def refresh_registry(
        previous_path: Path = PREVIOUS_PATH,
        fetch_page: PageFetcher = request_json,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """Fetch a complete registry or safely retain the last valid snapshot."""
    previous: List[Dict[str, object]] = []
    previous_error: Optional[Exception] = None
    if previous_path.exists():
        try:
            previous = _read_previous(previous_path)
        except ValueError as exc:
            previous_error = exc
    try:
        current = fetch_registry(fetch_page)
        minimum = max(1, math.ceil(len(previous) * 0.85))
        if previous and len(current) < minimum:
            raise ValueError(
                f'Patreon result shrank from {len(previous)} to {len(current)} rows'
            )
        return current, {
            'schema_version': 1,
            'source': 'patreon',
            'checked_at': utc_now(),
            'status': 'fresh',
            'published_count': len(current),
            'newest': current[0]['post_date'],
        }
    except Exception as exc:
        if not previous:
            if previous_error is not None:
                raise ValueError(
                    f'Patreon fetch failed and cached registry is invalid: '
                    f'{previous_error}'
                ) from exc
            raise
        return previous, {
            'schema_version': 1,
            'source': 'patreon',
            'checked_at': utc_now(),
            'status': 'cached-fallback',
            'published_count': len(previous),
            'newest': previous[0]['post_date'],
            'error': str(exc),
        }


def main() -> None:
    rows, status = refresh_registry()
    atomic_write_json(OUTPUT_PATH, rows)
    if STATUS_PATH is not None:
        atomic_write_json(STATUS_PATH, status)
    print(
        f"Patreon registry: {len(rows)} metadata-only rows "
        f"({status['status']})"
    )


if __name__ == '__main__':
    main()
