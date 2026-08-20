#!/usr/bin/env python3
"""Maintain Navnoor's Medium catalogue from fail-closed source evidence.

Medium's supported public RSS feed exposes the newest ten posts.  The legacy
profile GraphQL archive is still attempted for complete-catalogue recovery, but
it is not treated as an available or supported public interface.  RSS extends a
previously validated catalogue only when its complete window proves contiguous
lineage, or when one exact, expiring operator-reviewed public-profile sequence
bridges a known gap.  Every other RSS merge is quarantined and cannot become
trusted input for a later refresh.
"""
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from fetch_all_posts import (
    SSL_CONTEXT,
    atomic_write_json,
    body_word_count,
    bounded_excerpt,
    iso_instant,
    strip_html,
)


ROOT = Path(__file__).resolve().parent
# The prior trusted catalogue and reviewed bridge are code-owned repository
# inputs.  Candidate output is confined to one fixed name in the caller's
# working directory; refresh.sh supplies a private mktemp directory as that cwd.
# Do not restore path-bearing CLI or environment controls here: this collector
# does not need arbitrary filesystem authority.
OUTPUT_PATH = Path.cwd() / 'medium.candidate.json'
PREVIOUS_PATH = ROOT / 'medium_posts.json'
PROFILE_BRIDGE_PATH = ROOT / 'medium_profile_sequence_bridge.json'
FETCH_STATUS_PATH = Path.cwd() / 'medium-status.json'

USERNAME = 'navnoorbawa'
GRAPHQL_URL = f'https://{USERNAME}.medium.com/_/graphql'
RSS_URL = f'https://medium.com/feed/@{USERNAME}'
PAGE_LIMIT = 25
MAX_RSS_BYTES = 2_000_000
MAX_GRAPHQL_BYTES = 12_000_000
RSS_WINDOW_SIZE = 10
PROFILE_HISTORY_PREFIX_SIZE = 2
PROFILE_BRIDGE_MAX_LIFETIME_SECONDS = 3 * 24 * 60 * 60
MEMBER_PREVIEW_MAX_CHARS = 1_200
MEMBER_PREVIEW_KEYS = frozenset((
    'schema_version', 'surface', 'text', 'character_count', 'body_sha256',
))
PROFILE_BRIDGE_KEYS = frozenset((
    'schema_version', 'source', 'author_username', 'surface', 'profile_url',
    'reviewed_at', 'expires_at', 'rss_window_ids',
    'previous_history_prefix_ids',
))
PROFILE_BRIDGE_SURFACE = 'operator-reviewed-direct-public-profile-sequence'

HEADERS = {
    'User-Agent': 'substack-trades/1.0 (+https://github.com/navnoorthapar/substack-trades)',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
}

PROFILE_QUERY = r'''
query UserProfilePosts(
  $username: ID
  $limit: PaginationLimit
  $from: String
  $include: Boolean!
) {
  userResult(username: $username) {
    __typename
    ... on User {
      id
      username
      homepagePostsConnection(
        paging: {limit: $limit, from: $from}
        includeDistributedResponses: $include
      ) {
        posts {
          id
          title
          uniqueSlug
          mediumUrl
          canonicalUrl
          isPublished
          visibility
          firstPublishedAt
          latestPublishedAt
          pinnedByCreatorAt
          creator { id username }
          inResponseToPostResult {
            __typename
            ... on Post { id }
          }
          content {
            bodyModel {
              paragraphs {
                text
                type
                markups { href }
              }
            }
          }
        }
        pagingInfo { next { from limit } }
      }
    }
  }
}
'''

PROMO_MARKER = 'read this article free on substack'
SUBSTACK_URL_RE = re.compile(
    r'^https://(?:open\.substack\.com/pub/navnoorbawa|navnoorbawa\.substack\.com)'
    r'/p/([^/?#]+)',
    re.IGNORECASE,
)
MEDIUM_ID_RE = re.compile(r'(?:-|/)([0-9a-f]{12})(?:[/?#]|$)', re.IGNORECASE)
MEDIUM_SLUG_ID_RE = re.compile(
    r'^(?:.+-)?([0-9a-f]{12})$',
    re.IGNORECASE,
)
RSS_TRACKING_QUERY_RE = re.compile(
    r'^source=rss-[0-9a-f]{12}-{6}[0-9]+$'
)
MIN_PUBLICATION_TIMESTAMP_MS = 1262304000000  # 2010-01-01T00:00:00Z


def canonical_medium_item_identity(url, allow_rss_tracking=False):
    """Return canonical URL, decoded slug, and ID for one exact author item."""
    if not isinstance(url, str) or not url or url != url.strip():
        raise ValueError('Medium item URL is not a canonical author URL')
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError('Medium item URL cannot be parsed') from None
    if (
        parsed.scheme != 'https'
        or parsed.hostname != 'medium.com'
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ValueError('Medium item URL is not a canonical author URL')
    if parsed.query and (
        not allow_rss_tracking
        or RSS_TRACKING_QUERY_RE.fullmatch(parsed.query) is None
    ):
        raise ValueError('Medium item URL has an invalid query')
    prefix = f'/@{USERNAME}/'
    if not parsed.path.startswith(prefix):
        raise ValueError('Medium item URL has the wrong author path')
    raw_slug = parsed.path[len(prefix):]
    if (
        not raw_slug
        or '/' in raw_slug
        or re.search(r'%(?![0-9A-Fa-f]{2})', raw_slug)
    ):
        raise ValueError('Medium item URL contains a non-canonical path')
    try:
        slug = urllib.parse.unquote_to_bytes(raw_slug).decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Medium item URL slug is not canonical UTF-8') from None
    if (
        unicodedata.normalize('NFC', slug) != slug
        or '.' in slug
        or not all(character.isalnum() or character in {'-', '_'}
                   for character in slug)
    ):
        raise ValueError('Medium item URL contains a non-canonical path')
    encoded_slug = urllib.parse.quote(slug, safe='-_')
    if raw_slug != encoded_slug:
        raise ValueError('Medium item URL slug is not canonically encoded')
    id_match = MEDIUM_SLUG_ID_RE.fullmatch(slug)
    if id_match is None:
        raise ValueError('Medium item URL has no canonical post ID')
    canonical = urllib.parse.urlunsplit(
        ('https', 'medium.com', parsed.path, '', '')
    )
    expected = urllib.parse.urlunsplit(
        ('https', 'medium.com', parsed.path, parsed.query, '')
    )
    if url != expected:
        raise ValueError('Medium item URL is not in canonical form')
    return canonical, slug, id_match.group(1).casefold()


def _positive_timestamp(value):
    """Accept only plausible integer millisecond publication epochs."""
    return type(value) is int and value >= MIN_PUBLICATION_TIMESTAMP_MS


def _valid_publication_timestamps(first_published_at, latest_published_at):
    return (
        _positive_timestamp(first_published_at)
        and _positive_timestamp(latest_published_at)
        and latest_published_at >= first_published_at
    )


def _strict_json_object(raw):
    """Decode bounded UTF-8 JSON while rejecting non-standard/ambiguous input."""
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Medium GraphQL response is not UTF-8 JSON') from None

    def reject_constant(value):
        raise ValueError(f'Medium GraphQL response contains invalid JSON value {value}')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(
                    f'Medium GraphQL response repeats JSON key {key!r}'
                )
            result[key] = value
        return result

    try:
        result = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except json.JSONDecodeError:
        raise
    if not isinstance(result, dict):
        raise ValueError('Medium GraphQL response is not a JSON object')
    return result


def request_json(url, payload, attempts=3):
    """POST JSON with retries and require a valid GraphQL response."""
    body = json.dumps(payload).encode('utf-8')
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=HEADERS, method='POST')
            with urllib.request.urlopen(request, timeout=45, context=SSL_CONTEXT) as response:
                final_url = urllib.parse.urlsplit(response.geturl())
                if (
                    final_url.scheme != 'https'
                    or final_url.hostname != f'{USERNAME}.medium.com'
                    or final_url.username is not None
                    or final_url.password is not None
                    or final_url.port is not None
                    or final_url.path != '/_/graphql'
                    or final_url.query
                    or final_url.fragment
                ):
                    raise ValueError(
                        'Medium GraphQL redirected away from the canonical '
                        'HTTPS author endpoint'
                    )
                content_length = response.headers.get('Content-Length')
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError):
                        raise ValueError(
                            'Medium GraphQL returned an invalid Content-Length'
                        ) from None
                    if declared_length < 0 or declared_length > MAX_GRAPHQL_BYTES:
                        raise ValueError(
                            f'Medium GraphQL exceeds {MAX_GRAPHQL_BYTES} bytes'
                        )
                raw = response.read(MAX_GRAPHQL_BYTES + 1)
            if len(raw) > MAX_GRAPHQL_BYTES:
                raise ValueError(
                    f'Medium GraphQL exceeds {MAX_GRAPHQL_BYTES} bytes'
                )
            result = _strict_json_object(raw)
            errors = result.get('errors')
            if errors:
                if not isinstance(errors, list):
                    raise ValueError(
                        'Medium GraphQL errors payload is not a list'
                    )
                messages = '; '.join(
                    str(item.get('message') or item)
                    if isinstance(item, dict) else str(item)
                    for item in errors
                )
                raise ValueError(f'Medium GraphQL error: {messages}')
            return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    if last_error is None:
        raise ValueError('Medium GraphQL attempts must be at least one')
    raise last_error


def _fetch_archive_pass():
    """Return one complete duplicate-free Medium archive pass."""
    cursor = None
    seen_cursors = set()
    posts_by_id = {}
    user_id = None
    page = 0
    seen_post_ids = set()

    while True:
        cursor_key = cursor or '<first-page>'
        if cursor_key in seen_cursors:
            raise ValueError('Medium repeated a pagination cursor')
        seen_cursors.add(cursor_key)
        page += 1
        if page > 100:
            raise ValueError('Medium pagination exceeded the safety limit')

        payload = {
            'operationName': 'UserProfilePosts',
            'variables': {
                'username': USERNAME,
                'limit': PAGE_LIMIT,
                'from': cursor,
                'include': False,
            },
            'query': PROFILE_QUERY,
        }
        result = request_json(GRAPHQL_URL, payload)
        user = ((result.get('data') or {}).get('userResult') or {})
        if user.get('__typename') != 'User' or not user.get('id'):
            raise ValueError(f'Medium user @{USERNAME} was not found')
        if user_id is None:
            user_id = str(user['id'])
        elif str(user['id']) != user_id:
            raise ValueError('Medium changed user identity during pagination')

        connection = user.get('homepagePostsConnection') or {}
        page_posts = connection.get('posts')
        if not isinstance(page_posts, list):
            raise ValueError('Medium returned an unexpected posts payload')

        accepted = 0
        for row_index, post in enumerate(page_posts):
            if not isinstance(post, dict):
                raise ValueError(
                    f'Medium archive returned malformed post row {row_index}'
                )
            post_id = post.get('id')
            creator = post.get('creator')
            if (
                not isinstance(post_id, str)
                or not post_id
                or post_id != post_id.strip()
                or not isinstance(creator, dict)
                or not isinstance(creator.get('id'), str)
                or not creator['id']
                or type(post.get('isPublished')) is not bool
                or 'inResponseToPostResult' not in post
                or (
                    post.get('inResponseToPostResult') is not None
                    and not isinstance(post['inResponseToPostResult'], dict)
                )
            ):
                raise ValueError(
                    f'Medium archive returned malformed post row {row_index}'
                )
            if post_id in seen_post_ids:
                raise ValueError(
                    'Medium archive repeated a post ID across pagination'
                )
            seen_post_ids.add(post_id)
            if str(creator.get('id') or '') != user_id:
                continue
            if post.get('isPublished') is not True:
                continue
            if post.get('inResponseToPostResult') is not None:
                continue
            if not _valid_publication_timestamps(
                post.get('firstPublishedAt'),
                post.get('latestPublishedAt'),
            ):
                raise ValueError(
                    f'Medium authored post {post_id!r} has invalid '
                    'publication timestamps'
                )
            _, slug, url_post_id = canonical_medium_item_identity(
                post.get('mediumUrl')
            )
            unique_slug = post.get('uniqueSlug')
            if (
                not isinstance(unique_slug, str)
                or unique_slug != slug
                or url_post_id != post_id.casefold()
            ):
                raise ValueError(
                    f'Medium authored post {post_id!r} has inconsistent '
                    'canonical URL identity'
                )
            posts_by_id[post_id] = post
            accepted += 1

        next_page = ((connection.get('pagingInfo') or {}).get('next'))
        print(f'  Medium page {page}: {len(page_posts)} returned, '
              f'{accepted} authored ({len(posts_by_id)} total)')
        if not next_page:
            break
        cursor = next_page.get('from')
        if not cursor:
            raise ValueError('Medium supplied a next page without a cursor')

    if not posts_by_id:
        raise ValueError('Medium archive returned zero authored posts')
    return list(posts_by_id.values())


def _archive_signature(posts):
    payload = json.dumps(
        sorted(posts, key=lambda post: str(post.get('id') or '')),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def fetch_archive():
    """Return an exact archive only after two matching complete passes."""
    print('  Medium archive verification pass 1 of 2')
    first = _fetch_archive_pass()
    print('  Medium archive verification pass 2 of 2')
    second = _fetch_archive_pass()
    if _archive_signature(first) != _archive_signature(second):
        raise ValueError(
            'Medium archive changed between verification passes'
        )
    return second


def _paragraphs(post):
    value = (((post.get('content') or {}).get('bodyModel') or {}).get('paragraphs'))
    return value if isinstance(value, list) else []


def _full_title(post, paragraphs):
    display_title = str(post.get('title') or '').strip()
    # Medium truncates many Post.title values for profile cards.  The first
    # content heading contains the complete title for those articles.
    for index, paragraph in enumerate(paragraphs[:3]):
        if (paragraph.get('type') in {'H1', 'H2', 'H3'}
                and str(paragraph.get('text') or '').strip()):
            heading = str(paragraph['text']).strip()
            if index == 0 or display_title.endswith(('…', '...')):
                return heading
    return display_title


def _subtitle(paragraphs):
    for paragraph in paragraphs[:8]:
        text = str(paragraph.get('text') or '').strip()
        if paragraph.get('type') == 'P' and text and PROMO_MARKER not in text.casefold():
            return text
    return ''


def _mirror_slug(paragraphs):
    """Return a Substack slug only for Medium's explicit cross-post notice.

    Ordinary articles can link to related Substack posts, so a raw link alone is
    not proof that the Medium article is the same work.
    """
    for paragraph in paragraphs:
        text = str(paragraph.get('text') or '')
        if PROMO_MARKER not in text.casefold():
            continue
        for markup in paragraph.get('markups') or []:
            href = str((markup or {}).get('href') or '')
            match = SUBSTACK_URL_RE.match(href)
            if match:
                return urllib.parse.unquote(match.group(1)).strip('/')
    return None


def _iso_timestamp(milliseconds):
    if not _positive_timestamp(milliseconds):
        return ''
    try:
        return (datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
                .isoformat(timespec='milliseconds').replace('+00:00', 'Z'))
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def _member_preview(text, surface='anonymous-medium-profile'):
    """Return an exact proof for one bounded anonymous Medium surface."""
    preview = bounded_excerpt(text)
    return {
        'schema_version': 1,
        'surface': surface if preview else 'metadata-only',
        'text': preview,
        'character_count': len(preview),
        'body_sha256': hashlib.sha256(preview.encode('utf-8')).hexdigest(),
    }


def _trusted_member_preview(value):
    """Return the proven preview text, or None for legacy/untrusted caches."""
    if not isinstance(value, dict) or set(value) != MEMBER_PREVIEW_KEYS:
        return None
    text = value.get('text')
    if not isinstance(text, str) or len(text) > MEMBER_PREVIEW_MAX_CHARS:
        return None
    surface = value.get('surface')
    if surface not in {'anonymous-medium-profile', 'metadata-only'}:
        return None
    if value.get('character_count') != len(text):
        return None
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if value.get('body_sha256') != digest:
        return None
    if (not text) != (surface == 'metadata-only'):
        return None
    return text


def public_medium_post(post):
    """Strip a locked Medium record to its proven anonymous preview."""
    item = dict(post)
    audience = str(item.get('audience') or '').strip().casefold()
    if audience != 'locked':
        item.pop('member_preview', None)
        return item
    preview = _trusted_member_preview(item.get('member_preview'))
    if preview is None:
        preview = ''
    proof = _member_preview(preview)
    item['body_text'] = preview
    subtitle = str(item.get('subtitle') or '').strip()
    item['subtitle'] = subtitle if subtitle and subtitle in preview else ''
    item['wordcount'] = 0
    item['content_status'] = 'excerpt'
    item['member_preview'] = proof
    return item


def convert_post(post):
    """Convert Medium's GraphQL shape into the project's post schema."""
    paragraphs = _paragraphs(post)
    body_parts = [str(paragraph.get('text') or '').strip()
                  for paragraph in paragraphs if str(paragraph.get('text') or '').strip()]
    body_text = '\n\n'.join(body_parts)
    title = _full_title(post, paragraphs)
    display_title = str(post.get('title') or '').strip()
    post_id = str(post.get('id') or '')
    visibility = str(post.get('visibility') or '').upper()
    if visibility not in {'PUBLIC', 'LOCKED'}:
        raise ValueError('Medium post has an unsupported visibility')
    if visibility == 'LOCKED':
        body_text = bounded_excerpt(body_text)
    url = post.get('mediumUrl')
    url, unique_slug, url_post_id = canonical_medium_item_identity(url)
    if url_post_id != post_id.casefold():
        raise ValueError('Medium post ID does not match its canonical URL')
    if post.get('uniqueSlug') != unique_slug:
        raise ValueError('Medium unique slug does not match its canonical URL')
    if not _valid_publication_timestamps(
        post.get('firstPublishedAt'),
        post.get('latestPublishedAt'),
    ):
        raise ValueError('Medium post has invalid publication timestamps')
    source_updated_at = _iso_timestamp(post.get('latestPublishedAt'))

    item = {
        'source': 'medium',
        'source_id': post_id,
        'medium_id': post_id,
        'slug': unique_slug or post_id,
        'title': title or display_title or post_id,
        'display_title': display_title,
        'subtitle': _subtitle(paragraphs),
        'post_date': _iso_timestamp(post.get('firstPublishedAt')),
        'latest_published_at': source_updated_at,
        'url': url,
        'canonical_url': str(post.get('canonicalUrl') or '').strip(),
        'audience': visibility.casefold(),
        'visibility': visibility,
        'is_published': True,
        'wordcount': body_word_count(body_text),
        'body_text': body_text,
        # GraphQL supplies paragraph text but no independent declared-length
        # field against which completeness can be proved. Keep every capture
        # searchable as an exact current excerpt without claiming full text.
        'content_status': 'excerpt',
        'body_revision_status': 'current',
        'source_updated_at': source_updated_at,
        'observed_source_updated_at': source_updated_at,
        'mirror_substack_slug': _mirror_slug(paragraphs),
        'pinned': bool(post.get('pinnedByCreatorAt')),
    }
    if visibility == 'LOCKED':
        item['member_preview'] = _member_preview(body_text)
        item['wordcount'] = 0
    return item


def load_previous():
    path = PREVIOUS_PATH
    if not path.exists():
        return []
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
    except Exception as exc:
        raise ValueError(f'previous Medium catalogue is invalid: {exc}') from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError('previous Medium catalogue must be a list of objects')
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def newest_post_date(posts):
    dates = [post.get('post_date') for post in posts
             if isinstance(post, dict) and isinstance(post.get('post_date'), str)]
    return max(
        dates,
        key=iso_instant,
        default='',
    )


def write_fetch_status(
        status, mode, fetched_count, posts, error=None, provenance=None,
):
    payload = {
        'schema_version': 1,
        'source': 'medium',
        'checked_at': utc_now(),
        'status': status,
        'mode': mode,
        'fetched_count': fetched_count,
        'published_count': len(posts),
        'newest': newest_post_date(posts),
    }
    if error:
        payload['error'] = str(error)
    if provenance is not None:
        payload['provenance'] = provenance
    atomic_write_json(FETCH_STATUS_PATH, payload)


def fetch_rss_posts(attempts=3):
    """Fetch the latest ten posts for incremental fallback only."""
    last_error = None
    headers = dict(HEADERS)
    headers['Accept'] = 'application/rss+xml, application/xml, text/xml'
    headers.pop('Content-Type', None)
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(RSS_URL, headers=headers)
            with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
                final_url = urllib.parse.urlsplit(response.geturl())
                if (
                        final_url.scheme != 'https'
                        or final_url.hostname != 'medium.com'
                        or final_url.username is not None
                        or final_url.password is not None
                        or final_url.port is not None
                        or final_url.path != f'/feed/@{USERNAME}'
                        or final_url.query
                        or final_url.fragment):
                    raise ValueError('Medium RSS redirected away from canonical HTTPS')
                payload = response.read(MAX_RSS_BYTES + 1)
            if len(payload) > MAX_RSS_BYTES:
                raise ValueError(f'Medium RSS exceeds {MAX_RSS_BYTES} bytes')
            try:
                xml_text = payload.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise ValueError('Medium RSS is not UTF-8 XML') from None
            upper_xml = xml_text.upper()
            if '<!DOCTYPE' in upper_xml or '<!ENTITY' in upper_xml:
                raise ValueError('Medium RSS contains a prohibited XML declaration')
            # The input is bounded, UTF-8-only, and has no entity declarations.
            root = ET.fromstring(xml_text)  # noqa: S314
            items = root.findall('./channel/item')
            posts = []
            for item in items:
                link = (item.findtext('link') or item.findtext('guid') or '').strip()
                clean_url, slug, post_id = canonical_medium_item_identity(
                    link,
                    allow_rss_tracking=True,
                )
                date_value = item.findtext('pubDate') or ''
                parsed = email.utils.parsedate_to_datetime(date_value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                description = strip_html(item.findtext('description') or '')
                published = (
                    parsed.astimezone(timezone.utc)
                    .isoformat()
                    .replace('+00:00', 'Z')
                )
                posts.append({
                    'source': 'medium',
                    'source_id': post_id,
                    'medium_id': post_id,
                    'slug': slug,
                    'title': (item.findtext('title') or '').strip(),
                    'display_title': (item.findtext('title') or '').strip(),
                    'subtitle': description,
                    'post_date': published,
                    'latest_published_at': published,
                    'url': clean_url,
                    'canonical_url': '',
                    'audience': 'unknown',
                    'visibility': 'UNKNOWN',
                    'is_published': True,
                    'wordcount': body_word_count(description),
                    'body_text': description,
                    'content_status': 'excerpt',
                    # RSS proves this exact live excerpt. Bind the publication
                    # instant because RSS has no distinct update timestamp.
                    'body_revision_status': 'current',
                    'source_updated_at': published,
                    'observed_source_updated_at': published,
                    'mirror_substack_slug': None,
                    'pinned': False,
                })
            if not posts:
                raise ValueError('Medium RSS returned no recognizable posts')
            validate_rss_sequence(posts)
            return posts
        except (urllib.error.URLError, TimeoutError, ET.ParseError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    if last_error is None:
        raise ValueError('Medium RSS attempts must be at least one')
    raise last_error


def _rss_publication_instant(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('Medium RSS post has a non-UTC publication timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(
            'Medium RSS post has an invalid publication timestamp'
        ) from None
    if parsed.tzinfo is None:
        raise ValueError('Medium RSS post has a timezone-free publication timestamp')
    return parsed.astimezone(timezone.utc)


def validate_rss_sequence(posts):
    """Require one exact, duplicate-free newest-first RSS window."""
    if not isinstance(posts, list) or not posts:
        raise ValueError('Medium RSS returned no recognizable posts')
    ids = []
    urls = []
    instants = []
    for post in posts:
        if not isinstance(post, dict):
            raise ValueError('Medium RSS returned a malformed post row')
        post_id = post.get('medium_id') or post.get('source_id')
        url = post.get('url')
        if not isinstance(post_id, str) or not post_id:
            raise ValueError('Medium RSS post has no source identity')
        if not isinstance(url, str) or not url:
            raise ValueError('Medium RSS post has no canonical URL')
        ids.append(post_id)
        urls.append(url)
        instants.append(_rss_publication_instant(post.get('post_date')))
    if len(ids) != len(set(ids)):
        raise ValueError('Medium RSS repeated a post ID')
    if len(urls) != len(set(urls)):
        raise ValueError('Medium RSS repeated a canonical URL')
    if instants != sorted(instants, reverse=True):
        raise ValueError('Medium RSS posts are not in newest-first order')
    return instants


def _rss_signature(posts):
    payload = json.dumps(
        posts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def fetch_stable_rss_posts():
    """Return RSS only after two exact normalized windows agree."""
    print('  Medium RSS verification pass 1 of 2')
    first = fetch_rss_posts()
    print('  Medium RSS verification pass 2 of 2')
    second = fetch_rss_posts()
    if _rss_signature(first) != _rss_signature(second):
        raise ValueError('Medium RSS changed between verification passes')
    return second


def _post_identity(post, label):
    if not isinstance(post, dict):
        raise ValueError(f'{label} has a malformed row')
    post_id = post.get('medium_id') or post.get('source_id')
    if (
            not isinstance(post_id, str)
            or re.fullmatch(r'[0-9a-f]{12}', post_id) is None):
        raise ValueError(f'{label} has an invalid source identity')
    return post_id


def _history_by_recency(previous):
    rows = []
    seen_ids = set()
    for post in previous:
        post_id = _post_identity(post, 'previous Medium catalogue')
        if post_id in seen_ids:
            raise ValueError('previous Medium catalogue repeats a post ID')
        seen_ids.add(post_id)
        published = post.get('post_date')
        rows.append((post_id, published, _rss_publication_instant(published)))
    return sorted(rows, key=lambda row: row[2], reverse=True)


def require_complete_rss_window(latest, previous):
    """Require Medium's full ten-row public window for established history."""
    if len(previous) >= RSS_WINDOW_SIZE and len(latest) != RSS_WINDOW_SIZE:
        raise ValueError(
            f'Medium RSS returned {len(latest)} rows for established history; '
            f'exactly {RSS_WINDOW_SIZE} are required'
        )


def _bridge_instant(value, label):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError(f'Medium profile bridge {label} is not a UTC instant')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(
            f'Medium profile bridge {label} is not a valid instant'
        ) from None
    if parsed.tzinfo is None:
        raise ValueError(f'Medium profile bridge {label} has no timezone')
    parsed = parsed.astimezone(timezone.utc)
    canonical = parsed.isoformat(timespec='seconds').replace('+00:00', 'Z')
    if value != canonical:
        raise ValueError(
            f'Medium profile bridge {label} is not a canonical UTC instant'
        )
    return parsed


def _strict_bridge_object(path):
    def reject_constant(value):
        raise ValueError(
            f'Medium profile bridge contains invalid JSON value {value}'
        )

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(
                    f'Medium profile bridge repeats JSON key {key!r}'
                )
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f'Medium profile bridge is unavailable: {exc}') from exc
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Medium profile bridge is not UTF-8 JSON') from None
    try:
        value = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f'Medium profile bridge is invalid JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError('Medium profile bridge must be a JSON object')
    return value


def load_profile_bridge(path=None, now=None):
    """Load one exact, short-lived operator-reviewed profile sequence."""
    bridge_path = PROFILE_BRIDGE_PATH if path is None else Path(path)
    value = _strict_bridge_object(bridge_path)
    if set(value) != PROFILE_BRIDGE_KEYS:
        raise ValueError('Medium profile bridge does not match the exact schema')
    if (
            type(value.get('schema_version')) is not int
            or value.get('schema_version') != 1
            or value.get('source') != 'medium'
            or value.get('author_username') != USERNAME
            or value.get('surface') != PROFILE_BRIDGE_SURFACE
            or value.get('profile_url') != f'https://medium.com/@{USERNAME}'):
        raise ValueError('Medium profile bridge provenance is invalid')
    reviewed_at = _bridge_instant(value.get('reviewed_at'), 'reviewed_at')
    expires_at = _bridge_instant(value.get('expires_at'), 'expires_at')
    if expires_at <= reviewed_at:
        raise ValueError('Medium profile bridge expiry is not after its review')
    lifetime = (expires_at - reviewed_at).total_seconds()
    if lifetime > PROFILE_BRIDGE_MAX_LIFETIME_SECONDS:
        raise ValueError('Medium profile bridge lifetime exceeds three days')
    current = (
        _bridge_instant(utc_now(), 'validation_time')
        if now is None else now
    )
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError('Medium profile bridge validation time has no timezone')
    current = current.astimezone(timezone.utc)
    if current < reviewed_at:
        raise ValueError('Medium profile bridge review is in the future')
    if current > expires_at:
        raise ValueError('Medium profile bridge has expired')

    rss_ids = value.get('rss_window_ids')
    history_ids = value.get('previous_history_prefix_ids')
    for ids, expected_size, label in (
            (rss_ids, RSS_WINDOW_SIZE, 'RSS window'),
            (history_ids, PROFILE_HISTORY_PREFIX_SIZE, 'history prefix')):
        if (
                not isinstance(ids, list)
                or len(ids) != expected_size
                or any(
                    not isinstance(post_id, str)
                    or re.fullmatch(r'[0-9a-f]{12}', post_id) is None
                    for post_id in ids
                )
                or len(ids) != len(set(ids))):
            raise ValueError(f'Medium profile bridge {label} is invalid')
    if set(rss_ids) & set(history_ids):
        raise ValueError('Medium profile bridge sequences overlap')
    return value


def validate_profile_bridge(latest, previous, path=None, now=None):
    """Validate the reviewed sequence against this exact RSS/history edge."""
    validate_rss_sequence(latest)
    require_complete_rss_window(latest, previous)
    bridge = load_profile_bridge(path=path, now=now)
    latest_ids = [
        _post_identity(post, 'Medium RSS') for post in latest
    ]
    history_ids = [
        row[0] for row in _history_by_recency(previous)
    ]
    if latest_ids != bridge['rss_window_ids']:
        raise ValueError(
            'Medium profile bridge does not match the complete live RSS window'
        )
    if (
            history_ids[:PROFILE_HISTORY_PREFIX_SIZE]
            != bridge['previous_history_prefix_ids']):
        raise ValueError(
            'Medium profile bridge does not match the newest trusted '
            'history prefix'
        )
    if set(latest_ids) & set(history_ids):
        raise ValueError(
            'Medium profile bridge RSS window already overlaps trusted history'
        )
    return bridge


def profile_bridge_provenance(bridge):
    """Return the public, bounded provenance retained in fetch status."""
    return {
        'surface': bridge['surface'],
        'profile_url': bridge['profile_url'],
        'reviewed_at': bridge['reviewed_at'],
        'expires_at': bridge['expires_at'],
        'rss_window_ids': list(bridge['rss_window_ids']),
        'previous_history_prefix_ids': list(
            bridge['previous_history_prefix_ids']
        ),
    }


def validate_incremental_rss(latest, previous):
    """Prove that the live RSS window extends validated history without a gap.

    RSS is a bounded current surface, not a complete archive.  A known-item
    overlap proves that every item above the first overlap was observed, while
    requiring all later rows to be known rejects a hole inside that window.
    Historical rows remain explicitly unverified at the body-revision level.
    """
    rss_instants = validate_rss_sequence(latest)
    if not isinstance(previous, list) or not previous:
        raise ValueError('Medium RSS has no validated historical catalogue')
    require_complete_rss_window(latest, previous)
    history = _history_by_recency(previous)
    previous_ids = [row[0] for row in history]
    previous_instants = [row[2] for row in history]
    previous_instants_by_id = {row[0]: row[2] for row in history}
    if rss_instants[0] < max(previous_instants):
        raise ValueError('Medium RSS newest publication regressed behind history')

    known_ids = set(previous_ids)
    overlap_seen = False
    rss_ids = []
    for post in latest:
        post_id = _post_identity(post, 'Medium RSS')
        rss_ids.append(post_id)
        if post_id in known_ids:
            if (
                    _rss_publication_instant(post.get('post_date'))
                    != previous_instants_by_id[post_id]):
                raise ValueError(
                    'Medium RSS changed the exact publication timestamp for '
                    f'known post {post_id}'
                )
            overlap_seen = True
        elif overlap_seen:
            raise ValueError(
                'Medium RSS contains an unknown item below a history overlap'
            )
    if not overlap_seen:
        raise ValueError(
            'Medium RSS window has no overlap with validated history'
        )

    first_known = next(
        index for index, post_id in enumerate(rss_ids)
        if post_id in known_ids
    )
    known_tail = rss_ids[first_known:]
    if known_tail != previous_ids[:len(known_tail)]:
        raise ValueError(
            'Medium RSS overlap does not continue from the newest '
            'validated history edge'
        )


def validate_archive_rss_edge(archive, latest):
    """Bind a purported complete archive to the supported live RSS edge."""
    rss_instants = validate_rss_sequence(latest)
    if not isinstance(archive, list) or not archive:
        raise ValueError('Medium archive has no rows for RSS verification')
    expected_window = min(RSS_WINDOW_SIZE, len(archive))
    if len(latest) != expected_window:
        raise ValueError(
            f'Medium RSS returned {len(latest)} rows for an archive of '
            f'{len(archive)}; exactly {expected_window} are required'
        )
    archive_edge = sorted(
        archive,
        key=lambda post: _rss_publication_instant(post.get('post_date')),
        reverse=True,
    )[:expected_window]
    archive_ids = [
        _post_identity(post, 'Medium archive') for post in archive_edge
    ]
    rss_ids = [
        _post_identity(post, 'Medium RSS') for post in latest
    ]
    if archive_ids != rss_ids:
        raise ValueError(
            'Medium archive newest edge does not match the stable RSS ID order'
        )
    archive_instants = [
        _rss_publication_instant(post.get('post_date'))
        for post in archive_edge
    ]
    if archive_instants != rss_instants:
        raise ValueError(
            'Medium archive newest edge does not match stable RSS timestamps'
        )


def carried_cached_record(post):
    """Retain a searchable cached body without calling it a current capture."""
    item = dict(post)
    item['content_status'] = 'excerpt'
    item['body_revision_status'] = 'unverified'
    item['source_updated_at'] = (
        item.get('source_updated_at')
        if isinstance(item.get('source_updated_at'), str)
        else (
            item.get('latest_published_at')
            if isinstance(item.get('latest_published_at'), str)
            else ''
        )
    )
    # A carried row was not observed in this refresh. Retaining an older
    # observation timestamp here would falsely imply the cached body was
    # verified against the source version on this run.
    item['observed_source_updated_at'] = ''
    return public_medium_post(item)


def live_rss_record(post):
    """Normalize a live RSS match as a current, excerpt-only capture."""
    item = dict(post)
    item['content_status'] = 'excerpt'
    item['body_revision_status'] = 'current'
    # RSS proves this exact live excerpt, but does not expose a distinct
    # article-update timestamp. Bind the publication instant so a current
    # excerpt remains timestamp-qualified for wire and client contracts.
    published = (
        item.get('post_date')
        if isinstance(item.get('post_date'), str)
        else ''
    )
    item['source_updated_at'] = published
    item['observed_source_updated_at'] = published
    if not item.get('latest_published_at'):
        item['latest_published_at'] = published
    return item


def merge_rss_with_history(latest, previous):
    """Merge only a separately validated RSS window into trusted history."""
    previous_by_id = {
        _post_identity(post, 'previous Medium catalogue'): post
        for post in previous
    }
    by_id = {
        post_id: carried_cached_record(post)
        for post_id, post in previous_by_id.items()
    }
    for post in latest:
        post_id = _post_identity(post, 'Medium RSS')
        item = live_rss_record(post)
        if post_id in previous_by_id:
            # Incremental validation requires exact equality. Assigning the
            # trusted value makes the retention invariant explicit rather than
            # relying on an upstream row that merely compared equal.
            item['post_date'] = previous_by_id[post_id]['post_date']
        by_id[post_id] = item
    return sorted(
        by_id.values(),
        key=lambda post: post.get('post_date') or '',
        reverse=True,
    )


def _same_output_as_previous():
    try:
        return OUTPUT_PATH.resolve(strict=False) == PREVIOUS_PATH.resolve(strict=False)
    except OSError:
        return OUTPUT_PATH.absolute() == PREVIOUS_PATH.absolute()


def preserve_trusted_history(previous):
    """Supply the prior catalogue to a transaction without rewriting itself."""
    if not _same_output_as_previous():
        atomic_write_json(OUTPUT_PATH, previous)


def quarantine_output_path():
    if _same_output_as_previous():
        return None
    return OUTPUT_PATH.with_name(f'{OUTPUT_PATH.stem}.rss-quarantine.json')


def quarantine_rss_candidate(latest, previous, reason):
    """Write an explicitly untrusted diagnostic outside the candidate path."""
    path = quarantine_output_path()
    if path is None:
        return None
    candidate = merge_rss_with_history(latest, previous)
    atomic_write_json(path, {
        'schema_version': 1,
        'source': 'medium',
        'status': 'quarantined',
        'checked_at': utc_now(),
        'reason': str(reason),
        'trusted_history_count': len(previous),
        'untrusted_merged_count': len(candidate),
        'rss_window_ids': [
            _post_identity(post, 'Medium RSS') for post in latest
        ],
        'untrusted_posts': candidate,
    })
    return path


def validate_catalogue(posts, previous):
    ids = [post.get('medium_id') for post in posts]
    urls = [post.get('url') for post in posts]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError('Medium catalogue contains missing or duplicate post IDs')
    if any(not value for value in urls) or len(urls) != len(set(urls)):
        raise ValueError('Medium catalogue contains missing or duplicate URLs')
    if any(not post.get('post_date') for post in posts):
        raise ValueError('Medium catalogue contains a post without a publication date')

    if previous and not os.environ.get('FORCE_FETCH'):
        previous_ids = {post.get('medium_id') or post.get('source_id')
                        for post in previous if post.get('medium_id') or post.get('source_id')}
        missing_previous_ids = previous_ids - set(ids)
        if missing_previous_ids:
            raise ValueError(
                f'Medium archive omitted {len(missing_previous_ids)} of '
                f'{len(previous_ids)} previous post IDs; use FORCE_FETCH only '
                'after reviewing intentional removals'
            )


def main():
    try:
        previous = load_previous()
    except ValueError as exc:
        print(f'Cannot trust previous Medium catalogue: {exc}', file=sys.stderr)
        write_fetch_status('failed', 'previous_catalogue_invalid', 0, [], exc)
        return 1
    print(f'Fetching complete Medium archive for @{USERNAME}...')
    archive_error = None
    rss_error = None
    latest = None
    rss_attempted = False
    try:
        raw_posts = fetch_archive()
        archive_posts = [convert_post(post) for post in raw_posts]
        archive_posts.sort(
            key=lambda post: post.get('post_date') or '', reverse=True,
        )
        validate_catalogue(archive_posts, previous)
    except Exception as exc:
        archive_error = exc

    if archive_error is None:
        print('Verifying the complete archive against the stable RSS edge.')
        rss_attempted = True
        try:
            latest = fetch_stable_rss_posts()
            validate_archive_rss_edge(archive_posts, latest)
        except Exception as exc:
            rss_error = exc
            archive_error = ValueError(
                f'archive could not be bound to stable RSS: {exc}'
            )
        else:
            posts = archive_posts
            mode = 'complete_archive'
            status = 'ok'
            fetched_count = len(raw_posts)
            provenance = None

    if archive_error is not None:
        if not previous:
            print(f'Medium archive fetch failed with no previous catalogue: {archive_error}',
                  file=sys.stderr)
            write_fetch_status('failed', 'archive_failed', 0, [], archive_error)
            return 1
        print(f'Warning: complete Medium archive unavailable: {archive_error}', file=sys.stderr)
        print('Checking the latest RSS window against validated catalogue history.')
        if not rss_attempted:
            rss_attempted = True
            try:
                latest = fetch_stable_rss_posts()
            except Exception as exc:
                rss_error = exc
        if latest is None:
            # A cached catalogue proves only what was known previously.  If
            # supported discovery cannot verify either a failed legacy archive
            # or an alleged successful one, publishing it would conceal a
            # possible new article.
            message = (
                'Medium archive and stable RSS verification failed: '
                f'{rss_error}'
            )
            print(message, file=sys.stderr)
            write_fetch_status(
                'failed', 'archive_and_rss_failed', 0, previous,
                f'archive: {archive_error}; RSS: {rss_error}',
            )
            return 1
        bridge = None
        try:
            validate_incremental_rss(latest, previous)
            mode = 'validated_history_plus_current_rss'
            status = 'ok'
            provenance = None
        except ValueError as incremental_error:
            print(
                'Warning: Medium RSS could not prove a contiguous increment: '
                f'{incremental_error}',
                file=sys.stderr,
            )
            try:
                bridge = validate_profile_bridge(latest, previous)
            except ValueError as bridge_error:
                reason = (
                    f'incremental proof: {incremental_error}; '
                    f'reviewed profile bridge: {bridge_error}'
                )
                quarantine_path = quarantine_rss_candidate(
                    latest, previous, reason,
                )
                preserve_trusted_history(previous)
                write_fetch_status(
                    'degraded',
                    'trusted_history_rss_gap_quarantined',
                    len(latest),
                    previous,
                    reason,
                )
                if quarantine_path is None:
                    message = (
                        'Preserved trusted Medium history unchanged and did '
                        'not write the unproven RSS merge.'
                    )
                else:
                    message = (
                        'Preserved trusted Medium history unchanged; wrote the '
                        'unproven RSS merge only to quarantine at '
                        f'{quarantine_path}.'
                    )
                print(message, file=sys.stderr)
                return 0
            mode = 'operator_reviewed_profile_bridge_plus_current_rss'
            status = 'ok'
            provenance = profile_bridge_provenance(bridge)

        posts = merge_rss_with_history(latest, previous)
        try:
            validate_catalogue(posts, previous)
        except ValueError as catalogue_error:
            print(f'Medium fallback catalogue is invalid: {catalogue_error}', file=sys.stderr)
            write_fetch_status(
                'failed', 'cached_archive_plus_rss_invalid', len(latest), previous,
                catalogue_error,
            )
            return 1
        fetched_count = len(latest)

    atomic_write_json(OUTPUT_PATH, posts)
    write_fetch_status(
        status, mode, fetched_count, posts, provenance=provenance,
    )
    public_count = sum(post.get('visibility') == 'PUBLIC' for post in posts)
    locked_count = sum(post.get('visibility') == 'LOCKED' for post in posts)
    mirror_count = sum(bool(post.get('mirror_substack_slug')) for post in posts)
    mode_summary = mode.replace('_', ' ')
    print(f'Saved {len(posts)} Medium posts to {OUTPUT_PATH} via {mode_summary}.')
    print(f'  {public_count} public, {locked_count} member-only, '
          f'{mirror_count} explicit Substack mirrors')
    return 0


def cli(argv):
    """Expose no filesystem or network controls to the command line."""
    if argv:
        print('fetch_medium_posts.py accepts no arguments', file=sys.stderr)
        return 2
    return main()


if __name__ == '__main__':
    raise SystemExit(cli(sys.argv[1:]))
