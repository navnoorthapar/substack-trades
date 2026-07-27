#!/usr/bin/env python3
"""Build deterministic, source-grounded research threads for the terminal.

Threads reuse the high-precision entities already emitted by ``research_graph``.
They organize publication history; they do not infer an author's current view,
position, conviction, consistency, accuracy, or performance.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from research_graph import ENTITY_ALIASES, normalized_search_words


THREAD_SCHEMA_VERSION = 1

_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TERM_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

_MARKET_INSTRUMENT_KEYS = {
    'bitcoin', 'bonds', 'copper', 'dxy', 'ethereum', 'euro', 'futures',
    'gold', 'natural-gas', 'non-deliverable-forwards', 'oil', 'options',
    'precious-metals', 'silver', 'spx', 'swaps', 'treasuries', 'vix',
    'vvix', 'yen',
}
_MODEL_MECHANISM_KEYS = {
    'avellaneda-stoikov', 'basis-trade', 'black-litterman', 'black-scholes',
    'carry-trade', 'delta-hedging', 'dispersion-trading',
    'expected-shortfall', 'factor-model', 'gamma', 'gamma-scalping', 'heston',
    'hull-white', 'kelly-criterion', 'market-making',
    'market-microstructure', 'market-structure', 'mean-reversion',
    'monte-carlo', 'risk-parity', 'rough-volatility', 'sabr', 'sharpe-ratio',
    'sortino-ratio', 'statistical-arbitrage', 'stochastic-volatility',
    'value-at-risk', 'variance-swaps', 'volatility-arbitrage',
    'volatility-risk-premium',
}
_LABEL_OVERRIDES = {
    'aqr': 'AQR',
    'avellaneda-stoikov': 'Avellaneda–Stoikov',
    'basis-trade': 'Basis Trade',
    'black-litterman': 'Black–Litterman',
    'black-scholes': 'Black–Scholes',
    'blackrock': 'BlackRock',
    'bluecrest': 'BlueCrest',
    'bonds': 'Bonds',
    'carry-trade': 'Carry Trade',
    'cfm': 'CFM',
    'cftc': 'CFTC',
    'd-e-shaw': 'D. E. Shaw',
    'delta-hedging': 'Delta Hedging',
    'dispersion-trading': 'Dispersion Trading',
    'dxy': 'DXY',
    'esma': 'ESMA',
    'factor-model': 'Factor Model',
    'futures': 'Futures',
    'gamma-scalping': 'Gamma Scalping',
    'hrt': 'HRT',
    'hsbc': 'HSBC',
    'hull-white': 'Hull–White',
    'ice': 'ICE',
    'jpmorgan': 'JPMorgan',
    'kelly-criterion': 'Kelly Criterion',
    'ljm': 'LJM',
    'ltcm': 'LTCM',
    'man-ahl': 'Man AHL',
    'market-making': 'Market Making',
    'market-microstructure': 'Market Microstructure',
    'market-structure': 'Market Structure',
    'mean-reversion': 'Mean Reversion',
    'monte-carlo': 'Monte Carlo',
    'options': 'Options',
    'precious-metals': 'Precious Metals',
    'qvr': 'QVR',
    'risk-parity': 'Risk Parity',
    'rough-volatility': 'Rough Volatility',
    'sabr': 'SABR',
    'sec': 'SEC',
    'sharpe-ratio': 'Sharpe Ratio',
    'sortino-ratio': 'Sortino Ratio',
    'spx': 'S&P 500',
    'statistical-arbitrage': 'Statistical Arbitrage',
    'stochastic-volatility': 'Stochastic Volatility',
    'swaps': 'Swaps',
    'treasuries': 'Treasuries',
    'value-at-risk': 'Value at Risk',
    'variance-swaps': 'Variance Swaps',
    'vix': 'VIX',
    'volatility-arbitrage': 'Volatility Arbitrage',
    'volatility-risk-premium': 'Volatility Risk Premium',
    'vvix': 'VVIX',
    'xtx-markets': 'XTX Markets',
}
_LOWERCASE_LABEL_WORDS = {'and', 'for', 'of', 'the'}
_SECTION_MATCH_CODES = {
    'mechanism': 'm',
    'evidence': 'e',
    'countercase': 'c',
    'falsifier': 'f',
    'implementation': 'i',
}
_MATCH_CODE_ORDER = 'tsoemcfix'


def _normalized_words(value: Any) -> Tuple[str, ...]:
    return normalized_search_words(value)


def _contains_tokens(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    target = tuple(needle)
    width = len(target)
    return any(
        tuple(haystack[start:start + width]) == target
        for start in range(len(haystack) - width + 1)
    )


def _entity_in_text(entity: str, value: Any) -> bool:
    value_words = _normalized_words(value)
    aliases = ENTITY_ALIASES.get(entity, (entity.replace('-', ' '),))
    return any(
        _contains_tokens(value_words, _normalized_words(alias))
        for alias in aliases
    )


def _entity_in_title(entity: str, title: str) -> bool:
    return _entity_in_text(entity, title)


def _entity_match_codes(entity: str, article: Mapping[str, Any]) -> str:
    codes: Set[str] = set()
    if _entity_in_text(entity, article.get('title')):
        codes.add('t')
    if _entity_in_text(entity, article.get('subtitle')):
        codes.add('s')
    brief = article.get('brief')
    if isinstance(brief, Mapping):
        lead = brief.get('lead')
        if isinstance(lead, Mapping) and _entity_in_text(entity, lead.get('text')):
            codes.add('o')
        sections = brief.get('sections')
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, Mapping):
                    continue
                if _entity_in_text(
                        entity,
                        ' '.join((
                            str(section.get('heading') or ''),
                            str(section.get('text') or ''),
                        ))):
                    codes.add(_SECTION_MATCH_CODES.get(
                        str(section.get('kind') or ''), 'x',
                    ))
    return ''.join(code for code in _MATCH_CODE_ORDER if code in codes)


def entity_label(entity: str) -> str:
    """Return a stable human label for a normalized search entity."""
    if entity in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[entity]
    source = ENTITY_ALIASES.get(entity, (entity.replace('-', ' '),))[0]
    words = str(source).replace('-', ' ').split()
    return ' '.join(
        word.casefold() if index and word.casefold() in _LOWERCASE_LABEL_WORDS
        else word.capitalize()
        for index, word in enumerate(words)
    )


def entity_kind(entity: str) -> str:
    """Return the deliberately broad display kind for a thread entity."""
    if entity in _MARKET_INSTRUMENT_KEYS:
        return 'market / instrument'
    if entity in _MODEL_MECHANISM_KEYS:
        return 'model / mechanism'
    return 'organization / institution'


def _publication_sort_value(value: str) -> Tuple[datetime, str]:
    raw = str(value or '').strip()
    try:
        if _DAY_RE.fullmatch(raw):
            parsed = datetime.strptime(raw, '%Y-%m-%d').replace(
                tzinfo=timezone.utc,
            )
            return parsed, 'day'
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('publication timestamp has no timezone')
        return parsed.astimezone(timezone.utc), 'instant'
    except ValueError as error:
        raise ValueError(
            f'invalid article published_at {raw!r}: {error}',
        ) from error


def _required_string(value: Any, label: str) -> str:
    result = str(value or '').strip()
    if not result:
        raise ValueError(f'{label} must be a non-empty string')
    return result


def _canonical_url(value: Any, label: str) -> str:
    return _required_string(value, label).rstrip('/')


def _validated_search_by_url(
        search_index: Mapping[str, Any],
) -> Dict[str, Mapping[str, Any]]:
    search_rows = search_index.get('articles')
    if not isinstance(search_rows, list):
        raise ValueError('search index articles must be a list')
    search_by_url: Dict[str, Mapping[str, Any]] = {}
    for row in search_rows:
        if not isinstance(row, Mapping):
            raise ValueError('search index article rows must be objects')
        url = _canonical_url(row.get('url'), 'search index url')
        entities = row.get('entities')
        if not isinstance(entities, list) or entities != sorted(set(entities)):
            raise ValueError('search index row entities must be sorted and unique')
        if not all(isinstance(entity, str) and _TERM_RE.fullmatch(entity)
                   for entity in entities):
            raise ValueError('search index row contains a malformed entity')
        if url in search_by_url:
            raise ValueError(f'duplicate search index URL: {url}')
        search_by_url[url] = row
    return search_by_url


def build_thread_index(
        articles: Sequence[Mapping[str, Any]],
        search_index: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return a compact repeated-topic chronology for body-backed articles."""
    if not isinstance(articles, Sequence) or isinstance(articles, (str, bytes)):
        raise ValueError('thread articles must be a sequence')
    search_by_url = _validated_search_by_url(search_index)

    article_by_id: Dict[str, Mapping[str, Any]] = {}
    publication_order: Dict[str, Tuple[datetime, str]] = {}
    raw_entities_by_article: Dict[str, List[str]] = {}
    entity_members: Dict[str, Set[str]] = defaultdict(set)

    for article in articles:
        if not isinstance(article, Mapping):
            raise ValueError('thread article rows must be objects')
        article_id = _required_string(article.get('id'), 'article id')
        url = _canonical_url(article.get('url'), 'article url')
        _required_string(article.get('title'), 'article title')
        published_at = _required_string(
            article.get('published_at'), 'article published_at',
        )
        if article_id in article_by_id:
            raise ValueError(f'duplicate thread article id: {article_id}')
        search_row = search_by_url.get(url)
        if search_row is None:
            raise ValueError(f'thread article is absent from search index: {url}')
        entities = list(search_row['entities'])
        article_by_id[article_id] = article
        publication_value = _publication_sort_value(published_at)
        if article.get('publication_precision') != publication_value[1]:
            raise ValueError(
                f'thread article {article_id} publication precision is inconsistent',
            )
        publication_order[article_id] = publication_value
        raw_entities_by_article[article_id] = entities
        for entity in entities:
            entity_members[entity].add(article_id)

    recurring = {
        entity: members
        for entity, members in entity_members.items()
        if len(members) >= 2
    }

    def chronological_key(article_id: str) -> Tuple[datetime, str]:
        return publication_order[article_id][0], article_id

    topics: Dict[str, Dict[str, Any]] = {}
    for entity in sorted(recurring):
        ordered_ids = sorted(recurring[entity], key=chronological_key)
        match_codes = [
            _entity_match_codes(entity, article_by_id[article_id])
            for article_id in ordered_ids
        ]
        if any(not codes for codes in match_codes):
            raise ValueError(
                f'thread topic {entity} has no auditable match location',
            )
        topics[entity] = {
            'label': entity_label(entity),
            'kind': entity_kind(entity),
            'article_count': len(ordered_ids),
            'article_ids': ordered_ids,
            'match_codes': match_codes,
        }

    defaults: Dict[str, str] = {}
    for article in articles:
        article_id = str(article['id'])
        entity_keys = [
            entity for entity in raw_entities_by_article[article_id]
            if entity in topics
        ]
        entity_keys.sort(key=lambda entity: (
            topics[entity]['article_count'],
            topics[entity]['label'].casefold(),
            entity,
        ))
        if not entity_keys:
            continue

        title_entities = [
            entity for entity in entity_keys
            if _entity_in_title(entity, str(article['title']))
        ]
        defaults[article_id] = (title_entities or entity_keys)[0]

    result: Dict[str, Any] = {
        'schema_version': THREAD_SCHEMA_VERSION,
        'topic_count': len(topics),
        'article_count': len(defaults),
        'topics': topics,
        'defaults': defaults,
    }
    validate_thread_index(result, articles, search_index)
    return result


def validate_thread_index(
        index: Mapping[str, Any],
        articles: Sequence[Mapping[str, Any]],
        search_index: Mapping[str, Any],
) -> None:
    """Validate internal chronology, membership, and overlap invariants."""
    if index.get('schema_version') != THREAD_SCHEMA_VERSION:
        raise ValueError('thread index has an unsupported schema version')
    topics = index.get('topics')
    defaults = index.get('defaults')
    if not isinstance(topics, Mapping) or not isinstance(defaults, Mapping):
        raise ValueError('thread index topics and defaults must be objects')
    if index.get('topic_count') != len(topics):
        raise ValueError('thread topic count is inconsistent')
    if index.get('article_count') != len(defaults):
        raise ValueError('thread article count is inconsistent')

    source_articles = {
        _required_string(article.get('id'), 'article id'): article
        for article in articles
    }
    if len(source_articles) != len(articles):
        raise ValueError('thread source article ids must be unique')
    search_by_url = _validated_search_by_url(search_index)

    topic_members: Dict[str, Set[str]] = {}
    for entity, topic in topics.items():
        if not isinstance(entity, str) or not _TERM_RE.fullmatch(entity):
            raise ValueError('thread topic key is malformed')
        if not isinstance(topic, Mapping) or set(topic) != {
            'label', 'kind', 'article_count', 'article_ids', 'match_codes',
        }:
            raise ValueError(f'thread topic {entity} has an invalid shape')
        if topic.get('label') != entity_label(entity):
            raise ValueError(f'thread topic {entity} label is inconsistent')
        if topic.get('kind') != entity_kind(entity):
            raise ValueError(f'thread topic {entity} kind is inconsistent')
        article_ids = topic.get('article_ids')
        if not isinstance(article_ids, list) or len(article_ids) < 2:
            raise ValueError(f'thread topic {entity} must contain two articles')
        if len(article_ids) != len(set(article_ids)):
            raise ValueError(f'thread topic {entity} repeats an article')
        if not all(article_id in source_articles for article_id in article_ids):
            raise ValueError(f'thread topic {entity} references an unknown article')
        for article_id in article_ids:
            source_url = _canonical_url(
                source_articles[article_id].get('url'), 'article url',
            )
            search_row = search_by_url.get(source_url)
            if search_row is None or entity not in search_row['entities']:
                raise ValueError(
                    f'thread topic {entity} is not owned by the search index',
                )
        ordered = sorted(
            article_ids,
            key=lambda article_id: (
                _publication_sort_value(str(
                    source_articles[article_id].get('published_at'),
                ))[0],
                article_id,
            ),
        )
        if article_ids != ordered:
            raise ValueError(f'thread topic {entity} is not chronological')
        if topic.get('article_count') != len(article_ids):
            raise ValueError(f'thread topic {entity} count is inconsistent')
        match_codes = topic.get('match_codes')
        expected_match_codes = [
            _entity_match_codes(entity, source_articles[article_id])
            for article_id in article_ids
        ]
        if (
            not isinstance(match_codes, list)
            or match_codes != expected_match_codes
            or any(not codes for codes in match_codes)
        ):
            raise ValueError(f'thread topic {entity} match provenance is invalid')
        topic_members[entity] = set(article_ids)

    expected_article_ids = set().union(*topic_members.values()) if topic_members else set()
    if set(defaults) != expected_article_ids:
        raise ValueError('thread defaults do not cover recurring-topic articles')
    for article_id, default_topic in defaults.items():
        if article_id not in source_articles:
            raise ValueError('thread index references an unknown source article')
        if (
            not isinstance(default_topic, str)
            or default_topic not in topic_members
            or article_id not in topic_members[default_topic]
        ):
            raise ValueError(f'thread article {article_id} has invalid default topic')


__all__ = [
    'THREAD_SCHEMA_VERSION',
    'build_thread_index',
    'entity_kind',
    'entity_label',
    'validate_thread_index',
]
