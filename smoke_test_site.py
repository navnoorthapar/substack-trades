#!/usr/bin/env python3
"""Verify that the public terminal serves the exact dataset just deployed."""

import argparse
import binascii
import hashlib
import html as html_lib
import json
import re
import ssl
import struct
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from data_contract import (
    DATA_ENDPOINT_NAMES,
    data_bundle_checksum,
    validate_data_layer,
)
from share_cards import render_article_stub


MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_DEFERRED_BYTES = 2 * 1024 * 1024
MAX_SUPPORT_ASSET_BYTES = 600 * 1024
MAX_SHARE_CARD_BYTES = 100_000
MAX_SHARE_STUB_BYTES = 64 * 1024
DATA_ENDPOINT_MAX_BYTES = {
    'articles_index.json': 4 * 1024 * 1024,
    'latest.json': 256 * 1024,
    'manifest.json': 64 * 1024,
    'search_index.json': 500_000,
    'related.json': 4 * 1024 * 1024,
    'families.json': 512 * 1024,
}
HTML_ASSET_NAME = 'index.html'
DEFERRED_ASSET_NAME = 'article_briefs.json'
OBSERVATION_ASSET_NAME = 'observations.json'
SUPPORT_ASSET_NAMES = (
    'favicon.svg', 'og.jpg', 'robots.txt', 'site.webmanifest', 'sitemap.xml',
)
SUPPORT_CONTENT_TYPES = {
    'favicon.svg': {'image/svg+xml'},
    'og.jpg': {'image/jpeg'},
    'robots.txt': {'text/plain'},
    'site.webmanifest': {'application/manifest+json', 'application/json', 'text/plain'},
    'sitemap.xml': {'application/xml', 'text/xml', 'text/plain'},
}
REQUIRED_META = {
    'nrt-revision',
    'nrt-article-count',
    'nrt-observation-count',
    'nrt-data-checksum',
    'nrt-brief-archive-sha256',
    'nrt-observation-archive-sha256',
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
ASSET_DIGEST_META = {
    DEFERRED_ASSET_NAME: 'nrt-brief-archive-sha256',
    OBSERVATION_ASSET_NAME: 'nrt-observation-archive-sha256',
}
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
PNG_HEADER = (1200, 630, 8, 3, 0, 0, 0)


class TerminalHTMLParser(HTMLParser):
    """Collect the small set of deployment markers needed by the smoke test."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.meta_values = {}
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
                normalized_name = name.casefold()
                content = attributes.get('content', '')
                self.meta[normalized_name] = content
                self.meta_values.setdefault(normalized_name, []).append(content)
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


def load_content_article_count(path):
    """Count only body-backed rows represented in the consumer terminal UI."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception as exc:
        raise ValueError(f'article snapshot could not be read: {exc}') from exc
    if not isinstance(value, list) or not value:
        raise ValueError('article snapshot must be a non-empty JSON list')
    if not all(
        isinstance(article, dict) and isinstance(article.get('content_status'), str)
        for article in value
    ):
        raise ValueError('article snapshot has a record without content_status')
    count = sum(article['content_status'] != 'registry' for article in value)
    if count < 1:
        raise ValueError('article snapshot has no content-backed terminal articles')
    return count


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
    return embedded_asset_digests(html)


def embedded_asset_digests(html):
    """Return the exact deferred-asset digests bound into the tested HTML."""
    parser = TerminalHTMLParser()
    parser.feed(html)
    parser.close()
    digests = {}
    for asset_name, meta_name in ASSET_DIGEST_META.items():
        values = parser.meta_values.get(meta_name, [])
        if len(values) != 1:
            raise ValueError(
                f'expected exactly one embedded SHA-256 digest for {asset_name}; '
                f'found {len(values)}'
            )
        if not CHECKSUM_RE.fullmatch(values[0]):
            raise ValueError(
                f'embedded digest for {asset_name} is not a lowercase SHA-256 digest'
            )
        digests[asset_name] = values[0]
    return digests


def cache_busted_url(url, revision, attempt):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['nrt_smoke_revision'] = revision
    query['nrt_smoke_attempt'] = str(attempt)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def same_origin(first_url, second_url):
    """Return whether two absolute URLs share scheme, host, and effective port."""
    first = urlsplit(first_url)
    second = urlsplit(second_url)

    def origin(parts):
        default_port = 443 if parts.scheme.casefold() == 'https' else 80
        return (
            parts.scheme.casefold(),
            (parts.hostname or '').casefold(),
            parts.port or default_port,
        )

    return origin(first) == origin(second)


def deferred_asset_url(page_url, asset_name=DEFERRED_ASSET_NAME):
    """Resolve a release-bound JSON asset beside the page, never off-origin."""
    if asset_name not in ASSET_DIGEST_META:
        raise ValueError(f'unsupported deferred asset: {asset_name}')
    return sibling_asset_url(page_url, asset_name)


def sibling_asset_url(page_url, asset_name):
    """Resolve one trusted basename beside the page without query inheritance."""
    if not asset_name or asset_name != Path(asset_name).name:
        raise ValueError('release asset name must be a plain basename')
    parts = urlsplit(page_url)
    path = parts.path or '/'
    if path.endswith('/'):
        asset_path = f'{path}{asset_name}'
    else:
        leaf = path.rsplit('/', 1)[-1].casefold()
        if leaf.endswith(('.html', '.htm')):
            parent = path.rsplit('/', 1)[0]
            asset_path = f'{parent}/{asset_name}'
        else:
            asset_path = f'{path}/{asset_name}'
    return urlunsplit((parts.scheme, parts.netloc, asset_path, '', ''))


def data_asset_url(page_url, asset_name):
    """Resolve one allowlisted nested ``data/`` endpoint beside the page."""
    if asset_name not in DATA_ENDPOINT_NAMES:
        raise ValueError(f'unsupported data endpoint: {asset_name}')
    parts = urlsplit(page_url)
    path = parts.path or '/'
    if path.endswith('/'):
        parent = path
    else:
        leaf = path.rsplit('/', 1)[-1].casefold()
        if leaf.endswith(('.html', '.htm')):
            parent = path.rsplit('/', 1)[0] + '/'
        else:
            parent = path + '/'
    asset_path = f'{parent}data/{asset_name}'
    return urlunsplit((parts.scheme, parts.netloc, asset_path, '', ''))


def data_payload_checksum(payloads):
    """Reproduce ``data_bundle_checksum`` from fetched endpoint bytes."""
    if set(payloads) != set(DATA_ENDPOINT_NAMES):
        raise ValueError('fetched data endpoint set does not match the public contract')
    digest = hashlib.sha256()
    for asset_name in DATA_ENDPOINT_NAMES:
        payload = payloads[asset_name]
        if not isinstance(payload, bytes):
            raise ValueError(f'fetched data endpoint {asset_name} is not bytes')
        digest.update(f'data/{asset_name}'.encode('utf-8'))
        digest.update(b'\0')
        digest.update(payload)
    return digest.hexdigest()


def fetch_data_bundle(page_url, revision, attempt, timeout):
    """Fetch all fixed data endpoints over same-origin HTTPS with strict caps."""
    def fetch_one(asset_name):
        asset_url = data_asset_url(page_url, asset_name)
        if not same_origin(page_url, asset_url):
            raise ValueError(f'data endpoint {asset_name} URL is not same-origin')
        requested_url = cache_busted_url(asset_url, revision, attempt)
        request = Request(
            requested_url,
            headers={
                'Accept': 'application/json',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'User-Agent': 'navnoor-terminal-deployment-smoke/1.0',
            },
        )
        context = verified_ssl_context()
        with urlopen(request, timeout=timeout, context=context) as response:
            final_url = response.geturl()
            if urlsplit(final_url).scheme != 'https':
                raise ValueError(
                    f'data endpoint {asset_name} redirected away from HTTPS: {final_url}'
                )
            if not same_origin(page_url, final_url):
                raise ValueError(
                    f'data endpoint {asset_name} redirected off-origin: {final_url}'
                )
            final_parts = urlsplit(final_url)
            expected_parts = urlsplit(asset_url)
            if final_parts.path != expected_parts.path:
                raise ValueError(
                    f'data endpoint {asset_name} redirected to another same-origin path'
                )
            status = getattr(response, 'status', None)
            if status is None:
                status = response.getcode()
            if status != 200:
                raise ValueError(f'data endpoint {asset_name} returned HTTP {status}')
            content_type = response.headers.get_content_type()
            if content_type != 'application/json':
                raise ValueError(
                    f'data endpoint {asset_name} returned {content_type}, '
                    'not application/json'
                )
            maximum = DATA_ENDPOINT_MAX_BYTES[asset_name]
            payload = response.read(maximum + 1)
        if not payload:
            raise ValueError(f'data endpoint {asset_name} is empty')
        if len(payload) > maximum:
            raise ValueError(f'data endpoint {asset_name} exceeds {maximum} bytes')
        return asset_name, payload

    with ThreadPoolExecutor(max_workers=len(DATA_ENDPOINT_NAMES)) as executor:
        return dict(executor.map(fetch_one, DATA_ENDPOINT_NAMES))


def validate_live_data_bundle(payloads, expected_articles, expected_checksum):
    """Run the public contract against fetched bytes and the trusted UI identity."""
    if set(payloads) != set(DATA_ENDPOINT_NAMES):
        raise ValueError('fetched data endpoint set does not match the public contract')
    try:
        articles = json.loads(payloads['articles_index.json'].decode('utf-8'))
        manifest = json.loads(payloads['manifest.json'].decode('utf-8'))
    except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'live data bundle is not valid UTF-8 JSON: {exc}') from exc
    if not isinstance(articles, list) or not articles:
        raise ValueError('live articles_index.json must be a non-empty array')
    if not all(isinstance(article, dict) for article in articles):
        raise ValueError('live articles_index.json contains a non-object record')
    content_articles = sum(
        article.get('content_status') != 'registry' for article in articles
    )
    if content_articles != expected_articles:
        raise ValueError(
            f'live data bundle contains {content_articles} content articles, '
            f'expected {expected_articles}'
        )
    if not isinstance(manifest, dict):
        raise ValueError('live data manifest must be an object')
    snapshot = {
        'data_checksum': expected_checksum,
        'checked_at': manifest.get('generated_at'),
        'article_count': len(articles),
    }
    with tempfile.TemporaryDirectory(prefix='nrt-live-data-') as directory:
        root = Path(directory)
        data_dir = root / 'data'
        data_dir.mkdir()
        for asset_name in DATA_ENDPOINT_NAMES:
            payload = payloads[asset_name]
            if not isinstance(payload, bytes):
                raise ValueError(f'live data endpoint {asset_name} is not bytes')
            (data_dir / asset_name).write_bytes(payload)
        source_path = root / 'source_articles.json'
        source_path.write_bytes(payloads['articles_index.json'])
        summary = validate_data_layer(root, source_path, snapshot)
        if summary['data_bundle_sha256'] != data_bundle_checksum(root):
            raise ValueError('live data bundle checksum changed during validation')
    return summary


def _share_article_key(article):
    """Return a deterministic newest-first key for share-proof sampling."""
    return (
        str(article.get('post_date') or ''),
        str(article.get('slug') or ''),
        str(article.get('url') or ''),
    )


def representative_share_articles(articles):
    """Select one content record and one registry-only record deterministically."""
    if not isinstance(articles, list) or not all(
        isinstance(article, dict) for article in articles
    ):
        raise ValueError('share proof catalogue must be an array of objects')
    content = [
        article for article in articles
        if article.get('content_status') != 'registry'
    ]
    registry = [
        article for article in articles
        if article.get('content_status') == 'registry'
    ]
    if not content or not registry:
        raise ValueError(
            'share proof requires content-backed and registry-only articles'
        )
    selected = [max(content, key=_share_article_key), max(registry, key=_share_article_key)]
    seen_slugs = set()
    for article in selected:
        slug = str(article.get('slug') or '')
        if (
            not slug or len(slug) > 180 or slug in {'.', '..'}
            or '/' in slug or '\\' in slug or '\x00' in slug
        ):
            raise ValueError(f'share proof article has unsafe slug: {slug!r}')
        if slug in seen_slugs:
            raise ValueError(f'share proof article slug is duplicated: {slug!r}')
        seen_slugs.add(slug)
    return selected


def share_proof_asset_names(articles):
    """Return the fixed card/stub path order bound into the proof digest."""
    names = []  # type: list[str]
    for article in representative_share_articles(articles):
        slug = str(article['slug'])
        names.extend((f'cards/{slug}.png', f'a/{slug}.html'))
    return tuple(names)


def share_proof_payload_checksum(payloads, articles):
    """Hash exact representative card/stub bytes in a path-bound order."""
    asset_names = share_proof_asset_names(articles)
    if set(payloads) != set(asset_names):
        raise ValueError('fetched share-proof asset set does not match its catalogue')
    digest = hashlib.sha256()
    for asset_name in asset_names:
        payload = payloads[asset_name]
        if not isinstance(payload, bytes):
            raise ValueError(f'fetched share-proof asset {asset_name} is not bytes')
        digest.update(asset_name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(payload)
        digest.update(b'\0')
    return digest.hexdigest()


def share_proof_bundle_checksum(directory, articles_path):
    """Hash the locally built representative card/stub proof bundle."""
    try:
        articles = json.loads(Path(articles_path).read_text(encoding='utf-8'))
    except Exception as exc:
        raise ValueError(f'share proof catalogue could not be read: {exc}') from exc
    root = Path(directory)
    payloads = {
        asset_name: (root / asset_name).read_bytes()
        for asset_name in share_proof_asset_names(articles)
    }
    return share_proof_payload_checksum(payloads, articles)


def _site_root(page_url):
    """Return the canonical site root used when the static stubs were built."""
    parts = urlsplit(page_url)
    path = parts.path or '/'
    if not path.endswith('/'):
        leaf = path.rsplit('/', 1)[-1].casefold()
        if leaf.endswith(('.html', '.htm')):
            path = path.rsplit('/', 1)[0] + '/'
        else:
            path += '/'
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip('/'), '', ''))


def share_asset_url(page_url, asset_name, articles):
    """Resolve one selected share-proof asset under ``cards/`` or ``a/``."""
    if asset_name not in share_proof_asset_names(articles):
        raise ValueError(f'unsupported share-proof asset: {asset_name}')
    parts = urlsplit(page_url)
    path = parts.path or '/'
    if not path.endswith('/'):
        leaf = path.rsplit('/', 1)[-1].casefold()
        if leaf.endswith(('.html', '.htm')):
            path = path.rsplit('/', 1)[0] + '/'
        else:
            path += '/'
    encoded_name = '/'.join(quote(segment, safe='-') for segment in asset_name.split('/'))
    return urlunsplit((parts.scheme, parts.netloc, f'{path}{encoded_name}', '', ''))


def _validate_share_card(payload, asset_name):
    """Require the exact deterministic 1200x630 indexed-PNG header contract."""
    header = payload[:33]
    if (
        len(header) != 33
        or header[:8] != PNG_SIGNATURE
        or header[8:12] != struct.pack('>I', 13)
        or header[12:16] != b'IHDR'
        or struct.unpack('>IIBBBBB', header[16:29]) != PNG_HEADER
        or struct.unpack('>I', header[29:33])[0]
        != binascii.crc32(header[12:29]) & 0xffffffff
    ):
        raise ValueError(
            f'share card {asset_name} is not an exact 1200x630 indexed PNG'
        )


def _stable_article_id(article):
    raw = str(article.get('url') or '').strip()
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
    identity = urlunsplit((scheme, host, path, parts.query, ''))
    return f'a_{hashlib.sha256(identity.encode("utf-8")).hexdigest()[:14]}'


def _validate_share_stub(payload, article, page_url, asset_name):
    """Require exact generated bytes and explicit OG/redirect semantics."""
    root = _site_root(page_url)
    article_id = _stable_article_id(article)
    expected = render_article_stub(article, article_id, root).encode('utf-8')
    if payload != expected:
        raise ValueError(f'share stub {asset_name} differs from trusted build bytes')

    slug = quote(str(article['slug']), safe='-')
    canonical = f'{root}/a/{slug}.html'
    image_url = f'{root}/cards/{slug}.png'
    if article.get('content_status') == 'registry':
        route = str(article.get('url') or '')
        if urlsplit(route).scheme != 'https':
            raise ValueError('registry-only share stub route is not HTTPS')
    else:
        route = f'../#selected={quote(article_id, safe="_- ").replace(" ", "%20")}'
    def escaped(value):
        return html_lib.escape(str(value), quote=True)

    required_tokens = (
        f'<link rel="canonical" href="{escaped(canonical)}">',
        f'<meta property="og:title" content="{escaped(article.get("title") or "Untitled research")}">',
        f'<meta property="og:url" content="{escaped(canonical)}">',
        f'<meta property="og:image" content="{escaped(image_url)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta http-equiv="refresh" content="0;url={escaped(route)}">',
        f'<script>location.replace({json.dumps(route, ensure_ascii=True)});</script>',
        f'<body><p><a href="{escaped(route)}">',
    )
    text = payload.decode('utf-8')
    missing = [token for token in required_tokens if token not in text]
    if missing:
        raise ValueError(f'share stub {asset_name} is missing required metadata')
    if article.get('content_status') == 'registry' and '#selected=' in text:
        raise ValueError('registry-only share stub incorrectly targets a local dossier')


def validate_share_proof(payloads, articles, page_url):
    """Semantically validate both representative card/stub pairs."""
    selected = representative_share_articles(articles)
    expected_names = share_proof_asset_names(articles)
    if set(payloads) != set(expected_names):
        raise ValueError('fetched share-proof asset set does not match its catalogue')
    for article in selected:
        slug = str(article['slug'])
        card_name = f'cards/{slug}.png'
        stub_name = f'a/{slug}.html'
        _validate_share_card(payloads[card_name], card_name)
        _validate_share_stub(payloads[stub_name], article, page_url, stub_name)
    return {
        'article_count': len(selected),
        'asset_count': len(expected_names),
        'content_slug': str(selected[0]['slug']),
        'registry_slug': str(selected[1]['slug']),
    }


def fetch_share_proof(page_url, articles, revision, attempt, timeout):
    """Fetch representative content and registry card/stub pairs over HTTPS."""
    asset_names = share_proof_asset_names(articles)

    def fetch_one(asset_name):
        asset_url = share_asset_url(page_url, asset_name, articles)
        if not same_origin(page_url, asset_url):
            raise ValueError(f'share-proof asset {asset_name} URL is not same-origin')
        requested_url = cache_busted_url(asset_url, revision, attempt)
        request = Request(
            requested_url,
            headers={
                'Accept': 'image/png' if asset_name.endswith('.png') else 'text/html',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'User-Agent': 'navnoor-terminal-deployment-smoke/1.0',
            },
        )
        context = verified_ssl_context()
        with urlopen(request, timeout=timeout, context=context) as response:
            final_url = response.geturl()
            if urlsplit(final_url).scheme != 'https':
                raise ValueError(
                    f'share-proof asset {asset_name} redirected away from HTTPS'
                )
            if not same_origin(page_url, final_url):
                raise ValueError(
                    f'share-proof asset {asset_name} redirected off-origin'
                )
            if urlsplit(final_url).path != urlsplit(asset_url).path:
                raise ValueError(
                    f'share-proof asset {asset_name} redirected to another path'
                )
            status = getattr(response, 'status', None)
            if status is None:
                status = response.getcode()
            if status != 200:
                raise ValueError(f'share-proof asset {asset_name} returned HTTP {status}')
            expected_type = 'image/png' if asset_name.endswith('.png') else 'text/html'
            content_type = response.headers.get_content_type()
            if content_type != expected_type:
                raise ValueError(
                    f'share-proof asset {asset_name} returned {content_type}, '
                    f'not {expected_type}'
                )
            maximum = (
                MAX_SHARE_CARD_BYTES if asset_name.endswith('.png')
                else MAX_SHARE_STUB_BYTES
            )
            payload = response.read(maximum + 1)
        if not payload or len(payload) > maximum:
            raise ValueError(
                f'share-proof asset {asset_name} is empty or exceeds {maximum} bytes'
            )
        return asset_name, payload

    with ThreadPoolExecutor(max_workers=len(asset_names)) as executor:
        return dict(executor.map(fetch_one, asset_names))


def support_bundle_checksum(directory):
    """Bind every launch support asset to one deterministic release digest."""
    root = Path(directory)
    digest = hashlib.sha256()
    for asset_name in SUPPORT_ASSET_NAMES:
        payload = (root / asset_name).read_bytes()
        digest.update(asset_name.encode('ascii'))
        digest.update(b'\0')
        digest.update(payload)
        digest.update(b'\0')
    return digest.hexdigest()


def support_payload_checksum(payloads):
    digest = hashlib.sha256()
    for asset_name in SUPPORT_ASSET_NAMES:
        if asset_name not in payloads:
            raise ValueError(f'missing support asset: {asset_name}')
        digest.update(asset_name.encode('ascii'))
        digest.update(b'\0')
        digest.update(payloads[asset_name])
        digest.update(b'\0')
    return digest.hexdigest()


def validate_deferred_payload(payload, expected_checksum):
    """Fail closed unless the deferred dossier belongs to the exact snapshot."""
    if not isinstance(payload, dict):
        raise ValueError('deferred article dossier must be a JSON object')
    schema_version = payload.get('schema_version')
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError('deferred article dossier schema_version must be 1')
    checksum = payload.get('data_checksum')
    if not isinstance(checksum, str) or not CHECKSUM_RE.fullmatch(checksum):
        raise ValueError(
            'deferred article dossier data_checksum is not a lowercase SHA-256 digest'
        )
    if checksum != expected_checksum:
        raise ValueError(
            'deferred article dossier data_checksum is '
            f'{checksum}, expected {expected_checksum}'
        )
    briefs = payload.get('briefs')
    if not isinstance(briefs, dict) or not briefs:
        raise ValueError('deferred article dossier briefs must be a non-empty object')


def validate_observation_payload(payload, expected_checksum, expected_count):
    """Fail closed unless parser observations belong to the exact snapshot."""
    if not isinstance(payload, dict):
        raise ValueError('deferred observation archive must be a JSON object')
    if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('deferred observation archive schema_version must be 1')
    checksum = payload.get('data_checksum')
    if not isinstance(checksum, str) or not CHECKSUM_RE.fullmatch(checksum):
        raise ValueError('deferred observation data_checksum is not a lowercase SHA-256 digest')
    if checksum != expected_checksum:
        raise ValueError(f'deferred observation data_checksum is {checksum}, expected {expected_checksum}')
    rows = payload.get('observations')
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise ValueError(f'deferred observation count is {len(rows) if isinstance(rows, list) else "invalid"}, expected {expected_count}')
    ids = [str(row.get('id') or '') for row in rows if isinstance(row, dict)]
    if len(ids) != expected_count or not all(ids) or len(ids) != len(set(ids)):
        raise ValueError('deferred observation identities are missing or duplicated')


def fetch_deferred_json(
    page_url,
    asset_name,
    revision,
    attempt,
    timeout,
    *,
    expected_sha256,
):
    """Fetch, byte-verify, and decode a same-origin release asset over HTTPS."""
    if not CHECKSUM_RE.fullmatch(str(expected_sha256 or '')):
        raise ValueError(f'expected digest for {asset_name} is invalid')
    asset_url = deferred_asset_url(page_url, asset_name)
    if not same_origin(page_url, asset_url):
        raise ValueError(f'{asset_name} URL is not same-origin')
    requested_url = cache_busted_url(asset_url, revision, attempt)
    request = Request(
        requested_url,
        headers={
            'Accept': 'application/json',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'User-Agent': 'navnoor-terminal-deployment-smoke/1.0',
        },
    )
    context = verified_ssl_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        final_url = response.geturl()
        if urlsplit(final_url).scheme != 'https':
            raise ValueError(
                f'{asset_name} redirected away from HTTPS: {final_url}'
            )
        if not same_origin(page_url, final_url):
            raise ValueError(
                f'{asset_name} redirected off-origin: {final_url}'
            )
        status = getattr(response, 'status', None)
        if status is None:
            status = response.getcode()
        if status != 200:
            raise ValueError(f'{asset_name} returned HTTP {status}')
        content_type = response.headers.get_content_type()
        if content_type != 'application/json':
            raise ValueError(
                f'{asset_name} returned '
                f'{content_type}, not application/json'
            )
        payload = response.read(MAX_DEFERRED_BYTES + 1)
    if len(payload) > MAX_DEFERRED_BYTES:
        raise ValueError(
            f'{asset_name} exceeds {MAX_DEFERRED_BYTES} bytes'
        )
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(f'{asset_name} SHA-256 is {actual_sha256}, expected {expected_sha256}')
    try:
        value = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{asset_name} is not valid UTF-8 JSON: {exc}') from exc
    return value


def fetch_deferred_briefs(
    page_url, revision, attempt, timeout, *, expected_sha256,
):
    return fetch_deferred_json(
        page_url,
        DEFERRED_ASSET_NAME,
        revision,
        attempt,
        timeout,
        expected_sha256=expected_sha256,
    )


def fetch_deferred_observations(
    page_url, revision, attempt, timeout, *, expected_sha256,
):
    return fetch_deferred_json(
        page_url,
        OBSERVATION_ASSET_NAME,
        revision,
        attempt,
        timeout,
        expected_sha256=expected_sha256,
    )


def fetch_support_bundle(page_url, revision, attempt, timeout):
    """Fetch and validate every same-origin discovery/social support asset."""
    def fetch_one(asset_name):
        asset_url = sibling_asset_url(page_url, asset_name)
        if not same_origin(page_url, asset_url):
            raise ValueError(f'{asset_name} URL is not same-origin')
        requested_url = cache_busted_url(asset_url, revision, attempt)
        request = Request(
            requested_url,
            headers={
                'Accept': '*/*',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'User-Agent': 'navnoor-terminal-deployment-smoke/1.0',
            },
        )
        context = verified_ssl_context()
        with urlopen(request, timeout=timeout, context=context) as response:
            final_url = response.geturl()
            if urlsplit(final_url).scheme != 'https':
                raise ValueError(f'{asset_name} redirected away from HTTPS: {final_url}')
            if not same_origin(page_url, final_url):
                raise ValueError(f'{asset_name} redirected off-origin: {final_url}')
            status = getattr(response, 'status', None)
            if status is None:
                status = response.getcode()
            if status != 200:
                raise ValueError(f'{asset_name} returned HTTP {status}')
            content_type = response.headers.get_content_type()
            if content_type not in SUPPORT_CONTENT_TYPES[asset_name]:
                raise ValueError(f'{asset_name} returned unexpected content type {content_type}')
            payload = response.read(MAX_SUPPORT_ASSET_BYTES + 1)
        if not payload or len(payload) > MAX_SUPPORT_ASSET_BYTES:
            raise ValueError(f'{asset_name} is empty or exceeds {MAX_SUPPORT_ASSET_BYTES} bytes')
        return asset_name, payload

    # These independent, small files are fetched concurrently so one retry has
    # a single timeout budget rather than five sequential timeout budgets.
    with ThreadPoolExecutor(max_workers=len(SUPPORT_ASSET_NAMES)) as executor:
        payloads = dict(executor.map(fetch_one, SUPPORT_ASSET_NAMES))
    return support_payload_checksum(payloads)


def verified_ssl_context():
    """Use the platform trust store, with certifi as a verified macOS fallback."""
    try:
        import certifi  # Optional; GitHub-hosted runners use the system store.
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def fetch_html(url, revision, attempt, timeout, *, expected_sha256):
    """Fetch one uncached copy of the deployed page over verified HTTPS."""
    if not CHECKSUM_RE.fullmatch(str(expected_sha256 or '')):
        raise ValueError('trusted build digest for index.html is invalid')
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
    context = verified_ssl_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        final_url = response.geturl()
        if urlsplit(final_url).scheme != 'https':
            raise ValueError(f'deployment redirected away from HTTPS: {final_url}')
        if not same_origin(url, final_url):
            raise ValueError(f'deployment redirected off-origin: {final_url}')
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
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f'index.html SHA-256 is {actual_sha256}, expected {expected_sha256}'
        )
    return payload.decode('utf-8'), final_url


def smoke_test(
    url,
    expected_revision,
    expected_articles,
    expected_observations,
    expected_checksum,
    expected_html_sha256,
    expected_brief_sha256,
    expected_observation_sha256,
    retries=12,
    retry_delay=10.0,
    timeout=20.0,
    expected_support_sha256=None,
    expected_data_sha256=None,
    expected_share_sha256=None,
):
    """Retry through Pages propagation and fail unless the exact release is live."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.netloc:
        raise ValueError('deployment URL must be an absolute HTTPS URL')
    if retries < 1:
        raise ValueError('retries must be at least 1')
    if retry_delay < 0 or timeout <= 0:
        raise ValueError('retry delay cannot be negative and timeout must be positive')
    trusted_asset_digests = {
        HTML_ASSET_NAME: expected_html_sha256,
        DEFERRED_ASSET_NAME: expected_brief_sha256,
        OBSERVATION_ASSET_NAME: expected_observation_sha256,
    }
    for asset_name, digest in trusted_asset_digests.items():
        if not CHECKSUM_RE.fullmatch(str(digest or '')):
            raise ValueError(
                f'trusted build digest for {asset_name} is not a lowercase SHA-256 digest'
            )
    if expected_support_sha256 is not None and not CHECKSUM_RE.fullmatch(
        str(expected_support_sha256 or '')
    ):
        raise ValueError('trusted build digest for support assets is invalid')
    if expected_data_sha256 is not None and not CHECKSUM_RE.fullmatch(
        str(expected_data_sha256 or '')
    ):
        raise ValueError('trusted build digest for data endpoints is invalid')
    if expected_share_sha256 is not None and not CHECKSUM_RE.fullmatch(
        str(expected_share_sha256 or '')
    ):
        raise ValueError('trusted build digest for share proof is invalid')
    if expected_share_sha256 is not None and expected_data_sha256 is None:
        raise ValueError('share proof requires the exact live data bundle')

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            html, page_url = fetch_html(
                url,
                expected_revision,
                attempt,
                timeout,
                expected_sha256=expected_html_sha256,
            )
            declared_asset_digests = validate_html(
                html,
                expected_revision,
                expected_articles,
                expected_observations,
                expected_checksum,
            )
            for asset_name, expected_digest in trusted_asset_digests.items():
                if asset_name == HTML_ASSET_NAME:
                    continue
                declared_digest = declared_asset_digests[asset_name]
                if declared_digest != expected_digest:
                    raise ValueError(
                        f'{asset_name} HTML digest is {declared_digest}, '
                        f'expected trusted build digest {expected_digest}'
                    )
            deferred = fetch_deferred_briefs(
                page_url,
                expected_revision,
                attempt,
                timeout,
                expected_sha256=expected_brief_sha256,
            )
            validate_deferred_payload(deferred, expected_checksum)
            observations = fetch_deferred_observations(
                page_url,
                expected_revision,
                attempt,
                timeout,
                expected_sha256=expected_observation_sha256,
            )
            validate_observation_payload(
                observations, expected_checksum, expected_observations,
            )
            if expected_support_sha256 is not None:
                actual_support_sha256 = fetch_support_bundle(
                    page_url, expected_revision, attempt, timeout,
                )
                if actual_support_sha256 != expected_support_sha256:
                    raise ValueError(
                        'support asset bundle SHA-256 is '
                        f'{actual_support_sha256}, expected {expected_support_sha256}'
                    )
            if expected_data_sha256 is not None:
                data_payloads = fetch_data_bundle(
                    page_url, expected_revision, attempt, timeout,
                )
                actual_data_sha256 = data_payload_checksum(data_payloads)
                if actual_data_sha256 != expected_data_sha256:
                    raise ValueError(
                        'data endpoint bundle SHA-256 is '
                        f'{actual_data_sha256}, expected {expected_data_sha256}'
                    )
                validate_live_data_bundle(
                    data_payloads, expected_articles, expected_checksum,
                )
                if expected_share_sha256 is not None:
                    articles = json.loads(
                        data_payloads['articles_index.json'].decode('utf-8')
                    )
                    share_payloads = fetch_share_proof(
                        page_url, articles, expected_revision, attempt, timeout,
                    )
                    actual_share_sha256 = share_proof_payload_checksum(
                        share_payloads, articles,
                    )
                    if actual_share_sha256 != expected_share_sha256:
                        raise ValueError(
                            'share proof bundle SHA-256 is '
                            f'{actual_share_sha256}, expected '
                            f'{expected_share_sha256}'
                        )
                    validate_share_proof(share_payloads, articles, page_url)
            data_note = ', data endpoints' if expected_data_sha256 is not None else ''
            share_note = (
                ', representative content/registry share pairs'
                if expected_share_sha256 is not None else ''
            )
            print(
                f'Smoke test passed on attempt {attempt}: HTTPS, revision '
                f'{expected_revision[:12]}, {expected_articles} articles, '
                f'{expected_observations} observations, exact release-bound HTML, '
                f'deferred assets, support bundle{data_note}{share_note}.'
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
    parser.add_argument('--expected-html-sha256', required=True)
    parser.add_argument('--expected-brief-sha256', required=True)
    parser.add_argument('--expected-observation-sha256', required=True)
    parser.add_argument('--expected-support-sha256', required=True)
    parser.add_argument('--expected-data-sha256', required=True)
    parser.add_argument('--expected-share-sha256', required=True)
    parser.add_argument('--retries', type=int, default=12)
    parser.add_argument('--retry-delay', type=float, default=10.0)
    parser.add_argument('--timeout', type=float, default=20.0)
    args = parser.parse_args()

    try:
        article_count = load_content_article_count(args.articles_file)
        observation_count = load_list_count(args.observations_file, 'observation snapshot')
        checksum = snapshot_checksum(args.articles_file, args.observations_file)
        smoke_test(
            args.url,
            args.expected_revision,
            article_count,
            observation_count,
            checksum,
            args.expected_html_sha256,
            args.expected_brief_sha256,
            args.expected_observation_sha256,
            retries=args.retries,
            retry_delay=args.retry_delay,
            timeout=args.timeout,
            expected_support_sha256=args.expected_support_sha256,
            expected_data_sha256=args.expected_data_sha256,
            expected_share_sha256=args.expected_share_sha256,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'SMOKE TEST FAILED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
