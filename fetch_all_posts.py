#!/usr/bin/env python3
"""
Fetch all posts from navnoorbawa.substack.com API and save content locally.
"""
import urllib.request
import urllib.error
import urllib.parse
import hashlib
import json
import math
import os
import ssl
import time
import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from article_briefs import build_article_brief

ROOT = Path(__file__).parent
POSTS_PATH = Path(os.environ.get('POSTS_OUTPUT', ROOT / 'all_posts.json')).expanduser()
ARTICLE_INDEX_PATH = Path(os.environ.get(
    'ARTICLES_OUTPUT', ROOT / 'articles_index.json'
)).expanduser()
PREVIOUS_POSTS_PATH = Path(os.environ.get(
    'PREVIOUS_POSTS', ROOT / 'all_posts.json'
)).expanduser()
_status_output = os.environ.get('FETCH_STATUS_OUTPUT')
FETCH_STATUS_PATH = Path(_status_output).expanduser() if _status_output else None

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/html, */*',
}
# Stored article identity stays on the publication subdomain so existing
# catalogue URLs and validators remain stable. Live API responses may land on
# the owned custom domain after Substack's publication redirect.
SUBSTACK_ORIGIN = 'https://navnoorbawa.substack.com'
SUBSTACK_API_HOSTS = frozenset({
    'navnoorbawa.substack.com',
    'www.navnoorbawaresearch.com',
})
MAX_LIST_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DETAIL_RESPONSE_BYTES = 4 * 1024 * 1024
DETAIL_RESPONSE_TIMEOUT_SECONDS = 15
MAX_DETAIL_REQUESTS_PER_RUN = 12
MIN_COMPLETE_BODY_WORD_RATIO = 0.97
MAX_EXCERPT_CHARS = 1_200


def build_ssl_context():
    """Return a verified TLS context, including on broken python.org installs.

    Some macOS Python installers point OpenSSL at a certificate bundle that no
    longer exists. The system trust bundle remains available, so prefer it when
    Python's configured CA file is missing. Verification is never disabled.
    """
    default_cafile = ssl.get_default_verify_paths().cafile
    if default_cafile and Path(default_cafile).is_file():
        return ssl.create_default_context()

    candidates = [
        os.environ.get('SSL_CERT_FILE'),
        '/etc/ssl/cert.pem',
        '/private/etc/ssl/cert.pem',
        '/etc/ssl/certs/ca-certificates.crt',
    ]
    for cafile in candidates:
        if cafile and Path(cafile).is_file():
            return ssl.create_default_context(cafile=cafile)

    # Let Python raise its normal certificate error rather than silently
    # weakening TLS if no trusted CA bundle is available.
    return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()

class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.fed = []
        self.in_style = False
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script'):
            self.in_style = True
        # Add newlines for block elements
        if tag in ('p', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'tr'):
            self.fed.append('\n')

    def handle_endtag(self, tag):
        if tag in ('style', 'script'):
            self.in_style = False
        if tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'tr'):
            self.fed.append('\n')

    def handle_data(self, d):
        if not self.in_style:
            self.fed.append(d)

    def get_data(self):
        return ''.join(self.fed)


def strip_html(html_content):
    s = MLStripper()
    s.feed(html_content)
    text = s.get_data()
    text = html.unescape(text)
    # Clean up excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def body_word_count(value):
    """Count text tokens closely enough to verify Substack's declared total."""
    if not isinstance(value, str):
        return 0
    return len(re.findall(r"\b[\w’'-]+\b", value, flags=re.UNICODE))


def is_public_body(post):
    """Only explicitly public Substack rows may prove current full access."""
    return post.get('audience') == 'everyone'


def complete_body_text(post, body_html):
    """Return stripped HTML only when it proves full-body coverage."""
    if (
        not is_public_body(post)
        or not isinstance(body_html, str)
        or not body_html.strip()
    ):
        return None
    declared_wordcount = post.get('wordcount')
    if type(declared_wordcount) is not int or declared_wordcount <= 0:
        return None
    body_text = strip_html(body_html)
    minimum_words = max(
        1,
        math.ceil(declared_wordcount * MIN_COMPLETE_BODY_WORD_RATIO),
    )
    if body_word_count(body_text) < minimum_words:
        return None
    return body_text


def bounded_excerpt(value):
    """Keep a bounded exact-source excerpt without ending on a partial word."""
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    # The ellipsis is part of the published preview and therefore part of the
    # hard character budget.
    cutoff = MAX_EXCERPT_CHARS - 1
    boundary = text.rfind(' ', 0, cutoff + 1)
    if boundary < 1:
        return ''
    return text[:boundary].rstrip() + '…'


def public_source_post(post):
    """Return the body view that may enter tracked or published artifacts.

    The ignored local cache may retain an author's older full body after a
    post becomes subscriber-only.  That private continuity must never be
    confused with the anonymous source surface used by the public pipeline.
    """
    item = dict(post)
    audience = str(item.get('audience') or '').strip().casefold()
    if audience == 'everyone':
        item.pop('public_preview_text', None)
        item.pop('public_preview_updated_at', None)
        item.pop('member_preview', None)
        return item
    if audience != 'only_paid':
        raise ValueError('Substack post has an unsupported publication audience')
    preview = bounded_excerpt(item.get('public_preview_text'))
    digest = hashlib.sha256(preview.encode('utf-8')).hexdigest()
    item['body_text'] = preview
    item['body_html_length'] = 0
    item['body_source'] = (
        'anonymous-list-preview' if preview else 'metadata-only'
    )
    item['wordcount'] = 0
    item['content_status'] = 'excerpt'
    item['body_revision_status'] = 'current'
    observed = item.get('observed_source_updated_at')
    if not isinstance(observed, str):
        observed = ''
    current_revision = item.get('public_preview_updated_at')
    if not isinstance(current_revision, str):
        current_revision = observed
    item['source_updated_at'] = current_revision
    item['observed_source_updated_at'] = current_revision
    item['member_preview'] = {
        'schema_version': 1,
        'surface': (
            'anonymous-substack-list' if preview else 'metadata-only'
        ),
        'text': preview,
        'character_count': len(preview),
        'body_sha256': digest,
    }
    item.pop('public_preview_text', None)
    item.pop('public_preview_updated_at', None)
    return item


class DetailBudgetExhausted(RuntimeError):
    """Raised when one refresh reaches its bounded detail-request budget."""


def fetch_json(url, max_bytes, timeout=30):
    """Fetch one bounded UTF-8 JSON response from a trusted Substack host."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(
        req,
        timeout=timeout,
        context=SSL_CONTEXT,
    ) as response:
        final_url = response.geturl()
        actual = urllib.parse.urlsplit(final_url)
        if (
            actual.scheme != 'https'
            or actual.hostname not in SUBSTACK_API_HOSTS
            or actual.port is not None
        ):
            raise ValueError('Substack redirected to an untrusted origin')
        declared_length = response.headers.get('Content-Length')
        if declared_length:
            try:
                declared_bytes = int(declared_length)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > max_bytes:
                raise ValueError('Substack response exceeds the configured size limit')
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError('Substack response exceeds the configured size limit')
    return json.loads(payload.decode('utf-8'))


def fetch_posts(limit=50, offset=0, attempts=3):
    url = f'{SUBSTACK_ORIGIN}/api/v1/posts?limit={limit}&offset={offset}'
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            data = fetch_json(url, MAX_LIST_RESPONSE_BYTES)
            if not isinstance(data, list) or not all(isinstance(post, dict) for post in data):
                raise ValueError('Substack returned an unexpected response shape')
            for post in data:
                if not post.get('slug') or not post.get('post_date') or not post.get('title'):
                    raise ValueError(
                        'Substack returned a post without a slug, title, or publication date'
                    )
                if post.get('audience') not in {'everyone', 'only_paid'}:
                    raise ValueError(
                        'Substack returned an unsupported publication audience'
                    )
                if post.get('body_html') is not None and not isinstance(
                    post.get('body_html'), str
                ):
                    raise ValueError('Substack returned a non-text body_html value')
                if post.get('truncated_body_text') is not None and not isinstance(
                    post.get('truncated_body_text'), str
                ):
                    raise ValueError(
                        'Substack returned a non-text truncated_body_text value'
                    )
            return data
        except (
            urllib.error.URLError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    if last_error is None:
        raise ValueError('Substack fetch attempts must be at least one')
    raise last_error


def fetch_post_detail(slug, expected_updated_at, attempts=1):
    """Fetch HTML omitted by the paginated list endpoint."""
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError('Substack detail fetch requires a slug')
    if not isinstance(expected_updated_at, str) or not expected_updated_at:
        raise ValueError('Substack detail fetch requires a source revision')
    encoded_slug = urllib.parse.quote(slug.strip(), safe='')
    url = f'{SUBSTACK_ORIGIN}/api/v1/posts/{encoded_slug}'
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            data = fetch_json(
                url,
                MAX_DETAIL_RESPONSE_BYTES,
                timeout=DETAIL_RESPONSE_TIMEOUT_SECONDS,
            )
            if not isinstance(data, dict):
                raise ValueError('Substack detail response is not an object')
            if data.get('slug') != slug:
                raise ValueError('Substack detail response has the wrong slug')
            if data.get('is_published') is not True:
                raise ValueError('Substack detail response is not published')
            if data.get('updated_at') != expected_updated_at:
                raise ValueError(
                    'Substack detail response revision differs from its list row'
                )
            body_html = data.get('body_html')
            if not isinstance(body_html, str) or not body_html.strip():
                raise ValueError('Substack detail response has no body HTML')
            return data
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
        except (UnicodeDecodeError, ValueError):
            raise
    if last_error is None:
        raise ValueError('Substack detail fetch attempts must be at least one')
    raise last_error


def post_record(post, body_text, body_html_length, content_status, body_source):
    """Return one normalized post without overstating the captured access."""
    if not isinstance(body_text, str):
        raise ValueError('Substack body text must be a string')
    if content_status not in {'full', 'excerpt'}:
        raise ValueError('Substack content status is invalid')
    if post.get('is_published', True) is not True:
        raise ValueError('Substack returned an unpublished post')
    wordcount = post.get('wordcount', 0)
    if type(wordcount) is not int or wordcount < 0 or content_status == 'excerpt':
        wordcount = 0
    source_updated_at = post.get('updated_at')
    if not isinstance(source_updated_at, str):
        source_updated_at = ''
    return {
        'source': 'substack',
        'source_id': post.get('slug', ''),
        'slug': post.get('slug', ''),
        'title': post.get('title', ''),
        'subtitle': post.get('subtitle', ''),
        'post_date': post.get('post_date', ''),
        'url': f"{SUBSTACK_ORIGIN}/p/{post.get('slug', '')}",
        'audience': post.get('audience', ''),
        'meter_type': post.get('meter_type', ''),
        'type': post.get('type', ''),
        'is_published': True,
        'wordcount': wordcount,
        'body_text': body_text,
        'body_html_length': body_html_length,
        'body_source': body_source,
        'source_updated_at': source_updated_at,
        'observed_source_updated_at': source_updated_at,
        'body_revision_status': 'current',
        'content_status': content_status,
    }


def previous_full_body(previous):
    """Accept cached full text only when its own declared coverage is proven."""
    if not isinstance(previous, dict):
        return False
    declared_wordcount = previous.get('wordcount')
    body_text = previous.get('body_text')
    if (
        previous.get('content_status') != 'full'
        or type(declared_wordcount) is not int
        or declared_wordcount <= 0
        or not isinstance(body_text, str)
        or not body_text.strip()
    ):
        return False
    minimum_words = max(
        1,
        math.ceil(declared_wordcount * MIN_COMPLETE_BODY_WORD_RATIO),
    )
    return (
        body_word_count(body_text) >= minimum_words
    )


def cached_body_covers_post(post, previous):
    """Require cached text to cover the current row's declared word count."""
    if not previous_full_body(previous):
        return False
    declared_wordcount = post.get('wordcount')
    if type(declared_wordcount) is not int or declared_wordcount <= 0:
        return False
    minimum_words = max(
        1,
        math.ceil(declared_wordcount * MIN_COMPLETE_BODY_WORD_RATIO),
    )
    return body_word_count(previous['body_text']) >= minimum_words


def previous_excerpt(previous):
    return (
        isinstance(previous, dict)
        and previous.get('content_status') == 'excerpt'
        and isinstance(previous.get('body_text'), str)
        and bool(previous['body_text'].strip())
    )


def exact_list_body_surfaces(post):
    """Return the distinct bounded text surfaces proven by the current list."""
    surfaces = []
    truncated = bounded_excerpt(post.get('truncated_body_text'))
    if truncated:
        surfaces.append(truncated)
    body_html = post.get('body_html')
    if isinstance(body_html, str) and body_html.strip():
        html_excerpt = bounded_excerpt(strip_html(body_html))
        if html_excerpt and html_excerpt not in surfaces:
            surfaces.append(html_excerpt)
    return tuple(surfaces)


def comparable_list_surface(surface):
    """Remove only a terminal truncation marker for compatibility checks."""
    text = surface.rstrip()
    if text.endswith('…'):
        return text[:-1].rstrip()
    if text.endswith('...'):
        return text[:-3].rstrip()
    return text


def list_surfaces_are_compatible(left, right):
    """Return whether two exact list surfaces are prefix/subset variants."""
    left_comparable = comparable_list_surface(left)
    right_comparable = comparable_list_surface(right)
    if not left_comparable or not right_comparable:
        return left_comparable == right_comparable
    return (
        left_comparable in right_comparable
        or right_comparable in left_comparable
    )


def canonical_list_body_surface(post):
    """Return the longest deterministic list surface and its coherence."""
    surfaces = exact_list_body_surfaces(post)
    if not surfaces:
        return '', True
    canonical = max(surfaces, key=lambda surface: (len(surface), surface))
    compatible = all(
        list_surfaces_are_compatible(left, right)
        for index, left in enumerate(surfaces)
        for right in surfaces[index + 1:]
    )
    return canonical, compatible


def conflicting_current_list_surface(post, previous):
    """Return a current list surface that disproves a same-revision cache.

    Substack's ``updated_at`` is not a sufficient content identity signal: the
    exact list preview can change while that timestamp remains fixed. A public
    cache may be treated as current only while every exact list surface remains
    consistent with it. Paid rows are deliberately excluded because their
    anonymous preview is published separately from the author-owned body cache.
    """
    if not is_public_body(post):
        return ''
    canonical_surface, surfaces_compatible = canonical_list_body_surface(post)
    # Disagreeing current source surfaces are themselves a live provenance
    # conflict. Always select the same longest bounded surface so repeated
    # refreshes cannot alternate between two exact captures.
    if canonical_surface and not surfaces_compatible:
        return canonical_surface
    if not isinstance(previous, dict):
        return ''
    current_revision = post.get('updated_at')
    previous_revision = previous.get('source_updated_at')
    if (
        not isinstance(current_revision, str)
        or not current_revision
        or not isinstance(previous_revision, str)
        or current_revision != previous_revision
    ):
        return ''
    previous_body = previous.get('body_text')
    if not isinstance(previous_body, str) or not previous_body.strip():
        return ''

    if not canonical_surface:
        return ''
    if previous.get('content_status') == 'excerpt':
        # An excerpt is covered only by the exact canonical surface. A longer
        # cached capture can share the canonical prefix while containing text
        # the current anonymous list no longer proves; collapse it once rather
        # than continuing to label that stale tail current.
        if canonical_surface != previous_body:
            return canonical_surface
        return ''
    # A list excerpt can be a proper subset of a verified full body. Terminal
    # ellipses are presentation markers rather than authored compatibility.
    if not list_surfaces_are_compatible(canonical_surface, previous_body):
        return canonical_surface
    return ''


def preserved_body_record(
        post, previous, body_source, content_status='excerpt'):
    """Combine current source metadata with a separately proven cached body."""
    record = post_record(
        post,
        previous['body_text'],
        previous.get('body_html_length', 0),
        content_status,
        body_source,
    )
    # source_updated_at identifies the revision that supplied body_text. Never
    # stamp an inaccessible or failed current revision onto an older body.
    previous_body_revision = previous.get('source_updated_at')
    record['source_updated_at'] = (
        previous_body_revision
        if isinstance(previous_body_revision, str)
        else ''
    )
    observed_revision = post.get('updated_at')
    record['observed_source_updated_at'] = (
        observed_revision if isinstance(observed_revision, str) else ''
    )
    if (
        record['source_updated_at']
        and record['observed_source_updated_at']
    ):
        record['body_revision_status'] = (
            'current'
            if record['source_updated_at']
            == record['observed_source_updated_at']
            else 'prior'
        )
    else:
        record['body_revision_status'] = 'unverified'
    return record


def legacy_cache_matches(post, previous):
    """Conservatively migrate a pre-provenance cache without network churn."""
    if not previous_full_body(previous) or previous.get('source_updated_at'):
        return False
    for key in (
        'slug',
        'title',
        'post_date',
        'wordcount',
        'meter_type',
        'type',
    ):
        if post.get(key) != previous.get(key):
            return False
    return True


def resolve_post_body(post, previous=None, detail_fetcher=None):
    """Resolve a list record to full text, an honest excerpt, or a safe skip."""
    body_html = post.get('body_html')
    list_body_text = complete_body_text(post, body_html)
    if list_body_text is not None:
        return (
            post_record(post, list_body_text, len(body_html), 'full', 'list'),
            'list',
            '',
        )

    source_updated_at = post.get('updated_at')
    canonical_list_surface, list_surfaces_compatible = (
        canonical_list_body_surface(post)
    )
    conflicting_list_surface = conflicting_current_list_surface(post, previous)
    matching_full_cache = (
        cached_body_covers_post(post, previous)
        and isinstance(source_updated_at, str)
        and source_updated_at
        and previous.get('source_updated_at') == source_updated_at
        and not conflicting_list_surface
    )
    if matching_full_cache and is_public_body(post):
        return (
            post_record(
                post,
                previous['body_text'],
                previous.get('body_html_length', 0),
                'full',
                'cached-unchanged',
            ),
            'cache',
            '',
        )
    if matching_full_cache:
        return (
            preserved_body_record(
                post,
                previous,
                'cached-access-limited',
                content_status='excerpt',
            ),
            'access-cache',
            f"{post.get('slug', '')}: preserved the prior exact body because "
            'the current source is access-limited',
        )

    if (
        previous_excerpt(previous)
        and not is_public_body(post)
        and isinstance(source_updated_at, str)
        and source_updated_at
        and previous.get('source_updated_at') == source_updated_at
    ):
        return (
            post_record(
                post,
                previous['body_text'],
                0,
                'excerpt',
                'cached-excerpt',
            ),
            'excerpt-cache',
            f"{post.get('slug', '')}: upstream still exposes only the exact "
            'previously captured excerpt',
        )

    if (
        previous_excerpt(previous)
        and not is_public_body(post)
        and isinstance(source_updated_at, str)
        and source_updated_at
        and previous.get('observed_source_updated_at') == source_updated_at
    ):
        return (
            preserved_body_record(
                post,
                previous,
                'cached-provenance-limited',
            ),
            'excerpt-cache',
            f"{post.get('slug', '')}: retained a prior exact capture as an "
            'excerpt without claiming current full-body coverage',
        )

    if not is_public_body(post) and legacy_cache_matches(post, previous):
        return (
            preserved_body_record(post, previous, 'cached-legacy-unverified'),
            'legacy-cache',
            f"{post.get('slug', '')}: preserved a metadata-matched legacy body "
            'without claiming that it matches the currently observed revision',
        )

    truncated = post.get('truncated_body_text')
    if not is_public_body(post):
        if isinstance(truncated, str) and truncated.strip():
            return (
                post_record(
                    post,
                    bounded_excerpt(truncated),
                    0,
                    'excerpt',
                    'list-excerpt',
                ),
                'excerpt',
                f"{post.get('slug', '')}: indexed only the exact public excerpt "
                'because the full article is access-limited',
            )
        return (
            post_record(post, '', 0, 'excerpt', 'metadata-only'),
            'metadata',
            f"{post.get('slug', '')}: the article is access-limited and the "
            'list endpoint supplied no exact public excerpt; indexed metadata only',
        )

    detail_excerpt = ''
    try:
        loader = detail_fetcher or fetch_post_detail
        detail = loader(post.get('slug', ''), source_updated_at)
        detail_html = detail['body_html']
        detail_excerpt = bounded_excerpt(strip_html(detail_html))
        full_detail_text = complete_body_text(post, detail_html)
        if full_detail_text is None:
            raise ValueError(
                'detail HTML does not cover the declared article word count'
            )
    except Exception as exc:
        if conflicting_list_surface:
            if list_surfaces_compatible:
                warning = (
                    f"{post.get('slug', '')}: the exact current list excerpt "
                    'changed without a source revision signal; indexed that '
                    f'current excerpt and could not verify the full body '
                    f'({type(exc).__name__})'
                )
            else:
                warning = (
                    f"{post.get('slug', '')}: current list text surfaces "
                    'disagreed; indexed the deterministic longest exact '
                    f'bounded surface and could not verify the full body '
                    f'({type(exc).__name__})'
                )
            return (
                post_record(
                    post,
                    conflicting_list_surface,
                    0,
                    'excerpt',
                    'source-excerpt',
                ),
                'excerpt',
                warning,
            )
        if previous_full_body(previous):
            return (
                preserved_body_record(post, previous, 'cached-fallback'),
                'fallback',
                f"{post.get('slug', '')}: full current body could not be verified; "
                f'preserved the prior exact body ({type(exc).__name__})',
            )
        if previous_excerpt(previous):
            return (
                preserved_body_record(
                    post,
                    previous,
                    'cached-excerpt-fallback',
                ),
                'excerpt-cache',
                f"{post.get('slug', '')}: full current body could not be "
                'verified; preserved the prior exact excerpt '
                f'({type(exc).__name__})',
            )
        excerpt_candidates = (
            canonical_list_surface,
            detail_excerpt,
        )
        excerpt = ''
        for candidate in excerpt_candidates:
            excerpt = bounded_excerpt(candidate)
            if excerpt:
                break
        if excerpt:
            return (
                post_record(post, excerpt, 0, 'excerpt', 'source-excerpt'),
                'excerpt',
                f"{post.get('slug', '')}: full body could not be verified; "
                f'indexed only the exact available excerpt ({type(exc).__name__})',
            )
        return (
            post_record(post, '', 0, 'excerpt', 'metadata-only'),
            'metadata',
            f"{post.get('slug', '')}: full body could not be verified and no "
            f'source excerpt was available; indexed metadata only '
            f'({type(exc).__name__})',
        )

    return (
        post_record(
            post,
            full_detail_text,
            len(detail_html),
            'full',
            'detail',
        ),
        'detail',
        '',
    )


BODY_HEALTH_STABILITY_FIELDS = (
    'slug',
    'title',
    'subtitle',
    'post_date',
    'audience',
    'meter_type',
    'type',
    'is_published',
)


def body_resolution_degrades_source(post, previous):
    """Return whether a body-resolution notice is a live source failure.

    Access-limited posts are expected to expose only an anonymous excerpt or
    metadata, so their coverage notices must not make an otherwise complete,
    stable catalogue permanently unhealthy.  Likewise, a previously disclosed
    public excerpt gap is not a new outage while both its source revision and
    publication metadata remain unchanged.  New or changed public rows still
    fail closed until their current public body provenance can be verified.
    """
    if not is_public_body(post):
        return False
    if not isinstance(previous, dict):
        return True
    if previous.get('content_status') != 'excerpt':
        return True
    if conflicting_current_list_surface(post, previous):
        return True

    current_revision = post.get('updated_at')
    previous_revision = previous.get('source_updated_at')
    if (
        not isinstance(current_revision, str)
        or not current_revision
        or not isinstance(previous_revision, str)
        or current_revision != previous_revision
    ):
        return True
    return any(
        post.get(field) != previous.get(field)
        for field in BODY_HEALTH_STABILITY_FIELDS
    )


def article_metadata(post):
    """Keep the small, deployable subset needed to render every article."""
    post = public_source_post(post)
    value = {
        'source': 'substack',
        'source_id': post.get('slug', ''),
        'slug': post.get('slug', ''),
        'title': post.get('title', ''),
        'subtitle': post.get('subtitle', ''),
        'post_date': post.get('post_date', ''),
        'url': post.get('url', ''),
        'audience': post.get('audience', ''),
        'wordcount': post.get('wordcount', 0),
        'content_status': post.get('content_status', 'full'),
        'body_revision_status': (
            post.get('body_revision_status') or 'current'
        ),
        'source_updated_at': (
            post.get('source_updated_at')
            if isinstance(post.get('source_updated_at'), str)
            else ''
        ),
        'observed_source_updated_at': (
            post.get('observed_source_updated_at')
            if isinstance(post.get('observed_source_updated_at'), str)
            else ''
        ),
    }
    if isinstance(post.get('member_preview'), dict):
        value['member_preview'] = post['member_preview']
    value['brief'] = build_article_brief(post)
    return value


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def iso_instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def detail_attempt_instant(value):
    """Parse only the strict UTC timestamp emitted for private retry state."""
    if not isinstance(value, str) or not value.endswith('Z'):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def newest_post_date(posts):
    dates = [post.get('post_date') for post in posts
             if isinstance(post, dict) and isinstance(post.get('post_date'), str)]
    return max(
        dates,
        key=iso_instant,
        default='',
    )


def write_fetch_status(
    status,
    mode,
    fetched_count,
    posts,
    error=None,
    published_count=None,
    newest=None,
):
    """Write optional machine-readable fetch provenance without touching content."""
    if FETCH_STATUS_PATH is None:
        return
    payload = {
        'schema_version': 1,
        'source': 'substack',
        'checked_at': utc_now(),
        'status': status,
        'mode': mode,
        'fetched_count': fetched_count,
        'published_count': (
            len(posts) if published_count is None else published_count
        ),
        'newest': newest_post_date(posts) if newest is None else newest,
    }
    if error:
        payload['error'] = str(error)
    atomic_write_json(FETCH_STATUS_PATH, payload)


def fail_fetch(message, mode, fetched_count, posts):
    write_fetch_status('failed', mode, fetched_count, posts, message)
    print(message)
    raise SystemExit(1)


CATALOGUE_STABILITY_FIELDS = (
    'id',
    'slug',
    'title',
    'subtitle',
    'post_date',
    'updated_at',
    'audience',
    'meter_type',
    'type',
    'is_published',
    'wordcount',
    'body_html',
    'truncated_body_text',
)


def catalogue_signature(posts):
    """Hash only list fields that can affect the persisted source snapshot."""
    signature = []
    for post in posts:
        projection = {
            field: post.get(field) for field in CATALOGUE_STABILITY_FIELDS
        }
        payload = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        signature.append(hashlib.sha256(payload).hexdigest())
    return tuple(signature)


def fetch_complete_catalogue(limit=50):
    """Return one complete offset pass, rejecting every cross-page overlap."""
    listed_posts = []
    offset = 0
    seen_ids = set()
    seen_slugs = set()
    while True:
        print(f"  Fetching offset={offset}...", end=' ', flush=True)
        posts = fetch_posts(limit=limit, offset=offset)
        if not posts:
            print("No more posts.")
            break
        for post in posts:
            slug = post.get('slug')
            post_id = post.get('id')
            if slug in seen_slugs or (
                post_id is not None and post_id in seen_ids
            ):
                raise ValueError(
                    'Substack pagination overlapped a prior page; '
                    'refusing an incoherent catalogue'
                )
            seen_slugs.add(slug)
            if post_id is not None:
                seen_ids.add(post_id)
            listed_posts.append(post)
        print(f"Got {len(posts)} posts")
        if len(posts) < limit:
            print("Reached end of posts.")
            break
        offset += limit
        time.sleep(0.5)
    return listed_posts


def main():
    all_posts = []  # type: list[dict]
    fetched_count = 0
    limit = 50
    resolution_counts = {
        'list': 0,
        'detail': 0,
        'cache': 0,
        'access-cache': 0,
        'excerpt-cache': 0,
        'legacy-cache': 0,
        'fallback': 0,
        'excerpt': 0,
        'metadata': 0,
    }
    degraded_messages = []
    coverage_notices = []
    detail_request_count = 0
    detail_attempted_slugs = set()
    detail_attempted_at = utc_now()

    def fetch_detail_with_budget(slug, expected_updated_at):
        nonlocal detail_request_count
        if detail_request_count >= MAX_DETAIL_REQUESTS_PER_RUN:
            raise DetailBudgetExhausted(
                'per-refresh Substack detail-request budget exhausted'
            )
        detail_request_count += 1
        detail_attempted_slugs.add(slug)
        return fetch_post_detail(slug, expected_updated_at)

    previous_posts = []
    previous_by_slug = {}
    if PREVIOUS_POSTS_PATH.exists():
        try:
            with open(PREVIOUS_POSTS_PATH, 'r', encoding='utf-8') as handle:
                previous_posts = json.load(handle)
            if not isinstance(previous_posts, list) or not all(
                isinstance(post, dict) for post in previous_posts
            ):
                raise ValueError('expected a list of post objects')
            for previous_post in previous_posts:
                slug = previous_post.get('slug')
                if not isinstance(slug, str) or not slug or slug in previous_by_slug:
                    raise ValueError('missing or duplicate previous post slug')
                attempted_at = previous_post.get('detail_attempted_at')
                if (
                    attempted_at is not None
                    and detail_attempt_instant(attempted_at) is None
                ):
                    raise ValueError(
                        f'invalid private detail retry timestamp for {slug!r}'
                    )
                previous_by_slug[slug] = previous_post
        except Exception as exc:
            fail_fetch(
                f'Previous Substack snapshot is invalid: {exc}',
                'complete_api',
                fetched_count,
                all_posts,
            )

    print("Fetching posts from Substack API (catalogue pass 1 of 2)...")
    try:
        listed_posts = fetch_complete_catalogue(limit)
        fetched_count = len(listed_posts)
        print("Confirming stable Substack catalogue (pass 2 of 2)...")
        confirmation_posts = fetch_complete_catalogue(limit)
    except Exception as exc:
        fail_fetch(
            'Fetch did not complete coherently — leaving previous '
            f'all_posts.json untouched ({type(exc).__name__}: {exc}).',
            'complete_api',
            fetched_count,
            [],
        )

    if catalogue_signature(listed_posts) != catalogue_signature(
        confirmation_posts
    ):
        fail_fetch(
            'Substack catalogue changed between verification passes — '
            'leaving previous all_posts.json untouched.',
            'complete_api',
            fetched_count,
            listed_posts,
        )
    print(f"Stable catalogue confirmed: {fetched_count} unique posts")

    # Resolve genuinely new public rows first, then rows never attempted, then
    # the oldest attempted rows. The private timestamp survives only in the
    # ignored body cache, so a fixed newest-first request budget cannot starve
    # the thirteenth unresolved article forever.
    def detail_priority(indexed_post):
        index, post = indexed_post
        previous = previous_by_slug.get(post.get('slug'))
        if previous is None:
            return 0, datetime.min.replace(tzinfo=timezone.utc), index
        attempted = detail_attempt_instant(previous.get('detail_attempted_at'))
        if attempted is None:
            return 1, datetime.min.replace(tzinfo=timezone.utc), index
        return 2, attempted, index

    scheduled_posts = sorted(enumerate(listed_posts), key=detail_priority)
    resolved_by_index = {}
    for index, post in scheduled_posts:
        slug = post.get('slug')
        previous = previous_by_slug.get(slug)
        resolved, resolution, warning = resolve_post_body(
            post,
            previous,
            fetch_detail_with_budget,
        )
        if not is_public_body(post):
            # Retain the exact anonymous list preview separately from any
            # author-owned cached body. Public derivation consumes only this
            # bounded field through public_source_post().
            resolved['public_preview_text'] = bounded_excerpt(
                post.get('truncated_body_text')
            )
            resolved['public_preview_updated_at'] = (
                post.get('updated_at')
                if isinstance(post.get('updated_at'), str)
                else ''
            )
        if slug in detail_attempted_slugs:
            resolved['detail_attempted_at'] = detail_attempted_at
        elif (
            isinstance(previous, dict)
            and detail_attempt_instant(previous.get('detail_attempted_at'))
            is not None
        ):
            resolved['detail_attempted_at'] = previous['detail_attempted_at']
        resolution_counts[resolution] += 1
        if warning:
            print(f'Warning: {warning}')
            if body_resolution_degrades_source(post, previous):
                degraded_messages.append(warning)
            else:
                coverage_notices.append(warning)
        resolved_by_index[index] = resolved
    all_posts = [
        resolved_by_index[index] for index in range(len(listed_posts))
    ]

    print(f"\nTotal posts fetched: {len(all_posts)}")

    if not all_posts:
        fail_fetch("Fetch returned zero posts — leaving previous all_posts.json untouched.",
                   'complete_api', fetched_count, all_posts)

    if previous_posts:
        prev_count = len(previous_posts)
        prev_slugs = set(previous_by_slug)
        # A small decrease can be a legitimate deletion/unpublish. A large one
        # is much more likely to be a changed or truncated API response.
        minimum_safe_count = max(1, int(prev_count * 0.9))
        if (prev_count and len(all_posts) < minimum_safe_count
                and not os.environ.get('FORCE_FETCH')):
            fail_fetch(
                f"Refusing to overwrite: fetched {len(all_posts)} posts, below the "
                f"90% safety floor of {minimum_safe_count} from the previous {prev_count}. "
                f"Set FORCE_FETCH=1 to override.",
                'complete_api', fetched_count, all_posts,
            )
        current_slugs = {post.get('slug') for post in all_posts if post.get('slug')}
        missing_slugs = prev_slugs - current_slugs
        maximum_missing = max(5, int(prev_count * 0.1))
        if len(missing_slugs) > maximum_missing and not os.environ.get('FORCE_FETCH'):
            fail_fetch(
                f"Refusing to overwrite: {len(missing_slugs)} previously fetched posts "
                f"disappeared (safety limit: {maximum_missing}). Set FORCE_FETCH=1 to override.",
                'complete_api', fetched_count, all_posts,
            )
        if prev_count and len(all_posts) < prev_count:
            print(f"Warning: feed decreased from {prev_count} to {len(all_posts)} posts; "
                  "accepting the complete snapshot (likely a deletion/unpublish).")

    # Keep the full local corpus untracked and a small metadata index tracked.
    # The latter lets the deployed site show new articles even when no trade is
    # extracted from them.
    article_index = [article_metadata(post) for post in all_posts]
    atomic_write_json(POSTS_PATH, all_posts)
    atomic_write_json(ARTICLE_INDEX_PATH, article_index)
    degraded = bool(degraded_messages)
    if degraded:
        mode = 'complete_api_degraded_body_provenance'
    elif resolution_counts['detail']:
        mode = 'complete_api_plus_details'
    elif resolution_counts['cache']:
        mode = 'complete_api_cached_bodies'
    else:
        mode = 'complete_api'
    write_fetch_status(
        'degraded' if degraded else 'ok',
        mode,
        fetched_count,
        all_posts,
        (
            f"{len(degraded_messages)} body record(s) could not prove current "
            'full-body access; retained only provenance-separated prior text '
            'or exact source excerpts'
            if degraded
            else None
        ),
        published_count=fetched_count,
        newest=newest_post_date(listed_posts),
    )

    print(f"Saved to {POSTS_PATH}")
    print(f"Saved article index to {ARTICLE_INDEX_PATH}")
    print(
        'Body resolution: '
        + ', '.join(
            f'{label}={count}'
            for label, count in resolution_counts.items()
            if count
        )
    )
    if detail_request_count:
        print(
            'Body detail requests: '
            f'{detail_request_count}/{MAX_DETAIL_REQUESTS_PER_RUN}'
        )
    if coverage_notices:
        print(
            'Body coverage notices (stable or access-limited): '
            f'{len(coverage_notices)}'
        )

    # Print summary
    for p in all_posts[:5]:
        print(f"  {(p.get('post_date') or '')[:10]} | {p.get('title', '')[:60]} | {p.get('wordcount', 0)} words")
    if len(all_posts) > 5:
        print(f"\n... and {len(all_posts)-5} more")

    # Check how many have full content
    with_content = sum(1 for p in all_posts if len(p['body_text']) > 500)
    print(f"Posts with substantial content (>500 chars): {with_content}/{len(all_posts)}")


if __name__ == '__main__':
    main()
