#!/usr/bin/env python3
"""Fail closed when a refresh would publish stale, corrupt, or mismatched data."""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from article_briefs import validate_brief_against_body, validate_brief_structure
from extract_trades import (
    classify_direction,
    extract_outcome,
    extract_fund_name,
    extract_quant_details,
    extract_thesis,
    extract_underlying,
    find_instruments,
    has_negated_trade_signal,
)
from filter_trades import clean_underlying
from write_snapshot_manifest import data_checksum


VALID_DIRECTIONS = {
    'long', 'short', 'long/short', 'arbitrage/relative value', 'unspecified',
}
VALID_INSTRUMENTS = {
    'equity', 'volatility', 'option', 'bond', 'futures', 'commodity', 'FX',
    'repo', 'swap', 'CDS', 'prediction_market', 'weather_derivative',
    'unspecified',
}
VALID_SOURCES = {'substack', 'medium', 'patreon', 'fxempire'}
CONTENT_SOURCES = {'substack', 'medium'}
REGISTRY_SOURCES = {'patreon', 'fxempire'}
VALID_CONTENT_STATUSES = {'full', 'excerpt', 'registry'}
VALID_FAMILIES = {
    'firm-mechanics',
    'career-structure',
    'model-critique',
    'scandal-enforcement',
    'event-reaction',
    'market-structure',
    'other',
}
VALID_FETCH_STATUSES = {'ok', 'degraded'}
DATE_ONLY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TIMESTAMP_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'
    r'(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$'
)
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
MEDIUM_ID_RE = re.compile(r'(?:-|/)([0-9a-f]{12})$', re.IGNORECASE)
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=10)
EMPTY_BODY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
REGISTRY_BRIEF_KEYS = {
    'schema_version', 'body_sha256', 'lead', 'sections', 'fallback_evidence',
    'checkpoints',
}
REGISTRY_ARTICLE_KEYS = {
    'source', 'source_id', 'slug', 'title', 'subtitle', 'post_date', 'url',
    'audience', 'wordcount', 'content_status', 'brief', 'family',
}


def load_json(path, label):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except Exception as exc:
        raise ValueError(f'{label} is not valid JSON: {exc}') from exc


def load_list(path, label):
    value = load_json(path, label)
    if not isinstance(value, list):
        raise ValueError(f'{label} must contain a JSON list')
    return value


def load_object(path, label):
    value = load_json(path, label)
    if not isinstance(value, dict):
        raise ValueError(f'{label} must contain a JSON object')
    return value


def require(condition, message):
    if not condition:
        raise ValueError(message)


def require_string(value, message, allow_empty=False):
    require(isinstance(value, str), message)
    require(allow_empty or bool(value.strip()), message)
    return value


def validate_registry_brief(brief, label):
    """Require the exact brief emitted for a metadata-only, empty-body row."""
    require(isinstance(brief, dict),
            f'{label} registry brief is not an object')
    require(set(brief) == REGISTRY_BRIEF_KEYS,
            f'{label} registry brief does not match the exact empty-body contract')
    require(type(brief.get('schema_version')) is int
            and brief['schema_version'] == 1,
            f'{label} registry brief does not match the exact empty-body contract')
    require(brief.get('body_sha256') == EMPTY_BODY_SHA256,
            f'{label} registry brief does not match the exact empty-body contract')
    require(brief.get('lead') is None
            and brief.get('fallback_evidence') is None,
            f'{label} registry brief does not match the exact empty-body contract')
    require(brief.get('sections') == [] and brief.get('checkpoints') == [],
            f'{label} registry brief does not match the exact empty-body contract')


def parse_iso_date(value, label, date_only=False):
    """Validate an exact calendar date or timezone-qualified ISO timestamp."""
    require(isinstance(value, str), f'{label} is not a string')
    try:
        if DATE_ONLY_RE.fullmatch(value):
            parsed_date = Date.fromisoformat(value)
            parsed_datetime = datetime.combine(
                parsed_date, datetime.min.time(), tzinfo=timezone.utc
            )
        else:
            require(not date_only and TIMESTAMP_RE.fullmatch(value),
                    f'{label} is not a strict ISO date')
            parsed_datetime = datetime.fromisoformat(value.replace('Z', '+00:00'))
            require(parsed_datetime.tzinfo is not None,
                    f'{label} timestamp has no timezone')
            parsed_date = parsed_datetime.date()
    except (ValueError, OverflowError):
        raise ValueError(f'{label} is not a real ISO date') from None
    if date_only:
        require(DATE_ONLY_RE.fullmatch(value), f'{label} must be YYYY-MM-DD')
    return parsed_datetime, parsed_date.isoformat()


def canonical_url_identity(source, url):
    """Return the immutable source identity only for a canonical article URL."""
    require(isinstance(url, str) and url, 'article URL is empty')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').casefold()
        port = parsed.port
    except ValueError:
        raise ValueError('article URL cannot be parsed') from None
    require(parsed.scheme == 'https' and parsed.username is None
            and parsed.password is None and port is None,
            'article URL is not canonical HTTPS')
    require(not parsed.query and not parsed.fragment,
            'article URL contains a query or fragment')
    require(parsed.path == parsed.path.rstrip('/') and '//' not in parsed.path,
            'article URL has a non-canonical path')
    canonical = urlunsplit(('https', host, parsed.path, '', ''))
    require(url == canonical, 'article URL is not in canonical form')

    if source == 'substack':
        require(host == 'navnoorbawa.substack.com',
                'Substack URL has the wrong host')
        match = re.fullmatch(r'/p/([A-Za-z0-9][A-Za-z0-9_-]*)', parsed.path)
        if match is None:
            raise ValueError('Substack URL has no canonical post slug')
        return match.group(1)
    if source == 'medium':
        require(host == 'medium.com', 'Medium URL has the wrong host')
        prefix = '/@navnoorbawa/'
        require(parsed.path.startswith(prefix), 'Medium URL has the wrong author path')
        match = MEDIUM_ID_RE.search(parsed.path)
        if match is None:
            raise ValueError('Medium URL has no canonical post ID')
        return match.group(1).casefold()
    if source == 'patreon':
        require(host == 'www.patreon.com', 'Patreon URL has the wrong host')
        match = re.fullmatch(
            r'/NavnoorBawa/posts/[A-Za-z0-9][A-Za-z0-9_-]*-([0-9]+)',
            parsed.path,
        )
        if match is None:
            raise ValueError('Patreon URL has no canonical creator post ID')
        return match.group(1)
    if source == 'fxempire':
        require(host == 'www.fxempire.com', 'FX Empire URL has the wrong host')
        match = re.fullmatch(
            r'/(?:forecasts|news|education)/article/'
            r'[A-Za-z0-9][A-Za-z0-9_-]*-([0-9]+)',
            parsed.path,
        )
        if match is None:
            raise ValueError('FX Empire URL has no canonical article ID')
        return match.group(1)
    raise ValueError('article has an invalid source')


def validate_source_url(source, url):
    try:
        canonical_url_identity(source, url)
        return True
    except ValueError:
        return False


def validate_article_record(record, index, label):
    require(isinstance(record, dict), f'{label} {index} is not an object')
    source = record.get('source')
    require(source in VALID_SOURCES, f'{label} {index} has an invalid source')
    url = require_string(record.get('url'), f'{label} {index} has no URL')
    source_id = require_string(record.get('source_id'),
                               f'{label} {index} has no explicit source ID')
    identity = canonical_url_identity(source, url)
    require(source_id.casefold() == identity.casefold(),
            f'{label} {index} source ID does not match its canonical URL')
    title = require_string(record.get('title'), f'{label} {index} has no title')
    require('content_status' in record,
            f'{label} {index} has no explicit content status')
    content_status = record.get('content_status')
    require(type(content_status) is str and content_status in VALID_CONTENT_STATUSES,
            f'{label} {index} has an invalid content status')
    if source in CONTENT_SOURCES:
        require(content_status in {'full', 'excerpt'},
                f'{label} {index} content source cannot be registry-only')
    else:
        require(content_status == 'registry',
                f'{label} {index} registry source must be metadata-only')
        require(record.get('audience') in {'public', 'paid'},
                f'{label} {index} registry entry has no public/paid access flag')
    if label == 'article':
        family = record.get('family')
        require(family in VALID_FAMILIES, f'{label} {index} has an invalid family')
        if content_status == 'registry':
            allowed_keys = REGISTRY_ARTICLE_KEYS | {'alternate_urls'}
            if source == 'patreon':
                allowed_keys.add('access')
            require(set(record) >= REGISTRY_ARTICLE_KEYS
                    and set(record) <= allowed_keys,
                    f'{label} {index} has fields outside the metadata-only '
                    'registry contract')
            if source == 'patreon':
                require(record.get('access') in {'public', 'paid'}
                        and record.get('access') == record.get('audience'),
                        f'{label} {index} Patreon access is missing or inconsistent')
            else:
                require(record.get('audience') == 'public',
                        f'{label} {index} FX Empire metadata must be public')
            require('brief' in record,
                    f'{label} {index} has no metadata-only brief boundary')
            validate_registry_brief(record['brief'], f'{label} {index}')
    timestamp = record.get('post_date')
    _, calendar_date = parse_iso_date(timestamp, f'{label} {index} publication date')
    if 'wordcount' in record:
        wordcount = record['wordcount']
        require(type(wordcount) is int and wordcount >= 0,
                f'{label} {index} has an invalid word count')
        if content_status == 'registry':
            require(wordcount == 0,
                    f'{label} {index} registry entry has a non-zero word count')
    alternate_urls = record.get('alternate_urls', {})
    require(isinstance(alternate_urls, dict),
            f'{label} {index} alternate URLs are not an object')
    require(set(alternate_urls).issubset(VALID_SOURCES - {source}),
            f'{label} {index} alternate URLs contain an invalid source')
    for alternate_source, alternate_url in alternate_urls.items():
        canonical_url_identity(alternate_source, alternate_url)
    return {
        'source': source,
        'source_id': source_id,
        'identity': (source, identity.casefold()),
        'url': url,
        'title': title,
        'post_date': timestamp,
        'calendar_date': calendar_date,
        'content_status': content_status,
    }


def validate_posts(posts):
    require(posts, 'post snapshot is empty')
    post_by_url = {}
    identities = []
    for index, post in enumerate(posts):
        metadata = validate_article_record(post, index, 'post')
        body_text = post.get('body_text')
        require(isinstance(body_text, str), f'post {index} has no source body text')
        metadata['body_text'] = body_text
        require(post.get('is_published') is True,
                f'post {index} is not explicitly published')
        require(metadata['url'] not in post_by_url,
                'post snapshot contains duplicate canonical URLs')
        post_by_url[metadata['url']] = metadata
        identities.append(metadata['identity'])
    require(len(identities) == len(set(identities)),
            'post snapshot contains duplicate canonical source identities')
    return post_by_url


def validate_article_index(articles, post_by_url):
    require(articles, 'article index is empty')
    article_by_url = {}
    identities = []
    for index, article in enumerate(articles):
        metadata = validate_article_record(article, index, 'article')
        url = metadata['url']
        if metadata['content_status'] != 'registry':
            require(url in post_by_url,
                    f'article {index} is not present in the fetched post snapshot')
            post = post_by_url[url]
            require(metadata['identity'] == post['identity'],
                    f'article {index} source metadata does not match its post')
            require(metadata['title'] == post['title'],
                    f'article {index} title does not match its fetched post')
            require(metadata['post_date'] == post['post_date'],
                    f'article {index} date does not match its fetched post')
            require(metadata['content_status'] == post['content_status'],
                    f'article {index} content status does not match its fetched post')
            require('brief' in article, f'article {index} has no source-backed brief')
            try:
                validate_brief_against_body(article['brief'], post['body_text'])
            except ValueError as exc:
                raise ValueError(
                    f'article {index} brief validation failed: {exc}'
                ) from None
        else:
            require('brief' in article,
                    f'article {index} has no metadata-only brief boundary')
            try:
                validate_brief_structure(article['brief'])
            except ValueError as exc:
                raise ValueError(
                    f'article {index} brief validation failed: {exc}'
                ) from None
        require(url not in article_by_url,
                'article index contains duplicate canonical URLs')
        article_by_url[url] = metadata
        identities.append(metadata['identity'])
    require(len(identities) == len(set(identities)),
            'article index contains duplicate canonical source identities')
    content_urls = {
        url for url, metadata in article_by_url.items()
        if metadata['content_status'] != 'registry'
    }
    require(content_urls == set(post_by_url),
            'article index does not exactly match the fetched posts')
    return article_by_url


def validate_deployable_articles(articles):
    """Validate the tracked article catalogue when the local post snapshot is absent."""
    require(articles, 'article index is empty')
    article_by_url = {}
    identities = []
    has_briefs = any(isinstance(article, dict) and 'brief' in article
                     for article in articles)
    for index, article in enumerate(articles):
        metadata = validate_article_record(article, index, 'article')
        if has_briefs:
            require('brief' in article, f'article {index} has no source-backed brief')
            try:
                validate_brief_structure(article['brief'])
            except ValueError as exc:
                raise ValueError(
                    f'article {index} brief validation failed: {exc}'
                ) from None
        require(metadata['url'] not in article_by_url,
                'article index contains duplicate canonical URLs')
        article_by_url[metadata['url']] = metadata
        identities.append(metadata['identity'])
    require(len(identities) == len(set(identities)),
            'article index contains duplicate canonical source identities')
    return article_by_url


def validate_trades(trades, article_by_url):
    require(trades, 'trade output is empty')
    represented_articles = set()
    seen = set()
    required = ('article_title', 'article_url', 'article_date', 'trade_description',
                'description_truncated', 'instruments', 'direction')
    for index, trade in enumerate(trades):
        require(isinstance(trade, dict), f'trade {index} is not an object')
        missing = [key for key in required if key not in trade]
        require(not missing, f'trade {index} is missing fields: {", ".join(missing)}')
        url = require_string(trade.get('article_url'), f'trade {index} has no article URL')
        title = require_string(trade.get('article_title'),
                               f'trade {index} has no article title')
        date = trade.get('article_date')
        _, calendar_date = parse_iso_date(date, f'trade {index} article date', date_only=True)
        desc = require_string(trade.get('trade_description'),
                              f'trade {index} has no description')
        description_truncated = trade.get('description_truncated')
        instruments = trade.get('instruments')
        direction = trade.get('direction')
        require(url in article_by_url, f'trade {index} points to an unknown article')
        article = article_by_url[url]
        require(title == article['title'],
                f'trade {index} title does not match its article')
        require(calendar_date == article['calendar_date'],
                f'trade {index} date does not match its article')
        require(len(desc.strip()) >= 20,
                f'trade {index} has an empty/short description')
        require(type(description_truncated) is bool,
                f'trade {index} description_truncated is not a boolean')
        require(isinstance(instruments, list) and instruments,
                f'trade {index} has no instrument list')
        require(all(isinstance(instrument, str) for instrument in instruments),
                f'trade {index} instrument list has a non-string value')
        require(len(instruments) == len(set(instruments)),
                f'trade {index} has duplicate instruments')
        unknown_instruments = set(instruments) - VALID_INSTRUMENTS
        require(not unknown_instruments,
                f'trade {index} has invalid instruments: {sorted(unknown_instruments)}')
        require(type(direction) is str and direction in VALID_DIRECTIONS,
                f'trade {index} has invalid direction: {direction!r}')

        # Regex directions are deterministic evidence extracted from this exact
        # published passage.  The local LLM is allowed to resolve only a regex
        # abstention, so it may supply a direction when this result is
        # ``unspecified`` but must never override a concrete regex label.
        passage_direction = classify_direction(desc)
        require(
            direction == 'unspecified'
            or not has_negated_trade_signal(desc)
            or passage_direction != 'unspecified',
            f'trade {index} assigns direction {direction!r} to a passage with '
            'an explicitly negated trade signal',
        )
        if passage_direction != 'unspecified':
            require(direction == passage_direction,
                    f'trade {index} direction is not derived from its exact '
                    'trade_description')
        require(instruments == find_instruments(desc),
                f'trade {index} instruments are not derived from its exact '
                'trade_description')

        # These are the fields displayed as evidence in the terminal.  Compare
        # them with a fresh extraction from the bounded, user-visible passage so
        # adjacent paragraphs or hidden source text cannot leak into a record.
        passage_fields = {
            'underlying': clean_underlying(extract_underlying(desc)),
            'edge_or_thesis': extract_thesis(desc),
            'any_quant_detail': extract_quant_details(desc),
            'outcome_if_mentioned': extract_outcome(desc),
            'fund_name_if_mentioned': (
                extract_fund_name(desc) or extract_fund_name(title)
            ),
        }
        for optional in ('underlying', 'edge_or_thesis', 'any_quant_detail',
                         'outcome_if_mentioned', 'fund_name_if_mentioned'):
            if optional in trade:
                require(trade[optional] is None or isinstance(trade[optional], str),
                        f'trade {index} field {optional} has an invalid type')
        for field, expected in passage_fields.items():
            if field in trade:
                require(trade[field] == expected,
                        f'trade {index} field {field} is not derived from its exact '
                        'trade_description')
        duplicate_key = (url, desc[:150])
        require(duplicate_key not in seen, f'trade {index} duplicates an earlier trade')
        seen.add(duplicate_key)
        represented_articles.add(url)
    return represented_articles


def validate_trade_regression(trades, represented_articles, previous_path, minimum_ratio):
    if not previous_path or not previous_path.exists():
        return
    previous = load_list(previous_path, 'previous trade output')
    if not previous:
        return
    previous_articles = {trade.get('article_url') for trade in previous
                         if isinstance(trade, dict) and trade.get('article_url')}
    minimum_trades = max(1, int(len(previous) * minimum_ratio))
    minimum_articles = max(1, int(len(previous_articles) * minimum_ratio))
    require(len(trades) >= minimum_trades,
            f'trade count collapsed from {len(previous)} to {len(trades)} '
            f'(minimum allowed: {minimum_trades})')
    require(len(represented_articles) >= minimum_articles,
            f'trade-bearing article count collapsed from {len(previous_articles)} to '
            f'{len(represented_articles)} (minimum allowed: {minimum_articles})')


# Backwards-compatible name used by older callers.
validate_regression = validate_trade_regression


def validate_article_regression(articles, previous_path, minimum_ratio):
    """Guard each source independently so one healthy source cannot hide an outage."""
    if not previous_path or not previous_path.exists():
        return
    previous = load_list(previous_path, 'previous article index')
    current_counts = Counter(
        article.get('source') for article in articles if isinstance(article, dict)
    )
    previous_counts = Counter(
        article.get('source') for article in previous if isinstance(article, dict)
    )
    for source, previous_count in previous_counts.items():
        require(source in VALID_SOURCES,
                f'previous article index has an invalid source: {source!r}')
        minimum = max(1, int(previous_count * minimum_ratio))
        require(current_counts[source] >= minimum,
                f'{source} article count collapsed from {previous_count} to '
                f'{current_counts[source]} (minimum allowed: {minimum})')


def validate_manifest(manifest, articles, trades, article_path, trade_path, now=None):
    require(manifest.get('schema_version') == 2,
            'snapshot manifest has an unsupported schema version')
    checked_at_value = manifest.get('checked_at')
    checked_at, _ = parse_iso_date(checked_at_value, 'manifest checked_at')
    require(TIMESTAMP_RE.fullmatch(checked_at_value),
            'manifest checked_at must be a timezone-qualified timestamp')
    validation_now = now or datetime.now(timezone.utc)
    require(validation_now.tzinfo is not None,
            'manifest validation clock must be timezone-aware')
    future_cutoff = validation_now.astimezone(timezone.utc) + MAX_FUTURE_CLOCK_SKEW
    require(checked_at <= future_cutoff,
            'manifest checked_at is implausibly far in the future')
    latest_publication = manifest.get('latest_publication')
    parse_iso_date(latest_publication, 'manifest latest_publication')
    content_articles = [
        article for article in articles if article.get('content_status') != 'registry'
    ]
    registry_articles = [
        article for article in articles if article.get('content_status') == 'registry'
    ]
    require(content_articles, 'article index has no content-backed research')
    expected_latest = max(
        (article['post_date'] for article in content_articles),
        key=lambda value: parse_iso_date(value, 'article publication date')[0],
    )
    require(latest_publication == expected_latest,
            'manifest latest_publication does not match the article index')
    require(type(manifest.get('article_count')) is int
            and manifest['article_count'] == len(content_articles),
            'manifest article count does not match the article index')
    require(type(manifest.get('catalog_count')) is int
            and manifest['catalog_count'] == len(articles),
            'manifest catalog count does not match the article index')
    require(type(manifest.get('registry_count')) is int
            and manifest['registry_count'] == len(registry_articles),
            'manifest registry count does not match the article index')
    catalog_latest = manifest.get('catalog_latest_publication')
    parse_iso_date(catalog_latest, 'manifest catalog_latest_publication')
    expected_catalog_latest = max(
        (article['post_date'] for article in articles),
        key=lambda value: parse_iso_date(value, 'catalog publication date')[0],
    )
    require(catalog_latest == expected_catalog_latest,
            'manifest catalog_latest_publication does not match the article index')
    require(type(manifest.get('observation_count')) is int
            and manifest['observation_count'] == len(trades),
            'manifest observation count does not match the trade output')
    checksum = manifest.get('data_checksum')
    require(isinstance(checksum, str) and SHA256_RE.fullmatch(checksum),
            'manifest data checksum is not a SHA-256 digest')
    expected_checksum = data_checksum(article_path.read_bytes(), trade_path.read_bytes())
    require(checksum == expected_checksum,
            'manifest data checksum does not match the deployed snapshot')

    sources = manifest.get('sources')
    included = Counter(article['source'] for article in articles)
    require(isinstance(sources, dict) and set(sources) == set(included),
            'manifest source set does not match the article index')
    for source in sorted(included):
        item = sources[source]
        require(isinstance(item, dict), f'manifest {source} status is not an object')
        source_checked_value = item.get('checked_at')
        source_checked, _ = parse_iso_date(
            source_checked_value, f'manifest {source} checked_at'
        )
        require(TIMESTAMP_RE.fullmatch(source_checked_value),
                f'manifest {source} checked_at must be a timestamp')
        require(source_checked <= checked_at,
                f'manifest {source} checked_at is later than the manifest')
        require(source_checked <= future_cutoff,
                f'manifest {source} checked_at is implausibly far in the future')
        require(item.get('status') in VALID_FETCH_STATUSES,
                f'manifest {source} has an invalid fetch status')
        require(isinstance(item.get('mode'), str) and item['mode'].strip(),
                f'manifest {source} has no fetch mode')
        for field in ('published_count', 'fetched_count', 'included_count'):
            require(type(item.get(field)) is int and item[field] >= 0,
                    f'manifest {source} {field} is not a non-negative integer')
        require(item['included_count'] == included[source],
                f'manifest {source} included count does not match the article index')
        require(item['published_count'] >= item['included_count'],
                f'manifest {source} published count is below its included count')
        newest_instant, _ = parse_iso_date(
            item.get('newest'), f'manifest {source} newest publication'
        )
        included_dates = [article['post_date'] for article in articles
                          if article['source'] == source]
        if included_dates:
            included_newest_instant = max(
                parse_iso_date(value, f'{source} included newest publication')[0]
                for value in included_dates
            )
            require(newest_instant >= included_newest_instant,
                    f'manifest {source} newest publication predates included data')
    return checked_at


def validate_previous_manifest(manifest, previous):
    require(previous.get('schema_version') in {1, 2},
            'previous manifest has an unsupported schema version')
    current_checked, _ = parse_iso_date(manifest.get('checked_at'),
                                        'manifest checked_at')
    previous_checked, _ = parse_iso_date(previous.get('checked_at'),
                                         'previous manifest checked_at')
    require(TIMESTAMP_RE.fullmatch(previous.get('checked_at') or ''),
            'previous manifest checked_at must be a timestamp')
    require(current_checked >= previous_checked,
            'manifest checked_at moved backwards')
    previous_checksum = previous.get('data_checksum')
    require(isinstance(previous_checksum, str) and SHA256_RE.fullmatch(previous_checksum),
            'previous manifest data checksum is invalid')
    parse_iso_date(previous.get('latest_publication'),
                   'previous manifest latest_publication')
    for field in ('article_count', 'observation_count'):
        require(type(previous.get(field)) is int and previous[field] >= 0,
                f'previous manifest {field} is invalid')
    if manifest.get('data_checksum') == previous_checksum:
        consistency_fields = ['article_count', 'observation_count', 'latest_publication']
        if previous.get('schema_version') == 2:
            consistency_fields.extend((
                'catalog_count', 'registry_count', 'catalog_latest_publication',
            ))
        for field in consistency_fields:
            require(manifest.get(field) == previous.get(field),
                    f'unchanged checksum has inconsistent {field}')
    previous_sources = previous.get('sources')
    require(isinstance(previous_sources, dict)
            and set(previous_sources).issubset(VALID_SOURCES)
            and CONTENT_SOURCES.issubset(previous_sources),
            'previous manifest sources are invalid')
    current_sources = manifest.get('sources') or {}
    for source in previous_sources:
        previous_source = previous_sources.get(source)
        current_source = current_sources.get(source)
        if not isinstance(previous_source, dict) or not isinstance(current_source, dict):
            raise ValueError(f'previous manifest has no {source} status')
        previous_source_checked, _ = parse_iso_date(
            previous_source.get('checked_at'), f'previous manifest {source} checked_at'
        )
        current_source_checked, _ = parse_iso_date(
            current_source.get('checked_at'), f'manifest {source} checked_at'
        )
        require(TIMESTAMP_RE.fullmatch(previous_source.get('checked_at') or ''),
                f'previous manifest {source} checked_at must be a timestamp')
        require(previous_source_checked <= previous_checked,
                f'previous manifest {source} checked_at is later than its manifest')
        require(current_source_checked >= previous_source_checked,
                f'{source} fetch checked_at moved backwards')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--posts', type=Path,
        help='optional local post snapshot for strict source-to-article validation',
    )
    parser.add_argument('--articles', type=Path, required=True)
    parser.add_argument('--trades', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--previous-articles', type=Path)
    parser.add_argument('--previous-trades', type=Path)
    parser.add_argument('--previous-manifest', type=Path)
    parser.add_argument('--minimum-ratio', type=float, default=0.5)
    parser.add_argument('--minimum-article-ratio', type=float, default=0.9)
    args = parser.parse_args()

    try:
        require(0 < args.minimum_ratio <= 1, 'minimum ratio must be in (0, 1]')
        require(0 < args.minimum_article_ratio <= 1,
                'minimum article ratio must be in (0, 1]')
        articles = load_list(args.articles, 'article index')
        trades = load_list(args.trades, 'trade output')
        if args.posts:
            posts = load_list(args.posts, 'post snapshot')
            post_by_url = validate_posts(posts)
            article_by_url = validate_article_index(articles, post_by_url)
        else:
            article_by_url = validate_deployable_articles(articles)
        validate_article_regression(
            articles, args.previous_articles, args.minimum_article_ratio
        )
        represented_articles = validate_trades(trades, article_by_url)
        validate_trade_regression(trades, represented_articles, args.previous_trades,
                                  args.minimum_ratio)

        manifest_path = args.manifest
        default_manifest = args.articles.parent / 'snapshot_manifest.json'
        if manifest_path is None and default_manifest.exists():
            manifest_path = default_manifest
        manifest = None
        if manifest_path:
            manifest = load_object(manifest_path, 'snapshot manifest')
            validate_manifest(
                manifest, articles, trades, args.articles, args.trades
            )
        if args.previous_manifest:
            require(manifest is not None,
                    '--previous-manifest requires a current snapshot manifest')
            previous_manifest = load_object(args.previous_manifest, 'previous manifest')
            validate_previous_manifest(manifest, previous_manifest)
    except (OSError, ValueError) as exc:
        print(f'VALIDATION FAILED: {exc}', file=sys.stderr)
        return 1

    newest = max(article['post_date'][:10] for article in articles)
    manifest_note = f', checked {manifest["checked_at"]}' if manifest else ''
    print(f'Validation passed: {len(articles)} articles, {len(trades)} observations, '
          f'{len(represented_articles)} observation-bearing articles, newest {newest}'
          f'{manifest_note}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
