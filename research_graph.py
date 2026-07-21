#!/usr/bin/env python3
"""Build deterministic search and related-article data from public research.

The functions in this module are deliberately pure: callers supply the article
snapshot and receive JSON-serializable objects.  This keeps the extraction and
ranking contract independently testable and avoids coupling the machine data
layer to the consumer-facing terminal.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Counter as CounterType
from typing import Dict, List, Mapping, Sequence, Set, Tuple

from article_briefs import is_boilerplate_text


MAX_SEARCH_INDEX_BYTES = 500_000
RELATED_COUNT = 5
SCORE_DECIMALS = 6

_GREEK_NAMES = {
    'Γ': ' gamma ',
    'γ': ' gamma ',
    'Δ': ' delta ',
    'δ': ' delta ',
    'Θ': ' theta ',
    'θ': ' theta ',
    'Ρ': ' rho ',
    'ρ': ' rho ',
    'Ν': ' nu ',
    'ν': ' nu ',
}
_POSSESSIVE_RE = re.compile(r"(?i)(?<=\w)'s\b")
_WORD_RE = re.compile(r'[a-z0-9]+')
_CAPITALIZED_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:[.'’][A-Za-z0-9]+)*|&"
)
_CAPITALIZED_SPLIT_RE = re.compile(r'[,;:!?()\[\]{}|/\n—–]+')
_CAPITALIZED_CONNECTORS = {'and', 'for', 'of', 'the', '&'}
_ORGANIZATION_SUFFIXES = {
    'advisors', 'authority', 'bank', 'capital', 'commission', 'exchange',
    'management', 'partners', 'research', 'securities', 'technologies',
    'university',
}
_CAPITALIZED_HARD_BLOCK = {
    'actionable', 'analysis', 'article', 'bawa', 'bottom', 'built', 'case',
    'complete', 'crude',
    'deep', 'documented', 'explained', 'extract', 'extracted', 'fund', 'funds',
    'generated', 'github', 'hedge', 'how', 'inside', 'lost', 'made', 'makes',
    'navnoor', 'profit', 'profits', 'repository', 'return', 'returns', 'study',
    'traded', 'trades', 'trading', 'what', 'when', 'why', 'won',
}
_CAPITALIZED_GENERIC = {
    'advisors', 'alpha', 'approach', 'arbitrage', 'authority', 'bank', 'capital',
    'central', 'data', 'equity', 'exchange', 'financial', 'fund', 'funds',
    'investment', 'management', 'market', 'markets', 'model', 'models',
    'option', 'options', 'partners', 'portfolio', 'quant', 'quantitative',
    'quants', 'research', 'risk', 'securities', 'strategy', 'system', 'systems',
    'technologies', 'trade', 'trading', 'university', 'volatility',
}

# Aliases are matched as complete normalized token sequences.  Short tickers
# intentionally appear only in this allowlist; arbitrary all-caps prose is too
# noisy to promote into a public entity index.
ENTITY_ALIASES: Mapping[str, Tuple[str, ...]] = {
    # Firms, institutions, venues, and regulators.
    'aqr': ('aqr',),
    'bank-of-america': ('bank of america', 'bofa'),
    'bank-of-england': ('bank of england', 'boe'),
    'barclays': ('barclays',),
    'blackrock': ('blackrock',),
    'bluecrest': ('bluecrest', 'bluecrest capital'),
    'brevan-howard': ('brevan howard',),
    'bridgewater': ('bridgewater', 'bridgewater associates'),
    'calpers': ('calpers',),
    'capstone': ('capstone', 'capstone investment advisors'),
    'cfm': ('cfm', 'capital fund management'),
    'cftc': ('cftc', 'commodity futures trading commission'),
    'citadel': ('citadel', 'citadel securities'),
    'crescat': ('crescat', 'crescat capital'),
    'd-e-shaw': ('d e shaw', 'de shaw'),
    'elliott-management': ('elliott management',),
    'esma': ('esma', 'european securities and markets authority'),
    'federal-reserve': ('federal reserve', 'the fed'),
    'flow-traders': ('flow traders',),
    'goldman-sachs': ('goldman sachs',),
    'greenlight': ('greenlight', 'greenlight capital'),
    'hrt': ('hrt', 'hudson river trading'),
    'hsbc': ('hsbc',),
    'ice': ('intercontinental exchange', 'ice exchange'),
    'jane-street': ('jane street',),
    'jpmorgan': ('jpmorgan', 'jp morgan', 'j p morgan'),
    'ljm': ('ljm', 'ljm preservation and growth'),
    'ltcm': ('ltcm', 'long term capital management'),
    'man-ahl': ('man ahl',),
    'millennium': ('millennium', 'millennium management'),
    'morgan-stanley': ('morgan stanley',),
    'optiver': ('optiver',),
    'point72': ('point72', 'point 72'),
    'polymarket': ('polymarket',),
    'qvr': ('qvr', 'qvr advisors'),
    'renaissance': ('renaissance', 'renaissance technologies'),
    'sec': ('sec', 'securities and exchange commission'),
    'squarepoint': ('squarepoint', 'squarepoint capital'),
    'third-point': ('third point',),
    'two-sigma': ('two sigma',),
    'universa': ('universa', 'universa investments'),
    'virtu': ('virtu', 'virtu financial'),
    'vitol': ('vitol',),
    'worldquant': ('worldquant',),
    'xtx-markets': ('xtx markets',),
    # Instruments, markets, and tickers.
    'bitcoin': ('bitcoin', 'btc'),
    'bonds': ('bond', 'bonds'),
    'copper': ('copper',),
    'dxy': ('dxy', 'dollar index'),
    'ethereum': ('ethereum', 'eth'),
    'euro': ('euro', 'eur'),
    'futures': ('future', 'futures'),
    'gold': ('gold',),
    'natural-gas': ('natural gas',),
    'non-deliverable-forwards': (
        'non deliverable forward', 'non deliverable forwards', 'ndf', 'ndfs',
    ),
    'oil': ('oil', 'crude oil', 'wti', 'brent'),
    'options': ('option', 'options'),
    'precious-metals': ('precious metal', 'precious metals'),
    'silver': ('silver',),
    'spx': ('spx', 's p x', 's and p 500'),
    'swaps': ('swap', 'swaps'),
    'treasuries': ('treasury', 'treasuries'),
    'vix': ('vix',),
    'vvix': ('vvix',),
    'yen': ('yen', 'jpy'),
    # Models, mechanisms, and systematic techniques.
    'avellaneda-stoikov': ('avellaneda stoikov',),
    'basis-trade': ('basis trade', 'basis trading'),
    'black-litterman': ('black litterman',),
    'black-scholes': ('black scholes',),
    'carry-trade': ('carry trade', 'carry trading'),
    'delta-hedging': ('delta hedge', 'delta hedging'),
    'dispersion-trading': ('dispersion', 'dispersion trade', 'dispersion trading'),
    'expected-shortfall': ('expected shortfall',),
    'factor-model': ('factor model', 'factor models'),
    'gamma': ('gamma',),
    'gamma-scalping': ('gamma scalp', 'gamma scalping'),
    'heston': ('heston', 'heston model'),
    'hull-white': ('hull white', 'hull white model'),
    'kelly-criterion': (
        'kelly criterion', 'fractional kelly', 'half kelly',
    ),
    'market-making': ('market maker', 'market makers', 'market making'),
    'market-microstructure': ('market microstructure',),
    'market-structure': ('market structure', 'market structures'),
    'mean-reversion': ('mean reversion', 'mean reverting'),
    'monte-carlo': ('monte carlo',),
    'risk-parity': ('risk parity',),
    'rough-volatility': ('rough vol', 'rough volatility'),
    'sabr': ('sabr',),
    'sharpe-ratio': ('sharpe ratio',),
    'sortino-ratio': ('sortino ratio',),
    'statistical-arbitrage': ('stat arb', 'statistical arbitrage'),
    'stochastic-volatility': ('stochastic vol', 'stochastic volatility'),
    'value-at-risk': ('value at risk',),
    'variance-swaps': ('variance swap', 'variance swaps'),
    'volatility-arbitrage': ('volatility arbitrage',),
    'volatility-risk-premium': ('volatility risk premium',),
}

_TFIDF_STOPWORDS = frozenset(
    """
    a about after against all also an and any are as at back be because been before
    behind being between both but by can complete could data did do documented
    does during each every evidence exact few finance financial first five for
    four from fund funds further get gets got had has have having hedge her here
    hers him his
    how i if in inside institution institutional investment investors is it its
    itself made make makes making market markets may might million more most much
    must my new nine no nor not now of on once one only or other our ours out over
    own paper per piece position positions research return returns same said says
    second she should six so some
    still strategy strategies such than that the their them then there these they
    ten third this those three through to too trade trading under up use used
    using very via was
    we were what when where which while who why will with would year years you
    your neutral article billion buy sell actually rather roughly
    """.split()
)


def _normalized_words(value: Any) -> Tuple[str, ...]:
    """Return stable ASCII tokens while retaining useful financial aliases."""
    text = unicodedata.normalize('NFKC', str(value or ''))
    for symbol, name in _GREEK_NAMES.items():
        text = text.replace(symbol, name)
    text = text.replace('’', "'").replace('‘', "'")
    text = _POSSESSIVE_RE.sub('', text)
    text = text.replace('&', ' and ')
    text = (
        unicodedata.normalize('NFKD', text)
        .encode('ascii', 'ignore')
        .decode('ascii')
        .casefold()
    )
    return tuple(_WORD_RE.findall(text))


def _normalize_term(value: Any) -> str:
    return '-'.join(_normalized_words(value))


def _required_text(article: Mapping[str, Any], field: str) -> str:
    value = article.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'article {field} must be a non-empty string')
    return value


def _clean_subtitle(article: Mapping[str, Any]) -> str:
    value = str(article.get('subtitle') or '').strip()
    return '' if is_boilerplate_text(value) else value


def _span_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ''
    return str(value.get('text') or '').strip()


def _section_values(article: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    brief = article.get('brief')
    if not isinstance(brief, Mapping):
        return []
    sections = brief.get('sections')
    if not isinstance(sections, list):
        return []
    return [section for section in sections if isinstance(section, Mapping)]


def _search_text_parts(article: Mapping[str, Any]) -> List[str]:
    """Return exactly the title/subtitle/lead/section scope promised by D2."""
    parts = [str(article.get('title') or ''), _clean_subtitle(article)]
    brief = article.get('brief')
    if isinstance(brief, Mapping):
        parts.append(_span_text(brief.get('lead')))
    for section in _section_values(article):
        parts.extend((
            str(section.get('heading') or '').strip(),
            str(section.get('text') or '').strip(),
        ))
    return [part for part in parts if part]


def _brief_text_parts(article: Mapping[str, Any]) -> List[str]:
    brief = article.get('brief')
    if not isinstance(brief, Mapping):
        return []
    parts = [_span_text(brief.get('lead'))]
    for section in _section_values(article):
        parts.extend((
            str(section.get('heading') or '').strip(),
            str(section.get('text') or '').strip(),
        ))
    parts.append(_span_text(brief.get('fallback_evidence')))
    checkpoints = brief.get('checkpoints')
    if isinstance(checkpoints, list):
        for checkpoint in checkpoints:
            parts.append(_span_text(checkpoint))
    return [part for part in parts if part]


def _contains_tokens(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    length = len(needle)
    target = tuple(needle)
    return any(tuple(haystack[start:start + length]) == target
               for start in range(len(haystack) - length + 1))


_NORMALIZED_ALIASES: Mapping[str, Tuple[Tuple[str, ...], ...]] = {
    canonical: tuple(_normalized_words(alias) for alias in aliases)
    for canonical, aliases in ENTITY_ALIASES.items()
}
_ALIAS_TOKEN_SEQUENCES = tuple(
    alias
    for aliases in _NORMALIZED_ALIASES.values()
    for alias in aliases
    if alias
)


def _is_capitalized_token(token: str) -> bool:
    if token == '&' or token.casefold() in _CAPITALIZED_CONNECTORS:
        return True
    letters = ''.join(character for character in token if character.isalpha())
    return bool(letters) and (letters.isupper() or letters[0].isupper())


def _overlaps_seed_alias(words: Sequence[str]) -> bool:
    return any(_contains_tokens(words, alias) for alias in _ALIAS_TOKEN_SEQUENCES)


def _capitalized_candidates(value: str) -> Set[str]:
    candidates: Set[str] = set()
    for segment in _CAPITALIZED_SPLIT_RE.split(value):
        tokens = _CAPITALIZED_TOKEN_RE.findall(segment)
        runs: List[List[str]] = []
        current: List[str] = []
        for token in tokens:
            token_words = _normalized_words(token)
            is_blocked = any(
                word in _CAPITALIZED_HARD_BLOCK for word in token_words
            )
            if _is_capitalized_token(token) and not is_blocked:
                if current or token.casefold() not in _CAPITALIZED_CONNECTORS:
                    current.append(token)
                continue
            if current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)

        for phrase_tokens in runs:
            while (
                    phrase_tokens
                    and phrase_tokens[-1].casefold() in _CAPITALIZED_CONNECTORS):
                phrase_tokens.pop()
            words = _normalized_words(' '.join(phrase_tokens))
            if not 2 <= len(words) <= 3 or len('-'.join(words)) > 64:
                continue
            if words[-1] not in _ORGANIZATION_SUFFIXES:
                continue
            content_words = [
                word for word in words
                if word not in _CAPITALIZED_CONNECTORS
            ]
            if all(word in _CAPITALIZED_GENERIC for word in content_words):
                continue
            if _overlaps_seed_alias(words):
                continue
            candidates.add('-'.join(words))
    return candidates


def _candidate_discovery_parts(article: Mapping[str, Any]) -> List[str]:
    parts = [str(article.get('title') or ''), _clean_subtitle(article)]
    parts.extend(
        str(section.get('heading') or '').strip()
        for section in _section_values(article)
    )
    return [part for part in parts if part]


def _discover_capitalized_terms(
        articles: Sequence[Mapping[str, Any]]) -> Dict[str, Tuple[str, ...]]:
    documents: Dict[str, Set[int]] = defaultdict(set)
    for article_index, article in enumerate(articles):
        article_candidates: Set[str] = set()
        for part in _candidate_discovery_parts(article):
            article_candidates.update(_capitalized_candidates(part))
        for candidate in article_candidates:
            documents[candidate].add(article_index)

    accepted: Dict[str, Tuple[str, ...]] = {}
    for candidate in sorted(documents):
        words = tuple(candidate.split('-'))
        organization = words[-1] in _ORGANIZATION_SUFFIXES
        # Headline capitalization also produces phrases such as "Bottom Line"
        # and "Formula Behind".  A recognized organization suffix is the
        # precision gate for inferred names; models and tickers are curated.
        if organization:
            accepted[candidate] = words
    return accepted


def _validated_articles(articles: Sequence[Mapping[str, Any]]) -> None:
    identities: Set[str] = set()
    for article in articles:
        if not isinstance(article, Mapping):
            raise ValueError('every article must be an object')
        source = _required_text(article, 'source')
        slug = _required_text(article, 'slug')
        _required_text(article, 'title')
        _required_text(article, 'post_date')
        _required_text(article, 'url')
        identity = f'{source}:{slug}'
        if identity in identities:
            raise ValueError(f'duplicate article identity: {identity}')
        identities.add(identity)


def build_search_index(articles: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return the compact entity-to-article lookup for one article snapshot."""
    if not isinstance(articles, Sequence) or isinstance(articles, (str, bytes)):
        raise ValueError('articles must be a sequence of objects')
    _validated_articles(articles)

    capitalized_terms = _discover_capitalized_terms(articles)
    per_article_entities: List[List[str]] = []
    inverted: Dict[str, List[int]] = defaultdict(list)

    for article_index, article in enumerate(articles):
        words = _normalized_words(' '.join(_search_text_parts(article)))
        found = {
            canonical
            for canonical, aliases in _NORMALIZED_ALIASES.items()
            if any(_contains_tokens(words, alias) for alias in aliases)
        }
        found.update(
            canonical
            for canonical, phrase_words in capitalized_terms.items()
            if _contains_tokens(words, phrase_words)
        )
        ordered = sorted(found)
        per_article_entities.append(ordered)
        for entity in ordered:
            inverted[entity].append(article_index)

    article_rows = []
    for article, entities in zip(articles, per_article_entities):
        article_rows.append({
            'slug': article['slug'],
            'source': article['source'],
            'title': article['title'],
            'post_date': article['post_date'],
            'url': article['url'],
            'entities': entities,
        })

    result: Dict[str, Any] = {
        'entities': {key: inverted[key] for key in sorted(inverted)},
        'articles': article_rows,
    }
    payload_size = len(json.dumps(
        result, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8'))
    if payload_size >= MAX_SEARCH_INDEX_BYTES:
        raise ValueError(
            f'search index is {payload_size} bytes; limit is below '
            f'{MAX_SEARCH_INDEX_BYTES} bytes'
        )
    return result


def _feature_counter(value: str) -> CounterType[str]:
    words = [
        word for word in _normalized_words(value)
        if len(word) >= 3
        and not word.isdigit()
        and word not in _TFIDF_STOPWORDS
    ]
    features = list(words)
    features.extend(
        f'{words[index]}-{words[index + 1]}'
        for index in range(len(words) - 1)
    )
    return Counter(features)


def article_feature_terms(article: Mapping[str, Any]) -> Set[str]:
    """Return the exact normalized lexical features used to explain links."""
    features = set(_feature_counter(str(article.get('title') or '')))
    features.update(_feature_counter(_clean_subtitle(article)))
    features.update(_feature_counter(' '.join(_brief_text_parts(article))))
    return features


def _normalized_vector(
        counts: Mapping[str, int], idf: Mapping[str, float]) -> Dict[str, float]:
    weighted = {
        feature: (1.0 + math.log(count)) * idf[feature]
        for feature, count in counts.items()
        if feature in idf and count > 0
    }
    magnitude = math.sqrt(sum(value * value for value in weighted.values()))
    if not magnitude:
        return {}
    return {
        feature: value / magnitude
        for feature, value in weighted.items()
    }


def _dot(first: Mapping[str, float], second: Mapping[str, float]) -> float:
    if len(first) > len(second):
        first, second = second, first
    return sum(value * second.get(feature, 0.0)
               for feature, value in first.items())


def _validated_search_entities(
        articles: Sequence[Mapping[str, Any]],
        search_index: Mapping[str, Any],
) -> Tuple[List[Set[str]], Dict[str, int]]:
    rows = search_index.get('articles')
    inverted = search_index.get('entities')
    if not isinstance(rows, list) or len(rows) != len(articles):
        raise ValueError('search index article rows do not match the snapshot')
    if not isinstance(inverted, Mapping):
        raise ValueError('search index entities must be an object')

    per_article: List[Set[str]] = []
    for position, (article, row) in enumerate(zip(articles, rows)):
        if not isinstance(row, Mapping):
            raise ValueError('search index article row must be an object')
        for field in ('slug', 'source', 'title', 'post_date', 'url'):
            if row.get(field) != article.get(field):
                raise ValueError(
                    f'search index article {position} has mismatched {field}'
                )
        row_entities = row.get('entities')
        if not isinstance(row_entities, list):
            raise ValueError('search index article entities must be a list')
        if row_entities != sorted(set(row_entities)):
            raise ValueError('search index article entities must be sorted and unique')
        if not all(isinstance(entity, str) and _normalize_term(entity) == entity
                   for entity in row_entities):
            raise ValueError('search index contains a malformed entity term')
        per_article.append(set(row_entities))

    document_frequency: Dict[str, int] = {}
    for entity, positions in inverted.items():
        if not isinstance(entity, str) or _normalize_term(entity) != entity:
            raise ValueError('search index contains a malformed inverted term')
        if not isinstance(positions, list):
            raise ValueError('search index entity references must be a list')
        expected = [
            position for position, entities in enumerate(per_article)
            if entity in entities
        ]
        if positions != expected:
            raise ValueError(
                f'search index references for {entity} are inconsistent'
            )
        document_frequency[entity] = len(expected)
    if set(inverted) != set().union(*per_article):
        raise ValueError('search index omits an article entity from its inverse map')
    return per_article, document_frequency


def _shared_feature_reasons(
        first_index: int,
        second_index: int,
        vectors: Sequence[Sequence[Mapping[str, float]]],
        shared_entities: Sequence[str],
) -> List[str]:
    reasons = [f'shared: {entity}' for entity in shared_entities[:3]]
    if reasons:
        return reasons

    entity_parts = {
        part
        for entity in shared_entities
        for part in entity.split('-')
    }
    entity_terms = set(shared_entities)
    contributions: Dict[str, float] = defaultdict(float)
    field_weights = (0.45, 0.15, 0.25)
    for field_index, weight in enumerate(field_weights):
        first = vectors[field_index][first_index]
        second = vectors[field_index][second_index]
        for feature in first.keys() & second.keys():
            contributions[feature] += weight * first[feature] * second[feature]

    ordered_features = sorted(
        contributions,
        key=lambda feature: (
            0 if '-' in feature else 1,
            -round(contributions[feature], 12),
            feature,
        ),
    )
    for feature in ordered_features:
        if feature in entity_terms or feature in entity_parts:
            continue
        if any(
                feature.startswith(f'{entity}-')
                or feature.endswith(f'-{entity}')
                for entity in entity_terms):
            continue
        reason = f'shared: {feature}'
        if reason not in reasons:
            reasons.append(reason)
        if len(reasons) == 3:
            break
    return reasons


def _cross_field_reasons(
        first_features: Set[str], second_features: Set[str],
        document_frequency: Mapping[str, int],
) -> List[str]:
    """Explain sparse links using only exact terms present in both articles."""
    shared = first_features & second_features
    ordered = sorted(
        shared,
        key=lambda feature: (
            document_frequency.get(feature, 0),
            0 if '-' in feature else 1,
            feature,
        ),
    )
    return [f'shared: {feature}' for feature in ordered[:3]]


def build_related_graph(
        articles: Sequence[Mapping[str, Any]],
        search_index: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return five explainable, field-weighted related articles per article."""
    if len(articles) < RELATED_COUNT + 1:
        raise ValueError(
            f'at least {RELATED_COUNT + 1} articles are required for related links'
        )
    _validated_articles(articles)
    per_article_entities, entity_df = _validated_search_entities(
        articles, search_index,
    )
    article_count = len(articles)

    field_counters: List[List[CounterType[str]]] = [[], [], []]
    for article in articles:
        field_counters[0].append(_feature_counter(str(article['title'])))
        field_counters[1].append(_feature_counter(_clean_subtitle(article)))
        field_counters[2].append(_feature_counter(
            ' '.join(_brief_text_parts(article))
        ))

    document_frequency: CounterType[str] = Counter()
    article_features: List[Set[str]] = []
    for position in range(article_count):
        document_features: Set[str] = set()
        for counters in field_counters:
            document_features.update(counters[position])
        article_features.append(document_features)
        document_frequency.update(document_features)
    maximum_frequency = max(2, int(article_count * 0.45))
    idf = {
        feature: math.log(
            (article_count + 1.0) / (frequency + 1.0)
        ) + 1.0
        for feature, frequency in document_frequency.items()
        if 2 <= frequency <= maximum_frequency
    }
    vectors: List[List[Dict[str, float]]] = [
        [_normalized_vector(counts, idf) for counts in counters]
        for counters in field_counters
    ]
    entity_idf = {
        entity: math.log(
            (article_count + 1.0) / (frequency + 1.0)
        ) + 1.0
        for entity, frequency in entity_df.items()
    }
    entity_norms = [
        sum(entity_idf[entity] ** 2 for entity in entities)
        for entities in per_article_entities
    ]

    result: Dict[str, Any] = {}
    field_weights = (0.45, 0.15, 0.25)
    for first_index, article in enumerate(articles):
        ranked = []
        for second_index, candidate in enumerate(articles):
            if first_index == second_index:
                continue
            lexical_score = sum(
                field_weights[field_index] * _dot(
                    vectors[field_index][first_index],
                    vectors[field_index][second_index],
                )
                for field_index in range(3)
            )
            shared = (
                per_article_entities[first_index]
                & per_article_entities[second_index]
            )
            entity_score = 0.0
            if shared and entity_norms[first_index] and entity_norms[second_index]:
                numerator = sum(entity_idf[entity] ** 2 for entity in shared)
                entity_score = numerator / math.sqrt(
                    entity_norms[first_index] * entity_norms[second_index]
                )
            shared_entities = sorted(
                shared,
                key=lambda entity: (entity_df[entity], entity),
            )
            why = _shared_feature_reasons(
                first_index, second_index, vectors, shared_entities,
            )
            cross_field_score = 0.0
            if not why:
                why = _cross_field_reasons(
                    article_features[first_index],
                    article_features[second_index],
                    document_frequency,
                )
                if why:
                    # Cross-field matches are a low-weight coverage floor for
                    # sparse registry titles. They can complete top-five
                    # coverage, but never outrank normal field-aligned TF-IDF.
                    cross_field_score = round(
                        min(
                            0.01,
                            sum(
                                1.0 / document_frequency[
                                    reason.removeprefix('shared: ')
                                ]
                                for reason in why
                            ) / 100.0,
                        ),
                        SCORE_DECIMALS,
                    )
            score = round(min(
                1.0,
                lexical_score + 0.15 * entity_score + cross_field_score,
            ), SCORE_DECIMALS)
            if not math.isfinite(score) or score <= 0.0:
                continue
            if not why:
                continue
            identity = f'{candidate["source"]}:{candidate["slug"]}'
            ranked.append((score, identity, second_index, why))

        ranked.sort(key=lambda row: (-row[0], row[1]))
        if len(ranked) < RELATED_COUNT:
            identity = f'{article["source"]}:{article["slug"]}'
            raise ValueError(
                f'{identity} has only {len(ranked)} explainable related articles'
            )
        related_rows = []
        for score, _identity, second_index, why in ranked[:RELATED_COUNT]:
            candidate = articles[second_index]
            related_rows.append({
                'slug': candidate['slug'],
                'source': candidate['source'],
                'title': candidate['title'],
                'url': candidate['url'],
                'score': score,
                'why': why,
            })
        key = f'{article["source"]}:{article["slug"]}'
        result[key] = related_rows
    return result


__all__ = [
    'article_feature_terms', 'build_related_graph', 'build_search_index',
]
