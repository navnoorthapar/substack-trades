#!/usr/bin/env python3
"""Verify that the public terminal serves the exact dataset just deployed."""

import argparse
import hashlib
import json
import re
import ssl
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


MAX_RESPONSE_BYTES = 12 * 1024 * 1024
REQUIRED_META = {
    'nrt-revision',
    'nrt-article-count',
    'nrt-observation-count',
    'nrt-data-checksum',
}
REQUIRED_ELEMENT_IDS = {
    'search',
    'filter-rail',
    'main-panel',
    'data-table',
    'table-body',
    'inspector',
}
CHECKSUM_RE = re.compile(r'^[0-9a-f]{64}$')


class TerminalHTMLParser(HTMLParser):
    """Collect the small set of deployment markers needed by the smoke test."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.element_ids = set()
        self.title_parts = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get('id')
        if element_id:
            self.element_ids.add(element_id)
        if tag.casefold() == 'meta':
            name = attributes.get('name')
            if name:
                self.meta[name.casefold()] = attributes.get('content', '')
        elif tag.casefold() == 'title':
            self._in_title = True

    def handle_endtag(self, tag):
        if tag.casefold() == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)

    @property
    def title(self):
        return ''.join(self.title_parts).strip()


def load_list_count(path, label):
    """Load an expected snapshot and return its record count."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception as exc:
        raise ValueError(f'{label} could not be read: {exc}') from exc
    if not isinstance(value, list) or not value:
        raise ValueError(f'{label} must be a non-empty JSON list')
    return len(value)


def snapshot_checksum(articles_path, observations_path):
    """Reproduce the manifest digest from the exact deployed input bytes."""
    digest = hashlib.sha256()
    digest.update(Path(articles_path).read_bytes())
    digest.update(b'\0')
    digest.update(Path(observations_path).read_bytes())
    return digest.hexdigest()


def validate_html(
    html,
    expected_revision,
    expected_articles,
    expected_observations,
    expected_checksum,
):
    """Validate provenance metadata and the terminal's essential UI shell."""
    parser = TerminalHTMLParser()
    parser.feed(html)
    parser.close()

    missing_meta = sorted(REQUIRED_META - parser.meta.keys())
    if missing_meta:
        raise ValueError(f'missing deployment metadata: {", ".join(missing_meta)}')

    if parser.meta['nrt-revision'] != expected_revision:
        raise ValueError(
            'deployed revision does not match the requested revision '
            f'({parser.meta["nrt-revision"]!r} != {expected_revision!r})'
        )

    expected_counts = {
        'nrt-article-count': expected_articles,
        'nrt-observation-count': expected_observations,
    }
    for name, expected in expected_counts.items():
        try:
            actual = int(parser.meta[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{name} is not an integer') from exc
        if actual != expected:
            raise ValueError(f'{name} is {actual}, expected {expected}')

    checksum = parser.meta['nrt-data-checksum']
    if not CHECKSUM_RE.fullmatch(checksum):
        raise ValueError('nrt-data-checksum is not a lowercase SHA-256 digest')
    if checksum != expected_checksum:
        raise ValueError(
            f'nrt-data-checksum is {checksum}, expected {expected_checksum}'
        )

    missing_ids = sorted(REQUIRED_ELEMENT_IDS - parser.element_ids)
    if missing_ids:
        raise ValueError(f'missing core interface elements: {", ".join(missing_ids)}')
    if parser.title != 'Navnoor Research Terminal':
        raise ValueError(f'unexpected page title: {parser.title!r}')


def cache_busted_url(url, revision, attempt):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['nrt_smoke_revision'] = revision
    query['nrt_smoke_attempt'] = str(attempt)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def fetch_html(url, revision, attempt, timeout):
    """Fetch one uncached copy of the deployed page over verified HTTPS."""
    requested_url = cache_busted_url(url, revision, attempt)
    request = Request(
        requested_url,
        headers={
            'Accept': 'text/html',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'User-Agent': 'navnoor-terminal-deployment-smoke/1.0',
        },
    )
    context = ssl.create_default_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        final_url = response.geturl()
        if urlsplit(final_url).scheme != 'https':
            raise ValueError(f'deployment redirected away from HTTPS: {final_url}')
        status = getattr(response, 'status', None)
        if status is None:
            status = response.getcode()
        if status != 200:
            raise ValueError(f'deployment returned HTTP {status}')
        content_type = response.headers.get_content_type()
        if content_type != 'text/html':
            raise ValueError(f'deployment returned {content_type}, not text/html')
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError(f'deployed page exceeds {MAX_RESPONSE_BYTES} bytes')
    return payload.decode('utf-8')


def smoke_test(
    url,
    expected_revision,
    expected_articles,
    expected_observations,
    expected_checksum,
    retries=12,
    retry_delay=10.0,
    timeout=20.0,
):
    """Retry through Pages propagation and fail unless the exact release is live."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.netloc:
        raise ValueError('deployment URL must be an absolute HTTPS URL')
    if retries < 1:
        raise ValueError('retries must be at least 1')
    if retry_delay < 0 or timeout <= 0:
        raise ValueError('retry delay cannot be negative and timeout must be positive')

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            html = fetch_html(url, expected_revision, attempt, timeout)
            validate_html(
                html,
                expected_revision,
                expected_articles,
                expected_observations,
                expected_checksum,
            )
            print(
                f'Smoke test passed on attempt {attempt}: HTTPS, revision '
                f'{expected_revision[:12]}, {expected_articles} articles, '
                f'{expected_observations} observations.'
            )
            return
        except Exception as exc:  # Retries intentionally cover HTTP and stale-cache failures.
            last_error = exc
            print(f'Smoke attempt {attempt}/{retries} failed: {exc}', file=sys.stderr)
            if attempt < retries:
                time.sleep(retry_delay)
    raise ValueError(f'deployment did not become healthy: {last_error}')


def main():
    parser = argparse.ArgumentParser(
        description='Verify the exact Navnoor Research Terminal release is live.',
    )
    parser.add_argument('url', help='deployed GitHub Pages URL')
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--articles-file', type=Path, required=True)
    parser.add_argument('--observations-file', type=Path, required=True)
    parser.add_argument('--retries', type=int, default=12)
    parser.add_argument('--retry-delay', type=float, default=10.0)
    parser.add_argument('--timeout', type=float, default=20.0)
    args = parser.parse_args()

    try:
        article_count = load_list_count(args.articles_file, 'article snapshot')
        observation_count = load_list_count(args.observations_file, 'observation snapshot')
        checksum = snapshot_checksum(args.articles_file, args.observations_file)
        smoke_test(
            args.url,
            args.expected_revision,
            article_count,
            observation_count,
            checksum,
            retries=args.retries,
            retry_delay=args.retry_delay,
            timeout=args.timeout,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'SMOKE TEST FAILED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
