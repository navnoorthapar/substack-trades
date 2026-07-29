#!/usr/bin/env python3
"""Fetch the complete authored-post catalogue from Navnoor's Medium profile.

Medium's RSS feed exposes only the newest ten posts.  The author subdomain's
public profile GraphQL connection is paginated and currently exposes the full
archive, including member-only article metadata and previews.  A previous good
catalogue is preserved (and the RSS feed is merged into it) if that undocumented
connection is temporarily unavailable.
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
    iso_instant,
    strip_html,
)


ROOT = Path(__file__).parent
OUTPUT_PATH = Path(os.environ.get('MEDIUM_OUTPUT', ROOT / 'medium_posts.json')).expanduser()
PREVIOUS_PATH = Path(os.environ.get('PREVIOUS_MEDIUM', ROOT / 'medium_posts.json')).expanduser()
_status_output = os.environ.get('FETCH_STATUS_OUTPUT')
FETCH_STATUS_PATH = Path(_status_output).expanduser() if _status_output else None

USERNAME = 'navnoorbawa'
GRAPHQL_URL = f'https://{USERNAME}.medium.com/_/graphql'
RSS_URL = f'https://medium.com/feed/@{USERNAME}'
PAGE_LIMIT = 25
MAX_RSS_BYTES = 2_000_000
MAX_GRAPHQL_BYTES = 12_000_000

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

    return {
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


def load_previous(path=PREVIOUS_PATH):
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


def write_fetch_status(status, mode, fetched_count, posts, error=None):
    if FETCH_STATUS_PATH is None:
        return
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
                posts.append({
                    'source': 'medium',
                    'source_id': post_id,
                    'medium_id': post_id,
                    'slug': slug,
                    'title': (item.findtext('title') or '').strip(),
                    'display_title': (item.findtext('title') or '').strip(),
                    'subtitle': description,
                    'post_date': parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'latest_published_at': '',
                    'url': clean_url,
                    'canonical_url': '',
                    'audience': 'unknown',
                    'visibility': 'UNKNOWN',
                    'is_published': True,
                    'wordcount': body_word_count(description),
                    'body_text': description,
                    'content_status': 'excerpt',
                    # RSS proves this exact live excerpt, but does not expose a
                    # separate article-update timestamp.
                    'body_revision_status': 'current',
                    'source_updated_at': '',
                    'observed_source_updated_at': '',
                    'mirror_substack_slug': None,
                    'pinned': False,
                })
            if not posts:
                raise ValueError('Medium RSS returned no recognizable posts')
            return posts
        except (urllib.error.URLError, TimeoutError, ET.ParseError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    if last_error is None:
        raise ValueError('Medium RSS attempts must be at least one')
    raise last_error


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
    return item


def live_rss_record(post):
    """Normalize a live RSS match as a current, excerpt-only capture."""
    item = dict(post)
    item['content_status'] = 'excerpt'
    item['body_revision_status'] = 'current'
    item['source_updated_at'] = ''
    item['observed_source_updated_at'] = ''
    return item


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
    try:
        raw_posts = fetch_archive()
        posts = [convert_post(post) for post in raw_posts]
        posts.sort(key=lambda post: post.get('post_date') or '', reverse=True)
        validate_catalogue(posts, previous)
        mode = 'complete_archive'
        status = 'ok'
        fetched_count = len(raw_posts)
    except Exception as archive_error:
        if not previous:
            print(f'Medium archive fetch failed with no previous catalogue: {archive_error}',
                  file=sys.stderr)
            write_fetch_status('failed', 'archive_failed', 0, [], archive_error)
            return 1
        print(f'Warning: complete Medium archive unavailable: {archive_error}', file=sys.stderr)
        print('Merging the latest RSS items into the previous complete catalogue.')
        try:
            latest = fetch_rss_posts()
        except Exception as rss_error:
            # A cached catalogue proves only what was known previously.  If
            # both live discovery paths are down, publishing it as freshly
            # checked would conceal a possible new article.
            message = f'Medium archive and RSS fetches both failed: {rss_error}'
            print(message, file=sys.stderr)
            write_fetch_status(
                'failed', 'archive_and_rss_failed', 0, previous,
                f'archive: {archive_error}; RSS: {rss_error}',
            )
            return 1
        by_id = {
            str(post.get('medium_id') or post.get('source_id')):
                carried_cached_record(post)
            for post in previous
            if post.get('medium_id') or post.get('source_id')
        }
        new_count = 0
        for post in latest:
            post_id = post['medium_id']
            if post_id not in by_id:
                new_count += 1
            # A live RSS row replaces any cached row with the exact excerpt
            # visible in this refresh. Rows absent from RSS remain searchable
            # but explicitly unverified.
            by_id[post_id] = live_rss_record(post)
        posts = sorted(by_id.values(), key=lambda post: post.get('post_date') or '', reverse=True)
        try:
            validate_catalogue(posts, previous)
        except ValueError as catalogue_error:
            print(f'Medium fallback catalogue is invalid: {catalogue_error}', file=sys.stderr)
            write_fetch_status(
                'failed', 'cached_archive_plus_rss_invalid', len(latest), previous,
                catalogue_error,
            )
            return 1
        mode = 'cached_archive_plus_rss'
        status = 'degraded'
        fetched_count = len(latest)

    atomic_write_json(OUTPUT_PATH, posts)
    write_fetch_status(status, mode, fetched_count, posts)
    public_count = sum(post.get('visibility') == 'PUBLIC' for post in posts)
    locked_count = sum(post.get('visibility') == 'LOCKED' for post in posts)
    mirror_count = sum(bool(post.get('mirror_substack_slug')) for post in posts)
    mode_summary = mode.replace('_', ' ')
    if status == 'degraded':
        mode_summary += f' ({new_count} new)'
    print(f'Saved {len(posts)} Medium posts to {OUTPUT_PATH} via {mode_summary}.')
    print(f'  {public_count} public, {locked_count} member-only, '
          f'{mirror_count} explicit Substack mirrors')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
