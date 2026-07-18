#!/usr/bin/env python3
"""Merge Substack and Medium while publishing each cross-post only once."""
import copy
import difflib
import html
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from article_briefs import build_article_brief
from fetch_all_posts import atomic_write_json


ROOT = Path(__file__).parent
SUBSTACK_PATH = Path(os.environ.get('SUBSTACK_POSTS', ROOT / 'all_posts.json')).expanduser()
MEDIUM_PATH = Path(os.environ.get('MEDIUM_POSTS', ROOT / 'medium_posts.json')).expanduser()
OVERRIDES_PATH = Path(os.environ.get(
    'DEDUPE_OVERRIDES', ROOT / 'medium_dedupe_overrides.json'
)).expanduser()
POSTS_OUTPUT = Path(os.environ.get(
    'POSTS_OUTPUT', ROOT / 'all_sources_posts.json'
)).expanduser()
ARTICLES_OUTPUT = Path(os.environ.get(
    'ARTICLES_OUTPUT', ROOT / 'articles_index.json'
)).expanduser()
REPORT_OUTPUT_VALUE = os.environ.get('DEDUPE_REPORT_OUTPUT')
REPORT_OUTPUT = Path(REPORT_OUTPUT_VALUE).expanduser() if REPORT_OUTPUT_VALUE else None


NUMBER_WORDS = {
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
    'fourteen': '14', 'fifteen': '15', 'sixteen': '16',
    'seventeen': '17', 'eighteen': '18', 'nineteen': '19', 'twenty': '20',
}


def load_list(path, label):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
    except Exception as exc:
        raise ValueError(f'{label} is not valid JSON: {exc}') from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f'{label} must be a JSON list of objects')
    return value


def normalize_title(value):
    """Normalize presentation differences without erasing meaningful words."""
    text = html.unescape(str(value or '')).replace('\u00a0', ' ')
    text = text.replace('&', ' and ')
    text = unicodedata.normalize('NFKD', text.casefold())
    text = ''.join(character for character in text if not unicodedata.combining(character))
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    tokens = [NUMBER_WORDS.get(token, token) for token in text.split()]
    return ' '.join(tokens)


def _title_variants(post):
    values = [post.get('title'), post.get('display_title')]
    variants = []
    for value in values:
        normalized = normalize_title(value)
        if normalized and normalized not in variants:
            variants.append(normalized)
    return variants


def _is_truncated_title(post):
    display = str(post.get('display_title') or post.get('title') or '').rstrip()
    return display.endswith(('…', '...', '..'))


def _parse_day(value):
    try:
        return datetime.strptime(str(value or '')[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _date_distance(left, right):
    left_day = _parse_day(left)
    right_day = _parse_day(right)
    if not left_day or not right_day:
        return 10 ** 9
    return abs((left_day - right_day).days)


def _token_jaccard(left, right):
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def load_overrides(path=OVERRIDES_PATH):
    if not path.exists():
        return []
    try:
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except Exception as exc:
        raise ValueError(f'dedupe overrides are not valid JSON: {exc}') from exc
    entries = payload.get('overrides') if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ValueError('dedupe overrides must contain an overrides list')
    return entries


def _canonical_substack_post(post):
    item = copy.deepcopy(post)
    item['source'] = 'substack'
    item['source_id'] = str(item.get('source_id') or item.get('slug') or '')
    item['content_status'] = item.get('content_status') or 'full'
    item['url'] = str(item.get('url') or '').strip().rstrip('/')
    return item


def _canonical_medium_post(post):
    item = copy.deepcopy(post)
    item['source'] = 'medium'
    item['source_id'] = str(item.get('source_id') or item.get('medium_id') or '')
    item['medium_id'] = str(item.get('medium_id') or item['source_id'])
    item['content_status'] = item.get('content_status') or 'excerpt'
    item['url'] = str(item.get('url') or '').strip().rstrip('/')
    return item


def _find_exact_or_prefix_match(medium, substack_by_title):
    variants = _title_variants(medium)
    for variant in variants:
        candidates = substack_by_title.get(variant, [])
        if len(candidates) == 1:
            return candidates[0], 'normalized-title'

    if not _is_truncated_title(medium):
        return None, None
    # A visibly ellipsized Medium profile title is safe to match as a long,
    # unique prefix.  Short generic prefixes are deliberately not accepted.
    for variant in variants:
        if len(variant) < 30 or len(variant.split()) < 6:
            continue
        candidates = []
        for normalized, posts in substack_by_title.items():
            if normalized.startswith(variant):
                candidates.extend(posts)
        if len(candidates) == 1:
            return candidates[0], 'ellipsized-title-prefix'
    return None, None


def _find_override_match(medium, overrides, substack_by_title):
    variants = set(_title_variants(medium))
    for override in overrides:
        key = normalize_title(override.get('medium_title_key') or override.get('medium_title'))
        if key not in variants:
            continue
        target = normalize_title(override.get('substack_title'))
        candidates = substack_by_title.get(target, [])
        if len(candidates) != 1:
            raise ValueError(
                f"override for {medium.get('title')!r} resolves to {len(candidates)} "
                'Substack articles instead of exactly one'
            )
        return candidates[0], 'reviewed-override'
    return None, None


def _find_subtitle_match(medium, substack_posts):
    medium_subtitle = normalize_title(medium.get('subtitle'))
    if len(medium_subtitle) < 60:
        return None, None
    candidates = [
        post for post in substack_posts
        if _date_distance(medium.get('post_date'), post.get('post_date')) <= 7
        and normalize_title(post.get('subtitle')) == medium_subtitle
    ]
    if len(candidates) == 1:
        return candidates[0], 'subtitle-and-date'
    return None, None


def _find_conservative_fuzzy_match(medium, substack_posts):
    """Match future same-day cross-posts with minor title rewrites.

    This is intentionally stricter than the reviewed initial inventory: a
    related article about the same fund must not be collapsed merely because it
    shares topic words.
    """
    medium_title = normalize_title(medium.get('title'))
    if not medium_title:
        return None, None
    scored = []
    for post in substack_posts:
        if _date_distance(medium.get('post_date'), post.get('post_date')) > 3:
            continue
        candidate_title = normalize_title(post.get('title'))
        ratio = difflib.SequenceMatcher(None, medium_title, candidate_title).ratio()
        jaccard = _token_jaccard(medium_title, candidate_title)
        if ratio >= 0.84 and jaccard >= 0.62:
            scored.append((ratio + jaccard, ratio, jaccard, post))
    if not scored:
        return None, None
    scored.sort(key=lambda row: row[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.12:
        return None, None
    return scored[0][3], 'title-similarity-and-date'


def article_metadata(post):
    value = {
        'source': post.get('source') or 'substack',
        'source_id': post.get('source_id') or post.get('slug') or '',
        'slug': post.get('slug', ''),
        'title': post.get('title', ''),
        'subtitle': post.get('subtitle', ''),
        'post_date': post.get('post_date', ''),
        'url': post.get('url', ''),
        'audience': post.get('audience', ''),
        'wordcount': post.get('wordcount', 0),
        'content_status': post.get('content_status', 'full'),
        'brief': build_article_brief(post),
    }
    if post.get('alternate_urls'):
        value['alternate_urls'] = post['alternate_urls']
    return value


def merge_sources(substack_posts, medium_posts, overrides=None):
    """Return combined posts, article metadata, and an auditable match report."""
    overrides = load_overrides() if overrides is None else overrides
    substack = [_canonical_substack_post(post) for post in substack_posts]
    medium = [_canonical_medium_post(post) for post in medium_posts]

    substack_by_slug = {post.get('slug'): post for post in substack if post.get('slug')}
    substack_by_title: dict[str, list[dict]] = {}
    for post in substack:
        substack_by_title.setdefault(normalize_title(post.get('title')), []).append(post)

    combined = list(substack)
    matches = []
    unique_medium = []
    used_medium_ids = set()

    for post in medium:
        medium_id = post.get('medium_id') or post.get('source_id')
        if not medium_id or medium_id in used_medium_ids:
            raise ValueError('Medium input contains missing or duplicate post IDs')
        used_medium_ids.add(medium_id)

        target = None
        reason = None
        mirror_slug = post.get('mirror_substack_slug')
        if mirror_slug and mirror_slug in substack_by_slug:
            target = substack_by_slug[mirror_slug]
            reason = 'explicit-cross-post-link'

        if target is None:
            target, reason = _find_exact_or_prefix_match(post, substack_by_title)
        if target is None:
            target, reason = _find_override_match(post, overrides, substack_by_title)
        if target is None:
            target, reason = _find_subtitle_match(post, substack)
        if target is None:
            target, reason = _find_conservative_fuzzy_match(post, substack)

        if target is not None:
            target.setdefault('alternate_urls', {})['medium'] = post.get('url')
            matches.append({
                'medium_id': medium_id,
                'medium_title': post.get('title'),
                'medium_url': post.get('url'),
                'substack_slug': target.get('slug'),
                'substack_title': target.get('title'),
                'substack_url': target.get('url'),
                'reason': reason,
            })
        else:
            unique_medium.append(post)
            combined.append(post)

    combined.sort(key=lambda post: str(post.get('post_date') or ''), reverse=True)
    articles = [article_metadata(post) for post in combined]
    match_reasons: dict[str, int] = {}
    report = {
        'substack_articles': len(substack),
        'medium_articles': len(medium),
        'duplicate_medium_articles': len(matches),
        'unique_medium_articles': len(unique_medium),
        'published_articles': len(articles),
        'match_reasons': match_reasons,
        'matches': matches,
        'unique_medium_ids': [post.get('medium_id') for post in unique_medium],
    }
    for match in matches:
        reason = match.get('reason')
        if not isinstance(reason, str):
            raise ValueError('matched Medium article has no deduplication reason')
        match_reasons[reason] = match_reasons.get(reason, 0) + 1
    return combined, articles, report


def main():
    substack_posts = load_list(SUBSTACK_PATH, 'Substack post snapshot')
    medium_posts = load_list(MEDIUM_PATH, 'Medium post snapshot')
    combined, articles, report = merge_sources(substack_posts, medium_posts)

    urls = [post.get('url') for post in combined]
    if any(not url for url in urls) or len(urls) != len(set(urls)):
        raise ValueError('combined post snapshot contains missing or duplicate URLs')

    atomic_write_json(POSTS_OUTPUT, combined)
    atomic_write_json(ARTICLES_OUTPUT, articles)
    if REPORT_OUTPUT:
        atomic_write_json(REPORT_OUTPUT, report)

    print(f"Substack: {report['substack_articles']} articles")
    print(f"Medium: {report['medium_articles']} authored posts")
    print(f"Deduplicated Medium cross-posts: {report['duplicate_medium_articles']}")
    for reason, count in sorted(report['match_reasons'].items()):
        print(f'  {reason}: {count}')
    print(f"Unique Medium articles added: {report['unique_medium_articles']}")
    print(f"Combined published catalogue: {report['published_articles']} articles")
    print(f'Saved combined posts to {POSTS_OUTPUT}')
    print(f'Saved article index to {ARTICLES_OUTPUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
