#!/usr/bin/env python3
"""Deterministic topic-family classification for published research metadata."""

import re
import unicodedata
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


ALLOWED_FAMILIES: Tuple[str, ...] = (
    'firm-mechanics',
    'career-structure',
    'model-critique',
    'scandal-enforcement',
    'event-reaction',
    'market-structure',
    'other',
)


# These rules deliberately favor precision.  A broad reference to a model,
# market, or hedge fund is not sufficient on its own to assign a family.
CAREER_PHRASES = (
    'career path', 'compensation', 'team economics', 'structure pods',
    'pod structure', 'multi pod', 'portfolio manager economics',
    'centralized allocation', 'star portfolio managers', 'hiring',
    'talent pipeline', 'organizational design', 'organisation design',
)
MISCONDUCT_PHRASES = (
    'manipulation', 'manipulating', 'manipulated', 'spoofing', 'fraud',
    'insider trading', 'market abuse', 'price fixing', 'testified under oath',
    'criminal case', 'criminal charges', 'arrested', 'convicted', 'indicted',
)
ENFORCEMENT_PHRASES = (
    'sec fined', 'cftc fined', 'sebi order', 'court documents',
    'regulatory action', 'enforcement action', 'paid a fine', 'paid fines',
)
MODEL_PHRASES = (
    'black scholes', 'hull white', 'avellaneda stoikov', 'sabr', 'heston',
    'value at risk', 'var model', 'sharpe ratio', 'sortino ratio',
    'kelly criterion', 'covariance matrix', 'mean reversion',
    'stochastic volatility', 'factor model', 'risk model', 'risk models',
    'maximum drawdown', 'downside deviation', 'ols', 'pca',
)
CRITIQUE_PHRASES = (
    ' is wrong', ' got wrong', 'problem', 'limitation', 'blind spot',
    'bias', 'missed', 'fails', 'failed', 'lost to', 'naive guess', 'fix',
    'overstate', 'understate', 'denominator error', 'cannot see',
    'can t be right', 'gets wrong', 'broke the model', 'broke the models',
)
MARKET_STRUCTURE_PHRASES = (
    'market structure', 'microstructure', 'mica', 'mifid',
    'central counterparty', 'clearing house', 'clearinghouse',
    'exchange outage', 'trading venue', 'dark pool', 'fixing window',
    'index rebalancing', 'settlement mechanics', 'ndf market',
    'non deliverable forward',
)
EVENT_PHRASES = (
    'iran war', 'oil war', 'trade war', 'tariff shock', 'tariff verdict',
    'invasion', 'sanctions', 'fomc', 'mpc decision', 'budget', 'election',
    'geopolitical', 'policy driven', 'fed turns', 'oil shock',
)
MECHANICS_PHRASES = (
    'how ', 'inside ', 'engine', 'generated', 'generates returns',
    'made ', 'built ', 'extract', 'profit', 'strategy', 'playbook',
    'trade that', 'monetizes', 'deconstructing',
)
NAMED_FIRMS = (
    'aqr', 'balyasny', 'blackstone', 'bluecrest', 'bridgewater', 'capstone',
    'cfm', 'citadel', 'da vinci trading', 'd e shaw', 'futures first',
    'graviton', 'greenlight', 'hrt', 'hudson river trading', 'jane street',
    'jump trading', 'millennium', 'optiver', 'pershing square', 'point72',
    'qrt', 'renaissance', 'saba', 'squarepoint', 'susquehanna',
    'two sigma', 'universa', 'virtu', 'vitol', 'worldquant',
)


def normalize_taxonomy_text(value: object) -> str:
    """Return lowercase ASCII tokens suitable for exact phrase rules."""
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    text = ''.join(character for character in text
                   if not unicodedata.combining(character))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


def _span_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return ''
    return str(value.get('text') or '')


def article_taxonomy_text(article: Mapping[str, object]) -> Tuple[str, str]:
    """Return normalized title and bounded title/subtitle/brief context."""
    title = normalize_taxonomy_text(article.get('title'))
    values = [str(article.get('title') or ''), str(article.get('subtitle') or '')]
    brief = article.get('brief')
    if isinstance(brief, Mapping):
        values.append(_span_text(brief.get('lead')))
        values.append(_span_text(brief.get('fallback_evidence')))
        sections = brief.get('sections')
        if isinstance(sections, Sequence) and not isinstance(sections, (str, bytes)):
            for section in sections:
                values.append(_span_text(section))
    return title, normalize_taxonomy_text(' '.join(values))


def _has_any(text: str, phrases: Iterable[str]) -> bool:
    padded = f' {text} '
    return any(f' {phrase.strip()} ' in padded for phrase in phrases)


def classify_family(article: Mapping[str, object]) -> str:
    """Assign exactly one documented topic family to an article.

    The precedence is intentional: explicit misconduct, organizational design,
    and a named-model critique are more specific than a general description of
    how a firm trades.  ``other`` is a safe abstention, not an error state.
    """
    title, context = article_taxonomy_text(article)

    if (_has_any(title, MISCONDUCT_PHRASES)
            or (_has_any(title, ENFORCEMENT_PHRASES)
                and _has_any(context, MISCONDUCT_PHRASES))):
        return 'scandal-enforcement'
    if _has_any(title, CAREER_PHRASES):
        return 'career-structure'
    if (_has_any(title, MODEL_PHRASES) and _has_any(title, CRITIQUE_PHRASES)):
        return 'model-critique'
    if _has_any(title, MARKET_STRUCTURE_PHRASES):
        return 'market-structure'
    if _has_any(title, EVENT_PHRASES):
        return 'event-reaction'
    if _has_any(context, NAMED_FIRMS) and _has_any(title, MECHANICS_PHRASES):
        return 'firm-mechanics'

    # Organizational language buried in an evidence span should not override
    # a more specific title-level market, event, or firm-mechanics signal.
    if _has_any(context, CAREER_PHRASES):
        return 'career-structure'

    # A model critique can occasionally put the model name in a subtitle or
    # exact brief span.  Requiring both signals keeps this fallback narrow.
    if _has_any(context, MODEL_PHRASES) and _has_any(context, CRITIQUE_PHRASES):
        return 'model-critique'
    if _has_any(context, MARKET_STRUCTURE_PHRASES):
        return 'market-structure'
    return 'other'


def add_families(articles: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    """Return shallow article copies with the additive ``family`` field."""
    results: List[Dict[str, object]] = []
    for article in articles:
        item = dict(article)
        item['family'] = classify_family(article)
        results.append(item)
    return results


def build_families_index(
        articles: Sequence[Mapping[str, object]]) -> Dict[str, List[str]]:
    """Partition article slugs by family while preserving catalogue order."""
    result: Dict[str, List[str]] = {
        family: [] for family in ALLOWED_FAMILIES
    }
    seen = set()
    for index, article in enumerate(articles):
        slug = str(article.get('slug') or '')
        if not slug or slug in seen:
            raise ValueError(f'article {index} has a missing or duplicate slug')
        seen.add(slug)
        family = str(article.get('family') or classify_family(article))
        if family not in result:
            raise ValueError(f'article {index} has an invalid family: {family!r}')
        result[family].append(slug)
    return result
