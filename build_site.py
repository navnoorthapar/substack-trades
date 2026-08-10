#!/usr/bin/env python3
"""Build the institutional research terminal at docs/index.html."""
import base64
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from article_briefs import is_boilerplate_text
from client_article_contract import (
    ARTICLE_WIRE_SCHEMA_VERSION,
    compact_client_article,
)
from data_contract import validate_data_layer, write_data_layer
from treasury_curve import DATASET_NAME as TREASURY_DATASET_NAME
from treasury_curve import build_rate_context, load_curve_dataset
from extract_trades import has_negated_trade_signal
from research_graph import build_related_graph, build_search_index
from research_taxonomy import build_families_index
from research_threads import build_thread_index
from share_cards import emit_share_assets
from validate_inline_scripts import extract_inline_scripts


ROOT = Path(__file__).parent
DOCS_DIR = Path(os.environ.get('SITE_OUTPUT_DIR', ROOT / 'docs')).expanduser()
DOCS_DIR.mkdir(parents=True, exist_ok=True)
SITE_URL = 'https://navnoorthapar.github.io/substack-trades/'
SOCIAL_IMAGE_URL = f'{SITE_URL}og.jpg'
SOCIAL_IMAGE_SOURCE = ROOT / 'assets' / 'og.jpg'
SUBSCRIPTION_URL = 'https://www.navnoorbawaresearch.com/subscribe'
THEME_REVISION = 'editorial-terminal-2026-07'
LIGHT_THEME_BG = '#f2e8dd'
DARK_THEME_BG = '#050607'

with open(ROOT / 'trades_extracted.json', encoding='utf-8') as handle:
    trades = json.load(handle)

article_index_path = ROOT / 'articles_index.json'
catalog_index = []
if article_index_path.exists():
    with open(article_index_path, encoding='utf-8') as handle:
        payload = json.load(handle)
    catalog_index = payload.get('articles', []) if isinstance(payload, dict) else payload
    if not isinstance(catalog_index, list) or not all(
            isinstance(article, dict) for article in catalog_index):
        raise ValueError('articles_index.json must contain a list of article objects')
else:
    print('Warning: articles_index.json missing; building from trade metadata only')

# Metadata-only Patreon and FX Empire registry records power the public data
# catalogue, discovery graph, share cards, and article stubs. They deliberately
# stay out of the body-backed terminal so every existing UI flow and dossier
# count remains unchanged.
article_index = [
    article for article in catalog_index
    if article.get('content_status') != 'registry'
]


def clean_date(value):
    """Return a sortable ISO date, sending malformed values to the bottom."""
    date = str(value or '')[:10]
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return '1970-01-01'
    return date


def clean_publication_time(value):
    """Return an exact, sortable publication value and its known precision."""
    raw = str(value or '').strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        datetime.strptime(raw, '%Y-%m-%d')
        return raw, 'day'
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError(f'Invalid publication timestamp: {raw!r}') from error
    if parsed.tzinfo is None:
        raise ValueError(f'Publication timestamp must include a timezone: {raw!r}')
    parsed.astimezone(timezone.utc)
    return raw, 'instant'


def publication_sort_key(article):
    """Return exact UTC publication chronology with deterministic ties."""
    value = str(article['published_at'])
    if article['publication_precision'] == 'day':
        instant = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    else:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        instant = instant.astimezone(timezone.utc)
    return instant, canonical_url_identity(article['url'])


def clean_source(value, url=''):
    source = str(value or '').strip().casefold()
    if source in {'substack', 'medium'}:
        return source
    try:
        host = (urlsplit(str(url)).hostname or '').casefold()
    except ValueError:
        host = ''
    return 'medium' if host == 'medium.com' else 'substack'


def clean_publication_access(source, audience):
    """Map public source metadata to a conservative reader-access label."""
    normalized = str(audience or '').strip().casefold()
    if source == 'substack':
        if normalized == 'only_paid':
            return 'member'
        if normalized == 'everyone':
            return 'public'
    elif source == 'medium':
        if normalized == 'locked':
            return 'member'
        if normalized == 'public':
            return 'public'
    return 'unknown'


def stable_id(prefix, value):
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:14]
    return f'{prefix}_{digest}'


def normalize_identity_text(value):
    """Return a stable, Unicode-aware identity key for durable client state."""
    normalized = unicodedata.normalize('NFKC', str(value or ''))
    return ' '.join(normalized.split()).casefold()


# Normalize obvious person/firm aliases without pretending every named person
# is interchangeable with an organization. The original extracted mention is
# retained on each record for provenance.
MANAGER_ALIAS_LABELS = {
    'citadel': 'Citadel / Ken Griffin',
    'griffin / citadel': 'Citadel / Ken Griffin',
    'griffin': 'Citadel / Ken Griffin',
    'bridgewater': 'Bridgewater / Ray Dalio',
    'dalio / bridgewater': 'Bridgewater / Ray Dalio',
    'dalio': 'Bridgewater / Ray Dalio',
    'ackman': 'Pershing Square / Bill Ackman',
    'ackman / pershing': 'Pershing Square / Bill Ackman',
    'druckenmiller': 'Duquesne / Stanley Druckenmiller',
    'duquesne': 'Duquesne / Stanley Druckenmiller',
    'point72': 'Point72 / Steve Cohen',
    'cohen / point72': 'Point72 / Steve Cohen',
    'tiger': 'Tiger Management / Julian Robertson',
    'robertson / tiger': 'Tiger Management / Julian Robertson',
    'third point': 'Third Point / Dan Loeb',
    'loeb / third point': 'Third Point / Dan Loeb',
    'brevan howard': 'Brevan Howard / Alan Howard',
    'howard': 'Brevan Howard / Alan Howard',
    'einhorn / greenlight': 'Greenlight / David Einhorn',
    'einhorn': 'Greenlight / David Einhorn',
}


def canonical_manager_label(value):
    raw = ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())
    if not raw:
        return '', ''
    return raw, MANAGER_ALIAS_LABELS.get(normalize_identity_text(raw), raw)


REFERENCE_LINE_RE = re.compile(
    r"^(?:https?://|[^.!?\n]{0,120}(?:—|–|:)[ \t]*https?://)",
    re.IGNORECASE,
)


def observation_metadata(
        description, direction, instruments, underlying, thesis, quant,
        description_truncated=False):
    """Return transparent documentation coverage and conservative review flags."""
    text = str(description or '').strip()
    fields = {
        'market': any(value and value != 'unspecified' for value in instruments),
        'stance': bool(direction and direction != 'unspecified'),
        'underlying': bool(str(underlying or '').strip()),
        'thesis': bool(str(thesis or '').strip()),
        'numeric': bool(str(quant or '').strip()),
    }
    reference_line = bool(REFERENCE_LINE_RE.search(text))
    negation_risk = has_negated_trade_signal(text)
    return {
        'documentation_fields': fields,
        'documentation_score': sum(fields.values()),
        'reference_line': reference_line,
        'negation_risk': negation_risk,
        'description_truncated': bool(description_truncated),
    }


def canonical_url_identity(value):
    """Canonicalize safe URL components used for stable article identity."""
    raw = str(value or '').strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.casefold()
    host = (parts.hostname or '').casefold()
    port = parts.port
    if port and not ((scheme == 'https' and port == 443) or (scheme == 'http' and port == 80)):
        host = f'{host}:{port}'
    path = parts.path.rstrip('/') or '/'
    return urlunsplit((scheme, host, path, parts.query, ''))


def json_for_script(value):
    """Serialize data without allowing it to terminate the script element."""
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            .replace('&', r'\u0026')
            .replace('<', r'\u003c')
            .replace('>', r'\u003e')
            .replace('\u2028', r'\u2028')
            .replace('\u2029', r'\u2029'))


def client_span(value):
    """Keep the compact text plus its exact, validated source-span identity."""
    if not isinstance(value, dict) or not value.get('text'):
        return None
    return {
        'text': value['text'],
        'truncated': bool(value.get('truncated')),
        'start': int(value.get('start') or 0),
        'end': int(value.get('end') or 0),
        'sha256': str(value.get('sha256') or ''),
    }


def client_brief(value):
    """Strip refresh-only hashes/offsets from the compact browser payload."""
    if not isinstance(value, dict):
        return {
            'lead': None, 'sections': [], 'fallback_evidence': None,
            'checkpoints': [],
        }
    sections = []
    for section in value.get('sections') or []:
        if not isinstance(section, dict) or not section.get('text'):
            continue
        sections.append({
            'kind': section.get('kind') or '',
            'heading': section.get('heading') or '',
            'text': section['text'],
            'truncated': bool(section.get('truncated')),
            'source_order': int(section.get('source_order') or 0),
            'start': int(section.get('start') or 0),
            'end': int(section.get('end') or 0),
            'sha256': str(section.get('sha256') or ''),
        })
    checkpoints = []
    for checkpoint in value.get('checkpoints') or []:
        if not isinstance(checkpoint, dict) or not checkpoint.get('text'):
            continue
        checkpoints.append({
            'date': checkpoint.get('date') or '',
            'date_label': checkpoint.get('date_label') or '',
            'text': checkpoint['text'],
            'context_kind': checkpoint.get('context_kind') or '',
            'truncated': bool(checkpoint.get('truncated')),
            'start': int(checkpoint.get('start') or 0),
            'end': int(checkpoint.get('end') or 0),
            'sha256': str(checkpoint.get('sha256') or ''),
        })
    return {
        'lead': client_span(value.get('lead')),
        'sections': sections,
        'fallback_evidence': client_span(value.get('fallback_evidence')),
        'checkpoints': checkpoints,
    }


trades_by_url = defaultdict(list)
for trade in trades:
    url = str(trade.get('article_url') or '').strip().rstrip('/')
    if url:
        trades_by_url[url].append(trade)

articles = []
seen_urls = set()
for metadata in article_index:
    url = str(metadata.get('url') or '').strip().rstrip('/')
    if not url or url in seen_urls:
        continue
    seen_urls.add(url)
    article_trades = trades_by_url.get(url, [])
    first = article_trades[0] if article_trades else {}
    try:
        wordcount = max(0, int(metadata.get('wordcount') or 0))
    except (TypeError, ValueError):
        wordcount = 0
    subtitle = str(metadata.get('subtitle') or '').strip()
    if is_boilerplate_text(subtitle):
        subtitle = ''
    published_at, publication_precision = clean_publication_time(
        metadata.get('post_date') or first.get('article_date'),
    )
    body_revision_status = (
        metadata.get('body_revision_status') or 'unverified'
    )
    content_status = metadata.get('content_status') or 'excerpt'
    if body_revision_status != 'current':
        content_status = 'excerpt'
    source = clean_source(metadata.get('source'), url)
    articles.append({
        'title': metadata.get('title') or first.get('article_title') or url,
        'subtitle': subtitle,
        'date': clean_date(metadata.get('post_date') or first.get('article_date')),
        'published_at': published_at,
        'publication_precision': publication_precision,
        'url': url,
        'source': source,
        'publication_access': clean_publication_access(
            source, metadata.get('audience')
        ),
        'member_preview_chars': (
            metadata.get('member_preview', {}).get('character_count', 0)
            if isinstance(metadata.get('member_preview'), dict)
            else 0
        ),
        'alternate_urls': metadata.get('alternate_urls') or {},
        'wordcount': wordcount,
        'content_status': content_status,
        'body_revision_status': body_revision_status,
        'source_updated_at': (
            metadata.get('source_updated_at')
            if isinstance(metadata.get('source_updated_at'), str)
            else ''
        ),
        'observed_source_updated_at': (
            metadata.get('observed_source_updated_at')
            if isinstance(metadata.get('observed_source_updated_at'), str)
            else ''
        ),
        'brief': client_brief(metadata.get('brief')),
        'trades': article_trades,
    })

# Keep trade-bearing articles additive if metadata is ever incomplete.
for url, article_trades in trades_by_url.items():
    if url in seen_urls:
        continue
    first = article_trades[0]
    published_at, publication_precision = clean_publication_time(
        first.get('article_date'),
    )
    articles.append({
        'title': first.get('article_title') or url,
        'subtitle': '',
        'date': clean_date(first.get('article_date')),
        'published_at': published_at,
        'publication_precision': publication_precision,
        'url': url,
        'source': clean_source(None, url),
        'publication_access': 'unknown',
        'member_preview_chars': 0,
        'alternate_urls': {},
        'wordcount': 0,
        'content_status': 'excerpt',
        'body_revision_status': 'unverified',
        'source_updated_at': '',
        'observed_source_updated_at': '',
        'brief': client_brief(None),
        'trades': article_trades,
    })

articles.sort(key=publication_sort_key, reverse=True)

manager_variants: defaultdict[str, Counter[str]] = defaultdict(Counter)
for trade in trades:
    raw_manager, canonical_manager = canonical_manager_label(
        trade.get('fund_name_if_mentioned')
    )
    if canonical_manager:
        manager_variants[normalize_identity_text(canonical_manager)][canonical_manager] += 1


def preferred_manager_label(key):
    variants = manager_variants[key]
    return sorted(
        variants,
        key=lambda label: (-variants[label], label.islower(), label.casefold(), label),
    )[0]


manager_labels = {
    key: preferred_manager_label(key)
    for key in manager_variants
}

# Flatten the client payload once. Article metadata is stored once instead of
# being repeated inside every extracted idea. Exact older dossiers and parser
# observations are checksum-bound deferred assets, keeping first-load parse and
# heap cost bounded without weakening release integrity.
client_articles = []
client_ideas = []
brief_archive = {}
# Keep the latest dossiers instant while leaving durable headroom for daily
# article growth; the checksum-bound archive loads only when an older dossier
# or archive-wide lens is requested.
INLINE_BRIEF_COUNT = 12
for article_position, article in enumerate(articles):
    article_id = stable_id('a', canonical_url_identity(article['url']))
    idea_ids = []
    directions = set()
    instruments = set()
    underlyings: dict[str, str] = {}
    managers = set()
    manager_keys = set()

    for trade in article['trades']:
        description = str(trade.get('trade_description') or '').strip()
        identity_description = (
            description[:-1] if len(description) >= 790 and description.endswith('…')
            else description
        )
        idea_id = stable_id(
            'i',
            canonical_url_identity(article['url']) + '\0' + normalize_identity_text(identity_description),
        )
        idea_ids.append(idea_id)
        direction = str(trade.get('direction') or 'unspecified')
        idea_instruments = [
            str(value) for value in (trade.get('instruments') or ['unspecified'])
            if value
        ] or ['unspecified']
        manager_raw, canonical_manager = canonical_manager_label(
            trade.get('fund_name_if_mentioned')
        )
        manager_key = normalize_identity_text(canonical_manager)
        manager = manager_labels.get(manager_key, '')
        thesis = trade.get('edge_or_thesis') or ''
        quant = trade.get('any_quant_detail') or ''
        underlying = trade.get('underlying') or ''
        metadata = observation_metadata(
            description, direction, idea_instruments, underlying, thesis, quant,
            trade.get('description_truncated', False),
        )
        directions.add(direction)
        instruments.update(idea_instruments)
        for underlying_value in str(underlying or '').split(';'):
            underlying_value = underlying_value.strip()
            if underlying_value and underlying_value not in {'—', '-'}:
                underlying_key = normalize_identity_text(underlying_value)
                underlyings.setdefault(underlying_key, underlying_value)
        if manager:
            managers.add(manager)
            manager_keys.add(manager_key)
        client_ideas.append({
            'id': idea_id,
            'article_id': article_id,
            'description': description,
            'direction': direction,
            'instruments': idea_instruments,
            'underlying': underlying,
            'thesis': thesis,
            'quant': quant,
            'outcome': trade.get('outcome_if_mentioned') or '',
            'manager': manager,
            'manager_key': manager_key,
            'manager_raw': manager_raw,
            **metadata,
        })

    read_minutes = max(1, round(article['wordcount'] / 220)) if article['wordcount'] else 0
    brief_value = article['brief']
    brief_kinds = {section.get('kind') for section in brief_value.get('sections', [])}
    brief_features = {
        'lead': bool(brief_value.get('lead')),
        'evidence': bool(
            'evidence' in brief_kinds or brief_value.get('fallback_evidence')
        ),
        'countercase': 'countercase' in brief_kinds,
        'falsifier': 'falsifier' in brief_kinds,
        'implementation': 'implementation' in brief_kinds,
        'mechanism': 'mechanism' in brief_kinds,
        'checkpoint_count': len(brief_value.get('checkpoints') or []),
    }
    client_article = {
        'id': article_id,
        'title': article['title'],
        'subtitle': article['subtitle'],
        'date': article['date'],
        'published_at': article['published_at'],
        'publication_precision': article['publication_precision'],
        'url': article['url'],
        'source': article['source'],
        'publication_access': article['publication_access'],
        'member_preview_chars': article['member_preview_chars'],
        'alternate_urls': article['alternate_urls'],
        'wordcount': article['wordcount'],
        'read_minutes': read_minutes,
        'content_status': article['content_status'],
        'body_revision_status': article['body_revision_status'],
        'source_updated_at': article['source_updated_at'],
        'observed_source_updated_at': article['observed_source_updated_at'],
        'brief': brief_value if article_position < INLINE_BRIEF_COUNT else None,
        'brief_features': brief_features,
        'idea_ids': idea_ids,
        'trade_count': len(idea_ids),
        'directions': sorted(directions),
        'instruments': sorted(instruments),
        'underlyings': sorted(
            underlyings.values(), key=lambda value: (value.casefold(), value)
        ),
        'managers': sorted(managers, key=lambda value: (value.casefold(), value)),
        'manager_keys': sorted(manager_keys),
        'has_quant': any(bool(trade.get('any_quant_detail')) for trade in article['trades']),
        'has_thesis': any(bool(trade.get('edge_or_thesis')) for trade in article['trades']),
        'has_outcome': any(bool(trade.get('outcome_if_mentioned')) for trade in article['trades']),
    }
    client_articles.append(client_article)
    if article_position >= INLINE_BRIEF_COUNT:
        brief_archive[article_id] = brief_value

article_ids = [article['id'] for article in client_articles]
idea_ids = [idea['id'] for idea in client_ideas]
if len(article_ids) != len(set(article_ids)):
    raise ValueError('Stable article ID collision detected')
if len(idea_ids) != len(set(idea_ids)):
    raise ValueError('Stable idea ID collision detected; extracted descriptions must be unique per article')

manager_counts = Counter(
    idea['manager_key'] for idea in client_ideas if idea.get('manager_key')
)
manager_rows = sorted(
    manager_counts.items(),
    key=lambda row: (
        -row[1], manager_labels[row[0]].casefold(), manager_labels[row[0]], row[0]
    ),
)
manager_buttons = []
for manager_key, count in manager_rows:
    escaped_text = html_lib.escape(manager_labels[manager_key])
    escaped_attr = html_lib.escape(manager_key, quote=True)
    manager_buttons.append(
        '<button type="button" class="facet-option manager-option" '
        f'data-filter="manager" data-value="{escaped_attr}">'
        f'<span>{escaped_text}</span><span class="facet-count" '
        f'data-count-manager="{escaped_attr}">{count}</span></button>'
    )

search_index = build_search_index(catalog_index)
search_urls = {
    str(row.get('url') or '').rstrip('/')
    for row in search_index.get('articles', [])
    if isinstance(row, dict)
}
source_article_by_url = {
    str(article['url']).rstrip('/'): article for article in articles
}
thread_index = build_thread_index(
    [
        {
            **article,
            'brief': source_article_by_url[str(article['url']).rstrip('/')]['brief'],
        }
        for article in client_articles
        if str(article['url']).rstrip('/') in search_urls
    ],
    search_index,
)

# The terminal hydrates this versioned compact wire representation before any
# article is indexed or rendered. The same strict contract is imported by the
# builder, release validator, and tests so their default semantics cannot drift.
embedded_articles = [
    compact_client_article(client_article) for client_article in client_articles
]

articles_json = json_for_script(embedded_articles)
threads_json = json_for_script(thread_index)

# The desk is the landing view, so its controls are rendered from counts fixed
# at build time. Waiting for the deferred observation archive before drawing
# anything would hand a first-time reader an empty screen.
article_date_by_id = {
    article['id']: article['date'] for article in client_articles
}


def _desk_facet_counts(select):
    counts: dict[str, int] = {}
    for idea in client_ideas:
        for value in dict.fromkeys(select(idea)):
            counts[value] = counts.get(value, 0) + 1
    return counts


_instrument_counts = _desk_facet_counts(
    lambda idea: [
        value for value in idea['instruments']
        if value and value != 'unspecified'
    ]
)
_instrument_articles: dict[str, set[str]] = {}
for _idea in client_ideas:
    for _instrument in dict.fromkeys(_idea['instruments']):
        if _instrument and _instrument != 'unspecified':
            _instrument_articles.setdefault(_instrument, set()).add(
                _idea['article_id']
            )
_underlying_counts = _desk_facet_counts(
    lambda idea: [
        part.strip() for part in str(idea['underlying'] or '').split(';')
        if part.strip() and part.strip() not in {'—', '-'}
    ]
)
_underlying_labels: dict[str, str] = {}
for _idea in client_ideas:
    for _part in str(_idea['underlying'] or '').split(';'):
        _part = _part.strip()
        if _part and _part not in {'—', '-'}:
            _underlying_labels.setdefault(normalize_identity_text(_part), _part)
_underlying_totals: dict[str, int] = {}
_underlying_articles: dict[str, set[str]] = {}
for _label, _count in _underlying_counts.items():
    _key = normalize_identity_text(_label)
    _underlying_totals[_key] = _underlying_totals.get(_key, 0) + _count
for _idea in client_ideas:
    for _part in str(_idea['underlying'] or '').split(';'):
        _part = _part.strip()
        if not _part or _part in {'—', '-'}:
            continue
        _key = normalize_identity_text(_part)
        _underlying_articles.setdefault(_key, set()).add(_idea['article_id'])
_instrument_facets = sorted(
    (
        (key, count, len(_instrument_articles.get(key, set())))
        for key, count in _instrument_counts.items()
    ),
    key=lambda row: (-row[2], -row[1], row[0]),
)
_underlying_facets = sorted(
    (
        (_underlying_labels[key], count, len(_underlying_articles.get(key, set())))
        for key, count in _underlying_totals.items() if count >= 2
    ),
    key=lambda row: (-row[2], -row[1], row[0].casefold()),
)
desk_facets = {
    'observation_count': len(client_ideas),
    'source_note_count': len({idea['article_id'] for idea in client_ideas}),
    'outcome_count': sum(1 for idea in client_ideas if idea['outcome']),
    'instruments': [
        [key, count, source_notes]
        for key, count, source_notes in _instrument_facets
    ],
    'underlyings': [
        [label, count, source_notes]
        for label, count, source_notes in _underlying_facets
    ],
    'periods': sorted(
        {
            article_date_by_id[idea['article_id']][:4]
            for idea in client_ideas
            if idea['article_id'] in article_date_by_id
        },
        reverse=True,
    ),
}
desk_facets_json = json_for_script(desk_facets)

# Rate conditions are read from the tracked official curve rather than fetched
# while rendering, so the release stays byte-reproducible. The bands are
# quantiles of the observation dates themselves: cut against the whole article
# record they would drop nine in ten observations into one bucket and describe
# nothing.
observation_days = [
    article_date_by_id[idea['article_id']]
    for idea in client_ideas
    if idea['article_id'] in article_date_by_id
]
rate_context = build_rate_context(
    load_curve_dataset(ROOT / TREASURY_DATASET_NAME), observation_days,
)
rate_context_json = json_for_script(rate_context)
manager_labels_json = json_for_script(manager_labels)
manager_html = '\n'.join(manager_buttons)

manifest_path = ROOT / 'snapshot_manifest.json'
if manifest_path.exists():
    with open(manifest_path, encoding='utf-8') as handle:
        snapshot_manifest = json.load(handle)
    if not isinstance(snapshot_manifest, dict):
        raise ValueError('snapshot_manifest.json must contain an object')
else:
    checksum = hashlib.sha256()
    checksum.update((ROOT / 'articles_index.json').read_bytes())
    checksum.update(b'\0')
    checksum.update((ROOT / 'trades_extracted.json').read_bytes())
    snapshot_manifest = {
        'schema_version': 1,
        'checked_at': '',
        'latest_publication': max(
            (article['date'] for article in client_articles), default='1970-01-01'
        ),
        'article_count': len(client_articles),
        'observation_count': len(client_ideas),
        'data_checksum': checksum.hexdigest(),
        'sources': {},
    }

# The public automation contract is generated from the exact tracked master,
# including metadata-only registry records that are intentionally absent from
# the consumer terminal payload above.
related_graph = build_related_graph(catalog_index, search_index)
families_index = build_families_index(catalog_index)
share_articles = [
    {
        **article,
        'id': stable_id('a', canonical_url_identity(str(article.get('url') or ''))),
    }
    for article in catalog_index
]

site_revision = str(os.environ.get('SITE_REVISION') or 'local')
snapshot_json = json_for_script(snapshot_manifest)
revision_meta = html_lib.escape(site_revision, quote=True)
checksum_meta = html_lib.escape(
    str(snapshot_manifest.get('data_checksum') or ''), quote=True,
)
brief_archive_payload = {
    'schema_version': 1,
    'data_checksum': snapshot_manifest.get('data_checksum') or '',
    'briefs': brief_archive,
}
observation_archive_payload = {
    'schema_version': 1,
    'data_checksum': snapshot_manifest.get('data_checksum') or '',
    'observations': client_ideas,
}
brief_archive_json = json.dumps(
    brief_archive_payload, ensure_ascii=False, separators=(',', ':')
)
observation_archive_json = json.dumps(
    observation_archive_payload, ensure_ascii=False, separators=(',', ':')
)
brief_archive_sha256 = hashlib.sha256(
    brief_archive_json.encode('utf-8')
).hexdigest()
observation_archive_sha256 = hashlib.sha256(
    observation_archive_json.encode('utf-8')
).hexdigest()

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Institutional research intelligence across hedge funds, systematic strategies, derivatives, and market structure.">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta name="color-scheme" content="light dark">
<meta name="application-name" content="Navnoor Research Terminal">
<meta name="theme-color" id="theme-color" content="__LIGHT_THEME_BG__">
<meta property="og:type" content="website">
<meta property="og:locale" content="en_US">
<meta property="og:site_name" content="Navnoor Research Terminal">
<meta property="og:title" content="Navnoor Research Terminal">
<meta property="og:description" content="Source-backed institutional research dossiers with exact passages, evidence ledgers, checkpoints, and decision boundaries.">
<meta property="og:url" content="https://navnoorthapar.github.io/substack-trades/">
<meta property="og:image" content="https://navnoorthapar.github.io/substack-trades/og.jpg">
<meta property="og:image:secure_url" content="https://navnoorthapar.github.io/substack-trades/og.jpg">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Navnoor Research Terminal institutional research intelligence preview">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Navnoor Research Terminal">
<meta name="twitter:description" content="Source-backed institutional research dossiers with exact passages, evidence ledgers, checkpoints, and decision boundaries.">
<meta name="twitter:image" content="https://navnoorthapar.github.io/substack-trades/og.jpg">
<meta name="twitter:image:alt" content="Navnoor Research Terminal institutional research intelligence preview">
<link rel="canonical" href="https://navnoorthapar.github.io/substack-trades/">
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<link rel="manifest" href="site.webmanifest">
<link rel="sitemap" type="application/xml" href="sitemap.xml">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="__CSP__">
<meta name="nrt-revision" content="__REVISION__">
<meta name="nrt-article-count" content="__ARTICLE_COUNT__">
<meta name="nrt-observation-count" content="__OBSERVATION_COUNT__">
<meta name="nrt-data-checksum" content="__DATA_CHECKSUM__">
<meta name="nrt-brief-archive-sha256" content="__BRIEF_ARCHIVE_SHA256__">
<meta name="nrt-observation-archive-sha256" content="__OBSERVATION_ARCHIVE_SHA256__">
<title>Navnoor Research Terminal</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebApplication","name":"Navnoor Research Terminal","url":"https://navnoorthapar.github.io/substack-trades/","description":"Source-backed institutional research dossiers with exact passages, evidence ledgers, checkpoints, and decision boundaries.","applicationCategory":"FinanceApplication","operatingSystem":"Any","isAccessibleForFree":true,"author":{"@type":"Person","name":"Navnoor Bawa","url":"https://medium.com/@navnoorbawa"}}
</script>
<script>
(function () {
  if (window.top !== window.self) {
    document.documentElement.textContent = 'Embedding is blocked. Open Navnoor Research Terminal directly.';
    try { window.top.location.replace(window.self.location.href); } catch (_error) {}
    throw new Error('Navnoor Research Terminal cannot run inside a frame');
  }
  try {
    var themeRevision = '__THEME_REVISION__';
    var storedRevision = localStorage.getItem('nrt-theme-revision');
    var sameRevision = storedRevision === themeRevision || storedRevision === 'editorial-brief-2026-07';
    var candidate = sameRevision ? localStorage.getItem('nrt-theme') : '';
    var stored = candidate === 'light' || candidate === 'dark' ? candidate : '';
    var theme = stored || 'light';
    localStorage.setItem('nrt-theme-revision',themeRevision);
    document.documentElement.dataset.theme = theme;
    document.getElementById('theme-color').content = theme === 'light' ? '__LIGHT_THEME_BG__' : '__DARK_THEME_BG__';
  } catch (_error) {
    document.documentElement.dataset.theme = 'light';
    document.getElementById('theme-color').content = '__LIGHT_THEME_BG__';
  }
})();
</script>
<style>
*,*::before,*::after{box-sizing:border-box}
html,body,h1,h2,h3,p,ul,ol,dl,dd,figure{margin:0}
button,input,select,textarea{font:inherit}
:root{
  --header-h:72px;
  --kpi-h:46px;
  --brief-compact-nav-h:89px;
  --rail-w:224px;
  --inspector-w:384px;
  --bg:__DARK_THEME_BG__;
  --surface-1:#0b0d0e;
  --surface-2:#101315;
  --surface-3:#171b1e;
  --surface-raised:#1d2226;
  --line:#2c3439;
  --line-strong:#4e5b62;
  --control-line:#6c7b83;
  --control-line-hover:#91a1aa;
  --text:#f4f6f7;
  --text-secondary:#c6ced3;
  --text-muted:#98a4ab;
  --accent:#62c8ff;
  --accent-strong:#d88900;
  --accent-hover:#f0a000;
  --accent-active:#bd7800;
  --accent-soft:#102936;
  --on-accent:#080909;
  --positive:#29d391;
  --positive-soft:#0c2a20;
  --positive-line:#238f65;
  --negative:#ff756e;
  --negative-soft:#321514;
  --negative-line:#a84542;
  --warning:#ffbe3d;
  --warning-soft:#2d220b;
  --warning-line:#9a6e11;
  --relative:#c39bff;
  --relative-soft:#251a38;
  --relative-line:#7053a5;
  --long-short:#e48bd2;
  --long-short-soft:#30172d;
  --long-short-line:#8f4f83;
  --long:#4cc9f0;
  --long-soft:#0c2831;
  --long-line:#2d809a;
  --short:#ff7aa2;
  --short-soft:#321824;
  --short-line:#a84464;
  --quant:#28d7e5;
  --quant-soft:#0b292c;
  --quant-line:#18818a;
  --number:#ffc857;
  --number-soft:#2d230d;
  --number-line:#9b7424;
  --checkpoint:#b7a7ff;
  --source-substack:#ff8b48;
  --source-medium:#a6b2b8;
  --brick:#ffb000;
  --brick-soft:#2a210d;
  --brick-line:#8f6500;
  --ochre:#ffbe3d;
  --ochre-soft:#2d220b;
  --green:#29d391;
  --green-soft:#0c2a20;
  --focus:#54c8ff;
  --selected:#2a210d;
  --selected-line:#ffb000;
  --selection-bg:#775600;
  --selection-text:#ffffff;
  --backdrop:rgba(0,0,0,.84);
  --shadow:0 24px 68px rgba(0,0,0,.72);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
  --serif:"Helvetica Neue",Arial,Helvetica,sans-serif;
  --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:__LIGHT_THEME_BG__;
  --surface-1:#fffaf4;
  --surface-2:#f7eee5;
  --surface-3:#e9ddd1;
  --surface-raised:#fffdf9;
  --line:#d4c6b8;
  --line-strong:#9b8775;
  --control-line:#6a5a4f;
  --control-line-hover:#443931;
  --text:#28221f;
  --text-secondary:#514741;
  --text-muted:#665a52;
  --accent:#075c63;
  --accent-strong:#075c63;
  --accent-hover:#034b52;
  --accent-active:#003c43;
  --accent-soft:#dcebea;
  --on-accent:#ffffff;
  --positive:#28604b;
  --positive-soft:#e1eee8;
  --positive-line:#7fa393;
  --negative:#96342d;
  --negative-soft:#f4e3df;
  --negative-line:#bd7d75;
  --warning:#78500c;
  --warning-soft:#f3ead5;
  --warning-line:#a98b54;
  --relative:#5c4f86;
  --relative-soft:#eae6f2;
  --relative-line:#968daf;
  --long-short:#6d3f75;
  --long-short-soft:#eee3f0;
  --long-short-line:#a58aa9;
  --long:#285e73;
  --long-soft:#e1edf0;
  --long-line:#789eaa;
  --short:#7b4157;
  --short-soft:#f1e4e9;
  --short-line:#aa8292;
  --quant:#075f69;
  --quant-soft:#dfecee;
  --quant-line:#739aa0;
  --number:#6f5016;
  --number-soft:#f2ead7;
  --number-line:#aa9360;
  --checkpoint:#4d5867;
  --source-substack:#963d21;
  --source-medium:#50595d;
  --brick:#8b2f3d;
  --brick-soft:#f1dfe3;
  --brick-line:#b87a84;
  --ochre:#78500c;
  --ochre-soft:#f3ead5;
  --green:#28604b;
  --green-soft:#e1eee8;
  --focus:#005f73;
  --selected:#d8e5e3;
  --selected-line:#075c63;
  --selection-bg:#bdd8d1;
  --selection-text:#28221f;
  --backdrop:rgba(40,34,31,.38);
  --shadow:0 18px 46px rgba(67,47,36,.18);
  --sans:"Helvetica Neue",Arial,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --serif:"Iowan Old Style",IowanOldStyle,Baskerville,Georgia,"Times New Roman",Times,serif;
}
html[data-theme="dark"]{color-scheme:dark}
html{background:var(--bg);color:var(--text);font:13px/1.45 var(--sans);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;accent-color:var(--accent-strong)}
::selection{background:var(--selection-bg);color:var(--selection-text)}
body{height:100vh;height:100dvh;overflow:hidden;background:var(--bg)}
button,a,input,select,textarea{outline:none}
button{color:inherit}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,[tabindex]:focus-visible{
  outline:2px solid var(--focus);outline-offset:2px
}
button:disabled{background:var(--surface-2);border-color:var(--line);color:var(--text-muted);opacity:.62;cursor:not-allowed}
a{color:var(--accent)}
.skip-link{
  position:fixed;left:12px;top:8px;z-index:1000;padding:9px 12px;
  background:var(--accent-strong);color:var(--on-accent);border-radius:4px;text-decoration:none;
  transform:translateY(-150%)
}
.skip-link:focus{transform:translateY(0)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.print-only{display:none}

/* Global command header */
.app-header{
  height:var(--header-h);display:grid;grid-template-columns:minmax(250px,330px) minmax(300px,680px) minmax(340px,1fr);
  align-items:center;gap:28px;padding:0 22px;border-bottom:1px solid var(--line);
  background:var(--surface-1);position:relative;z-index:50
}
.brand{display:flex;align-items:center;gap:12px;min-width:220px}
.brand-mark{
  width:40px;height:40px;display:grid;place-items:center;border:1px solid var(--line-strong);
  border-radius:0;color:var(--accent);font:700 10px var(--mono);letter-spacing:.08em;background:transparent
}
.brand-name{font:600 18px/1.05 var(--serif);letter-spacing:-.012em;white-space:nowrap}
.brand-sub{font:9px var(--mono);color:var(--text-muted);letter-spacing:.12em;text-transform:uppercase;margin-top:5px}
.global-search{position:relative;width:100%}
.search-glyph{position:absolute;left:2px;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:15px;pointer-events:none}
#search{
  width:100%;height:42px;border:0;border-bottom:1px solid var(--line-strong);border-radius:0;
  background:transparent;color:var(--text);padding:0 48px 0 26px;font-size:13px
}
#search::placeholder{color:var(--text-muted)}
#search:focus{border-color:var(--control-line);background:transparent}
#search:focus-visible{outline:0;box-shadow:inset 0 -2px var(--focus)}
.search-key{position:absolute;right:4px;top:50%;transform:translateY(-50%);font:10px var(--mono);color:var(--text-muted);border:1px solid var(--line-strong);border-radius:0;padding:2px 6px}
.header-right{display:flex;align-items:center;justify-content:flex-end;gap:8px;min-width:0}
.freshness{display:flex;align-items:center;gap:7px;max-width:350px;overflow:hidden;color:var(--text-secondary);font:10px var(--mono);white-space:nowrap;margin-right:4px}
.freshness>span:last-child{overflow:hidden;text-overflow:ellipsis}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--text-muted);box-shadow:0 0 0 3px var(--surface-3)}
.status-dot.fresh{background:var(--positive);box-shadow:0 0 0 3px var(--positive-soft)}
.status-dot.degraded{border-radius:1px;background:var(--warning);box-shadow:0 0 0 3px var(--warning-soft);transform:rotate(45deg)}
.status-dot.stale{border-radius:1px;background:var(--negative);box-shadow:0 0 0 3px var(--negative-soft)}
.freshness-separator{color:var(--text-muted)}
.utility-button{
  min-height:36px;padding:0 11px;border:1px solid var(--control-line);border-radius:0;
  background:transparent;color:var(--text-secondary);cursor:pointer
}
.utility-button:hover{background:var(--surface-3);color:var(--text);border-color:var(--control-line-hover)}
.utility-key{margin-left:6px;font:9.5px var(--mono);color:var(--text-muted);letter-spacing:.04em}
.utility-button:hover .utility-key{color:var(--text-secondary)}
#mobile-filter-button{display:none}

/* Compact global metrics */
.kpi-strip{
  height:var(--kpi-h);display:flex;align-items:stretch;border-bottom:1px solid var(--line);
  background:var(--bg);overflow-x:auto;scrollbar-width:none
}
.kpi-strip::-webkit-scrollbar{display:none}
.kpi-item{
  min-width:150px;flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:0 16px;
  border:0;border-right:1px solid var(--line);background:transparent;text-align:left;
  color:var(--text-secondary)
}
.kpi-provenance{min-width:360px}
.kpi-provenance .kpi-value{font-size:13px}
button.kpi-item{cursor:pointer}
button.kpi-item:hover{background:var(--surface-3);box-shadow:inset 0 -2px var(--selected-line)}
.kpi-value{font:600 15px var(--mono);color:var(--text)}
.kpi-label{font:10px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);line-height:1.3}
.kpi-detail{font:10px var(--mono);color:var(--text-secondary);white-space:nowrap}

/* Three-pane workstation */
.workspace{
  height:calc(100vh - var(--header-h) - var(--kpi-h));
  height:calc(100dvh - var(--header-h) - var(--kpi-h));
  display:grid;grid-template-columns:var(--rail-w) minmax(560px,1fr) var(--inspector-w);
  overflow:hidden
}
.filter-rail{
  min-width:0;border-right:1px solid var(--line);background:var(--surface-1);
  overflow-y:auto;overscroll-behavior:contain;padding-bottom:20px;scrollbar-width:thin
}
.rail-header{
  position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;
  height:44px;padding:0 12px;border-bottom:1px solid var(--line);background:var(--surface-1)
}
.rail-actions{display:flex;align-items:center;gap:2px}
#filter-close{display:none}
.rail-title{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.1em}
.text-button{border:0;background:transparent;color:var(--accent);font-size:11px;cursor:pointer;padding:7px}
.text-button:hover{text-decoration:underline}
.filter-group{padding:14px 10px;border-bottom:1px solid var(--line)}
.filter-heading{display:flex;align-items:center;justify-content:space-between;margin:0 2px 8px}
.filter-heading h2{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted)}
.facet-list{display:grid;gap:3px}
.facet-list.two-column{grid-template-columns:1fr 1fr}
.facet-option,.facet-clear{
  width:100%;min-height:30px;border:1px solid transparent;border-radius:3px;background:transparent;
  display:flex;align-items:center;gap:7px;padding:4px 7px;color:var(--text-secondary);
  cursor:pointer;text-align:left;font-size:11px
}
.facet-option:hover,.facet-clear:hover{background:var(--surface-3);color:var(--text)}
.facet-option.active,.facet-clear.active{
  background:var(--selected);border-color:transparent;color:var(--text);
  box-shadow:inset 2px 0 var(--selected-line)
}
.facet-option::before,.facet-clear::before{
  content:"";width:7px;height:7px;border:1px solid var(--control-line);border-radius:2px;flex:0 0 auto
}
.facet-option.active::before{background:var(--accent);border-color:var(--accent)}
.facet-clear::before{border-radius:50%}
.facet-clear.active::before{border:2px solid var(--accent);background:transparent}
.facet-count{margin-left:auto;color:var(--text-muted);font:10px var(--mono)}
.facet-option.active .facet-count{color:var(--text-secondary)}
.date-options{display:grid;grid-template-columns:repeat(4,1fr);gap:3px}
.date-option{
  min-height:30px;border:1px solid var(--control-line);background:var(--surface-2);color:var(--text-secondary);
  border-radius:3px;font:10px var(--mono);cursor:pointer
}
.date-option:hover{border-color:var(--control-line-hover);color:var(--text)}
.date-option.active{background:var(--selected);border-color:var(--selected-line);color:var(--text);box-shadow:inset 0 -2px var(--selected-line)}
.manager-search{
  width:100%;height:31px;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-2);
  color:var(--text);padding:0 8px;margin-bottom:6px;font-size:11px
}
.manager-options{max-height:188px;overflow:auto;scrollbar-width:thin}
.manager-option[hidden]{display:none}
.filter-note{font-size:10px;color:var(--text-muted);margin:8px 2px 0}
.rail-disclaimer{padding:14px 12px;color:var(--text-muted);font-size:10px;line-height:1.55}
.rail-disclaimer strong{color:var(--text-secondary);font-weight:600}
.research-only-filter{display:none}
body[data-view="research"] .research-only-filter{display:block}
.queue-only-filter{display:none}
body[data-view="queue"] .queue-only-filter{display:block}
.preset-list{display:grid;gap:4px}
.preset-button{
  width:100%;min-height:32px;border:1px solid var(--control-line);border-radius:3px;
  background:var(--surface-2);color:var(--text-secondary);padding:5px 8px;
  text-align:left;cursor:pointer;font-size:10.5px
}
.preset-button:hover{background:var(--surface-3);color:var(--text);border-color:var(--control-line-hover)}

.main-panel{min-width:0;background:var(--bg);display:flex;flex-direction:column;overflow:hidden}
/* One row on the desk. Wrapping would win over shrinking, so the bar holds a
   single line and the result summary yields width instead of pushing controls
   onto a second row; narrow viewports restore wrapping below. */
.command-bar{
  min-height:50px;display:flex;align-items:center;gap:10px;padding:7px 12px;
  border-bottom:1px solid var(--line);background:var(--surface-1);flex-wrap:nowrap
}
.view-tabs{display:flex;align-items:center;background:var(--surface-2);border:1px solid var(--control-line);border-radius:4px;padding:2px}
.view-tab{
  min-height:30px;border:0;border-radius:3px;background:transparent;color:var(--text-secondary);
  padding:0 11px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap
}
.view-tab.active{background:var(--surface-raised);color:var(--text);box-shadow:inset 0 -2px var(--selected-line)}
/* The summary yields width before the bar wraps, so the toolbar keeps one
   row across themes even though the dark theme sets a wider mono face. */
.result-summary{
  font:10px var(--mono);color:var(--text-muted);white-space:nowrap;
  min-width:0;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis
}
.command-spacer{flex:1 1 0;min-width:0}
.select-control{
  height:32px;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-2);
  color:var(--text-secondary);padding:0 28px 0 8px;font-size:11px;cursor:pointer
}
.select-control:hover{border-color:var(--control-line-hover);color:var(--text)}
.command-button{
  min-height:32px;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-2);
  color:var(--text-secondary);padding:0 9px;cursor:pointer;font-size:10px;white-space:nowrap
}
.command-button:hover{background:var(--surface-3);border-color:var(--control-line-hover);color:var(--text)}
.command-button.active{background:var(--selected);border-color:var(--selected-line);color:var(--text);box-shadow:inset 0 -2px var(--selected-line)}
.command-button:disabled{cursor:not-allowed;opacity:.5;background:var(--surface-1);color:var(--text-muted)}
body[data-view="briefing"] .table-command{display:none}
.queue-command{display:none}
body[data-view="queue"] .queue-command{display:inline-flex}
.storage-alert{
  display:flex;align-items:center;gap:9px;padding:8px 12px;border-bottom:1px solid var(--warning-line);
  background:var(--warning-soft);color:var(--warning);font-size:10.5px;line-height:1.45
}
.storage-alert[hidden]{display:none}
.storage-alert span{flex:1;min-width:0}
.storage-alert .command-button{border-color:var(--warning-line);color:var(--warning);background:transparent}
.active-filters{
  min-height:35px;display:flex;align-items:center;gap:6px;padding:5px 12px;border-bottom:1px solid var(--line);
  background:var(--surface-2);overflow-x:auto
}
.active-filters.empty{display:none}
.active-label{font:10px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-right:3px}
.filter-chip{
  display:inline-flex;align-items:center;gap:5px;min-height:24px;border:1px solid var(--control-line);
  background:var(--surface-1);color:var(--text-secondary);border-radius:3px;padding:0 7px;
  font-size:10px;white-space:nowrap;cursor:pointer
}
.filter-chip:hover{border-color:var(--control-line-hover);color:var(--text)}
.chip-x{color:var(--text-muted)}

.context-bar{
  min-height:42px;display:grid;grid-template-columns:auto minmax(160px,1fr) auto;align-items:center;
  gap:14px;padding:6px 12px;border-bottom:1px solid var(--line);background:var(--surface-2)
}
.context-metrics{display:flex;align-items:center;gap:15px;white-space:nowrap}
.context-metric{display:flex;align-items:baseline;gap:5px}
.context-metric b{font:600 12px var(--mono);color:var(--text)}
.context-metric span{font:10px var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)}
.direction-mix{height:6px;display:flex;overflow:hidden;border-radius:2px;background:var(--surface-3)}
.mix-segment{height:100%;min-width:0;box-shadow:inset -1px 0 var(--bg)}
.mix-long{background:var(--long)}
.mix-short{background:var(--short);background-image:repeating-linear-gradient(135deg,transparent 0 2px,var(--surface-3) 2px 4px)}
.mix-arb{background:var(--relative);background-image:repeating-linear-gradient(90deg,transparent 0 3px,var(--surface-3) 3px 5px)}
.mix-ls{background:var(--long-short);background-image:repeating-linear-gradient(45deg,transparent 0 1px,var(--surface-3) 1px 3px)}
.mix-unspecified{background:var(--text-muted);background-image:repeating-linear-gradient(90deg,transparent 0 1px,var(--surface-3) 1px 4px)}
.mix-legend{font:10px var(--mono);color:var(--text-muted);white-space:nowrap}

/* Evidence-bound research structuring and diligence desk */
.structure-shell{display:none;flex:1 1 auto;min-height:0;overflow:auto;padding:20px 22px 44px;background:var(--bg)}
body[data-view="structure"] .structure-shell{display:block}
body[data-view="structure"] .kpi-strip,
body[data-view="structure"] .table-shell,
body[data-view="structure"] .context-bar,
body[data-view="structure"] .active-filters,
body[data-view="structure"] .orphaned-queue,
body[data-view="structure"] .table-command,
body[data-view="structure"] .command-button[data-action="inspector"]{display:none}
body[data-view="structure"] .workspace{grid-template-columns:minmax(0,1fr)}
body[data-view="structure"] .filter-rail,body[data-view="structure"] .inspector{display:none}
body[data-view="structure"] .main-panel{grid-column:1/-1}
body[data-view="structure"] .global-search{display:none}
body[data-view="structure"] .app-header{grid-template-columns:minmax(250px,330px) minmax(0,1fr)}
body[data-view="structure"] .header-right{grid-column:2}
.structure-wrap{max-width:1480px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
.structure-head{padding:0}
.structure-title-row{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}
.structure-kicker{font:650 9px var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--accent);margin-bottom:4px}
.structure-head h2{font:650 24px/1.12 var(--sans);letter-spacing:-.025em}
.structure-boundary{max-width:520px;text-align:right;font:10px/1.5 var(--mono);color:var(--text-muted)}
.structure-lede{color:var(--text-secondary);font-size:12px;line-height:1.5;margin:5px 0 11px;max-width:82ch}
.structure-search-row{display:grid;grid-template-columns:minmax(260px,1fr) auto;gap:8px;max-width:900px}
.structure-search input{width:100%;min-height:44px;padding:0 13px;border-radius:0;
  border:1px solid var(--control-line);background:var(--surface-1);color:var(--text);
  font-size:14px;line-height:1.3;box-shadow:none}
.structure-search input::placeholder{color:var(--text-muted)}
.structure-search input:focus-visible{outline:2px solid var(--accent);outline-offset:2px;
  border-color:var(--accent)}
.structure-search-help{display:flex;align-items:center;border:1px solid var(--line);padding:0 12px;
  color:var(--text-muted);font:10px/1.35 var(--mono);background:var(--surface-2)}
.desk-starts{display:flex;align-items:center;flex-wrap:wrap;gap:5px;margin-top:8px}
.desk-starts-label{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;
  color:var(--text-muted);margin-right:2px}
.desk-start{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:0 9px;border-radius:0;
  border:1px solid var(--line-strong);background:var(--surface-2);color:var(--text);
  font-size:11px;cursor:pointer}
.desk-start:hover{border-color:var(--accent);color:var(--text)}
.desk-start.active{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.desk-start-count{font:600 9px var(--mono);color:var(--text-muted)}
.desk-start.active .desk-start-count{color:var(--on-accent)}
.desk-start.clear{background:transparent;color:var(--text-muted)}
.desk-refine{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.desk-refine-toggle{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:0 10px;
  border-radius:0;border:1px solid var(--control-line);background:var(--surface-2);
  color:var(--text);font-size:11px;cursor:pointer}
.desk-refine-toggle:hover{border-color:var(--control-line-hover)}
.desk-refine-toggle[aria-expanded="true"]{background:var(--accent-soft);border-color:var(--accent)}
.desk-refine-count{font:700 10px var(--mono);background:var(--accent);color:var(--on-accent);
  border-radius:999px;padding:1px 6px}
.desk-refine-hint{font-size:12px;color:var(--text-muted)}
.desk-active-filters{display:flex;flex-wrap:wrap;gap:6px}
.desk-active-filter{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:0 9px;
  border-radius:0;border:1px solid var(--accent);background:var(--accent-soft);
  color:var(--text);font-size:11px;cursor:pointer}
.desk-active-filter span{color:var(--text-muted);font-size:13px;line-height:1}
.desk-active-filter:hover{border-color:var(--negative);color:var(--negative)}
.desk-active-filter:hover span{color:var(--negative)}
.structure-controls{display:flex;flex-direction:column;gap:8px;padding:11px 12px;
  border:1px solid var(--line);border-radius:0;background:var(--surface-1)}
.structure-controls[hidden]{display:none}
.structure-control{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.structure-control-label{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;
  color:var(--text-muted);min-width:150px}
.structure-chips{display:flex;flex-wrap:wrap;gap:6px}
.structure-chip{display:inline-flex;align-items:center;gap:6px;min-height:32px;padding:0 8px;border-radius:0;
  border:1px solid var(--line-strong);background:var(--surface-2);color:var(--text-secondary);
  font-size:11px;cursor:pointer}
.structure-chip:hover{border-color:var(--control-line-hover);color:var(--text)}
.structure-chip.active{background:var(--accent-soft);border-color:var(--accent);color:var(--text)}
.structure-chip-count{font:600 10px var(--mono);color:var(--text-muted)}
.structure-chip.active .structure-chip-count{color:var(--accent)}
.desk-start-state{display:grid;grid-template-columns:minmax(260px,1.2fr) minmax(320px,1fr);
  gap:1px;border:1px solid var(--line);background:var(--line);margin-top:2px}
.desk-start-copy,.desk-start-steps{background:var(--surface-1);padding:20px}
.desk-start-copy h3{font-size:17px;line-height:1.25;margin-bottom:7px}
.desk-start-copy p{font-size:12px;line-height:1.6;color:var(--text-secondary);max-width:68ch}
.desk-start-steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line);padding:0}
.desk-start-step{background:var(--surface-2);padding:16px 13px}
.desk-start-step b{display:block;font:650 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.desk-start-step span{display:block;margin-top:6px;font-size:10.5px;line-height:1.45;color:var(--text-muted)}
.desk-note{border:1px solid var(--line-strong);border-radius:0;background:var(--surface-1);padding:0;box-shadow:none}
.desk-anchors{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:1px;background:var(--line);
  padding:0;margin:0;border-bottom:1px solid var(--line)}
.desk-anchor{display:flex;flex-direction:column;gap:3px}
.desk-anchor{padding:11px 13px;background:var(--surface-2)}
.desk-anchor b{font:650 20px/1 var(--mono);letter-spacing:-.03em;
  color:var(--text);font-variant-numeric:tabular-nums}
.desk-anchor span{font:500 9px var(--mono);text-transform:uppercase;letter-spacing:.07em;
  color:var(--text-muted)}
.desk-note-head{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:10px 12px;border-bottom:1px solid var(--line);background:var(--surface-2)}
.desk-note-head h3{font:650 11px var(--mono);letter-spacing:.07em;text-transform:uppercase}
.desk-note-actions{display:flex;gap:8px;flex-wrap:wrap}
.desk-note-actions .secondary-action:first-child{background:var(--accent);
  border-color:var(--accent);color:var(--on-accent);font-weight:600}
.desk-note-actions .secondary-action:first-child:hover{background:var(--accent-hover);
  border-color:var(--accent-hover)}
.desk-packet-anchor{display:flex;align-items:center;gap:9px;margin:0;padding:9px 12px;border-bottom:1px solid var(--line);font-size:11px;color:var(--text-secondary)}
.desk-packet-anchor strong{font:650 9.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--positive)}
.desk-note dl{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;margin:0;background:var(--line)}
.desk-note dl>div{background:var(--surface-1);padding:12px 13px}
.desk-note dt{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;
  color:var(--accent);margin-bottom:3px}
.desk-note dd{font-size:11px;line-height:1.5;color:var(--text-secondary);margin:0}
.structure-disclosure{border:1px solid var(--line);border-left:3px solid var(--warning);
  background:var(--surface-1);border-radius:0;padding:10px 12px;font-size:10.5px;color:var(--text-muted);line-height:1.5}
.structure-grid{display:grid;grid-template-columns:minmax(0,2.3fr) minmax(270px,.7fr);gap:12px;align-items:start}
.structure-panel{border:1px solid var(--line);border-radius:0;background:var(--surface-1);padding:0;overflow:hidden}
.structure-panel-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--surface-2)}
.structure-panel h3{font:650 11px var(--mono);letter-spacing:.07em;text-transform:uppercase;margin:0}
.structure-panel h4{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;
  color:var(--text-muted);margin:0;padding:10px 12px 5px}
.structure-summary{font-size:10.5px;color:var(--text-muted);line-height:1.45}
.structure-none,.structure-note{font-size:10.5px;color:var(--text-muted);line-height:1.5;padding:12px}
.structure-bars{display:flex;flex-direction:column;gap:5px;padding:0 12px 10px}
.structure-bars li{display:grid;grid-template-columns:minmax(92px,1.2fr) minmax(0,2fr) 68px;align-items:center;gap:8px;font-size:10.5px}
.structure-bar-label{color:var(--text-secondary);overflow-wrap:anywhere}
.structure-bar{height:7px;border-radius:4px;background:var(--surface-3);overflow:hidden}
.structure-bar i{display:block;height:100%;background:var(--accent);border-radius:4px}
.structure-bar-value{font:600 11px var(--mono);color:var(--text-muted);text-align:right}
.structure-managers{font-size:10.5px;color:var(--text-secondary);line-height:1.55;padding:0 12px 11px}
.structure-comparables{min-width:0}
.structure-matrix{display:grid;min-width:820px}
.structure-matrix-head,.structure-evidence-row{display:grid;grid-template-columns:86px minmax(210px,1.5fr) 118px 150px 128px minmax(160px,1fr);align-items:stretch}
.structure-matrix-head{min-height:34px;background:var(--surface-2);border-bottom:1px solid var(--line-strong);font:650 10px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted)}
.structure-matrix-head>span,.structure-evidence-cell{min-width:0;padding:8px 9px;border-right:1px solid var(--line)}
.structure-matrix-head>span:last-child,.structure-evidence-cell:last-child{border-right:0}
.structure-evidence-group{border-bottom:1px solid var(--line)}
.structure-evidence-group:last-child{border-bottom:0}
.structure-evidence-row{font-size:11px;color:var(--text-secondary)}
.structure-evidence-row:hover{background:var(--surface-2)}
.structure-evidence-cell{line-height:1.4}
.structure-evidence-cell strong{display:block;color:var(--text);font-size:11px;line-height:1.35}
.structure-evidence-cell small{display:block;margin-top:3px;color:var(--text-muted);font:10px/1.4 var(--mono)}
.structure-anchor{width:100%;min-height:34px;border:1px solid var(--control-line);background:var(--surface-1);color:var(--text-secondary);font:9.5px var(--mono);cursor:pointer}
.structure-anchor:hover{border-color:var(--control-line-hover);color:var(--text)}
.structure-anchor.active{border-color:var(--accent);background:var(--accent);color:var(--on-accent);font-weight:700}
.structure-anchor-status{display:flex;min-height:34px;align-items:center;justify-content:center;border:1px dashed var(--line-strong);padding:4px;text-align:center;font:650 10px/1.3 var(--mono);text-transform:uppercase;color:var(--text-muted)}
.structure-anchor-status.active{border-style:solid;border-color:var(--positive-line);background:var(--positive-soft);color:var(--positive)}
.structure-tier{display:inline-flex;min-height:24px;align-items:center;padding:0 6px;border:1px solid var(--line-strong);font:650 10px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--text-secondary)}
.structure-tier.exact{border-color:var(--positive-line);background:var(--positive-soft);color:var(--positive)}
.structure-tier.subject{border-color:var(--control-line);background:var(--accent-soft);color:var(--accent)}
.structure-tier.related{border-color:var(--number-line);background:var(--number-soft);color:var(--number)}
.structure-evidence-flags{display:flex;gap:3px;flex-wrap:wrap;margin-top:5px}
.structure-evidence-flag{display:inline-flex;min-height:22px;align-items:center;padding:0 5px;border:1px solid var(--line);font:10px var(--mono);color:var(--text-muted)}
.structure-evidence-flag.on{border-color:var(--quant-line);background:var(--quant-soft);color:var(--quant)}
.structure-row-detail{border-top:1px solid var(--line);background:var(--surface-2)}
.structure-row-detail-cell{display:block}
.structure-row-detail summary{min-height:38px;display:flex;align-items:center;gap:7px;padding:0 10px;cursor:pointer;color:var(--accent);font-size:11px}
.structure-row-detail summary::marker{content:""}
.structure-row-detail summary::before{content:"+";font:700 12px var(--mono)}
.structure-row-detail details[open] summary::before{content:"−"}
.structure-passages{display:grid;gap:1px;background:var(--line);border-top:1px solid var(--line)}
.structure-passage-item{background:var(--surface-1);padding:10px 12px}
.structure-passage-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;font:650 9px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--text-muted)}
.structure-use-passage{min-height:34px;padding:0 10px}
.structure-use-passage.active{border-color:var(--positive-line);background:var(--positive-soft);color:var(--positive)}
.structure-passage-item p{font-size:11px;line-height:1.55;color:var(--text-secondary)}
.structure-passage-meta{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px;font:10px var(--mono);color:var(--text-muted)}
.structure-passage-warning{color:var(--warning);font-weight:650}
.structure-thesis,.structure-outcome{font-size:12px;line-height:1.5;margin-top:8px;
  padding:7px 9px;border-radius:5px;background:var(--surface-3);color:var(--text-secondary)}
.structure-thesis span,.structure-outcome span{display:block;font:600 10px var(--mono);
  text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);margin-bottom:3px}
.structure-outcome{background:var(--positive-soft);border:1px solid var(--positive-line)}
.structure-outcome.none{background:var(--surface-3);border:1px dashed var(--line-strong);color:var(--text-muted)}
.structure-rates{margin-top:8px;padding:7px 9px;border-radius:0;background:var(--surface-3);
  border-left:2px solid var(--accent-strong);font:500 12px var(--mono);color:var(--text)}
.structure-rates > span{display:block;font:600 10px var(--mono);text-transform:uppercase;
  letter-spacing:.06em;color:var(--text-muted);margin-bottom:3px}
.structure-rates em{display:block;font-style:normal;font:500 10px var(--mono);
  color:var(--text-muted);margin-top:3px}
.structure-followup{margin-top:8px;padding:8px 10px;border-radius:0;background:var(--surface-3);
  border-left:2px solid var(--accent)}
.structure-followup > span{display:block;font:600 10px var(--mono);text-transform:uppercase;
  letter-spacing:.06em;color:var(--text-muted);margin-bottom:5px}
.structure-followup ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
.structure-followup li{display:flex;gap:8px;align-items:baseline;font-size:12px;line-height:1.45}
.structure-followup time{font:600 11px var(--mono);color:var(--text-muted);flex:none;min-width:66px}
.structure-followup a{color:var(--accent);overflow-wrap:anywhere}
.structure-followup em{font-style:normal;font:500 10px var(--mono);color:var(--text-muted);flex:none}
.structure-followup-more{font-size:11px;color:var(--text-muted);margin-top:5px}
.structure-followup.none{font-size:12px;color:var(--text-muted);margin-top:8px;padding:7px 9px;
  border-radius:5px;background:var(--surface-3);border:1px dashed var(--line-strong);border-left:1px dashed var(--line-strong)}
.structure-source{display:inline-flex;min-height:28px;align-items:center;color:var(--accent);white-space:nowrap}
.structure-more{min-height:38px;margin:10px 12px}
.structure-related{border:1px solid var(--line);background:var(--surface-1)}
.structure-related summary{min-height:44px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 12px;cursor:pointer;font:650 10px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--text-secondary)}
.structure-related summary::marker{content:""}
.structure-related-list{display:grid;border-top:1px solid var(--line)}
.structure-related-row{display:grid;grid-template-columns:88px minmax(0,1fr) auto;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);font-size:10.5px;color:var(--text-secondary)}
.structure-related-row:last-child{border-bottom:0}
.structure-related-row a{color:var(--accent)}
.structure-thin{color:var(--warning);font-weight:650}
.structure-macro-note{margin:10px 12px;padding:9px;border:1px solid var(--warning-line);background:var(--warning-soft);font-size:10px;line-height:1.5;color:var(--warning)}
@media (max-width:1080px){
  .structure-grid{grid-template-columns:minmax(0,1fr)}
  .structure-control-label{min-width:100%}
  .desk-note dl{grid-template-columns:repeat(2,minmax(0,1fr))}
  .structure-comparables{overflow-x:auto}
}
@media (max-width:759px){
  body[data-view="structure"]{--header-h:52px}
  body[data-view="structure"] .app-header{height:52px;grid-template-columns:auto minmax(0,1fr);grid-template-rows:52px;padding:0 8px}
  body[data-view="structure"] .global-search{display:none}
  body[data-view="structure"] .header-right{grid-column:2;grid-row:1}
  body[data-view="structure"] .brand{grid-column:1;grid-row:1}
  body[data-view="structure"] .command-bar{padding:0 8px 7px;gap:5px}
  body[data-view="structure"] .view-tabs{box-shadow:inset -20px 0 14px -16px var(--text-muted)}
  body[data-view="structure"] .result-summary,body[data-view="structure"] .command-button[data-action="export"]{display:none}
  .structure-shell{padding:15px 12px 32px}
  .structure-wrap{gap:11px}
  .structure-title-row{display:block}
  .structure-boundary{margin-top:6px;text-align:left}
  .structure-head h2{font-size:22px}
  .structure-lede{font-size:12px}
  .structure-search-row{grid-template-columns:1fr}
  .structure-search-help{min-height:38px}
  .desk-start{min-height:44px;padding:0 11px}
  .desk-start-state{grid-template-columns:1fr}
  .desk-start-copy{padding:16px}
  .desk-start-steps{grid-template-columns:1fr}
  .desk-start-step{padding:12px 14px}
  .desk-note-head{align-items:flex-start;display:block}
  .desk-note-actions{margin-top:9px;display:grid;grid-template-columns:1fr}
  .desk-note-actions .secondary-action{width:100%;min-height:44px}
  .desk-anchors,.desk-note dl{grid-template-columns:1fr 1fr}
  .desk-refine-toggle,.desk-active-filter,.structure-chip{min-height:44px}
  .structure-controls{padding:10px}
  .structure-chips{display:grid;grid-template-columns:1fr 1fr;width:100%}
  .structure-chip{white-space:normal;text-align:left;justify-content:space-between}
  .structure-matrix{min-width:0;display:block}
  .structure-matrix-head{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%)}
  .structure-evidence-row{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--line-strong)}
  .structure-evidence-cell{border-right:0;border-bottom:1px solid var(--line);padding:10px}
  .structure-evidence-cell:nth-child(2),.structure-evidence-cell:last-of-type{grid-column:1/-1}
  .structure-anchor{min-height:44px}
  .structure-anchor-status,.structure-use-passage{min-height:44px}
  .structure-source{min-height:44px}
  .structure-passage-head{align-items:stretch;flex-direction:column}
  .structure-row-detail{grid-column:1/-1}
  .structure-row-detail summary{min-height:44px}
  .structure-related-row{grid-template-columns:1fr}
  .structure-related-row a{display:inline-flex;min-height:44px;align-items:center}
}

/* Executive research brief */
.briefing-shell{display:none;flex:1 1 auto;min-height:0;overflow:auto;padding:16px;background:var(--bg)}
body[data-view="briefing"] .briefing-shell{display:block}
body[data-view="briefing"] .table-shell,body[data-view="briefing"] .context-bar{display:none}
.brief-hero{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:14px}
.brief-kicker{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:5px}
.brief-hero h2{font-size:20px;line-height:1.25;letter-spacing:-.015em}
.brief-hero p{max-width:760px;color:var(--text-secondary);font-size:11.5px;line-height:1.55;margin-top:5px}
.brief-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.brief-metrics{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:8px;margin-bottom:10px}
.brief-metric{border:1px solid var(--line);background:var(--surface-1);border-radius:4px;padding:12px;min-height:86px}
.brief-metric b{display:block;font:650 20px var(--mono);color:var(--text);margin-bottom:4px}
.brief-metric span{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)}
.brief-metric p{font-size:10px;color:var(--text-muted);margin-top:5px;line-height:1.4}
.brief-grid{display:grid;grid-template-columns:minmax(360px,1.45fr) minmax(280px,1fr);gap:10px}
.brief-stack{display:grid;gap:10px;align-content:start}
.brief-card{border:1px solid var(--line);background:var(--surface-1);border-radius:4px;overflow:hidden}
.brief-card-header{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--surface-2)}
.brief-card-header h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.07em;color:var(--text-secondary)}
.brief-card-header span{font:10px var(--mono);color:var(--text-muted)}
.brief-list{display:grid}
.brief-record{width:100%;display:grid;grid-template-columns:76px 88px minmax(0,1fr) auto;align-items:start;gap:8px;padding:10px 12px;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--text-secondary);text-align:left;cursor:pointer}
.brief-record:last-child{border-bottom:0}
.brief-record:hover{background:var(--surface-3)}
.brief-record-title{font-size:11.5px;line-height:1.4;color:var(--text);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.brief-record-context{font-size:10px;color:var(--text-muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brief-empty{padding:22px 12px;color:var(--text-muted);font-size:11px;text-align:center}
.coverage-list{display:grid;gap:9px;padding:12px}
.coverage-row{display:grid;grid-template-columns:112px 1fr 42px;align-items:center;gap:8px;font-size:10.5px;color:var(--text-secondary)}
.coverage-track{height:6px;background:var(--surface-3);border-radius:2px;overflow:hidden}
.coverage-fill{display:block;height:100%;background:var(--accent)}
.coverage-value{text-align:right;font:10px var(--mono);color:var(--text-muted)}
.brief-note{padding:12px;font-size:10.5px;line-height:1.55;color:var(--text-muted)}
.brief-note strong{color:var(--text-secondary)}
.owner-workflow-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:var(--line)}
.owner-workflow-item{padding:10px 12px;background:var(--surface-1)}
.owner-workflow-item b{display:block;font:650 15px var(--mono);color:var(--text)}
.owner-workflow-item span{display:block;margin-top:3px;font-size:10px;line-height:1.35;color:var(--text-muted)}
.operating-boundary{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
.operating-boundary div{padding:11px 12px;background:var(--surface-1)}
.operating-boundary h4{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-secondary);margin-bottom:5px}
.operating-boundary p{font-size:10px;line-height:1.5;color:var(--text-muted)}

/* Article intelligence brief: exact authored claims before extraction metrics */
body[data-view="briefing"] .workspace{grid-template-columns:minmax(0,1fr)}
body[data-view="briefing"] .filter-rail,body[data-view="briefing"] .inspector{display:none}
body[data-view="briefing"] .main-panel{grid-column:1/-1}
body[data-view="briefing"] #mobile-filter-button,
body[data-view="briefing"] .command-button[data-action="inspector"],
body[data-view="briefing"] .command-button[data-action="export"]{display:none}
.intel-wrap{width:min(1480px,100%);margin:0 auto;padding-bottom:24px}
.intel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:14px}
.intel-head-copy{max-width:760px}
.intel-head h2{font-size:23px;line-height:1.2;letter-spacing:-.025em}
.intel-head p{margin-top:6px;color:var(--text-secondary);font-size:12px;line-height:1.55}
.intel-lenses{display:flex;align-items:center;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.intel-lens{min-height:32px;border:1px solid var(--control-line);border-radius:4px;background:var(--surface-1);color:var(--text-secondary);padding:0 10px;cursor:pointer;font-size:10.5px;white-space:nowrap}
.intel-lens:hover{background:var(--surface-3);border-color:var(--control-line-hover);color:var(--text)}
.intel-lens.active{border-color:var(--selected-line);background:var(--accent-soft);color:var(--accent)}
.intel-grid{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(300px,.75fr);gap:12px;align-items:start}
.intel-lead,.intel-side-card,.intel-stream{border:1px solid var(--line);border-radius:6px;background:var(--surface-1);overflow:hidden}
.intel-lead{box-shadow:inset 0 2px var(--selected-line)}
.intel-lead-inner{padding:19px 21px 17px}
.intel-fact-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--surface-2)}
.intel-fact{padding:10px 13px;border-right:1px solid var(--line);min-width:0}
.intel-fact:last-child{border-right:0}
.intel-fact b{display:block;font:650 13px var(--mono);color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.intel-fact span{display:block;margin-top:2px;font:10px var(--mono);text-transform:uppercase;letter-spacing:.055em;color:var(--text-muted)}
.intel-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--text-muted);font:10px var(--mono);text-transform:uppercase;letter-spacing:.045em}
.intel-meta .source-badge{text-transform:none;letter-spacing:0}
.intel-title{font-size:28px;line-height:1.16;letter-spacing:-.025em;margin-top:12px;max-width:980px;overflow-wrap:anywhere}
.intel-claim{font-size:14px;line-height:1.52;color:var(--text-secondary);margin-top:10px;max-width:980px}
.intel-reasons{display:flex;gap:5px;flex-wrap:wrap;margin-top:13px}
.intel-reason{display:inline-flex;align-items:center;min-height:23px;border:1px solid var(--line-strong);border-radius:3px;background:var(--surface-2);color:var(--text-secondary);padding:0 7px;font:10px var(--mono)}
.intel-reason.accent{border-color:var(--quant-line);background:var(--quant-soft);color:var(--quant)}
.research-map{padding:14px 18px 15px;background:var(--surface-1);border-bottom:1px solid var(--line)}
.research-map-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:9px}
.research-map-head h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.075em;color:var(--text-secondary)}
.research-map-head p{max-width:560px;text-align:right;font-size:10px;line-height:1.4;color:var(--text-muted)}
.research-map-track{display:grid;grid-template-columns:repeat(7,minmax(92px,1fr));gap:5px;overflow-x:auto;scrollbar-width:thin;padding-bottom:2px}
.research-map-step{position:relative;min-height:58px;padding:8px;border:1px solid var(--line);border-radius:4px;background:var(--surface-2);text-align:left;color:var(--text-muted)}
button.research-map-step{border-color:var(--control-line);cursor:pointer}
button.research-map-step:hover{border-color:var(--control-line-hover);background:var(--surface-3)}
.research-map-step.captured{border-top:2px solid var(--quant-line);color:var(--text-secondary)}
.research-map-step.not-captured{border-style:dashed}
.research-map-step b{display:block;font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.045em;color:var(--text)}
.research-map-step span{display:block;margin-top:5px;font-size:10px;line-height:1.25;color:inherit}
.intel-section-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.intel-section{padding:15px 18px;background:var(--surface-1);min-height:145px}
.intel-section.full{grid-column:1/-1;min-height:0}
.intel-label{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:6px}
.intel-section h3{font-size:12px;line-height:1.35;color:var(--text);margin-bottom:7px}
.intel-passage{font-size:12.5px;line-height:1.62;color:var(--text-secondary);overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:5;overflow:hidden}
.intel-passage mark{background:var(--number-soft);color:var(--number);border-bottom:1px solid var(--number-line);border-radius:2px;padding:0 1px}
.source-tail{display:block;margin-top:7px;color:var(--text-muted);font:10px var(--mono);overflow-wrap:anywhere}
.evidence-ledger-section{border-bottom:1px solid var(--line);background:var(--surface-1)}
.evidence-ledger-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;padding:14px 18px 11px}
.evidence-ledger-head h3{font-size:14px;line-height:1.3;color:var(--text)}
.evidence-ledger-head p{margin-top:3px;max-width:720px;font-size:10px;line-height:1.45;color:var(--text-muted)}
.evidence-ledger-count{flex:0 0 auto;font:10px var(--mono);color:var(--text-muted)}
.evidence-ledger{border-top:1px solid var(--line)}
.ledger-row{display:grid;grid-template-columns:140px minmax(150px,.55fr) minmax(300px,1.55fr);align-items:start}
.ledger-row+.ledger-row{border-top:1px solid var(--line)}
.ledger-head{background:var(--surface-2);color:var(--text-muted);font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.06em}
.ledger-cell{min-width:0;padding:10px 12px;border-right:1px solid var(--line)}
.ledger-cell:last-child{border-right:0}
.ledger-role b{display:block;font:650 10px var(--mono);color:var(--text-secondary)}
.ledger-role span{display:block;margin-top:3px;font-size:10px;line-height:1.35;color:var(--text-muted)}
.ledger-values{display:flex;gap:4px;flex-wrap:wrap}
.ledger-value{display:inline-flex;align-items:center;min-height:22px;padding:0 6px;border:1px solid var(--number-line);border-radius:3px;background:var(--number-soft);color:var(--number);font:650 10px var(--mono)}
.ledger-passage{font-size:11.5px;line-height:1.55;color:var(--text-secondary);overflow-wrap:anywhere}
.ledger-passage mark{background:var(--number-soft);color:var(--number);border-bottom:1px solid var(--number-line);border-radius:2px;padding:0 1px}
.ledger-provenance{display:block;margin-top:5px;font:10px var(--mono);color:var(--text-muted)}
.intel-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap;padding:13px 18px;background:var(--surface-2)}
.intel-actions-note{margin-left:auto;color:var(--text-muted);font-size:10px;line-height:1.4;text-align:right;max-width:340px}
.intel-side{display:grid;gap:12px}
.intel-card-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--surface-2)}
.intel-card-head h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.075em;color:var(--text-secondary)}
.intel-card-head span{font:10px var(--mono);color:var(--text-muted)}
.checkpoint-list,.next-list{display:grid}
.checkpoint-list{position:relative}
.checkpoint{position:relative;padding:11px 12px 11px 27px;border-bottom:1px solid var(--line)}
.checkpoint::before{content:"";position:absolute;left:12px;top:17px;width:7px;height:7px;border:2px solid var(--checkpoint);border-radius:50%;background:var(--surface-1)}
.checkpoint::after{content:"";position:absolute;left:15px;top:26px;bottom:-12px;width:1px;background:var(--line-strong)}
.checkpoint:last-child::after{display:none}
.checkpoint:last-child,.next-item:last-child{border-bottom:0}
.checkpoint time{display:block;font:650 11px var(--mono);color:var(--checkpoint);margin-bottom:4px}
.checkpoint .next-title{display:block;font-size:11.5px;line-height:1.4;color:var(--text)}
.checkpoint .next-summary{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden;margin-top:4px;font-size:10.5px;line-height:1.48;color:var(--text-muted)}
.next-item{width:100%;display:block;padding:10px 12px;border:0;border-bottom:1px solid var(--line);background:transparent;text-align:left;cursor:pointer;color:var(--text-secondary)}
.next-item:hover{background:var(--surface-3)}
.next-item.selected{background:var(--selected);box-shadow:inset 2px 0 var(--selected-line)}
.next-item time{font:10px var(--mono);color:var(--text-muted)}
.next-item .next-title{display:block;margin-top:3px;font-size:11.5px;font-weight:650;line-height:1.38;color:var(--text)}
.next-item .next-summary{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;margin-top:4px;font-size:10px;line-height:1.42;color:var(--text-muted)}
.coverage-bars{display:grid;gap:9px;padding:12px}
.coverage-bar-row{display:grid;grid-template-columns:105px 1fr 42px;align-items:center;gap:8px}
.coverage-bar-row span{font-size:10px;color:var(--text-secondary)}
.coverage-bar-track{height:6px;border-radius:2px;background:var(--surface-3);overflow:hidden}
.coverage-bar-fill{display:block;height:100%;background:var(--quant);border-radius:2px}
.coverage-bar-row b{text-align:right;font:10px var(--mono);color:var(--text-muted)}
.coverage-caveat{padding:0 12px 12px;font-size:10px;line-height:1.4;color:var(--text-muted)}
.related-context{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.related-context span{display:inline-flex;min-height:20px;align-items:center;padding:0 5px;border:1px solid var(--line);border-radius:3px;background:var(--surface-1);font:10px var(--mono);color:var(--text-muted)}
.intel-stream{grid-column:1/-1;margin-top:12px}
.intel-stream-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line)}
.intel-article-card{min-width:0;padding:14px;background:var(--surface-1);border:0;text-align:left;color:var(--text-secondary);cursor:pointer}
.intel-article-card:hover{background:var(--surface-3)}
.intel-article-card .intel-meta{font-size:10px}
.intel-article-card .intel-card-title{font-size:13px;font-weight:650;line-height:1.38;color:var(--text);margin-top:7px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}
.intel-article-card .intel-card-claim{font-size:10.5px;line-height:1.5;color:var(--text-muted);margin-top:6px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}
.intel-article-card .intel-reasons{margin-top:9px}
.intel-empty{padding:26px;color:var(--text-muted);font-size:11px;text-align:center}
.evidence-gap{border-color:var(--warning-line);background:var(--warning-soft);color:var(--warning)}
.article-dossier-section{padding:14px 0;border-top:1px solid var(--line)}
.article-dossier-section h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:5px}
.article-dossier-section h4{font-size:11.5px;line-height:1.4;color:var(--text);margin-bottom:6px}
.article-dossier-section p{font-size:12px;line-height:1.62;color:var(--text-secondary)}
.article-dossier-section mark{background:var(--number-soft);color:var(--number);border-bottom:1px solid var(--number-line);border-radius:2px;padding:0 1px}
.premium-access{
  margin:16px 0;padding:16px;border:1px solid var(--brick-line);border-top:3px solid var(--brick);
  background:var(--brick-soft);color:var(--text)
}
.premium-access-brief{margin:0;padding:22px clamp(24px,3vw,48px);border-right:0;border-left:0}
.premium-access-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}
.premium-access-kicker{font:700 9px var(--mono);letter-spacing:.11em;text-transform:uppercase;color:var(--brick)}
.premium-access h2,.premium-access h3{margin-top:5px;font:600 20px/1.2 var(--serif);letter-spacing:-.01em;color:var(--text)}
.premium-access-inspector h3{font-size:17px}
.premium-access-state{
  display:inline-flex;align-items:center;min-height:24px;padding:0 7px;border:1px solid var(--brick-line);
  color:var(--brick);font:700 8.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;white-space:nowrap
}
.premium-access-copy{max-width:78ch;margin-top:8px;font-size:11.5px;line-height:1.58;color:var(--text-secondary)}
.premium-access-evidence{margin-top:14px;padding:11px 12px;border:1px solid var(--brick-line);background:var(--surface-1)}
.premium-access-evidence span{display:block;font:700 8.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--brick)}
.premium-access-evidence p{margin-top:5px;font:600 13px/1.45 var(--serif);color:var(--text)}
.premium-access-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:14px}
.premium-primary{border-color:var(--brick);background:var(--brick);color:var(--on-accent)}
.premium-primary:hover{border-color:var(--text);background:var(--text);color:var(--surface-1)}
.premium-access-note{flex:1 0 100%;font-size:9.5px;line-height:1.5;color:var(--text-muted)}
.premium-badge{border-color:var(--brick-line);background:var(--brick-soft);color:var(--brick)}
.checkpoint-mini{display:grid;grid-template-columns:92px 1fr;gap:8px;padding:8px 0;border-top:1px solid var(--line);font-size:10.5px;color:var(--text-secondary)}
.checkpoint-mini time{font:650 10px var(--mono);color:var(--checkpoint)}

/* Investment Committee Brief — editorial reading system */
body[data-view="briefing"] .kpi-strip,
body[data-view="briefing"] .command-bar,
body[data-view="briefing"] .active-filters,
body[data-view="briefing"] .orphaned-queue,
body[data-view="briefing"] .context-bar{display:none}
body[data-view="briefing"] .workspace{
  height:calc(100vh - var(--header-h));height:calc(100dvh - var(--header-h));
  grid-template-columns:minmax(0,1fr)
}
body[data-view="briefing"] .main-panel{grid-column:1/-1;background:var(--bg)}
body[data-view="briefing"] .briefing-shell{
  display:block;padding:0;overflow-y:auto;overscroll-behavior:contain;background:var(--bg)
}
.intel-wrap{
  width:min(1600px,100%);min-height:100%;margin:0 auto;padding:0;
  display:grid;grid-template-columns:220px minmax(620px,1fr) 360px;align-items:start
}
.ic-rail{
  position:sticky;top:0;height:calc(100vh - var(--header-h));height:calc(100dvh - var(--header-h));
  overflow-y:auto;border-right:1px solid var(--line);background:var(--surface-2);padding:28px 18px 24px
}
.ic-rail-brand{font:600 10px var(--mono);letter-spacing:.11em;text-transform:uppercase;color:var(--text-muted);margin:0 8px 12px}
.ic-nav{display:grid;gap:2px}
.ic-nav-button{
  width:100%;min-height:37px;display:flex;align-items:center;gap:9px;border:0;border-left:2px solid transparent;
  border-radius:0;background:transparent;color:var(--text-secondary);padding:7px 9px;text-align:left;cursor:pointer;font-size:11.5px
}
.ic-nav-button:hover{background:var(--surface-3);color:var(--text)}
.ic-nav-button.active{border-left-color:var(--accent);background:var(--selected);color:var(--text);font-weight:650}
.ic-nav-index{width:20px;color:var(--text-muted);font:9px var(--mono)}
.ic-jump-list{display:grid;gap:2px}
.ic-jump{
  width:100%;min-height:37px;display:grid;grid-template-columns:20px minmax(0,1fr) auto;align-items:center;gap:8px;
  border:0;border-left:2px solid transparent;border-radius:0;background:transparent;color:var(--text-secondary);
  padding:7px 9px;text-align:left;cursor:pointer;font-size:10.5px
}
.ic-jump:hover{border-left-color:var(--line-strong);background:var(--surface-3);color:var(--text)}
.ic-jump:focus-visible{border-left-color:var(--focus);background:var(--surface-3)}
.ic-jump i{font:8px var(--mono);font-style:normal;letter-spacing:.04em;text-transform:uppercase;color:var(--positive)}
.ic-jump.unavailable{cursor:default;color:var(--text-muted)}
.ic-jump.unavailable i{color:var(--text-muted)}
.ic-rail-rule{height:1px;background:var(--line);margin:24px 8px}
.ic-rail-heading{font:650 9px var(--mono);letter-spacing:.11em;text-transform:uppercase;color:var(--text-muted);margin:0 8px 8px}
.ic-lens-list{display:grid;gap:1px}
.ic-lens{
  width:100%;min-height:33px;border:0;border-left:2px solid transparent;border-radius:0;background:transparent;
  color:var(--text-secondary);padding:6px 9px;text-align:left;cursor:pointer;font-size:10.5px
}
.ic-lens:hover{background:var(--surface-3);color:var(--text)}
.ic-lens.active{border-left-color:var(--brick);background:var(--brick-soft);color:var(--text)}
.ic-library-facts{display:grid;gap:11px;margin:0 8px}
.ic-library-fact{display:flex;align-items:baseline;justify-content:space-between;gap:10px;color:var(--text-muted);font-size:10px}
.ic-library-fact b{font:650 11px var(--mono);color:var(--text-secondary)}
.ic-standard{margin:20px 8px 0;padding-top:15px;border-top:1px solid var(--line);color:var(--text-muted);font-size:9.5px;line-height:1.55}
.ic-compact-nav{
  display:none;grid-column:1/-1;position:sticky;top:0;z-index:30;
  border-bottom:1px solid var(--line-strong);background:var(--surface-2)
}
.ic-compact-group{min-width:0;display:grid;grid-template-columns:92px minmax(0,1fr);align-items:stretch;border-top:1px solid var(--line)}
.ic-compact-group:first-child{border-top:0}
.ic-compact-label{display:flex;align-items:center;padding:0 14px;border-right:1px solid var(--line);font:650 8.5px var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--text-muted)}
.ic-compact-scroll{min-width:0;display:flex;align-items:stretch;gap:2px;padding:4px 6px;overflow-x:auto;overscroll-behavior-x:contain;scrollbar-width:thin;-webkit-overflow-scrolling:touch}
.ic-compact-button{
  flex:0 0 auto;min-height:36px;display:inline-flex;align-items:center;border:1px solid transparent;border-radius:0;
  background:transparent;color:var(--text-secondary);padding:0 10px;white-space:nowrap;cursor:pointer;font-size:10.5px
}
.ic-compact-button:hover{border-color:var(--control-line);background:var(--surface-3);color:var(--text)}
.ic-compact-button.active,.ic-compact-button[aria-current="page"]{border-color:var(--selected-line);background:var(--selected);color:var(--text);font-weight:650}
.ic-compact-button.lens.active{border-color:var(--brick-line);background:var(--brick-soft)}
#brief-thesis,#brief-key-evidence,#brief-thread,#brief-analysis,#brief-dossier,#brief-evidence-ledger,#brief-checkpoints,#brief-archive{scroll-margin-top:12px}
.intel-lead{
  grid-column:2;min-width:0;border:0;border-left:1px solid var(--line);border-right:1px solid var(--line);
  border-radius:0;background:var(--surface-1);box-shadow:none;overflow:visible
}
.intel-lead-inner{padding:38px clamp(32px,3.2vw,58px) 26px}
.ic-document-meta{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:24px}
.ic-document-meta-left{display:flex;align-items:center;gap:9px;flex-wrap:wrap;color:var(--text-muted);font:9.5px var(--mono);letter-spacing:.04em;text-transform:uppercase}
.ic-document-meta-left .source-badge{text-transform:none;letter-spacing:0}
.ic-source-health.ok{color:var(--positive)}
.ic-source-health.degraded,.ic-source-health.unavailable{color:var(--warning);font-weight:650}
.ic-open-source{
  flex:0 0 auto;min-height:32px;display:inline-flex;align-items:center;border:1px solid var(--control-line);border-radius:0;
  color:var(--text-secondary);padding:0 10px;text-decoration:none;font-size:10px
}
.ic-open-source:hover{border-color:var(--control-line-hover);background:var(--surface-3);color:var(--text)}
.ic-topic{font:650 9.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--brick);margin-bottom:11px}
.intel-title{
  max-width:23ch;margin-top:0;font:600 clamp(32px,2.55vw,44px)/1.03 var(--serif);
  letter-spacing:-.035em;color:var(--text);text-wrap:balance
}
.ic-dek{max-width:72ch;margin-top:15px;font:400 16px/1.5 var(--serif);color:var(--text-secondary)}
.ic-opening-claim{
  display:grid;grid-template-columns:120px minmax(0,1fr);gap:18px;margin-top:26px;padding:17px 18px;
  border-left:3px solid var(--brick);background:var(--brick-soft)
}
.ic-claim-label{font:650 9px/1.45 var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--brick)}
.ic-opening-claim p{font:400 15px/1.58 var(--serif);color:var(--text)}
.ic-opening-claim .source-tail{grid-column:2;margin-top:-8px}
.ic-evidence-strip{
  display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--line)
}
.ic-evidence-card{min-width:0;padding:20px clamp(24px,2.5vw,40px);background:var(--surface-2)}
.ic-evidence-card:only-of-type{grid-column:1/-1}
.ic-evidence-overline{font:650 9px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--text-muted)}
.ic-evidence-values{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;margin-top:8px}
.ic-evidence-values span{font:600 clamp(22px,2vw,31px)/1 var(--serif);color:var(--accent)}
.ic-evidence-values i{font:10px var(--mono);font-style:normal;color:var(--text-muted)}
.ic-evidence-card h3{margin-top:9px;font:650 10px/1.4 var(--mono);color:var(--text-secondary)}
.ic-evidence-card p{margin-top:7px;font-size:11.5px;line-height:1.55;color:var(--text-secondary);display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:4;overflow:hidden}
.ic-evidence-card mark{background:transparent;color:var(--accent);font-weight:700}
.ic-evidence-empty{grid-column:1/-1;padding:20px 40px;background:var(--surface-2);color:var(--text-muted);font-size:11px;line-height:1.55}
.research-thread{padding:30px clamp(32px,3.2vw,58px) 32px;border-bottom:1px solid var(--line);background:var(--surface-1)}
.thread-header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}
.thread-header h2{font:600 27px/1.08 var(--serif);letter-spacing:-.025em;color:var(--text)}
.thread-header p{max-width:68ch;margin-top:8px;font:400 13px/1.55 var(--serif);color:var(--text-secondary)}
.thread-header p strong{color:var(--text);font-weight:600}
.thread-kind{flex:0 0 auto;padding:5px 7px;border:1px solid var(--line-strong);font:650 8.5px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--text-muted)}
.thread-topic-list{display:flex;gap:5px;margin-top:18px;padding-bottom:4px;overflow-x:auto;scrollbar-width:thin}
.thread-topic{flex:0 0 auto;min-height:34px;display:inline-flex;align-items:center;gap:8px;padding:0 9px;border:1px solid var(--control-line);border-radius:0;background:var(--surface-2);color:var(--text-secondary);font-size:10.5px;cursor:pointer}
.thread-topic:hover{border-color:var(--control-line-hover);background:var(--surface-3);color:var(--text)}
.thread-topic.active{border-color:var(--selected-line);box-shadow:inset 2px 0 var(--selected-line);background:var(--selected);color:var(--text);font-weight:650}
.thread-topic span{font:9px var(--mono);color:var(--text-muted)}
.thread-facts{display:grid;grid-template-columns:.7fr 1.45fr 1fr .8fr;margin-top:13px;border:1px solid var(--line);background:var(--surface-2)}
.thread-facts>div{min-width:0;padding:11px 12px;border-right:1px solid var(--line)}
.thread-facts>div:last-child{border-right:0}
.thread-facts b{display:block;overflow:hidden;text-overflow:ellipsis;font:650 11px var(--mono);color:var(--text)}
.thread-facts span{display:block;margin-top:4px;font:8.5px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--text-muted)}
.thread-comparison,.thread-timeline{margin-top:24px;border-top:2px solid var(--text)}
.thread-timeline{border-top-color:var(--accent)}
.thread-subhead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;padding:12px 0 11px}
.thread-subhead span{display:block;margin-bottom:3px;font:650 8.5px var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--text-muted)}
.thread-subhead h3{font:600 16px/1.25 var(--serif);color:var(--text)}
.thread-subhead>b{font:9px var(--mono);font-weight:500;color:var(--text-muted);white-space:nowrap}
.thread-first-note{padding:15px;border:1px solid var(--line);background:var(--surface-2);font-size:11px;line-height:1.55;color:var(--text-muted)}
.thread-role-table{border:1px solid var(--line)}
.thread-role-row{display:grid;grid-template-columns:minmax(120px,1fr) 62px 62px minmax(150px,1.2fr);align-items:center;min-height:33px;border-top:1px solid var(--line);font-size:10px;color:var(--text-secondary)}
.thread-role-row:first-child{border-top:0}
.thread-role-row>*{min-width:0;padding:7px 9px;border-right:1px solid var(--line)}
.thread-role-row>*:last-child{border-right:0}
.thread-role-row span{font-weight:600;color:var(--text)}
.thread-role-row i{font:10px var(--mono);font-style:normal;text-align:center;color:var(--text-muted)}
.thread-role-row i.captured{color:var(--accent);font-weight:700}
.thread-role-row b{font-size:9.5px;font-weight:500;color:var(--text-muted)}
.thread-role-row.head{min-height:29px;background:var(--surface-2);font:650 8.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted)}
.thread-role-row.head span,.thread-role-row.head b{font:inherit;color:inherit}
.thread-passage-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;margin-top:12px;background:var(--line);border:1px solid var(--line)}
.thread-passage-card{min-width:0;padding:15px;background:var(--surface-2)}
.thread-passage-meta{display:flex;align-items:center;justify-content:space-between;gap:12px;font:8.5px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--text-muted)}
.thread-passage-card h4{margin-top:8px;font:600 13px/1.35 var(--serif);color:var(--text)}
.thread-passage-card>p{margin-top:7px;font-size:11px;line-height:1.55;color:var(--text-secondary);display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:5;overflow:hidden}
.thread-passage-card>p mark{background:var(--number-soft);color:var(--number);border-bottom:1px solid var(--number-line)}
.thread-token-row{margin-top:11px;padding-top:9px;border-top:1px solid var(--line)}
.thread-token-row>span{display:block;font:8.5px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--text-muted)}
.thread-token-row>div{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}
.thread-token-row i{padding:3px 5px;border:1px solid var(--number-line);background:var(--number-soft);font:650 9px var(--mono);font-style:normal;color:var(--number)}
.thread-token-row em{font-size:9.5px;font-style:normal;color:var(--text-muted)}
.thread-token-note{margin-top:7px;font-size:9.5px;line-height:1.45;color:var(--text-muted)}
.thread-load-boundary{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-top:12px;padding:14px;border:1px dashed var(--line-strong);background:var(--surface-2)}
.thread-load-boundary strong{font-size:11px;color:var(--text)}
.thread-load-boundary p{max-width:62ch;margin-top:3px;font-size:9.5px;line-height:1.45;color:var(--text-muted)}
.thread-timeline ol{position:relative;display:grid;gap:0;margin:0;padding:0;list-style:none}
.thread-timeline ol::before{content:"";position:absolute;left:7px;top:19px;bottom:19px;width:1px;background:var(--line-strong)}
.thread-node{position:relative;min-width:0;padding-left:25px}
.thread-node::before{content:"";position:absolute;z-index:1;left:3px;top:20px;width:9px;height:9px;border:2px solid var(--line-strong);border-radius:50%;background:var(--surface-1)}
.thread-node.active::before{border-color:var(--accent);background:var(--accent)}
.thread-node button{width:100%;min-height:58px;display:block;padding:11px 12px;border:0;border-top:1px solid var(--line);background:transparent;color:var(--text-secondary);text-align:left;cursor:pointer}
.thread-node:first-child button{border-top:0}
.thread-node button:hover{background:var(--surface-3)}
.thread-node.active button{background:var(--selected);box-shadow:inset 2px 0 var(--selected-line)}
.thread-node-meta{display:flex;align-items:center;justify-content:space-between;gap:12px;font:8.5px var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--text-muted)}
.thread-node-meta i{font-style:normal;color:var(--text-muted)}
.thread-node strong{display:block;margin-top:5px;font:600 12.5px/1.35 var(--serif);color:var(--text)}
.thread-node-match{display:block;margin-top:5px;font:650 8.5px var(--mono);letter-spacing:.045em;text-transform:uppercase;color:var(--accent)}
.thread-node-framing{display:block;margin-top:4px;font-size:10px;line-height:1.45;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.thread-node-roles{display:flex;gap:4px;flex-wrap:wrap;margin-top:7px}
.thread-node-roles i{padding:2px 5px;border:1px solid var(--line);font:8px var(--mono);font-style:normal;color:var(--text-muted)}
.thread-boundary{margin-top:16px;padding-top:12px;border-top:1px solid var(--line);font-size:9.5px;line-height:1.5;color:var(--text-muted)}
.thread-boundary.compact{margin-top:10px;padding-top:0;border-top:0}
.ic-analysis{padding:30px clamp(32px,3.2vw,58px) 34px;border-bottom:1px solid var(--line)}
.ic-section-header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:18px}
.ic-section-header h2{font:600 25px/1.1 var(--serif);letter-spacing:-.02em;color:var(--text)}
.ic-section-header p{max-width:42ch;text-align:right;font-size:10px;line-height:1.5;color:var(--text-muted)}
.ic-analysis-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}
.ic-analysis-card{min-width:0;padding-top:13px;border-top:2px solid var(--text)}
.ic-analysis-card.evidence{border-top-color:var(--accent)}
.ic-analysis-card h3{display:flex;align-items:center;justify-content:space-between;gap:12px;font:650 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--text)}
.ic-source-kind{font:8.5px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--text-muted);white-space:nowrap}
.ic-analysis-card h4{margin-top:10px;font:600 15px/1.35 var(--serif);color:var(--text)}
.ic-analysis-card p{margin-top:8px;font:400 14px/1.62 var(--serif);color:var(--text-secondary)}
.ic-analysis-card p mark{background:var(--number-soft);color:var(--number);border-bottom:1px solid var(--number-line)}
.ic-analysis-card.missing p{font-family:var(--sans);font-size:11px;color:var(--text-muted)}
.ic-dossier{border-top:1px solid var(--line)}
.ic-dossier-head{padding:30px clamp(32px,3.2vw,58px) 14px}
.ic-dossier-head h2{font:600 25px/1.1 var(--serif);letter-spacing:-.02em}
.ic-dossier-head p{max-width:76ch;margin-top:7px;color:var(--text-muted);font-size:10.5px;line-height:1.55}
.intel-section-grid{border-top:1px solid var(--line)}
.intel-section{padding:22px clamp(24px,2.5vw,40px);min-height:0}
.intel-section h3{font:600 16px/1.35 var(--serif)}
.intel-passage{font:400 14px/1.68 var(--serif);display:block;overflow:visible;-webkit-line-clamp:unset}
.research-map,.evidence-ledger-section{background:var(--surface-1)}
.research-map{padding:24px clamp(32px,3.2vw,58px)}
.evidence-ledger-head{padding:24px clamp(32px,3.2vw,58px) 14px}
.ledger-row{grid-template-columns:145px minmax(150px,.48fr) minmax(300px,1.52fr)}
.ledger-passage{font:400 13px/1.62 var(--serif)}
.intel-actions{padding:18px clamp(32px,3.2vw,58px);background:var(--surface-2)}
.primary-action,.secondary-action{border-radius:0}
.intel-side.ic-sheet{
  grid-column:3;position:sticky;top:0;height:calc(100vh - var(--header-h));height:calc(100dvh - var(--header-h));
  display:block;overflow-y:auto;border:0;border-right:1px solid var(--line);background:var(--surface-1)
}
.ic-sheet-inner{padding:32px 26px 30px}
.ic-sheet-eyebrow{font:650 9px var(--mono);letter-spacing:.11em;text-transform:uppercase;color:var(--brick)}
.ic-sheet-title{margin-top:8px;font:600 29px/1.06 var(--serif);letter-spacing:-.025em;color:var(--text)}
.ic-sheet-intro{margin-top:10px;color:var(--text-muted);font-size:10.5px;line-height:1.55}
.ic-sheet-section{padding:18px 0;border-top:1px solid var(--line)}
.ic-sheet-section:first-of-type{margin-top:22px}
.ic-sheet-label{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;font:650 9px var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--text-muted)}
.ic-authored{display:inline-flex;align-items:center;min-height:18px;padding:0 5px;border:1px solid var(--line-strong);font:8px var(--mono);letter-spacing:.05em;color:var(--text-muted)}
.ic-sheet-section h3{font:600 13px/1.35 var(--serif);color:var(--text);margin-bottom:7px}
.ic-sheet-section p{font:400 12.5px/1.58 var(--serif);color:var(--text-secondary)}
.ic-sheet-section .missing{font-family:var(--sans);font-size:10.5px;color:var(--text-muted)}
.ic-sheet-section .source-tail{font-size:9px}
.ic-sheet-checkpoint{padding:9px 0;border-top:1px solid var(--line)}
.ic-sheet-checkpoint:first-child{border-top:0;padding-top:0}
.ic-sheet-checkpoint time{display:block;font:650 10px var(--mono);color:var(--ochre)}
.ic-sheet-checkpoint p{margin-top:4px;font-family:var(--sans);font-size:10.5px;line-height:1.48}
.ic-sheet-local{background:var(--surface-2);margin:4px -10px 0;padding:16px 10px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.ic-local-count{font:600 23px var(--serif);color:var(--text)}
.ic-local-caption{margin-top:3px!important;font-family:var(--sans)!important;font-size:10px!important;color:var(--text-muted)!important}
.ic-sheet-actions{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:12px}
.ic-sheet-actions .primary-action,.ic-sheet-actions .secondary-action{min-width:0;padding:7px;text-align:center}
.ic-boundary-note{margin-top:16px;padding-left:10px;border-left:2px solid var(--line-strong);font-size:9.5px!important;line-height:1.55!important;color:var(--text-muted)!important}
.intel-stream{grid-column:2/-1;margin:0;border:0;border-top:1px solid var(--line);border-radius:0;background:var(--surface-1)}
.intel-stream .intel-card-head{padding:16px 20px;background:var(--surface-2)}
.intel-stream-list{grid-template-columns:repeat(3,minmax(0,1fr))}
.intel-article-card .intel-card-title{font:600 15px/1.32 var(--serif)}
.ic-archive-grid{grid-column:2/-1;display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border-top:1px solid var(--line)}
.ic-archive-grid>.intel-side-card{border:0;border-radius:0}
.premium-archive-card{grid-column:1/-1;border-top:2px solid var(--brick)!important;background:var(--brick-soft)!important}
.premium-archive-intro{padding:12px 14px 0;font-size:10px;line-height:1.5;color:var(--text-secondary)}
.premium-archive-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-top:1px solid var(--brick-line)}
.premium-archive-actions p{font-size:9px;line-height:1.45;color:var(--text-muted)}

/* Dense master tables */
.command-bar,.active-filters,.context-bar{flex:0 0 auto}
.table-shell{flex:1 1 auto;min-height:0;overflow:auto;position:relative;scrollbar-width:thin;background:var(--surface-1)}
.data-table{min-width:760px}
.table-head{
  position:sticky;top:0;z-index:5;display:grid;align-items:center;min-height:34px;
  border-bottom:1px solid var(--line-strong);background:var(--surface-2);
  color:var(--text-muted);font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.06em
}
.idea-grid{grid-template-columns:82px 130px 118px 142px minmax(270px,1fr) 138px 76px 34px}
.research-grid{grid-template-columns:82px 78px minmax(360px,1fr) 76px 110px 90px 34px}
.head-cell{height:100%;display:flex;align-items:center;padding:0 9px;min-width:0}
.head-sort{
  width:100%;height:100%;display:flex;align-items:center;border:0;background:transparent;color:inherit;
  padding:0;text-align:left;text-transform:inherit;letter-spacing:inherit;font:inherit;cursor:pointer
}
.head-sort:hover{color:var(--text)}
.head-cell[aria-sort="ascending"] .head-sort::after{content:" ↑";color:var(--accent)}
.head-cell[aria-sort="descending"] .head-sort::after{content:" ↓";color:var(--accent)}
.data-row{
  display:grid;align-items:center;border-bottom:1px solid var(--line);background:var(--surface-1);
  color:var(--text-secondary);position:relative;cursor:default;min-width:760px
}
.data-row:hover{background:var(--surface-3)}
.data-row.selected{background:var(--selected);box-shadow:inset 3px 0 var(--selected-line),inset 0 1px var(--line-strong),inset 0 -1px var(--line-strong)}
.data-row[aria-selected="true"]{color:var(--text)}
.data-row:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.data-cell{min-width:0;padding:7px 9px;overflow-wrap:anywhere}
.cell-date{font:10px var(--mono);color:var(--text-muted);white-space:nowrap}
.direction-badge,.source-badge,.coverage-badge{
  display:inline-flex;align-items:center;min-height:21px;padding:0 6px;border:1px solid;
  border-radius:3px;font:600 10px var(--mono);white-space:nowrap
}
.dir-long{color:var(--long);border-color:var(--long-line);background:var(--long-soft)}
.dir-short{color:var(--short);border-color:var(--short-line);background:var(--short-soft)}
.dir-arb{color:var(--relative);border-color:var(--relative-line);background:var(--relative-soft)}
.dir-ls{color:var(--long-short);border-color:var(--long-short-line);background:var(--long-short-soft)}
.dir-unspecified{color:var(--text-muted);border-color:var(--line-strong);background:var(--surface-2)}
.source-badge{gap:5px;color:var(--text-secondary);border-color:var(--line-strong);background:var(--surface-2)}
.source-badge::before{content:"";width:4px;height:4px;border-radius:50%;flex:0 0 auto}
.source-substack::before{background:var(--source-substack)}
.source-medium::before{background:var(--source-medium)}
.instrument-primary{font:600 10px var(--mono);color:var(--text);text-transform:capitalize}
.instrument-secondary{font-size:10px;color:var(--text-muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.manager-name{font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.missing{color:var(--text-muted)}
.idea-title{font-size:11.5px;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.idea-context{font-size:10px;color:var(--text-muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.evidence-set{display:flex;gap:3px;align-items:center}
.evidence-flag{
  min-width:24px;height:19px;display:grid;place-items:center;border:1px solid var(--line);
  border-radius:3px;color:var(--text-muted);font:600 10px var(--mono)
}
.evidence-flag.on{border-color:var(--quant-line);color:var(--quant);background:var(--quant-soft)}
.documentation-badge{min-width:34px;height:19px;display:grid;place-items:center;border:1px solid var(--line-strong);border-radius:3px;color:var(--text-secondary);font:650 10px var(--mono);background:var(--surface-2)}
.documentation-badge.complete{color:var(--positive);border-color:var(--positive-line);background:var(--positive-soft)}
.review-flag{color:var(--warning);font:650 10px var(--mono)}
.new-badge{display:inline-flex;margin-left:6px;color:var(--accent);font:650 10px var(--mono);text-transform:uppercase}
.workflow-badge{display:inline-flex;margin-left:6px;padding:1px 5px;border:1px solid var(--line-strong);border-radius:3px;color:var(--text-secondary);font:650 10px var(--mono);text-transform:uppercase}
.workflow-badge.coverage{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
.pinned-selection{box-shadow:inset 3px 0 var(--selected-line)}
.row-open{
  width:27px;height:27px;display:grid;place-items:center;border:1px solid transparent;border-radius:3px;
  text-decoration:none;color:var(--text-muted);font-size:13px
}
.row-open:hover{border-color:var(--control-line-hover);background:var(--surface-3);color:var(--accent)}
.article-title{font-size:12px;font-weight:600;color:var(--text);line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.article-subtitle{font-size:10px;color:var(--text-muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.number-cell{font:11px var(--mono);color:var(--text);text-align:right}
.coverage-full{color:var(--positive);border-color:var(--positive-line)}
.coverage-excerpt{color:var(--warning);border-color:var(--warning-line)}
.coverage-revision{color:var(--warning);border-color:var(--warning-line);background:var(--warning-soft)}
.coverage-research{color:var(--text-muted);border-color:var(--line-strong)}
body.density-compact .data-row{min-height:48px}
body.density-comfortable .data-row{min-height:66px}
body.density-compact .idea-title{-webkit-line-clamp:1}
.empty-state{display:none;padding:80px 24px;text-align:center;color:var(--text-muted)}
.empty-state.visible{display:block}
.empty-state h2{font-size:14px;color:var(--text);margin-bottom:6px}
.empty-actions{display:flex;justify-content:center;gap:7px;flex-wrap:wrap;margin-top:14px}
.load-more-wrap{display:none;padding:12px;text-align:center;border-top:1px solid var(--line)}
.load-more-wrap.visible{display:block}
.load-more{
  min-height:34px;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-2);
  color:var(--text-secondary);padding:0 18px;cursor:pointer;font-size:11px
}
.load-more:hover{background:var(--surface-3);border-color:var(--control-line-hover);color:var(--text)}

/* Persistent evidence inspector */
.inspector{
  min-width:0;border-left:1px solid var(--line);background:var(--surface-1);
  overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin
}
.inspector-hidden .workspace{grid-template-columns:var(--rail-w) minmax(560px,1fr) 0}
.inspector-hidden .inspector{display:none}
.inspector-header{
  position:sticky;top:0;z-index:4;min-height:44px;display:flex;align-items:center;
  justify-content:space-between;padding:0 13px;border-bottom:1px solid var(--line);background:var(--surface-1)
}
.inspector-label{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted)}
.inspector-close{min-width:24px;border:0;background:transparent;color:var(--text-muted);cursor:pointer;padding:8px}
.inspector-content{padding:16px}
.inspector-empty{padding:70px 18px;text-align:center;color:var(--text-muted)}
.inspector-empty-mark{
  width:42px;height:42px;margin:0 auto 14px;display:grid;place-items:center;border:1px solid var(--line);
  border-radius:4px;background:var(--surface-2);font:600 11px var(--mono);color:var(--accent)
}
.inspector-empty h2{font-size:13px;color:var(--text);margin-bottom:6px}
.record-eyebrow{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:10px}
.record-id{font:10px var(--mono);color:var(--text-muted)}
.record-title{font-size:17px;line-height:1.35;color:var(--text);letter-spacing:-.01em;overflow-wrap:anywhere}
.record-subtitle{font-size:11px;color:var(--text-muted);line-height:1.55;margin-top:7px}
.record-actions{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0}
.primary-action,.secondary-action{
  min-height:34px;border-radius:3px;padding:0 10px;display:inline-flex;align-items:center;
  justify-content:center;gap:6px;text-decoration:none;cursor:pointer;font-size:10px
}
.primary-action{border:1px solid var(--accent-strong);background:var(--accent-strong);color:var(--on-accent);font-weight:700}
.primary-action:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.primary-action:active{background:var(--accent-active);border-color:var(--accent-active)}
.secondary-action{border:1px solid var(--control-line);background:var(--surface-2);color:var(--text-secondary)}
.secondary-action:hover{background:var(--surface-3);border-color:var(--control-line-hover);color:var(--text)}
.secondary-action.saved{color:var(--accent);border-color:var(--accent)}
.record-facts{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:14px}
.inspector-section{padding:13px 0;border-top:1px solid var(--line)}
.inspector-section h3{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:7px}
.inspector-section p{font-size:11.5px;line-height:1.62;color:var(--text-secondary);overflow-wrap:anywhere}
.inspector-section .primary-text{color:var(--text);font-size:12.5px}
.quant-block{
  padding:10px;border:1px solid var(--quant-line);
  border-radius:3px;background:var(--quant-soft);
  color:var(--quant);font:10.5px/1.6 var(--mono);overflow-wrap:anywhere
}
.reported-outcome{padding-left:9px;border-left:2px solid var(--line-strong)}
.provenance{
  margin-top:14px;padding:11px;border:1px solid var(--line);border-radius:3px;
  background:var(--surface-2);font-size:10.5px;line-height:1.55;color:var(--text-muted)
}
.article-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin:14px 0}
.article-stat{padding:10px;background:var(--surface-2)}
.article-stat b{display:block;font:600 12px var(--mono);color:var(--text)}
.article-stat span{font:10px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted)}
.extraction-map{width:100%;border-collapse:collapse;margin-top:8px;font-size:10px}
.extraction-map th,.extraction-map td{padding:7px 5px;border-top:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}
.extraction-map th:first-child,.extraction-map td:first-child{text-align:left}
.extraction-map th{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted)}
.extraction-map td{color:var(--text-secondary)}
.extraction-map td:first-child{color:var(--text);font-weight:600}
.extraction-note{margin-top:7px;font-size:10px!important;line-height:1.45!important;color:var(--text-muted)!important}
.related-ideas{display:grid;gap:5px}
.related-idea{
  border:1px solid var(--control-line);border-radius:3px;background:var(--surface-2);padding:8px;
  color:var(--text-secondary);text-align:left;cursor:pointer;font-size:10.5px;line-height:1.4
}
.related-idea:hover{background:var(--surface-3);border-color:var(--control-line-hover);color:var(--text)}
.review-notice{margin:8px 0 13px;padding:9px;border:1px solid var(--warning-line);border-radius:3px;background:var(--warning-soft);color:var(--warning);font-size:10.5px;line-height:1.5}
.diligence-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.diligence-item{display:flex;align-items:flex-start;gap:6px;padding:6px;border:1px solid var(--line);border-radius:3px;background:var(--surface-2);font-size:10px;color:var(--text-muted)}
.diligence-item.captured{color:var(--text-secondary)}
.diligence-mark{font:700 10px var(--mono);color:var(--text-muted)}
.diligence-item.captured .diligence-mark{color:var(--positive)}
.workflow-panel{margin:13px 0;padding:11px;border:1px solid var(--line-strong);border-radius:4px;background:var(--surface-2)}
.workflow-header{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.workflow-panel h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.07em}
.workflow-coverage{display:inline-flex;padding:3px 6px;border:1px solid var(--accent);border-radius:3px;background:var(--accent-soft);color:var(--accent);font:650 10px var(--mono)}
.workflow-subhead{margin:12px 0 3px;padding-top:10px;border-top:1px solid var(--line);font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-secondary)}
.workflow-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 8px}
.workflow-field{display:grid;gap:4px;margin-top:8px;color:var(--text-muted);font-size:10px}
.workflow-field.wide{grid-column:1/-1}
.workflow-field select,.workflow-field input,.workflow-field textarea{width:100%;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-1);color:var(--text);padding:7px;font-size:11px}
.workflow-field textarea{min-height:72px;resize:vertical;line-height:1.45}
.workflow-field textarea.compact{min-height:56px}
.workflow-gates{display:grid;gap:5px;margin-top:8px;border:0;padding:0}
.workflow-gates legend{font-size:10px;color:var(--text-muted);margin-bottom:3px}
.workflow-gate{display:flex;align-items:flex-start;gap:7px;padding:7px;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-1);color:var(--text-secondary);font-size:10px;line-height:1.4;cursor:pointer}
.workflow-gate:hover{border-color:var(--control-line-hover);background:var(--surface-3);color:var(--text)}
.workflow-gate input{width:16px;height:16px;flex:0 0 auto;margin:0;accent-color:var(--accent)}
.workflow-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.workflow-warning{font-size:10px;color:var(--text-muted);line-height:1.45;margin-top:8px}
.orphaned-queue{display:none;border-bottom:1px solid var(--line);background:var(--surface-1);padding:10px 12px}
.orphaned-queue.visible{display:block}
.orphaned-queue h2{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--warning)}
.orphaned-queue>p{margin:4px 0 8px;color:var(--text-muted);font-size:10px;line-height:1.45}
.orphaned-list{display:grid;gap:6px}
.orphaned-item{display:grid;grid-template-columns:88px minmax(0,1fr) auto;gap:8px;padding:8px;border:1px solid var(--line);border-radius:3px;background:var(--surface-2);font-size:10px;color:var(--text-secondary)}
.orphaned-item time,.orphaned-item small{font:10px var(--mono);color:var(--text-muted)}
.orphaned-item strong{display:block;color:var(--text);font-size:10.5px}
.orphaned-item p{margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text-muted)}
.orphaned-item a{color:var(--accent);text-decoration:none}
body.density-comfortable .idea-title{font-size:12.5px;line-height:1.5}
body.density-comfortable .idea-context,body.density-comfortable .instrument-secondary,body.density-comfortable .article-subtitle{font-size:11px}
body.density-comfortable .manager-name,body.density-comfortable .article-title{font-size:12px}
body.density-comfortable .data-cell{padding-top:9px;padding-bottom:9px}

/* Quiet editorial consistency across monitor, library, queue, and evidence sheet */
button,a{touch-action:manipulation}
body:not([data-view="briefing"]) .main-panel{background:var(--surface-1)}
body:not([data-view="briefing"]) .filter-rail{background:var(--surface-2)}
body:not([data-view="briefing"]) .rail-header{background:var(--surface-2);border-bottom-color:var(--line-strong)}
.command-bar{background:var(--surface-1);border-bottom-color:var(--line-strong);padding-left:16px;padding-right:16px}
.view-tabs{border:0;border-radius:0;background:transparent;padding:0;gap:2px}
.view-tab{border-radius:0;border-bottom:2px solid transparent;background:transparent}
.view-tab.active{background:var(--selected);box-shadow:none;border-bottom-color:var(--selected-line)}
.facet-option,.facet-clear,.date-option,.manager-search,.preset-button,
.select-control,.command-button,.filter-chip,.load-more,.row-open,.direction-badge,.source-badge,
.coverage-badge,.evidence-flag,.documentation-badge,.workflow-badge,.related-idea,
.workflow-panel,.workflow-field select,.workflow-field input,.workflow-field textarea,
.workflow-gate,.orphaned-item,.provenance,.quant-block,.review-notice{border-radius:0}
.filter-group{padding:16px 12px}
.facet-option.active,.facet-clear.active{box-shadow:inset 2px 0 var(--selected-line)}
.table-shell,.data-row{background:var(--surface-1)}
.table-head{background:var(--surface-2);border-bottom-color:var(--line-strong)}
.data-row:hover{background:var(--surface-2)}
.inspector{background:var(--surface-1);border-left-color:var(--line-strong)}
.inspector-header{min-height:48px;background:var(--surface-2);border-bottom-color:var(--line-strong)}
.inspector-content{padding:20px}
.record-title{font:600 21px/1.24 var(--serif);letter-spacing:-.015em}
.record-subtitle,.inspector-section p{font-family:var(--serif)}
.inspector-section h3,.inspector-label{letter-spacing:.11em}
.workflow-panel{border-top:2px solid var(--accent);background:var(--surface-2)}
.kpi-strip{background:var(--surface-2)}
.kpi-item{border-right-color:var(--line)}

/* Two purposeful visual modes: warm editorial paper and high-density terminal */
html[data-theme="light"] .app-header{
  border-top:3px solid var(--brick);border-bottom-color:var(--line-strong)
}
html[data-theme="light"] .brand-mark{
  border-color:var(--brick);background:var(--brick);color:var(--surface-1)
}
html[data-theme="light"] .brand-name{font-weight:700;letter-spacing:-.02em}
html[data-theme="light"] .brand-sub{color:var(--brick);font-family:var(--sans);font-weight:700}
html[data-theme="light"] .kpi-strip{border-bottom:2px solid var(--text)}
html[data-theme="light"] .table-head{border-top:1px solid var(--text);border-bottom-color:var(--text)}
html[data-theme="light"] .intel-title,
html[data-theme="light"] .ic-section-header h2,
html[data-theme="light"] .ic-dossier-head h2,
html[data-theme="light"] .ic-sheet-title,
html[data-theme="light"] .record-title{font-family:var(--serif)}
html[data-theme="light"] .intel-lead-inner{box-shadow:inset 0 3px var(--brick)}

html[data-theme="dark"] .app-header{
  border-top:3px solid var(--selected-line);border-bottom-color:var(--line-strong)
}
html[data-theme="dark"] .brand-mark{
  border-color:var(--selected-line);background:var(--selected-line);color:var(--on-accent)
}
html[data-theme="dark"] .brand-name{
  font:700 13px/1 var(--mono);letter-spacing:.045em;text-transform:uppercase
}
html[data-theme="dark"] .brand-sub{color:var(--accent);letter-spacing:.14em}
html[data-theme="dark"] #search{
  border:1px solid var(--line-strong);background:var(--surface-2);padding-left:34px
}
html[data-theme="dark"] #search:focus{border-color:var(--selected-line);background:var(--surface-2)}
html[data-theme="dark"] #search:focus-visible{box-shadow:inset 0 0 0 1px var(--selected-line)}
html[data-theme="dark"] .search-glyph{left:11px;color:var(--accent)}
html[data-theme="dark"] .search-key{right:8px;border-color:var(--control-line)}
html[data-theme="dark"] .utility-button,
html[data-theme="dark"] .view-tab,
html[data-theme="dark"] .command-button,
html[data-theme="dark"] .select-control{font-family:var(--mono);letter-spacing:.025em}
html[data-theme="dark"] .kpi-strip{border-bottom:2px solid var(--selected-line)}
html[data-theme="dark"] .kpi-value{color:var(--selected-line)}
html[data-theme="dark"] .view-tab.active{color:var(--selected-line)}
html[data-theme="dark"] .table-head{
  border-bottom-color:var(--selected-line);box-shadow:inset 0 -1px var(--selected-line)
}
html[data-theme="dark"] .intel-title{
  max-width:30ch;font-family:var(--sans);font-size:clamp(28px,2.1vw,36px);font-weight:700;
  line-height:1.08;letter-spacing:-.024em
}
html[data-theme="dark"] .ic-section-header h2,
html[data-theme="dark"] .ic-dossier-head h2,
html[data-theme="dark"] .ic-sheet-title,
html[data-theme="dark"] .record-title{font-family:var(--sans);font-weight:700}
html[data-theme="dark"] .ic-evidence-values span{font-family:var(--mono);color:var(--number)}
html[data-theme="dark"] .intel-side.ic-sheet{background:var(--surface-2)}
html[data-theme="dark"] .workflow-panel{border-top-color:var(--selected-line)}
html[data-theme="dark"] .primary-action{font-family:var(--mono);letter-spacing:.025em}
html[data-theme="dark"] .toast,
html[data-theme="dark"] .persistent-notice,
html[data-theme="dark"] dialog,
html[data-theme="dark"] kbd,
html[data-theme="dark"] .method-card{border-radius:0}
html[data-theme="dark"] ::-webkit-scrollbar-thumb{border-radius:0}

/* Overlays and feedback */
.drawer-backdrop{display:none}
.toast{
  position:fixed;left:50%;bottom:22px;z-index:200;transform:translate(-50%,20px);
  padding:9px 13px;border:1px solid var(--line-strong);border-radius:4px;
  background:var(--surface-raised);color:var(--text);box-shadow:var(--shadow);
  font-size:11px;opacity:0;pointer-events:none;transition:opacity .16s,transform .16s
}
.toast.show{opacity:1;transform:translate(-50%,0)}
.persistent-notice{
  position:fixed;left:50%;bottom:66px;z-index:205;display:flex;align-items:center;gap:9px;
  width:min(680px,calc(100vw - 28px));padding:10px 12px;border:1px solid var(--warning-line);
  border-radius:4px;background:var(--surface-raised);color:var(--warning);box-shadow:var(--shadow);
  font-size:10.5px;line-height:1.45
}
.persistent-notice[hidden]{display:none}
.persistent-notice span{flex:1}
.persistent-notice button{min-height:32px}
dialog{
  width:min(720px,calc(100vw - 32px));border:1px solid var(--line-strong);border-radius:5px;
  background:var(--surface-raised);color:var(--text);padding:0;box-shadow:var(--shadow)
}
dialog::backdrop{background:var(--backdrop)}
.dialog-header{display:flex;align-items:center;justify-content:space-between;padding:13px 15px;border-bottom:1px solid var(--line)}
.dialog-header h2{font-size:13px}
.shortcut-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);margin:15px;border:1px solid var(--line)}
.shortcut-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px;background:var(--surface-2);font-size:10.5px;color:var(--text-secondary)}
kbd{font:10px var(--mono);border:1px solid var(--line-strong);background:var(--surface-1);border-radius:3px;padding:2px 6px;color:var(--text)}
.dialog-foot{padding:0 15px 15px;color:var(--text-muted);font-size:10px}
/* Command palette: one keystroke to any view, dossier, or terminal action. */
#command-palette{width:min(640px,calc(100vw - 32px));margin-top:12vh}
.palette-field{display:flex;align-items:center;gap:9px;padding:11px 13px;border-bottom:1px solid var(--line)}
.palette-field svg{flex:none;color:var(--text-muted)}
#command-palette-input{
  flex:1;min-width:0;height:26px;border:0;background:transparent;color:var(--text);
  font:13px var(--sans);outline:none
}
#command-palette-input::placeholder{color:var(--text-muted)}
.palette-list{list-style:none;margin:0;padding:5px;max-height:min(52vh,420px);overflow-y:auto}
.palette-option{
  display:flex;align-items:center;gap:10px;padding:7px 9px;border-radius:3px;cursor:pointer;
  color:var(--text-secondary);font-size:11.5px;border:1px solid transparent
}
.palette-option[aria-selected="true"]{background:var(--selected);border-color:var(--selected-line);color:var(--text)}
.palette-kind{
  flex:none;min-width:58px;font:9px var(--mono);letter-spacing:.06em;text-transform:uppercase;
  color:var(--text-muted)
}
.palette-option[aria-selected="true"] .palette-kind{color:var(--accent)}
.palette-label{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.palette-meta{flex:none;font:9.5px var(--mono);color:var(--text-muted)}
.palette-empty{padding:18px 15px;color:var(--text-muted);font-size:11px}
.palette-foot{
  display:flex;flex-wrap:wrap;gap:11px;padding:9px 13px;border-top:1px solid var(--line);
  color:var(--text-muted);font-size:9.5px
}
.manual-copy-body{display:grid;gap:10px;padding:15px}
.manual-copy-body p{color:var(--text-secondary);font-size:11px;line-height:1.55}
.manual-copy-text{
  width:100%;min-height:150px;resize:vertical;padding:10px;border:1px solid var(--control-line);
  border-radius:3px;background:var(--surface-1);color:var(--text);font:11px/1.5 var(--mono)
}
.manual-copy-actions{display:flex;justify-content:flex-end;gap:8px}
.method-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:15px;border-top:1px solid var(--line)}
.method-card{border:1px solid var(--line);border-radius:3px;background:var(--surface-2);padding:11px}
.method-card h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}
.method-card p,.method-card li{font-size:10.5px;line-height:1.55;color:var(--text-secondary)}
.method-card ul{margin:6px 0 0;padding-left:17px}
.method-card a{color:var(--accent);text-underline-offset:2px}
noscript{position:fixed;inset:0;z-index:1000;display:grid;place-items:center;background:var(--bg);color:var(--text);padding:30px}

::-webkit-scrollbar{width:7px;height:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--control-line);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--control-line-hover)}
*{scrollbar-color:var(--control-line) transparent}

@media(max-width:1240px){
  .workspace{grid-template-columns:var(--rail-w) minmax(0,1fr)}
  .inspector{
    position:fixed;z-index:90;top:calc(var(--header-h) + var(--kpi-h));right:0;bottom:0;
    width:min(var(--inspector-w),calc(100vw - 40px));box-shadow:var(--shadow);
    transform:translateX(105%);transition:transform .18s ease
  }
  body.inspector-open .inspector{transform:translateX(0)}
  .inspector-hidden .workspace{grid-template-columns:var(--rail-w) minmax(0,1fr)}
  .inspector-hidden .inspector{display:block}
}
@media(max-width:1020px){
  .command-bar{flex-wrap:wrap}
  .app-header{grid-template-columns:auto minmax(220px,1fr) auto;gap:10px}
  .brand{min-width:0}
  .brand-sub{display:none}
  .freshness{display:flex;width:auto;max-width:86px;justify-content:center;margin:0;overflow:visible}
  .freshness-separator,.freshness>span:last-child{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
  .header-right{min-width:0}
  .workspace{grid-template-columns:minmax(0,1fr)}
  .filter-rail{
    position:fixed;z-index:100;top:calc(var(--header-h) + var(--kpi-h));left:0;bottom:0;
    width:min(300px,calc(100vw - 44px));box-shadow:var(--shadow);transform:translateX(-105%);
    transition:transform .18s ease
  }
  body.filters-open .filter-rail{transform:translateX(0)}
  #mobile-filter-button{display:inline-flex}
  #filter-close{display:inline-flex}
  .drawer-backdrop{
    position:fixed;z-index:80;inset:calc(var(--header-h) + var(--kpi-h)) 0 0;
    background:var(--backdrop)
  }
  body.filters-open .drawer-backdrop,body.inspector-open .drawer-backdrop{display:block}
  .intel-grid{grid-template-columns:1fr}
  .intel-side{grid-template-columns:1fr 1fr}
  .intel-stream{grid-column:auto}
  .intel-stream-list{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:899px){
  :root{--header-h:104px}
  .app-header{
    height:var(--header-h);grid-template-columns:minmax(0,1fr) auto;grid-template-rows:52px 52px;
    gap:0 8px;padding:0 10px
  }
  .brand{grid-column:1;grid-row:1;min-width:0}
  .brand-sub{display:none}
  .header-right{grid-column:2;grid-row:1;gap:5px}
  .header-library,#method-button{display:none}
  .global-search{grid-column:1/-1;grid-row:2}
  #search{height:44px;padding-left:27px;font-size:16px}
  .utility-button{min-height:44px}
}
@media(max-width:759px){
  :root{--header-h:104px;--kpi-h:42px}
  .app-header{
    height:var(--header-h);grid-template-columns:auto minmax(0,1fr) auto;
    grid-template-rows:52px 52px;padding:0 9px;gap:0 7px
  }
  .brand-name{display:none}
  .brand{grid-column:1;grid-row:1;min-width:auto}
  .brand-mark{width:32px;height:32px}
  .global-search{grid-column:1/-1;grid-row:2}
  .search-key,#method-button,.button-label{display:none}
  #search{height:44px;padding-right:9px;font-size:16px}
  .header-right{grid-column:2/4;grid-row:1;gap:5px}
  .utility-button{min-width:44px;min-height:44px;padding:0 8px}
  .facet-option,.facet-clear,.date-option,.manager-search,.preset-button{min-height:44px}
  .manager-search,.select-control,.workflow-field select,.workflow-field input,.workflow-field textarea{font-size:16px}
  .kpi-item{min-width:128px;padding:0 11px}
  .kpi-value{font-size:13px}
  .command-bar{padding:6px 8px;gap:6px}
  .view-tabs{width:100%;max-width:100%;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
  .view-tabs::-webkit-scrollbar{display:none}
  .view-tab{flex:0 0 auto}
  .view-tab{padding:0 8px;min-height:44px;font-size:11px}
  .result-summary{order:5;flex:1 0 100%}
  .command-spacer{display:none}
  .select-control,.command-button{min-height:44px}
  .storage-alert{flex-wrap:wrap;align-items:stretch;padding:9px 8px;gap:6px}
  .storage-alert span{flex:1 0 100%}
  .storage-alert .command-button{flex:1 1 140px;min-width:0;justify-content:center;white-space:normal}
  .text-button,.filter-chip,.primary-action,.secondary-action,.inspector-close,.load-more{min-height:44px}
  .text-button,.inspector-close{min-width:44px}
  .active-filters{padding-left:8px}
  .context-bar{grid-template-columns:1fr;padding:7px 8px;gap:6px}
  .context-metrics{gap:10px;overflow-x:auto}
  .mix-legend{display:none}
  .data-table{min-width:0}
  .table-head{
    position:absolute;width:1px;height:1px;min-height:1px;margin:-1px;padding:0;
    overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0
  }
  .data-row{min-width:0}
  .idea-grid{
    grid-template-columns:74px 1fr auto;
    grid-template-areas:
      "date bias source"
      "idea idea idea"
      "market manager evidence"
      "open open open";
    gap:0;padding:9px 8px
  }
  .research-grid{
    grid-template-columns:74px 1fr auto;
    grid-template-areas:
      "date source open"
      "article article article"
      "count coverage read";
    gap:0;padding:9px 8px
  }
  .data-cell{padding:4px 5px}
  .cell-date{grid-area:date}
  .cell-bias{grid-area:bias}
  .cell-market{grid-area:market}
  .cell-manager{grid-area:manager}
  .cell-idea{grid-area:idea}
  .cell-evidence{grid-area:evidence;justify-self:end}
  .cell-source{grid-area:source;justify-self:end}
  .cell-open{grid-area:open;display:none}
  .cell-article{grid-area:article}
  .cell-count{grid-area:count;text-align:left}
  .cell-coverage{grid-area:coverage}
  .cell-read{grid-area:read;text-align:right}
  body.density-compact .data-row,body.density-comfortable .data-row{min-height:unset}
  body.density-compact .idea-title{-webkit-line-clamp:2}
  .idea-context{white-space:normal;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical}
  .inspector{top:calc(var(--header-h) + var(--kpi-h));width:calc(100vw - 18px)}
  .shortcut-grid{grid-template-columns:1fr}
  .method-grid{grid-template-columns:1fr}
  .briefing-shell{padding:10px}
  .intel-head{display:block;margin-bottom:11px}
  .intel-head h2{font-size:19px}
  .intel-head p{font-size:11px}
  .intel-lenses{margin-top:10px;justify-content:flex-start;overflow-x:auto;flex-wrap:nowrap;padding:2px;scrollbar-width:none}
  .intel-lenses::-webkit-scrollbar{display:none}
  .intel-lens{min-height:44px;flex:0 0 auto}
  .intel-title{font-size:22px}
  .intel-claim{font-size:13px}
  .intel-lead-inner{padding:15px 14px}
  .intel-fact-strip{grid-template-columns:1fr 1fr}
  .intel-fact:nth-child(2){border-right:0}
  .intel-fact:nth-child(-n+2){border-bottom:1px solid var(--line)}
  .research-map{padding:13px 14px}
  .research-map-head{display:block}
  .research-map-head p{margin-top:4px;text-align:left}
  .research-map-track{grid-template-columns:repeat(7,112px)}
  .evidence-ledger-head{display:block;padding:13px 14px 10px}
  .evidence-ledger-count{display:block;margin-top:5px}
  .ledger-head{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%)}
  .ledger-row{display:block;padding:11px 14px}
  .ledger-row+.ledger-row{border-top:1px solid var(--line)}
  .ledger-cell{padding:0;border:0}
  .ledger-values{margin:8px 0}
  .ledger-passage{font-size:12px}
  .data-row,.data-row *{font-size:12px}
  .checkpoint-mini,.checkpoint-mini time,.provenance,.record-subtitle,
  .rail-disclaimer,.filter-note,.facet-option,.facet-clear,.kpi-label,
  .filter-heading h2,.preset-button,.freshness,.primary-action,.secondary-action,
  .result-summary,.inspector-label,.rail-title{font-size:12px}
  .intel-section-grid{grid-template-columns:1fr}
  .intel-section,.intel-section.full{grid-column:1;min-height:0;padding:13px 14px}
  .intel-passage{font-size:12px}
  .intel-actions{padding:12px 14px}
  .intel-actions-note{flex:1 0 100%;margin-left:0;text-align:left}
  .intel-side{grid-template-columns:1fr}
  .intel-stream-list{grid-template-columns:1fr}
  .intel-article-card{min-height:44px}
  .coverage-bar-row{grid-template-columns:94px 1fr 38px}
  .brief-hero{display:block}
  .brief-actions{justify-content:flex-start;margin-top:10px}
  .brief-metrics{grid-template-columns:1fr 1fr}
  .brief-grid{grid-template-columns:1fr}
  .brief-record{grid-template-columns:68px 80px minmax(0,1fr)}
  .brief-record .documentation-badge{display:none}
  .diligence-grid{grid-template-columns:1fr}
  .premium-access{padding:15px 14px}
  .premium-access-head{display:block}
  .premium-access-state{margin-top:9px}
  .premium-access-actions .primary-action,.premium-access-actions .secondary-action{width:100%}
  .premium-archive-actions{display:block}
  .premium-archive-actions .secondary-action{width:100%;margin-top:10px}
  .workflow-grid,.operating-boundary{grid-template-columns:1fr}
  .workflow-gate{min-height:44px;align-items:center}
  .orphaned-item{grid-template-columns:1fr}
}
@media(max-width:430px){
  .command-button[data-action="copy-view"],.command-button[data-action="density"]{display:none}
  .view-tab{font-size:10px}
  .context-metric:nth-child(n+3){display:none}
  .brief-metrics{grid-template-columns:1fr}
}
@media(max-width:1439px){
  .intel-wrap{grid-template-columns:minmax(0,1fr) 350px}
  .ic-rail{display:none}
  .ic-compact-nav{display:grid}
  #brief-thesis,#brief-key-evidence,#brief-thread,#brief-analysis,#brief-dossier,#brief-evidence-ledger,#brief-checkpoints,#brief-archive{scroll-margin-top:calc(var(--brief-compact-nav-h) + 7px)}
  .intel-lead{grid-column:1}
  .intel-side.ic-sheet{
    grid-column:2;top:var(--brief-compact-nav-h);
    height:calc(100vh - var(--header-h) - var(--brief-compact-nav-h));
    height:calc(100dvh - var(--header-h) - var(--brief-compact-nav-h))
  }
  .ic-archive-grid,.intel-stream{grid-column:1/-1}
}
@media(max-width:1023px){
  .intel-wrap{display:block}
  .intel-lead{border-left:0;border-right:0}
  .intel-side.ic-sheet{position:relative;top:auto;width:100%;height:auto;max-height:none;border-top:1px solid var(--line);border-right:0;overflow:visible}
  .ic-sheet-inner{padding:30px clamp(28px,5vw,48px)}
  .ic-sheet-section{display:grid;grid-template-columns:minmax(120px,.34fr) minmax(0,1fr);column-gap:24px}
  .ic-sheet-section .ic-sheet-label{grid-column:1;display:block}
  .ic-sheet-section h3,.ic-sheet-section>p,.ic-sheet-section>.source-tail,.ic-sheet-section>.ic-sheet-checkpoint{grid-column:2}
  .ic-sheet-section.ic-sheet-local{display:block;margin:4px 0 0;padding:20px 0;background:transparent}
  .ic-archive-grid{display:block}
  .ic-archive-grid>.intel-side-card+.intel-side-card{border-top:1px solid var(--line)}
}
@media(max-width:759px){
  :root{--header-h:104px;--brief-compact-nav-h:105px}
  .app-header{
    height:var(--header-h);grid-template-columns:minmax(0,1fr) auto;grid-template-rows:52px 52px;
    gap:0 8px;padding:0 10px
  }
  .brand{grid-column:1;grid-row:1;min-width:0}
  .brand-mark{width:32px;height:32px}
  .brand-name{display:block;font-size:15px}
  .brand-sub{display:none}
  .header-right{grid-column:2;grid-row:1;gap:5px}
  .header-library,#method-button{display:none}
  .global-search{grid-column:1/-1;grid-row:2}
  #search{height:44px;padding-left:27px;font-size:16px}
  body[data-view="briefing"] .workspace{height:calc(100vh - var(--header-h));height:calc(100dvh - var(--header-h))}
  .ic-compact-group{grid-template-columns:62px minmax(0,1fr)}
  .ic-compact-label{padding:0 8px;font-size:8px}
  .ic-compact-scroll{padding:4px}
  .ic-compact-button{min-height:44px;padding:0 11px;font-size:11px}
  #brief-thesis,#brief-key-evidence,#brief-thread,#brief-analysis,#brief-dossier,#brief-evidence-ledger,#brief-checkpoints,#brief-archive{scroll-margin-top:calc(var(--brief-compact-nav-h) + 7px)}
  .intel-lead-inner{padding:24px 18px 20px}
  .ic-document-meta{display:block;margin-bottom:20px}
  .ic-open-source{margin-top:12px;min-height:44px}
  .intel-title{max-width:none;font-size:clamp(28px,8.2vw,35px);line-height:1.05}
  .ic-dek{font-size:15px}
  .ic-opening-claim{grid-template-columns:1fr;gap:9px;margin-top:21px;padding:15px}
  .ic-opening-claim .source-tail{grid-column:1;margin-top:0}
  .ic-evidence-strip{grid-template-columns:1fr}
  .ic-evidence-card{padding:18px}
  .ic-evidence-card:only-of-type{grid-column:1}
  .ic-evidence-card p,.intel-article-card .intel-card-claim,.next-item .next-summary{font-size:12px}
  .research-thread{padding:26px 18px}
  .thread-header{display:block}
  .thread-kind{display:inline-block;margin-top:11px}
  .thread-topic{min-height:44px;font-size:12px}
  .thread-facts{grid-template-columns:1fr 1fr}
  .thread-facts>div:nth-child(2){border-right:0}
  .thread-facts>div:nth-child(-n+2){border-bottom:1px solid var(--line)}
  .thread-role-row{grid-template-columns:minmax(105px,1fr) 60px 74px}
  .thread-role-row>b{display:none}
  .thread-passage-grid{grid-template-columns:1fr}
  .thread-load-boundary{display:block}
  .thread-load-boundary .secondary-action{width:100%;min-height:44px;margin-top:12px}
  .thread-node button{min-height:72px;padding:12px}
  .thread-node-meta{display:block}
  .thread-node-meta i{display:block;margin-top:3px}
  .thread-node-framing{white-space:normal;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2}
  .thread-kind,.thread-topic span,.thread-facts b,.thread-facts span,.thread-subhead span,.thread-subhead>b,
  .thread-role-row,.thread-role-row i,.thread-role-row b,.thread-passage-meta,.thread-passage-card>p,
  .thread-token-row>span,.thread-token-row i,.thread-token-row em,.thread-token-note,
  .thread-load-boundary strong,.thread-load-boundary p,.thread-node-meta,.thread-node strong,
  .thread-node-match,.thread-node-framing,.thread-node-roles i,.thread-boundary,.thread-first-note{font-size:12px}
  .ic-analysis,.ic-dossier-head{padding:26px 18px}
  .ic-section-header{display:block}
  .ic-section-header p{margin-top:7px;text-align:left}
  .ic-analysis-grid{grid-template-columns:1fr;gap:23px}
  .research-map,.evidence-ledger-head{padding-left:18px;padding-right:18px}
  .intel-section,.intel-actions{padding-left:18px;padding-right:18px}
  .ic-sheet-inner{padding:28px 18px}
  .ic-sheet-section{display:block}
  .ic-sheet-section .ic-sheet-label{display:flex}
  .ic-sheet-section h3,.ic-sheet-section>p,.ic-sheet-section>.source-tail,.ic-sheet-section>.ic-sheet-checkpoint{grid-column:auto}
  .ic-sheet-actions{grid-template-columns:1fr}
  .intel-stream-list{grid-template-columns:1fr}
}
@media(max-width:520px){
  .brand-name{display:none}
}
@media print{
  @page{size:auto;margin:14mm}
  :root,html[data-theme="light"],html[data-theme="dark"]{
    color-scheme:light;
    --bg:#ffffff!important;--surface-1:#ffffff!important;--surface-2:#f7eee5!important;
    --surface-3:#e9ddd1!important;--surface-raised:#ffffff!important;
    --line:#d4c6b8!important;--line-strong:#8f7c6c!important;--control-line:#6a5a4f!important;
    --control-line-hover:#443931!important;--text:#28221f!important;--text-secondary:#514741!important;
    --text-muted:#665a52!important;--accent:#075c63!important;--accent-strong:#075c63!important;
    --brick:#8b2f3d!important;--brick-soft:#f1dfe3!important;--brick-line:#b87a84!important;
    --positive:#28604b!important;--warning:#78500c!important;
    --ochre:#78500c!important;--number:#6f5016!important;--number-soft:#f2ead7!important;
    --number-line:#aa9360!important;--checkpoint:#4d5867!important;--selected:#d8e5e3!important;
    --selected-line:#075c63!important;--shadow:none!important;
    --serif:"Iowan Old Style",IowanOldStyle,Baskerville,Georgia,"Times New Roman",Times,serif!important
  }
  html,body{height:auto!important;overflow:visible!important;background:#fff!important;color:#111!important}
  .skip-link,.app-header,.kpi-strip,.filter-rail,.ic-rail,.command-bar,.active-filters,.context-bar,.inspector,
  .drawer-backdrop,.intel-head,.ic-compact-nav,.ic-archive-grid,.intel-stream,.intel-actions,.ic-sheet-local,.ic-sheet-actions,.thread-topic-list,.thread-load-boundary .secondary-action,.premium-access,.toast,.persistent-notice,.storage-alert{display:none!important}
  .workspace,.main-panel,.briefing-shell{display:block!important;height:auto!important;overflow:visible!important;background:#fff!important}
  .briefing-shell{padding:0!important}
  .intel-wrap{display:flex!important;flex-direction:column!important;width:100%;min-height:0;padding:0}
  .intel-grid{display:block}
  .intel-lead{display:contents!important;border:0;box-shadow:none;background:#fff!important;overflow:visible}
  .intel-lead-inner{order:1;padding:0 0 12px;box-shadow:none!important}
  .ic-evidence-strip{order:2}
  .research-thread{order:6;padding:7mm 0;background:#fff!important}
  .ic-analysis{order:4}
  .ic-dossier{order:5}
  .intel-title{font-size:24pt;color:#111!important}
  .ic-opening-claim,.ic-evidence-card,.research-thread,.ic-analysis,.ic-dossier,.intel-passage,.ledger-passage{background:#fff!important;color:#222!important}
  .ic-opening-claim p,.ic-evidence-card p,.ic-analysis-card p,.ic-sheet-section p{color:#222!important}
  .ic-evidence-card p{display:block!important;overflow:visible!important;-webkit-line-clamp:unset!important}
  .intel-meta,.intel-label,.source-tail,.ledger-provenance,.research-map-head p{color:#555!important}
  .intel-fact-strip,.research-map,.evidence-ledger-section,.intel-section,.ledger-head{background:#fff!important}
  .research-map-step,.intel-reason,.ledger-value,.thread-topic,.thread-passage-card,.thread-node button{background:#fff!important;color:#222!important;border-color:#888!important}
  .intel-section-grid{display:block;background:#fff;border-color:#aaa}
  .intel-section{break-inside:avoid;border-bottom:1px solid #bbb}
  .intel-passage{display:block;overflow:visible;-webkit-line-clamp:unset}
  .ledger-row{break-inside:avoid;border-color:#bbb!important}
  .evidence-ledger-section,.research-map,.thread-comparison{break-inside:avoid}
  .intel-side.ic-sheet{
    display:block!important;position:static!important;width:100%!important;height:auto!important;max-height:none!important;
    order:3;margin-top:12mm;overflow:visible!important;border:1px solid #9da5a6!important;background:#fff!important;break-before:page
  }
  .screen-only{display:none!important}
  .print-only{display:inline!important}
  .ic-sheet-inner{padding:8mm!important}
  .ic-sheet-section{display:block!important;break-inside:avoid;border-color:#bbb!important}
  .ic-sheet-section h3,.ic-sheet-section>p,.ic-sheet-section>.source-tail,.ic-sheet-section>.ic-sheet-checkpoint{grid-column:auto!important}
  .ic-sheet-checkpoint{display:block!important;break-inside:avoid;border-color:#bbb!important}
  .ic-sheet-checkpoint time{color:#4d5867!important}
  .ic-sheet-title,.ic-sheet-section h3{color:#28221f!important}
  mark{background:transparent!important;color:#111!important;font-weight:700}
}
@media(prefers-contrast:more){
  :root{
    --line:#526170;
    --line-strong:#6b7b8b;
    --control-line:#91a0ae;
    --control-line-hover:#b2bec9;
    --text-muted:#aab5c1;
  }
  html[data-theme="light"]{
    --line:#aa9786;
    --line-strong:#7f6e60;
    --control-line:#5a4c42;
    --control-line-hover:#342b25;
    --text-muted:#554a43;
  }
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}
}
@media(forced-colors:active){
  .status-dot,.facet-option.active::before{border:1px solid ButtonText}
  .status-dot.fresh,.status-dot.degraded,.status-dot.stale{background:CanvasText}
  .mix-segment{background:CanvasText!important;background-image:none!important;border-inline-end:1px solid Canvas!important}
  .direction-mix{border:1px solid CanvasText}
  .mix-legend{display:inline!important;white-space:normal}
  .facet-option.active,.facet-clear.active,.date-option.active,.view-tab.active,
  .command-button.active,.intel-lens.active,.data-row.selected,.next-item.selected{
    outline:2px solid Highlight!important;outline-offset:-2px;box-shadow:none
  }
}
</style>
</head>
<body class="density-compact" data-view="structure">
<a class="skip-link" href="#main-panel">Skip to research results</a>
<p class="sr-only">Navnoor Research Terminal</p>

<header class="app-header">
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">N/R</div>
    <div>
      <div class="brand-name">Navnoor Research</div>
      <div class="brand-sub">Investment committee library</div>
    </div>
  </div>
  <div class="global-search">
    <label class="sr-only" for="search">Search claim, entity, market, evidence, or article</label>
    <span class="search-glyph" aria-hidden="true">⌕</span>
    <input id="search" type="search" autocomplete="off" spellcheck="false" maxlength="300"
      placeholder="Search claim, entity, market, evidence…" aria-keyshortcuts="Alt+/">
    <span class="search-key" aria-hidden="true">Alt /</span>
  </div>
  <div class="header-right">
    <div class="freshness" id="freshness-summary"><span class="status-dot" id="freshness-dot" aria-hidden="true"></span><span id="freshness-state">Unknown</span><span class="freshness-separator" aria-hidden="true">·</span><span id="freshness-label">research status loading</span></div>
    <button class="utility-button header-library" type="button" data-view="research">Library</button>
    <button class="utility-button" id="palette-button" type="button" aria-label="Open command palette" aria-keyshortcuts="Control+K Meta+K">Command <span class="utility-key" aria-hidden="true">⌘K</span></button>
    <button class="utility-button" id="method-button" type="button" aria-label="Show data methodology">Method</button>
    <button class="utility-button" id="theme-button" type="button" aria-label="Switch to dark theme">Dark</button>
    <button class="utility-button" id="shortcut-button" type="button" aria-label="Show keyboard shortcuts" aria-keyshortcuts="Alt+Shift+?">?</button>
    <button class="utility-button" id="mobile-filter-button" type="button" aria-expanded="false" aria-controls="filter-rail">Filters</button>
  </div>
</header>

<section class="kpi-strip" aria-label="Article intelligence coverage">
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-latest">—</span><span class="kpi-label">Research<br>published through</span>
  </div>
  <div class="kpi-item kpi-provenance" id="kpi-provenance" role="status">
    <span class="kpi-value" id="kpi-provenance-articles">0 current · 0 prior · 0 unverified</span>
    <span class="kpi-label">Article body captures<br><span class="kpi-detail" id="kpi-provenance-observations">0 current · 0 prior · 0 unverified observations</span></span>
  </div>
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-evidence">0</span><span class="kpi-label">Articles with a<br>contextual evidence passage</span>
  </div>
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-countercase">0</span><span class="kpi-label">Countercase or<br>falsifier sections</span>
  </div>
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-implementation">0</span><span class="kpi-label">Implementation or<br>capacity sections</span>
  </div>
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-checkpoints">0</span><span class="kpi-label">Dated public<br>checkpoints cited</span>
  </div>
</section>

<div class="drawer-backdrop" id="drawer-backdrop" aria-hidden="true"></div>

<div class="workspace">
  <aside class="filter-rail" id="filter-rail" aria-label="Research filters" tabindex="-1">
    <div class="rail-header">
      <span class="rail-title">Universe filters</span>
      <div class="rail-actions">
        <button class="text-button" id="clear-filters" type="button">Clear all</button>
        <button class="text-button" id="filter-close" type="button" aria-label="Close research filters">Close</button>
      </div>
    </div>

    <section class="filter-group" aria-labelledby="preset-filter-label">
      <div class="filter-heading"><h2 id="preset-filter-label">Research triage</h2></div>
      <div class="preset-list">
        <button class="preset-button" type="button" data-preset="recent">Recent high-context passages</button>
        <button class="preset-button" type="button" data-preset="new">New since last review</button>
        <button class="preset-button" type="button" data-preset="rv">Numeric relative value</button>
        <button class="preset-button" type="button" data-preset="entity">Mentioned entity</button>
      </div>
      <p class="filter-note">Presets organize research passages; they are not recommendations or confidence scores.</p>
    </section>

    <section class="filter-group" aria-labelledby="source-filter-label">
      <div class="filter-heading"><h2 id="source-filter-label">Publication channel</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="source"><span>Any channel</span><span class="facet-count" data-count-clear="source"></span></button>
        <button class="facet-option" type="button" data-filter="source" data-value="substack"><span>Substack</span><span class="facet-count" data-count-source="substack"></span></button>
        <button class="facet-option" type="button" data-filter="source" data-value="medium"><span>Medium</span><span class="facet-count" data-count-source="medium"></span></button>
      </div>
    </section>

    <section class="filter-group" aria-labelledby="revision-filter-label">
      <div class="filter-heading"><h2 id="revision-filter-label">Body revision provenance</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="revision"><span>Any body revision</span><span class="facet-count" data-count-clear="revision"></span></button>
        <button class="facet-option" type="button" data-filter="revision" data-value="current"><span>Current source capture</span><span class="facet-count" data-count-revision="current"></span></button>
        <button class="facet-option" type="button" data-filter="revision" data-value="prior"><span>Prior revision capture</span><span class="facet-count" data-count-revision="prior"></span></button>
        <button class="facet-option" type="button" data-filter="revision" data-value="unverified"><span>Revision unverified</span><span class="facet-count" data-count-revision="unverified"></span></button>
      </div>
      <p class="filter-note">Prior and unverified captures remain searchable but require source review.</p>
    </section>

    <section class="filter-group" aria-labelledby="date-filter-label">
      <div class="filter-heading"><h2 id="date-filter-label">Time window</h2></div>
      <div class="date-options">
        <button class="date-option" type="button" data-filter="range" data-value="30d">30D</button>
        <button class="date-option" type="button" data-filter="range" data-value="90d">90D</button>
        <button class="date-option" type="button" data-filter="range" data-value="1y">1Y</button>
        <button class="date-option active" type="button" data-filter="range" data-value="all">All</button>
      </div>
    </section>

    <section class="filter-group" aria-labelledby="direction-filter-label">
      <div class="filter-heading"><h2 id="direction-filter-label">Parsed directional language</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="direction"><span>Any parsed language</span><span class="facet-count" data-count-clear="direction"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="long"><span>Long language</span><span class="facet-count" data-count-direction="long"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="short"><span>Short language</span><span class="facet-count" data-count-direction="short"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="arbitrage/relative value"><span>Relative-value language</span><span class="facet-count" data-count-direction="arbitrage/relative value"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="long/short"><span>Long/short language</span><span class="facet-count" data-count-direction="long/short"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="unspecified"><span>No reliable stance</span><span class="facet-count" data-count-direction="unspecified"></span></button>
      </div>
    </section>

    <section class="filter-group" aria-labelledby="instrument-filter-label">
      <div class="filter-heading"><h2 id="instrument-filter-label">Market / instrument</h2></div>
      <div class="facet-list two-column">
        <button class="facet-clear active" type="button" data-clear-facet="instrument"><span>Any</span><span class="facet-count" data-count-clear="instrument"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="equity"><span>Equity</span><span class="facet-count" data-count-instrument="equity"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="option"><span>Options</span><span class="facet-count" data-count-instrument="option"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="volatility"><span>Volatility</span><span class="facet-count" data-count-instrument="volatility"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="bond"><span>Bonds</span><span class="facet-count" data-count-instrument="bond"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="futures"><span>Futures</span><span class="facet-count" data-count-instrument="futures"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="commodity"><span>Commodity</span><span class="facet-count" data-count-instrument="commodity"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="FX"><span>FX</span><span class="facet-count" data-count-instrument="FX"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="swap"><span>Swaps</span><span class="facet-count" data-count-instrument="swap"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="CDS"><span>CDS</span><span class="facet-count" data-count-instrument="CDS"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="repo"><span>Repo</span><span class="facet-count" data-count-instrument="repo"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="prediction_market"><span>Prediction</span><span class="facet-count" data-count-instrument="prediction_market"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="weather_derivative"><span>Weather</span><span class="facet-count" data-count-instrument="weather_derivative"></span></button>
        <button class="facet-option" type="button" data-filter="instrument" data-value="unspecified"><span>Not stated</span><span class="facet-count" data-count-instrument="unspecified"></span></button>
      </div>
    </section>

    <section class="filter-group" aria-labelledby="manager-filter-label">
      <div class="filter-heading">
        <h2 id="manager-filter-label">Mentioned entity</h2>
        <button class="text-button" type="button" data-clear-facet="manager">Any</button>
      </div>
      <label class="sr-only" for="manager-search">Search mentioned managers, firms, and entities</label>
      <input class="manager-search" id="manager-search" type="search" autocomplete="off" placeholder="Find mentioned entity…">
      <div class="facet-list manager-options" id="manager-options">
__MANAGER_BUTTONS__
      </div>
    </section>

    <section class="filter-group" aria-labelledby="evidence-filter-label">
      <div class="filter-heading"><h2 id="evidence-filter-label">Captured fields</h2></div>
      <div class="facet-list">
        <button class="facet-option" type="button" data-filter="quality" data-value="quant"><span>Numeric context</span><span class="facet-count" data-count-quality="quant"></span></button>
        <button class="facet-option" type="button" data-filter="quality" data-value="thesis"><span>Has edge / thesis</span><span class="facet-count" data-count-quality="thesis"></span></button>
        <button class="facet-option" type="button" data-filter="quality" data-value="outcome"><span>Reported outcome</span><span class="facet-count" data-count-quality="outcome"></span></button>
        <button class="facet-option" type="button" data-filter="quality" data-value="manager"><span>Mentioned entity</span><span class="facet-count" data-count-quality="manager"></span></button>
      </div>
    </section>

    <section class="filter-group" aria-labelledby="documentation-filter-label">
      <div class="filter-heading"><h2 id="documentation-filter-label">Documentation coverage</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-documentation><span>Any coverage</span></button>
        <button class="facet-option" type="button" data-filter="documentation" data-value="triage"><span>High-context triage</span></button>
        <button class="facet-option" type="button" data-filter="documentation" data-value="documented"><span>All 5 fields captured</span></button>
        <button class="facet-option" type="button" data-filter="documentation" data-value="strong"><span>At least 4 fields</span></button>
        <button class="facet-option" type="button" data-filter="documentation" data-value="needs-context"><span>Needs context (1–2)</span></button>
        <button class="facet-option" type="button" data-filter="documentation" data-value="review"><span>Extraction review flag</span></button>
      </div>
      <p class="filter-note">Five fields: market, parsed directional language, underlying, thesis, and numeric context. Coverage is not investment quality or evidence of a position.</p>
    </section>

    <section class="filter-group" aria-labelledby="access-filter-label">
      <div class="filter-heading"><h2 id="access-filter-label">Publication access</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="access"><span>Any source access</span><span class="facet-count" data-count-clear="access"></span></button>
        <button class="facet-option" type="button" data-filter="access" data-value="public"><span>Public at source</span><span class="facet-count" data-count-access="public"></span></button>
        <button class="facet-option" type="button" data-filter="access" data-value="member"><span>Member at source</span><span class="facet-count" data-count-access="member"></span></button>
        <button class="facet-option" type="button" data-filter="access" data-value="unknown"><span>Access unverified</span><span class="facet-count" data-count-access="unknown"></span></button>
      </div>
      <p class="filter-note">Source access is distinct from how much text this public index captured.</p>
    </section>

    <section class="filter-group" aria-labelledby="content-filter-label">
      <div class="filter-heading"><h2 id="content-filter-label">Indexed coverage</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="content"><span>Full + excerpt</span><span class="facet-count" data-count-clear="content"></span></button>
        <button class="facet-option" type="button" data-filter="content" data-value="full"><span>Full text indexed</span><span class="facet-count" data-count-content="full"></span></button>
        <button class="facet-option" type="button" data-filter="content" data-value="excerpt"><span>Excerpt / metadata</span><span class="facet-count" data-count-content="excerpt"></span></button>
      </div>
    </section>

    <section class="filter-group research-only-filter" aria-labelledby="coverage-filter-label">
      <div class="filter-heading"><h2 id="coverage-filter-label">Research coverage</h2></div>
      <div class="facet-list">
        <button class="date-option active" type="button" data-filter="coverage" data-value="all">All research</button>
        <button class="date-option" type="button" data-filter="coverage" data-value="ideas">With observations</button>
        <button class="date-option" type="button" data-filter="coverage" data-value="research">Research-only</button>
      </div>
    </section>

    <section class="filter-group queue-only-filter" aria-labelledby="queue-filter-label">
      <div class="filter-heading"><h2 id="queue-filter-label">Decision queue status</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-queue-status><span>Any queue status</span></button>
        <button class="facet-option" type="button" data-filter="queue-status" data-value="review"><span>Review</span></button>
        <button class="facet-option" type="button" data-filter="queue-status" data-value="diligence"><span>Diligence</span></button>
        <button class="facet-option" type="button" data-filter="queue-status" data-value="monitor"><span>Monitor</span></button>
        <button class="facet-option" type="button" data-filter="queue-status" data-value="archived"><span>Archived</span></button>
      </div>
    </section>

    <p class="rail-disclaimer"><strong>Published-research index.</strong> Observations are rules-based extracts, not verified positions, execution records, portfolio exposures, or investment recommendations. Review every source passage and original publication.</p>
  </aside>

  <main class="main-panel" id="main-panel" tabindex="-1">
    <div class="command-bar">
      <nav class="view-tabs" aria-label="Terminal views">
        <button class="view-tab active" type="button" data-view="structure" aria-keyshortcuts="Alt+Shift+1">Structure Desk</button>
        <button class="view-tab" type="button" data-view="briefing" aria-keyshortcuts="Alt+Shift+2">Latest Brief</button>
        <button class="view-tab" type="button" data-view="ideas" aria-keyshortcuts="Alt+Shift+3">Evidence Monitor</button>
        <button class="view-tab" type="button" data-view="research" aria-keyshortcuts="Alt+Shift+4">Research Library</button>
        <button class="view-tab" type="button" data-view="queue" aria-keyshortcuts="Alt+Shift+5">Decision Queue <span id="saved-count"></span></button>
      </nav>
      <span class="result-summary" id="result-summary"></span>
      <span class="command-spacer"></span>
      <label class="sr-only" for="sort-select">Sort results</label>
      <select class="select-control table-command" id="sort-select"></select>
      <button class="command-button table-command" type="button" data-action="density"><span class="button-label">Density: </span><span id="density-label">Compact</span></button>
      <button class="command-button" type="button" data-action="copy-view">Copy view</button>
      <button class="command-button" type="button" data-action="export">Export CSV</button>
      <button class="command-button queue-command" type="button" data-action="backup-queue">Backup queue</button>
      <button class="command-button queue-command" type="button" data-action="restore-queue">Restore queue</button>
      <button class="command-button queue-command" type="button" data-action="clear-queue">Clear tab queue</button>
      <button class="command-button active" type="button" data-action="inspector" aria-pressed="true" aria-expanded="true" aria-controls="inspector">Inspector</button>
    </div>

    <div class="storage-alert" id="storage-alert" role="alert" hidden>
      <span id="storage-alert-text">Tab-session storage is unavailable. In-memory packet edits will not survive a reload; back up the queue before leaving.</span>
      <button class="command-button" id="storage-retry" type="button" data-action="retry-storage">Retry save</button>
      <button class="command-button" id="storage-backup" type="button" data-action="backup-queue">Backup queue</button>
      <button class="command-button" id="storage-raw-backup" type="button" data-action="backup-raw-storage" hidden>Back up unreadable record</button>
      <button class="command-button" id="storage-clear" type="button" data-action="clear-unreadable-storage" hidden>Discard unreadable record</button>
    </div>

    <div class="active-filters empty" id="active-filters" role="region" aria-label="Active filters"></div>

    <section class="orphaned-queue" id="orphaned-queue" aria-labelledby="orphaned-title">
      <h2 id="orphaned-title">Retained source snapshots</h2>
      <p>These local decision packets refer to passages no longer present in the current extraction. They are preserved for auditability and remain available in queue backups.</p>
      <div class="orphaned-list" id="orphaned-list"></div>
    </section>

    <section class="context-bar" aria-label="Visible universe summary">
      <div class="context-metrics">
        <span class="context-metric"><b id="visible-primary">0</b><span id="visible-primary-label">observations</span></span>
        <span class="context-metric"><b id="visible-articles">0</b><span id="visible-secondary-label">notes</span></span>
        <span class="context-metric"><b id="visible-managers">0</b><span>mentioned entities</span></span>
      </div>
      <div class="direction-mix" id="direction-mix" role="img" aria-label="Parsed directional language in visible passages; not portfolio exposure"></div>
      <span class="mix-legend" id="mix-legend"></span>
    </section>

    <section class="briefing-shell" id="briefing-shell" aria-label="Article intelligence brief"></section>

    <section class="structure-shell" id="structure-shell" aria-label="Published-research structuring and diligence"></section>

    <section class="table-shell" id="table-shell" aria-label="Research results">
      <div class="data-table" id="data-table" role="grid" aria-label="Research results" aria-rowcount="0" aria-multiselectable="false">
        <div class="table-head idea-grid" id="table-head" role="row" aria-rowindex="1"></div>
        <div id="table-body" role="rowgroup"></div>
      </div>
      <div class="empty-state" id="empty-state">
        <h2 id="empty-title">No matching records</h2>
        <p id="empty-copy">Adjust the search or clear one of the active filters.</p>
        <div class="empty-actions">
          <button class="secondary-action" type="button" data-empty-action="clear">Clear search and filters</button>
          <button class="secondary-action" type="button" data-empty-action="browse">Browse observations</button>
        </div>
      </div>
      <div class="load-more-wrap" id="load-more-wrap">
        <button class="load-more" id="load-more" type="button"></button>
      </div>
    </section>
  </main>

  <aside class="inspector" id="inspector" aria-label="Evidence inspector">
    <div class="inspector-header">
      <span class="inspector-label">Research evidence</span>
      <button class="inspector-close" id="inspector-close" type="button" aria-label="Close evidence inspector">×</button>
    </div>
    <div id="inspector-content">
      <div class="inspector-empty">
        <div class="inspector-empty-mark">N/R</div>
        <h2>Select a record</h2>
        <p>Inspect the complete idea, evidence, provenance, and source without losing your position in the monitor.</p>
      </div>
    </div>
  </aside>
</div>

<input class="sr-only" id="queue-restore-input" type="file" accept="application/json,.json"
  aria-label="Restore decision queue from a JSON file" tabindex="-1">

<div class="toast" id="toast" role="status" aria-live="polite"></div>
<div class="persistent-notice" id="persistent-notice" role="alert" hidden>
  <span id="persistent-notice-text"></span>
  <button class="command-button" id="persistent-notice-action" type="button" hidden></button>
  <button class="command-button" type="button" data-dismiss-notice>Dismiss</button>
</div>
<div class="sr-only" id="announcer" aria-live="polite" aria-atomic="true"></div>

<dialog id="command-palette" aria-labelledby="command-palette-title">
  <h2 id="command-palette-title" class="sr-only">Command palette</h2>
  <div class="palette-field">
    <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg>
    <input id="command-palette-input" type="text" role="combobox" aria-expanded="true"
      aria-controls="command-palette-list" aria-autocomplete="list" aria-describedby="command-palette-hint"
      placeholder="Jump to a view, dossier, or action…" autocomplete="off" spellcheck="false">
    <kbd>Esc</kbd>
  </div>
  <ul class="palette-list" id="command-palette-list" role="listbox" aria-label="Commands and dossiers"></ul>
  <p class="palette-empty" id="command-palette-empty" hidden>No command or dossier in this release matches that text.</p>
  <div class="palette-foot" id="command-palette-hint">
    <span><kbd>↑</kbd> <kbd>↓</kbd> navigate</span>
    <span><kbd>Enter</kbd> run</span>
    <span><kbd>Esc</kbd> close</span>
    <span>Dossier results open the brief for that publication.</span>
  </div>
</dialog>

<dialog id="shortcut-dialog" aria-labelledby="shortcut-title">
  <div class="dialog-header">
    <h2 id="shortcut-title">Keyboard workflow &amp; data method</h2>
    <button class="inspector-close" type="button" data-close-dialog aria-label="Close method and keyboard reference">×</button>
  </div>
  <div class="shortcut-grid">
    <div class="shortcut-item"><span>Command palette — views, dossiers, actions</span><span><kbd>Ctrl</kbd>/<kbd>⌘</kbd> <kbd>K</kbd></span></div>
    <div class="shortcut-item"><span>Focus global search</span><span><kbd>/</kbd> or <kbd>Alt</kbd> <kbd>/</kbd></span></div>
    <div class="shortcut-item"><span>Jump to result grid</span><span><kbd>Alt</kbd> <kbd>Shift</kbd> <kbd>G</kbd></span></div>
    <div class="shortcut-item"><span>Move through rows</span><span><kbd>J</kbd>/<kbd>K</kbd> or <kbd>↑</kbd> <kbd>↓</kbd></span></div>
    <div class="shortcut-item"><span>First / last visible row</span><span><kbd>Home</kbd> <kbd>End</kbd></span></div>
    <div class="shortcut-item"><span>Open evidence inspector</span><kbd>Enter</kbd></div>
    <div class="shortcut-item"><span>Open original research</span><span><kbd>Alt</kbd> <kbd>Shift</kbd> <kbd>O</kbd></span></div>
    <div class="shortcut-item"><span>Add or archive selected decision packet</span><span><kbd>Alt</kbd> <kbd>Shift</kbd> <kbd>S</kbd></span></div>
    <div class="shortcut-item"><span>Copy selected citation</span><span><kbd>Alt</kbd> <kbd>Shift</kbd> <kbd>C</kbd></span></div>
    <div class="shortcut-item"><span>Toggle filters</span><span><kbd>Alt</kbd> <kbd>Shift</kbd> <kbd>F</kbd></span></div>
    <div class="shortcut-item"><span>Structure / Brief / Monitor / Library / Queue</span><span><kbd>Alt</kbd> <kbd>Shift</kbd> <kbd>1–5</kbd></span></div>
    <div class="shortcut-item"><span>Close panel</span><kbd>Esc</kbd></div>
    <div class="shortcut-item"><span>Show this reference</span><span><kbd>?</kbd> or <kbd>Alt</kbd> <kbd>Shift</kbd> <kbd>?</kbd></span></div>
  </div>
  <div class="method-grid">
    <section class="method-card">
      <h3>Scope &amp; refresh</h3>
      <p>This is a research index covering one author's Substack and Medium publication channels. Cross-posted articles are deduplicated. Scheduled checks run at 9 AM, 1 PM, and 10 PM Asia/Kolkata.</p>
    </section>
    <section class="method-card">
      <h3>Article dossier method</h3>
      <ul>
        <li>Opening authored passage uses the first validated eligible prose span, with a screened source subtitle only when no lead is available.</li>
        <li>Classified evidence, mechanism, countercase, falsifier, and implementation passages retain their authored headings; a numerical-context fallback is used only when no evidence heading was captured.</li>
        <li>Directional fields classify passage language only; they do not establish the actor, a position, exposure, or a current view.</li>
        <li>Every dossier span is validated against the article body with offsets and a SHA-256 hash before publication.</li>
        <li>Full and Excerpt / metadata describe indexed coverage, never research quality. Publication access separately reports whether the canonical source marked the article public, member-access, or unverified.</li>
      </ul>
    </section>
    <section class="method-card">
      <h3>Research Threads method</h3>
      <p>Threads connect body-backed articles only when the release’s high-precision topic index captures the same normalized organization, institution, market, instrument, model, or mechanism in at least two publications. Every timeline entry identifies the matched source field, uses the exact available publication timestamp, and sits inside a bounded seven-entry window. Capture comparisons report only whether research-role passages were detected in each source; opening-passage numbers stay attached to that exact passage. These fields do not infer a current view, reversal, contradiction, conviction, accuracy, performance, or portfolio action. Exact older passages load only on request and must match this release’s checksum.</p>
    </section>
    <section class="method-card">
      <h3>Decision boundary</h3>
      <p>Records are research observations—not verified trades, current holdings, or recommendations. This terminal supports published-source intake and a human-entered decision packet. It does not contain live prices, positions, P&amp;L, sizing, execution, portfolio risk, liquidity, financing, counterparties, investor records, or compliance approvals.</p>
    </section>
    <section class="method-card">
      <h3>Tab-session decision queue</h3>
      <p>Queue packets and self-attested diligence gates stay only in this browser tab session unless you export a plaintext backup. They are not an authenticated or immutable enterprise audit record. Do not enter confidential, personal, client, position, or regulated information.</p>
    </section>
    <section class="method-card">
      <h3>Privacy &amp; measurement</h3>
      <p>The terminal has no advertising, cookies, third-party analytics, session replay, or background data submission. Theme and review state use functional device storage; decision packets use only tab-session storage. The browser requests only release-bound files from this site; an external publication or subscription page opens only when you choose it. Subscription links contain no search, filter, article, or decision-queue context.</p>
    </section>
    <section class="method-card">
      <h3>Institutional basis</h3>
      <p>The workflow is informed by <a href="https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/investment-manager-selection" target="_blank" rel="noopener noreferrer">CFA Institute’s manager-selection framework</a>, <a href="https://www.aima.org/article/presenting-the-2025-edition.html" target="_blank" rel="noopener noreferrer">AIMA’s 2025 manager DDQ</a>, <a href="https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a" target="_blank" rel="noopener noreferrer">CFA Institute’s reasonable-basis standard</a>, <a href="https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-c" target="_blank" rel="noopener noreferrer">its research-record guidance</a>, and the <a href="https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-adviser-marketing" target="_blank" rel="noopener noreferrer">SEC’s substantiation and presentation principles for adviser marketing</a>. These references shape research questions, evidence retention, and disclosure boundaries; they do not certify a packet or establish legal compliance.</p>
    </section>
    <section class="method-card">
      <h3>Owner operating stack</h3>
      <p>Daily P&amp;L, exposure, leverage, concentration, stress, cash and margin require an OMS/PMS, market data, administrator, prime-broker and risk feeds. Operations, governance and investor oversight require controlled compliance, accounting and CRM systems.</p>
    </section>
  </div>
  <p class="dialog-foot">Shortcuts are disabled while typing in a form control. Always review the original publication and perform independent diligence.</p>
</dialog>

<dialog id="manual-copy-dialog" aria-labelledby="manual-copy-title">
  <div class="dialog-header">
    <h2 id="manual-copy-title">Copy text manually</h2>
    <button class="inspector-close" id="manual-copy-close" type="button" aria-label="Close manual copy dialog">×</button>
  </div>
  <div class="manual-copy-body">
    <p>Automatic clipboard access was blocked. The complete text is preserved below; select it and use your system copy command.</p>
    <textarea class="manual-copy-text" id="manual-copy-text" readonly aria-label="Text ready to copy"></textarea>
    <div class="manual-copy-actions">
      <button class="command-button" id="manual-copy-select" type="button">Select all text</button>
      <button class="primary-action" id="manual-copy-done" type="button">Done</button>
    </div>
  </div>
</dialog>

<noscript><div>This research terminal requires JavaScript to filter and inspect the embedded dataset.</div></noscript>

<script>
const ARTICLE_WIRE_SCHEMA_VERSION = __ARTICLE_WIRE_SCHEMA_VERSION__;
const SUBSCRIPTION_URL = __SUBSCRIPTION_URL_JSON__;
const ARTICLES = __ARTICLES_JSON__;
function hydrateEmbeddedArticle(article) {
  const publishedAt = String(article.published_at || '');
  const wordcount = Number(article.wordcount || 0);
  const wholeMinutes = Math.floor(wordcount / 220);
  const remainder = wordcount % 220;
  const roundedMinutes = wholeMinutes + (
    remainder > 110 || (remainder === 110 && wholeMinutes % 2 === 1) ? 1 : 0
  );
  const briefCode = Array.isArray(article._b) ? article._b : [0,0];
  const briefMask = Number(briefCode[0] || 0);
  const coverageMask = Number(article._q || 0);
  article.date = publishedAt.slice(0,10);
  article.publication_precision = /^\d{4}-\d{2}-\d{2}$/.test(publishedAt) ? 'day' : 'instant';
  article.read_minutes = wordcount ? Math.max(1,roundedMinutes) : 0;
  article.alternate_urls = article.alternate_urls || {};
  article.brief = article.brief || null;
  const publicationAccess = String(article.publication_access || '');
  article.publication_access = ['public','member','unknown'].includes(publicationAccess)
    ? publicationAccess : 'unknown';
  const memberPreviewChars = Number(article.member_preview_chars || 0);
  article.member_preview_chars = Number.isSafeInteger(memberPreviewChars) && memberPreviewChars >= 0 && memberPreviewChars <= 1200
    ? memberPreviewChars : 0;
  const revisionStatus = String(article.body_revision_status || '');
  article.body_revision_status = ['current','prior','unverified'].includes(revisionStatus)
    ? revisionStatus : 'unverified';
  article.source_updated_at = String(article.source_updated_at || '');
  article.observed_source_updated_at = String(article.observed_source_updated_at || '');
  if (article.publication_access === 'member') article.content_status = 'excerpt';
  if (article.body_revision_status !== 'current') article.content_status = 'excerpt';
  article.idea_ids = article.idea_ids || [];
  article.trade_count = article.idea_ids.length;
  article.directions = article.directions || [];
  article.instruments = article.instruments || [];
  article.underlyings = article.underlyings || [];
  article.managers = article.managers || [];
  article.manager_keys = article.manager_keys || [];
  article.brief_features = {
    lead:Boolean(briefMask & 1),
    evidence:Boolean(briefMask & 2),
    countercase:Boolean(briefMask & 4),
    falsifier:Boolean(briefMask & 8),
    implementation:Boolean(briefMask & 16),
    mechanism:Boolean(briefMask & 32),
    checkpoint_count:Number(briefCode[1] || 0)
  };
  article.has_quant = Boolean(coverageMask & 1);
  article.has_thesis = Boolean(coverageMask & 2);
  article.has_outcome = Boolean(coverageMask & 4);
  delete article._b;
  delete article._q;
  return article;
}
ARTICLES.forEach(hydrateEmbeddedArticle);
const THREADS = __THREADS_JSON__;
const RATE_CONTEXT = __RATE_CONTEXT_JSON__;
const DESK_FACETS = __DESK_FACETS_JSON__;
const THREAD_ARTICLES = (function () {
  const rows = Object.create(null);
  Object.keys(THREADS.topics || {}).forEach(function (topicKey) {
    const topic = THREADS.topics[topicKey];
    (topic.article_ids || []).forEach(function (articleId) {
      if (!rows[articleId]) rows[articleId] = {topics:[],default_topic:''};
      rows[articleId].topics.push(topicKey);
    });
  });
  Object.keys(rows).forEach(function (articleId) {
    rows[articleId].topics.sort(function (left,right) {
      const leftTopic = THREADS.topics[left];
      const rightTopic = THREADS.topics[right];
      return leftTopic.article_count - rightTopic.article_count ||
        leftTopic.label.localeCompare(rightTopic.label) || left.localeCompare(right);
    });
    rows[articleId].default_topic = THREADS.defaults[articleId];
  });
  return rows;
})();
let IDEAS = [];
const SNAPSHOT = __SNAPSHOT_JSON__;
const EMBEDDED_MANAGER_LABELS = __MANAGER_LABELS_JSON__;
const BRIEF_ARCHIVE_SHA256 = document.querySelector('meta[name="nrt-brief-archive-sha256"]').content;
const OBSERVATION_ARCHIVE_SHA256 = document.querySelector('meta[name="nrt-observation-archive-sha256"]').content;

let briefArchivePromise = null;
let briefArchiveReady = false;
let briefArchiveFailed = false;
const DEFERRED_BRIEF_KEYS = ['checkpoints','fallback_evidence','lead','sections'];
const DEFERRED_SPAN_KEYS = ['end','sha256','start','text','truncated'];
const DEFERRED_SECTION_KEYS = ['end','heading','kind','sha256','source_order','start','text','truncated'];
const DEFERRED_CHECKPOINT_KEYS = ['context_kind','date','date_label','end','sha256','start','text','truncated'];
const DEFERRED_SECTION_KINDS = new Set(['evidence','mechanism','countercase','falsifier','implementation']);
function hasExactObjectKeys(value,expectedKeys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const actualKeys = Object.keys(value).sort();
  return actualKeys.length === expectedKeys.length && actualKeys.every(function (key,index) { return key === expectedKeys[index]; });
}
function sha256Text(value) {
  if (!window.crypto || !window.crypto.subtle || typeof TextEncoder !== 'function') {
    return Promise.reject(new Error('Cryptographic dossier verification is unavailable'));
  }
  return window.crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)).then(function (buffer) {
    return Array.from(new Uint8Array(buffer)).map(function (byte) { return byte.toString(16).padStart(2,'0'); }).join('');
  });
}
function validateDeferredSpan(span,expectedKeys,label,hashChecks) {
  if (!hasExactObjectKeys(span,expectedKeys)) throw new Error(label + ' has an invalid shape');
  if (typeof span.text !== 'string' || !span.text.length || typeof span.truncated !== 'boolean') throw new Error(label + ' has invalid source text');
  if (!Number.isSafeInteger(span.start) || !Number.isSafeInteger(span.end) || span.start < 0 || span.end <= span.start) throw new Error(label + ' has invalid source offsets');
  if (span.end - span.start !== Array.from(span.text).length) throw new Error(label + ' source offsets do not match its text');
  if (typeof span.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(span.sha256)) throw new Error(label + ' has an invalid source hash');
  hashChecks.push({text:span.text,sha256:span.sha256,label:label});
}
function validDeferredCheckpointDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(value + 'T00:00:00Z');
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0,10) === value;
}
function validateDeferredFeatureParity(article,brief) {
  const features = article && article.brief_features || {};
  const kinds = brief.sections.map(function (section) { return section.kind; });
  const uniqueKinds = new Set(kinds);
  const sourceOrders = brief.sections.map(function (section) { return section.source_order; });
  const checkpointDates = brief.checkpoints.map(function (checkpoint) { return checkpoint.date; });
  if (uniqueKinds.size !== kinds.length) throw new Error('Deferred article dossier contains duplicate section kinds');
  if (new Set(sourceOrders).size !== sourceOrders.length || sourceOrders.some(function (value,index) { return index > 0 && value <= sourceOrders[index - 1]; })) {
    throw new Error('Deferred article dossier sections are not in unique source order');
  }
  if (checkpointDates.some(function (value,index) { return index > 0 && value < checkpointDates[index - 1]; })) {
    throw new Error('Deferred article checkpoints are not date ordered');
  }
  if (uniqueKinds.has('evidence') && brief.fallback_evidence !== null) throw new Error('Deferred dossier cannot contain explicit and fallback evidence together');
  const captured = {
    lead:brief.lead !== null,
    evidence:uniqueKinds.has('evidence') || brief.fallback_evidence !== null,
    mechanism:uniqueKinds.has('mechanism'),countercase:uniqueKinds.has('countercase'),
    falsifier:uniqueKinds.has('falsifier'),implementation:uniqueKinds.has('implementation')
  };
  ['lead','evidence','mechanism','countercase','falsifier','implementation'].forEach(function (key) {
    if (captured[key] !== Boolean(features[key])) throw new Error('Deferred dossier coverage does not match embedded release features');
  });
  if (brief.checkpoints.length !== Number(features.checkpoint_count || 0)) throw new Error('Deferred dossier checkpoint count does not match embedded release features');
}
async function validateDeferredBriefArchive(payload) {
  if (!payload || payload.schema_version !== 1 || payload.data_checksum !== SNAPSHOT.data_checksum || !payload.briefs || typeof payload.briefs !== 'object' || Array.isArray(payload.briefs)) {
    throw new Error('Deferred article dossiers do not match this release');
  }
  const expectedIds = ARTICLES.filter(function (article) { return article.brief === null; }).map(function (article) { return article.id; });
  const expectedIdSet = new Set(expectedIds);
  const actualIds = Object.keys(payload.briefs);
  if (actualIds.length !== expectedIds.length || actualIds.some(function (id) { return !expectedIdSet.has(id); }) || expectedIds.some(function (id) { return !Object.prototype.hasOwnProperty.call(payload.briefs,id); })) {
    throw new Error('Deferred article dossier identities do not match this release');
  }
  const hashChecks = [];
  expectedIds.forEach(function (id) {
    const brief = payload.briefs[id];
    if (!hasExactObjectKeys(brief,DEFERRED_BRIEF_KEYS) || !Array.isArray(brief.sections) || !Array.isArray(brief.checkpoints)) throw new Error('Deferred article dossier has an invalid shape');
    if (brief.lead !== null) validateDeferredSpan(brief.lead,DEFERRED_SPAN_KEYS,'Deferred lead',hashChecks);
    if (brief.fallback_evidence !== null) validateDeferredSpan(brief.fallback_evidence,DEFERRED_SPAN_KEYS,'Deferred fallback evidence',hashChecks);
    brief.sections.forEach(function (section) {
      if (!DEFERRED_SECTION_KINDS.has(section && section.kind) || typeof section.heading !== 'string' || !section.heading.length || !Number.isSafeInteger(section.source_order) || section.source_order < 0) throw new Error('Deferred article section has invalid metadata');
      validateDeferredSpan(section,DEFERRED_SECTION_KEYS,'Deferred article section',hashChecks);
    });
    brief.checkpoints.forEach(function (checkpoint) {
      if (!validDeferredCheckpointDate(checkpoint && checkpoint.date) || typeof checkpoint.date_label !== 'string' || !checkpoint.date_label.length || typeof checkpoint.context_kind !== 'string' || !DEFERRED_SECTION_KINDS.has(checkpoint.context_kind)) throw new Error('Deferred article checkpoint has invalid metadata');
      validateDeferredSpan(checkpoint,DEFERRED_CHECKPOINT_KEYS,'Deferred article checkpoint',hashChecks);
    });
    validateDeferredFeatureParity(ARTICLE_BY_ID.get(id),brief);
  });
  await Promise.all(hashChecks.map(function (check) {
    return sha256Text(check.text).then(function (actualHash) {
      if (actualHash !== check.sha256) throw new Error(check.label + ' source hash does not match its text');
    });
  }));
  return payload.briefs;
}
function releaseMismatchError(message) {
  const error = new Error(message);
  error.releaseMismatch = true;
  return error;
}
function recoverFromStaleReleaseShell() {
  const token = String(SNAPSHOT.data_checksum || '').slice(0,16);
  if (!token) return false;
  const current = new URL(window.location.href);
  if (current.searchParams.get('nrt_release') === token) return false;
  current.searchParams.set('nrt_release',token);
  window.location.replace(current.href);
  return true;
}
function fetchReleaseText(url,unavailableMessage) {
  const controller = new AbortController();
  const timeoutId = setTimeout(function () { controller.abort(); },20_000);
  return fetch(url,{
    credentials:'same-origin',cache:'no-cache',signal:controller.signal
  }).then(function (response) {
    if (!response.ok) throw new Error(unavailableMessage);
    return response.text();
  }).catch(function (error) {
    if (error && error.name === 'AbortError') throw new Error(unavailableMessage + ' (request timed out)');
    throw error;
  }).finally(function () {
    clearTimeout(timeoutId);
  });
}
function loadBriefArchive() {
  if (briefArchivePromise) return briefArchivePromise;
  briefArchiveFailed = false;
  const archiveUrl = 'article_briefs.json?v=' + encodeURIComponent(String(SNAPSHOT.data_checksum || ''));
  briefArchivePromise = fetchReleaseText(archiveUrl,'Deferred article dossiers are unavailable').then(function (archiveText) {
    return sha256Text(archiveText).then(function (actualHash) {
      if (actualHash !== BRIEF_ARCHIVE_SHA256) throw releaseMismatchError('Deferred article dossier asset does not match this release');
      try { return JSON.parse(archiveText); } catch (_error) { throw new Error('Deferred article dossier asset is invalid JSON'); }
    });
  }).then(function (payload) {
    return validateDeferredBriefArchive(payload);
  }).then(function (validatedBriefs) {
    Object.keys(validatedBriefs).forEach(function (id) {
      const article = ARTICLE_BY_ID.get(id);
      article.brief = validatedBriefs[id];
      refreshArticleSearch(article);
    });
    briefArchiveReady = true;
    briefArchiveFailed = false;
    relevanceScoreCache = new WeakMap();
    return validatedBriefs;
  }).catch(function (error) {
    briefArchiveFailed = true;
    briefArchivePromise = null;
    if (error && error.releaseMismatch) recoverFromStaleReleaseShell();
    throw error;
  });
  return briefArchivePromise;
}
function retryBriefArchive() {
  briefArchivePromise = null;
  briefArchiveReady = false;
  briefArchiveFailed = false;
  ARTICLES.forEach(function (article) { delete article._briefLoadFailed; });
  return loadBriefArchive();
}
function ensureArticleBrief(article) {
  if (!article || article.brief) return Promise.resolve(article && article.brief);
  return loadBriefArchive().then(function (briefs) {
    if (!Object.prototype.hasOwnProperty.call(briefs,article.id)) throw new Error('Exact deferred article dossier is unavailable');
    article.brief = briefs[article.id];
    return article.brief;
  }).catch(function () {
    article._briefLoadFailed = true;
    return null;
  });
}

const ARTICLE_BY_ID = new Map(ARTICLES.map(function (article) { return [article.id, article]; }));
let IDEA_BY_ID = new Map();
const MANAGER_LABELS = new Map(Object.entries(EMBEDDED_MANAGER_LABELS));
const MANAGERS = Array.from(MANAGER_LABELS.keys()).sort(function (a, b) { return MANAGER_LABELS.get(a).localeCompare(MANAGER_LABELS.get(b)); });
let observationsPromise = null;
let observationGatePromise = null;
let observationsReady = false;
let observationsFailed = false;
let pendingObservationFocus = null;
function installObservations(rows) {
  if (!Array.isArray(rows) || rows.length !== Number(SNAPSHOT.observation_count || 0)) throw new Error('Observation count does not match this release');
  const expectedArticleById = new Map();
  ARTICLES.forEach(function (article) {
    if (!Array.isArray(article.idea_ids)) throw new Error('Article observation references are invalid');
    article.idea_ids.forEach(function (id) {
      if (!id || expectedArticleById.has(id)) throw new Error('Article observation references are not unique');
      expectedArticleById.set(id,article.id);
    });
  });
  if (expectedArticleById.size !== rows.length) throw new Error('Article references do not match the observation count');
  const nextMap = new Map();
  rows.forEach(function (idea) {
    if (!idea || !idea.id || nextMap.has(idea.id) || expectedArticleById.get(idea.id) !== idea.article_id) throw new Error('Observation archive contains an invalid identity or owner');
    if (!VALID_DIRECTIONS.has(idea.direction) || !Array.isArray(idea.instruments) || !idea.instruments.length || idea.instruments.some(function (value) { return !VALID_INSTRUMENTS.has(value); })) throw new Error('Observation archive contains an invalid classification');
    if (typeof idea.description !== 'string' || typeof idea.underlying !== 'string' || typeof idea.thesis !== 'string' || typeof idea.quant !== 'string' || typeof idea.outcome !== 'string' || typeof idea.manager !== 'string') throw new Error('Observation archive contains an invalid source field');
    nextMap.set(idea.id,idea);
  });
  if (nextMap.size !== expectedArticleById.size || Array.from(expectedArticleById.keys()).some(function (id) { return !nextMap.has(id); })) throw new Error('Observation archive is incomplete');
  IDEAS = rows;
  IDEA_BY_ID = nextMap;
  ARTICLES.forEach(function (article) {
    article._ideas = article.idea_ids.map(function (id) { return IDEA_BY_ID.get(id); });
    refreshArticleSearch(article);
  });
  IDEAS.forEach(function (idea) {
    idea._article = ARTICLE_BY_ID.get(idea.article_id);
    idea._search = normalize([
      idea._article.title,idea._article.subtitle,idea._article.source,
      idea.description,idea.direction,idea.instruments.join(' '),idea.underlying,
      idea.thesis,idea.quant,idea.outcome,idea.manager
    ].join(' '));
  });
  observationsReady = true;
  observationsFailed = false;
  queryCacheKey = null;
  relevanceScoreCache = new WeakMap();
  migrateLegacySavedIdeas();
  return IDEAS;
}
function loadObservations() {
  if (observationsReady) return Promise.resolve(IDEAS);
  if (observationsPromise) return observationsPromise;
  observationsFailed = false;
  const url = 'observations.json?v=' + encodeURIComponent(String(SNAPSHOT.data_checksum || ''));
  observationsPromise = fetchReleaseText(url,'Observation archive is unavailable').then(function (archiveText) {
    return sha256Text(archiveText).then(function (actualHash) {
      if (actualHash !== OBSERVATION_ARCHIVE_SHA256) throw releaseMismatchError('Observation archive asset does not match this release');
      try { return JSON.parse(archiveText); } catch (_error) { throw new Error('Observation archive asset is invalid JSON'); }
    });
  }).then(function (payload) {
    if (!payload || payload.schema_version !== 1 || payload.data_checksum !== SNAPSHOT.data_checksum) throw new Error('Observation archive does not match this release');
    return installObservations(payload.observations);
  }).catch(function (error) {
    observationsFailed = true;
    observationsPromise = null;
    if (error && error.releaseMismatch) recoverFromStaleReleaseShell();
    throw error;
  });
  return observationsPromise;
}
function retryObservations() {
  observationsPromise = null;
  observationsReady = false;
  observationsFailed = false;
  return loadObservations();
}
const MAX_DATE = ARTICLES.reduce(function (latest, article) { return article.date > latest ? article.date : latest; }, '1970-01-01');
const VALID_SOURCES = new Set(['substack','medium']);
const VALID_BODY_REVISIONS = new Set(['current','prior','unverified']);
const VALID_DIRECTIONS = new Set(['long','short','arbitrage/relative value','long/short','unspecified']);
const VALID_INSTRUMENTS = new Set(['equity','option','volatility','bond','futures','commodity','FX','swap','CDS','repo','prediction_market','weather_derivative','unspecified']);
const VALID_QUALITY = new Set(['quant','thesis','outcome','manager']);
const VALID_RATE_BANDS = new Set(['low','mid','high']);
const VALID_CONTENT = new Set(['full','excerpt']);
const VALID_PUBLICATION_ACCESS = new Set(['public','member','unknown']);
const VALID_DOCUMENTATION = new Set(['triage','documented','strong','needs-context','review']);
const VALID_QUEUE_STATUSES = new Set(['review','diligence','monitor','archived']);
const VALID_PRIORITIES = new Set(['low','normal','high']);
const VALID_CONFIDENCE = new Set(['unrated','low','medium','high']);
const BRIEF_LENSES = Object.freeze([
  ['all','Latest'],['checkpoint','Public checkpoints'],['evidence','Contextual evidence'],
  ['countercase','Countercase'],['falsifier','Falsifiers'],['implementation','Implementation / capacity']
]);
const VALID_BRIEF_LENSES = new Set(BRIEF_LENSES.map(function (row) { return row[0]; }));
const DILIGENCE_GATES = [
  ['source','Original publication reviewed'],
  ['independent','Independent evidence obtained'],
  ['market','Live market price and valuation checked'],
  ['liquidity','Liquidity, capacity, borrow and funding checked'],
  ['portfolio','Portfolio exposure, correlation and stress checked'],
  ['compliance','Legal and compliance constraints checked']
];
const PACKET_CASE_FIELDS = ['thesis','contrary','catalyst','horizon','payoff','risk','implementation','portfolio'];
const WORKFLOW_TEXT_LIMITS = {
  tags:500,note:4000,owner:120,horizon:160,thesis:1800,contrary:1600,
  catalyst:1400,payoff:1800,risk:1800,implementation:1800,portfolio:1800,
  next_action:700
};
const MAX_QUEUE_ITEMS = 250;
const PAGE_SIZE = {briefing:24,ideas:50,research:80,queue:100,structure:8};
const WORKFLOW_KEY = 'nrt-decision-queue-session-v3';
const RESTORE_ROLLBACK_KEY = 'nrt-decision-queue-restore-rollback-v1';
const LEGACY_LOCAL_WORKFLOW_KEYS = ['nrt-decision-queue-v2','nrt-decision-queue-v1','nrt-saved-ideas'];
const QUEUE_BOUNDARY_ACK_KEY = 'nrt-queue-session-boundary-v2';
const REVIEWED_ARTICLE_IDS_KEY = 'nrt-reviewed-article-ids-v1';
const LEGACY_LAST_SEEN_KEY = 'nrt-last-seen-publication';

function normalize(value) {
  return String(value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
}
function articleBriefSearch(article) {
  if (!article || !article.brief) return '';
  return [
    article.brief.lead && article.brief.lead.text,
    (article.brief.sections || []).map(function (section) { return section.heading + ' ' + section.text; }).join(' '),
    (article.brief.checkpoints || []).map(function (checkpoint) { return checkpoint.text; }).join(' '),
    article.brief.fallback_evidence && article.brief.fallback_evidence.text
  ].join(' ');
}
function refreshArticleSearch(article) {
  if (!article) return;
  article._search = normalize([
    article.title,article.subtitle,article.source,article.instruments.join(' '),
    article.directions.join(' '),article.managers.join(' '),articleBriefSearch(article),
    (article._ideas || []).map(function (idea) {
      return [idea.description,idea.underlying,idea.thesis,idea.quant,idea.outcome,idea.manager].join(' ');
    }).join(' ')
  ].join(' '));
}
function instrumentLabel(value) {
  const labels = {
    option:'Options', volatility:'Volatility', equity:'Equity', bond:'Bonds',
    futures:'Futures', commodity:'Commodity', FX:'FX', repo:'Repo', swap:'Swaps',
    CDS:'CDS', prediction_market:'Prediction market',
    weather_derivative:'Weather derivative', unspecified:'Not stated'
  };
  return labels[value] || value;
}
function directionLabel(value) {
  const labels = {
    long:'Long directional language', short:'Short directional language',
    'arbitrage/relative value':'Relative-value language',
    'long/short':'Long/short language', unspecified:'No reliable stance'
  };
  return labels[value] || 'No reliable stance';
}
function compactDirectionLabel(value) {
  if (value === 'unspecified') return 'No stance';
  if (value === 'long') return 'Long language';
  if (value === 'short') return 'Short language';
  if (value === 'long/short') return 'L/S language';
  return 'RV language';
}
function directionClass(value) {
  if (value === 'long') return 'dir-long';
  if (value === 'short') return 'dir-short';
  if (value === 'arbitrage/relative value') return 'dir-arb';
  if (value === 'long/short') return 'dir-ls';
  return 'dir-unspecified';
}
function sourceLabel(value) {
  return value === 'medium' ? 'Medium' : 'Substack';
}
function sourceModeLabel(value) {
  const labels = {
    complete_api:'Complete API',
    complete_api_degraded_body_provenance:'Complete catalogue · body provenance limited',
    cached_archive_plus_rss:'Cached archive + RSS',
    rss:'RSS'
  };
  return labels[value] || String(value || 'Mode not recorded').replace(/_/g,' ');
}
function sourceCollectionSummary(value) {
  const info = SNAPSHOT.sources && SNAPSHOT.sources[value] || {};
  const statusLabels = {ok:'OK',degraded:'Degraded',error:'Unavailable'};
  const status = statusLabels[info.status] || 'Status unknown';
  return sourceLabel(value) + ' collection: ' + status + ' · ' + sourceModeLabel(info.mode) + ' · checked ' + formatReleaseCheckedAt(info.checked_at);
}
function bodyRevisionLabel(article) {
  if (!article || article.body_revision_status === 'unverified') return 'Revision unverified';
  if (article.body_revision_status === 'prior') return 'Prior revision capture';
  return 'Current source capture';
}
function bodyRevisionSummary(article) {
  if (!article) return 'Body revision provenance is unavailable.';
  const sourceRevision = String(article.source_updated_at || '');
  const observedRevision = String(article.observed_source_updated_at || '');
  if (article.body_revision_status === 'prior') {
    return 'Historical excerpt: captured from source revision ' + sourceRevision +
      '; the source currently reports revision ' + observedRevision +
      '. Re-open the original before relying on the passage as current.';
  }
  if (article.body_revision_status === 'unverified') {
    const revision = sourceRevision ? ' Last attributed body revision: ' + sourceRevision + '.' : '';
    const observed = observedRevision ? ' Source revision observed this refresh: ' + observedRevision + '.' : '';
    return 'Cached excerpt retained for search, but its body revision was not verified during this refresh.' +
      revision + observed + ' Re-open the original before relying on the passage.';
  }
  return sourceRevision
    ? 'Body capture matches source revision ' + sourceRevision + '.'
    : 'Current live capture; this source did not expose a separate revision timestamp.';
}
function bodyRevisionWarningMarkup(article) {
  if (!article || article.body_revision_status === 'current') return '';
  return '<div class="review-notice revision-warning" role="note"><strong>' +
    escapeHtml(bodyRevisionLabel(article)) + ':</strong> ' +
    escapeHtml(bodyRevisionSummary(article)) + '</div>';
}
function escapeHtml(value) {
  return String(value || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}
function safeUrl(value) {
  try {
    const original = String(value || '');
    const url = new URL(original);
    const host = url.hostname.toLowerCase();
    if (url.href !== original || url.protocol !== 'https:' ||
        url.username || url.password || url.port ||
        url.search || url.hash || /%(?:2f|5c)/i.test(url.pathname)) return '#';
    const substackSlug = (
      host === 'navnoorbawa.substack.com' ||
      host === 'www.navnoorbawaresearch.com'
    ) && url.pathname.startsWith('/p/') ? url.pathname.slice(3) : '';
    const mediumPrefix = '/@navnoorbawa/';
    const validSubstackPath = /^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(substackSlug);
    let validMediumPath = false;
    if (host === 'medium.com' && url.pathname.startsWith(mediumPrefix)) {
      const rawSlug = url.pathname.slice(mediumPrefix.length);
      try {
        const slug = decodeURIComponent(rawSlug);
        validMediumPath = rawSlug === encodeURIComponent(slug) &&
          slug.normalize('NFC') === slug && /^[\p{L}\p{N}_-]+$/u.test(slug) &&
          /^(?:.+-)?[0-9a-f]{12}$/i.test(slug);
      } catch (_error) {
        validMediumPath = false;
      }
    }
    const ownedPath = validSubstackPath || validMediumPath;
    return ownedPath ? url.href : '#';
  } catch (_error) {
    return '#';
  }
}
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function formatDate(value) {
  if (!value || value === '1970-01-01') return '—';
  const parts = value.split('-').map(Number);
  return MONTHS[parts[1] - 1] + ' ' + parts[2] + ', ' + parts[0];
}
function shortDate(value) {
  if (!value || value === '1970-01-01') return '—';
  const parts = value.split('-').map(Number);
  return String(parts[2]).padStart(2,'0') + ' ' + MONTHS[parts[1] - 1] + ' ' + String(parts[0]).slice(2);
}
function number(value) {
  return Number(value || 0).toLocaleString();
}
function hasValue(value) {
  return Boolean(String(value || '').trim());
}
function isArticleView() {
  return state.view === 'briefing' || state.view === 'research';
}
function briefSection(article,kind) {
  return ((article && article.brief && article.brief.sections) || []).find(function (section) {
    return section.kind === kind;
  }) || null;
}
function articleClaim(article) {
  if (article && article.publication_access === 'member' && !hasIndexedMemberPreview(article)) {
    return String(article.subtitle || article.title || 'No anonymous body preview is available in this release.');
  }
  const lead = article && article.brief && article.brief.lead;
  return String((lead && lead.text) || (article && article.subtitle) || 'No opening authored passage is available in this index.');
}
function hasIndexedMemberPreview(article) {
  return Boolean(article && article.publication_access === 'member' && Number(article.member_preview_chars || 0) > 0);
}
function isPaidSubstackArticle(article) {
  return Boolean(article && article.source === 'substack' && article.publication_access === 'member');
}
function publicationAccessLabel(article) {
  if (!article || article.publication_access === 'unknown') return 'Access unverified';
  return article.publication_access === 'member' ? 'Member at source' : 'Public at source';
}
function sourceAccessLabel(article) {
  if (isPaidSubstackArticle(article)) return hasIndexedMemberPreview(article) ? 'Subscriber preview' : 'Subscriber source · metadata only';
  if (article && article.source === 'medium' && article.publication_access === 'member') return hasIndexedMemberPreview(article) ? 'Member preview' : 'Medium member source · metadata only';
  return article && article.content_status === 'full' ? 'Full text indexed' : 'Excerpt indexed';
}
function sourceActionLabel(article) {
  if (isPaidSubstackArticle(article)) return 'Read full note on Substack ↗';
  if (article && article.source === 'medium' && article.publication_access === 'member') return 'Open member article ↗';
  return 'Open original ↗';
}
function accessBadgeMarkup(article) {
  if (isPaidSubstackArticle(article)) {
    return '<span class="coverage-badge premium-badge">' + (hasIndexedMemberPreview(article) ? 'Subscriber preview' : 'Subscriber metadata') + '</span>';
  }
  if (article && article.source === 'medium' && article.publication_access === 'member') {
    return '<span class="coverage-badge">' + (hasIndexedMemberPreview(article) ? 'Medium member preview' : 'Medium member metadata') + '</span>';
  }
  return '';
}
function boundedPromotionText(value,limit) {
  const text = String(value || '').replace(/\s+/g,' ').trim();
  if (text.length <= limit) return text;
  const head = text.slice(0,limit + 1);
  const boundary = head.lastIndexOf(' ');
  return (boundary > Math.floor(limit * 0.7) ? head.slice(0,boundary) : text.slice(0,limit)).trimEnd() + '…';
}
function premiumAccessMarkup(article,context) {
  if (!isPaidSubstackArticle(article)) return '';
  const labelId = 'premium-access-' + String(context || 'record') + '-' + article.id;
  const headingTag = context === 'brief' ? 'h2' : 'h3';
  const modeClass = context === 'brief' ? ' premium-access-brief' : ' premium-access-inspector';
  const hasCapturedPreview = hasIndexedMemberPreview(article);
  const kicker = hasCapturedPreview
    ? article.body_revision_status === 'current' ? 'Subscriber source · public preview indexed' : 'Subscriber source · stored public preview · ' + bodyRevisionLabel(article).toLowerCase()
    : 'Subscriber source · metadata only · no anonymous body preview';
  const framing = boundedPromotionText(hasCapturedPreview ? articleClaim(article) : (article.subtitle || article.title),320);
  const framingLabel = hasCapturedPreview ? 'Indexed public preview' : 'Published metadata';
  const framingMarkup = context === 'brief' ? '' :
    '<div class="premium-access-evidence"><span>' + escapeHtml(framingLabel) + '</span><p>' + escapeHtml(framing) + '</p></div>';
  const boundaryCopy = hasCapturedPreview
    ? 'This release contains only the anonymous source preview proven by its snapshot. The complete subscriber article—and any argument, evidence, countercase, or implementation context not captured here—remains on Navnoor Research.'
    : 'This release contains the published title, date, source, and available subtitle, but no anonymous article-body preview. The complete subscriber article remains on Navnoor Research.';
  return '<section class="premium-access' + modeClass + '" aria-labelledby="' + labelId + '">' +
    '<div class="premium-access-head"><div><div class="premium-access-kicker">' + escapeHtml(kicker) + '</div><' + headingTag + ' id="' + labelId + '">Continue the complete research note</' + headingTag + '></div><span class="premium-access-state">Full note on Substack</span></div>' +
    '<p class="premium-access-copy">' + escapeHtml(boundaryCopy) + '</p>' +
    framingMarkup +
    '<div class="premium-access-actions"><a class="primary-action premium-primary" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer" aria-label="Read the full note on Substack (opens in a new tab)">Read full note on Substack ↗</a><a class="secondary-action" href="' + escapeHtml(SUBSCRIPTION_URL) + '" target="_blank" rel="noopener noreferrer" aria-label="See Navnoor Research subscription plans (opens in a new tab)">See subscription plans ↗</a><p class="premium-access-note">Review current price, trial eligibility, renewal, and cancellation terms on Substack before purchasing. This terminal sends no search, filter, or decision-queue data.</p></div></section>';
}
function articleEvidence(article) {
  return briefSection(article,'evidence') || (article && article.brief && article.brief.fallback_evidence) || null;
}
function articleHasEvidence(article) {
  return Boolean(articleEvidence(article) || (article && article.brief_features && article.brief_features.evidence));
}
function articleHasBriefKind(article,kind) {
  return Boolean(briefSection(article,kind) || (article && article.brief_features && article.brief_features[kind]));
}
function briefLensMatches(article) {
  if (state.briefLens === 'all') return true;
  if (state.briefLens === 'checkpoint') return Boolean((article.brief && article.brief.checkpoints && article.brief.checkpoints.length) || (article.brief_features && article.brief_features.checkpoint_count));
  if (state.briefLens === 'evidence') return articleHasEvidence(article);
  return articleHasBriefKind(article,state.briefLens);
}
function setFromParam(params, key, validValues) {
  const raw = params.get(key);
  if (!raw) return new Set();
  return new Set(raw.split('|').map(function (value) { return value.trim(); }).filter(function (value) {
    return value && (!validValues || validValues.has(value));
  }));
}
function isNewArticle(article) {
  return Boolean(article && !reviewedArticleIds.has(article.id) && (
    reviewBaselineExists || (article.date && article.date > firstVisitCutoff)
  ));
}
function reviewFlagged(idea) {
  return Boolean(
    idea.reference_line ||
    idea.negation_risk ||
    idea.description_truncated ||
    !idea._article ||
    idea._article.body_revision_status !== 'current'
  );
}
function validDateInput(value) {
  const text = String(value || '');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return '';
  const parsed = new Date(text + 'T00:00:00Z');
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0,10) === text ? text : '';
}
function validTimestamp(value) {
  const text = String(value || '');
  if (!text || text.length > 40) return '';
  const parsed = new Date(text);
  const time = parsed.getTime();
  if (Number.isNaN(time) || time < Date.UTC(2000,0,1) || time > Date.now() + 10 * 60 * 1000) return '';
  return parsed.toISOString();
}
function sourceSnapshotForIdea(id) {
  const idea = IDEA_BY_ID.get(id);
  if (!idea) return null;
  const article = idea._article || ARTICLE_BY_ID.get(idea.article_id);
  if (!article) return null;
  return {
    article_id:String(article.id || '').slice(0,80),
    title:String(article.title || '').slice(0,500),
    url:String(article.url || '').slice(0,1200),
    date:String(article.date || '').slice(0,10),
    source:String(article.source || '').slice(0,20),
    publication_access:VALID_PUBLICATION_ACCESS.has(article.publication_access) ? article.publication_access : 'unknown',
    passage:String(passageText(idea) || '').slice(0,1200),
    direction:String(idea.direction || 'unspecified').slice(0,40),
    instruments:(idea.instruments || []).slice(0,20).map(function (value) { return String(value).slice(0,80); }),
    underlying:String(idea.underlying || '').slice(0,500),
    body_revision_status:String(article.body_revision_status || 'unverified').slice(0,20),
    source_updated_at:String(article.source_updated_at || '').slice(0,40),
    observed_source_updated_at:String(article.observed_source_updated_at || '').slice(0,40),
    data_checksum:String(SNAPSHOT.data_checksum || '')
  };
}
function normalizeSourceSnapshot(value,id) {
  const current = sourceSnapshotForIdea(id);
  if (!value || typeof value !== 'object') return current;
  const url = safeUrl(String(value.url || ''));
  if (url === '#') return current;
  return {
    article_id:String(value.article_id || '').slice(0,80),
    title:String(value.title || 'Retained research source').slice(0,500),
    url:url.slice(0,1200),
    date:validDateInput(value.date),
    source:VALID_SOURCES.has(value.source) ? value.source : (url.includes('medium.com') ? 'medium' : 'substack'),
    publication_access:VALID_PUBLICATION_ACCESS.has(value.publication_access) ? value.publication_access : 'unknown',
    passage:String(value.passage || '').slice(0,1200),
    direction:VALID_DIRECTIONS.has(value.direction) ? value.direction : 'unspecified',
    instruments:Array.isArray(value.instruments) ? value.instruments.slice(0,20).map(function (item) { return String(item).slice(0,80); }) : [],
    underlying:String(value.underlying || '').slice(0,500),
    body_revision_status:['current','prior','unverified'].includes(value.body_revision_status) ? value.body_revision_status : 'unverified',
    source_updated_at:String(value.source_updated_at || '').slice(0,40),
    observed_source_updated_at:String(value.observed_source_updated_at || '').slice(0,40),
    data_checksum:String(value.data_checksum || '').slice(0,64)
  };
}
function blankChecks() {
  return DILIGENCE_GATES.reduce(function (result,row) { result[row[0]] = false; return result; },{});
}
function normalizeWorkflowItem(value) {
  if (!value || typeof value !== 'object') return null;
  const id = String(value.id || '').slice(0,100);
  if (!id) return null;
  const snapshot = normalizeSourceSnapshot(value.source_snapshot,id);
  if (!snapshot) return null;
  const checks = blankChecks();
  DILIGENCE_GATES.forEach(function (row) {
    checks[row[0]] = Boolean(value.checks && value.checks[row[0]]);
  });
  const item = {
    id:id,
    status:VALID_QUEUE_STATUSES.has(value.status) ? value.status : 'review',
    priority:VALID_PRIORITIES.has(value.priority) ? value.priority : 'normal',
    confidence:VALID_CONFIDENCE.has(value.confidence) ? value.confidence : 'unrated',
    review_date:validDateInput(value.review_date),
    verified_at:validTimestamp(value.verified_at),
    checks:checks,
    source_snapshot:snapshot,
    updated_at:validTimestamp(value.updated_at)
  };
  Object.keys(WORKFLOW_TEXT_LIMITS).forEach(function (field) {
    item[field] = String(value[field] || '').slice(0,WORKFLOW_TEXT_LIMITS[field]);
  });
  return item;
}
function newWorkflowItem(id) {
  return normalizeWorkflowItem({
    id:id,status:'review',priority:'normal',confidence:'unrated',checks:blankChecks(),
    source_snapshot:sourceSnapshotForIdea(id),updated_at:new Date().toISOString()
  });
}
function packetCoverage(item) {
  if (!item) return {caseCount:0,controlCount:0,workflowCount:0,completed:0,total:18,complete:false};
  const caseCount = PACKET_CASE_FIELDS.filter(function (field) { return hasValue(item[field]); }).length;
  const controlCount = DILIGENCE_GATES.filter(function (row) { return Boolean(item.checks && item.checks[row[0]]); }).length;
  const workflowCount = Number(hasValue(item.owner)) + Number(Boolean(item.review_date)) +
    Number(item.confidence !== 'unrated') + Number(hasValue(item.next_action));
  const completed = caseCount + controlCount + workflowCount;
  return {caseCount:caseCount,controlCount:controlCount,workflowCount:workflowCount,completed:completed,total:18,complete:completed === 18};
}
function reviewIsOverdue(item) {
  return Boolean(item && item.status !== 'archived' && item.review_date && item.review_date < new Date().toISOString().slice(0,10));
}
function documentationMatches(idea) {
  if (state.documentation === 'all') return true;
  if (state.documentation === 'documented') return idea.documentation_score === 5;
  if (state.documentation === 'strong') return idea.documentation_score >= 4;
  if (state.documentation === 'needs-context') return idea.documentation_score <= 2;
  if (state.documentation === 'review') return reviewFlagged(idea);
  if (state.documentation === 'triage') {
    return idea.documentation_score >= 4 && !reviewFlagged(idea) && idea._article.content_status === 'full';
  }
  return true;
}
let workflowStorageDirty = false;
let workflowStorageUnavailable = false;
let lastPersistedWorkflow = '';
let workflowLoadBlocked = false;
let unreadableWorkflowRaw = '';
let unreadableWorkflowLocation = '';
let lastRestoreWorkflowItems = null;
let workflowLegacyMigrated = false;
let legacyStorageCheckUnavailable = false;
let legacySavedIdsPendingClear = false;
let legacyCleanupPending = false;
function workflowSerialization() {
  return JSON.stringify(Array.from(workflowItems.values()).slice(0,MAX_QUEUE_ITEMS));
}
function clearLegacyLocalWorkflowKeys() {
  LEGACY_LOCAL_WORKFLOW_KEYS.forEach(function (key) { localStorage.removeItem(key); });
  localStorage.removeItem('nrt-queue-storage-boundary-v1');
}
function syncWorkflowStorageAlert() {
  const alert = document.getElementById('storage-alert');
  if (!alert) return;
  alert.hidden = !workflowStorageUnavailable && !workflowLoadBlocked;
  document.getElementById('storage-alert-text').textContent = workflowLoadBlocked
    ? 'A stored tab-session or legacy queue record could not be read. Saving is blocked so recoverable data is not overwritten. Back up the unreadable record or discard it explicitly.'
    : 'Tab-session storage is unavailable. In-memory packet edits will not survive a reload; back up the queue before leaving.';
  document.getElementById('storage-retry').hidden = workflowLoadBlocked;
  document.getElementById('storage-backup').hidden = workflowLoadBlocked;
  document.getElementById('storage-raw-backup').hidden = !workflowLoadBlocked || !unreadableWorkflowRaw;
  document.getElementById('storage-clear').hidden = !workflowLoadBlocked;
}
function persistWorkflow() {
  savedIdeas = new Set(workflowItems.keys());
  const serialized = workflowSerialization();
  if (workflowLoadBlocked) {
    workflowStorageDirty = true;
    workflowStorageUnavailable = true;
    syncWorkflowStorageAlert();
    showToast('Queue saving is blocked until the unreadable stored record is resolved');
    return false;
  }
  try {
    sessionStorage.setItem(WORKFLOW_KEY,serialized);
    lastPersistedWorkflow = serialized;
    workflowStorageDirty = false;
    workflowStorageUnavailable = false;
    if (legacyCleanupPending) {
      try {
        clearLegacyLocalWorkflowKeys();
        legacyCleanupPending = false;
        legacyStorageCheckUnavailable = false;
        workflowLegacyMigrated = true;
        showPersistentNotice('The legacy persistent queue was removed after a successful tab-session save. Export a plaintext backup before closing the tab if it must be retained.','Back up queue','backup-queue');
      } catch (_error) {
        legacyStorageCheckUnavailable = true;
      }
    }
    syncWorkflowStorageAlert();
    return true;
  } catch (_error) {
    workflowStorageDirty = serialized !== lastPersistedWorkflow;
    workflowStorageUnavailable = true;
    syncWorkflowStorageAlert();
    showToast('Queue could not be saved in this tab session');
    return false;
  }
}

ARTICLES.forEach(function (article) {
  article._ideas = article.idea_ids.map(function (id) { return IDEA_BY_ID.get(id); }).filter(Boolean);
  refreshArticleSearch(article);
});
IDEAS.forEach(function (idea) {
  idea._article = ARTICLE_BY_ID.get(idea.article_id);
  idea._search = normalize([
    idea._article.title, idea._article.subtitle, idea._article.source,
    idea.description, idea.direction, idea.instruments.join(' '), idea.underlying,
    idea.thesis, idea.quant, idea.outcome, idea.manager
  ].join(' '));
});

let workflowItems = new Map();
let pendingLegacyIdeaIds = [];
let storedWorkflowRaw = '';
let storedWorkflowValidated = false;
function installStoredWorkflow(raw) {
  const stored = raw ? JSON.parse(raw) : [];
  if (!Array.isArray(stored)) throw new Error('stored queue schema is not an array');
  stored.slice(0,MAX_QUEUE_ITEMS).forEach(function (value) {
    const item = normalizeWorkflowItem(value);
    if (!item || workflowItems.has(item.id)) throw new Error('stored queue item is invalid or duplicated');
    workflowItems.set(item.id,item);
  });
}
try {
  const sessionRaw = sessionStorage.getItem(WORKFLOW_KEY) || '';
  if (sessionRaw) {
    storedWorkflowRaw = sessionRaw;
    unreadableWorkflowLocation = 'session';
    installStoredWorkflow(sessionRaw);
    storedWorkflowValidated = true;
  } else {
    let legacyRaw = '';
    let legacySavedRaw = '';
    try {
      legacyRaw = localStorage.getItem('nrt-decision-queue-v2') ||
        localStorage.getItem('nrt-decision-queue-v1') || '';
      legacySavedRaw = !legacyRaw ? (localStorage.getItem('nrt-saved-ideas') || '') : '';
    } catch (_error) {
      legacyStorageCheckUnavailable = true;
    }
    if (legacyRaw) {
      storedWorkflowRaw = legacyRaw;
      unreadableWorkflowLocation = 'legacy-local';
      installStoredWorkflow(legacyRaw);
      storedWorkflowValidated = true;
      const serialized = workflowSerialization();
      legacyCleanupPending = true;
      sessionStorage.setItem(WORKFLOW_KEY,serialized);
      lastPersistedWorkflow = serialized;
      try {
        clearLegacyLocalWorkflowKeys();
        legacyCleanupPending = false;
        workflowLegacyMigrated = true;
      } catch (_error) {
        legacyStorageCheckUnavailable = true;
      }
    } else if (legacySavedRaw) {
      storedWorkflowRaw = legacySavedRaw;
      unreadableWorkflowLocation = 'legacy-local';
      const legacy = JSON.parse(legacySavedRaw);
      if (!Array.isArray(legacy)) throw new Error('legacy saved queue schema is not an array');
      pendingLegacyIdeaIds = legacy.slice(0,MAX_QUEUE_ITEMS).map(String);
      legacySavedIdsPendingClear = true;
      storedWorkflowValidated = true;
    }
  }
} catch (_error) {
  if (storedWorkflowRaw && !storedWorkflowValidated) {
    workflowLoadBlocked = true;
    unreadableWorkflowRaw = storedWorkflowRaw;
  } else {
    workflowStorageUnavailable = true;
  }
}
let savedIdeas = new Set(workflowItems.keys());
if (!lastPersistedWorkflow) lastPersistedWorkflow = workflowSerialization();
try {
  const rollbackRaw = sessionStorage.getItem(RESTORE_ROLLBACK_KEY) || '';
  if (rollbackRaw) {
    const rollbackPayload = JSON.parse(rollbackRaw);
    if (!Array.isArray(rollbackPayload)) throw new Error('invalid rollback payload');
    const rollback = new Map();
    rollbackPayload.forEach(function (value) {
      const item = normalizeWorkflowItem(value);
      if (!item || rollback.has(item.id)) throw new Error('invalid rollback item');
      rollback.set(item.id,item);
    });
    lastRestoreWorkflowItems = rollback;
  }
} catch (_error) {
  try { sessionStorage.removeItem(RESTORE_ROLLBACK_KEY); } catch (_ignored) {}
}
function migrateLegacySavedIdeas() {
  if (!pendingLegacyIdeaIds.length || workflowItems.size >= MAX_QUEUE_ITEMS) return;
  pendingLegacyIdeaIds.forEach(function (id) {
    if (IDEA_BY_ID.has(id) && workflowItems.size < MAX_QUEUE_ITEMS) {
      const item = newWorkflowItem(id);
      if (item) workflowItems.set(id,item);
    }
  });
  pendingLegacyIdeaIds = [];
  const persisted = workflowItems.size ? persistWorkflow() : true;
  if (persisted && legacySavedIdsPendingClear) {
    try {
      clearLegacyLocalWorkflowKeys();
      legacyCleanupPending = false;
      workflowLegacyMigrated = true;
      legacySavedIdsPendingClear = false;
      showPersistentNotice('A legacy persistent queue was moved into this safer tab session and removed from origin-wide storage. Export a plaintext backup before closing the tab if it must be retained.','Back up queue','backup-queue');
    } catch (_error) {
      legacyStorageCheckUnavailable = true;
    }
  }
}

const firstVisitCutoff = (function () {
  const newest = new Date(MAX_DATE + 'T00:00:00Z');
  newest.setUTCDate(newest.getUTCDate() - 7);
  return newest.toISOString().slice(0,10);
})();
let reviewedArticleIds = new Set();
let reviewBaselineExists = false;
try {
  const reviewedIds = JSON.parse(localStorage.getItem(REVIEWED_ARTICLE_IDS_KEY) || 'null');
  if (Array.isArray(reviewedIds)) {
    reviewedArticleIds = new Set(reviewedIds.map(String).filter(function (id) { return ARTICLE_BY_ID.has(id); }));
    reviewBaselineExists = true;
  } else {
    const legacyLastSeen = localStorage.getItem(LEGACY_LAST_SEEN_KEY) || '';
    if (legacyLastSeen) {
      reviewedArticleIds = new Set(ARTICLES.filter(function (article) {
        return article.date <= legacyLastSeen;
      }).map(function (article) { return article.id; }));
      reviewBaselineExists = true;
    }
  }
} catch (_error) {}

let storedDensity = 'compact';
let storedInspector = true;
try {
  storedDensity = localStorage.getItem('nrt-density') === 'comfortable' ? 'comfortable' : 'compact';
  storedInspector = localStorage.getItem('nrt-inspector') !== 'hidden';
} catch (_error) {}

const state = {
  view:'structure',
  query:'',
  sources:new Set(),
  revisions:new Set(),
  directions:new Set(),
  instruments:new Set(),
  managers:new Set(),
  quality:new Set(),
  publicationAccess:new Set(),
  content:new Set(),
  queueStatuses:new Set(),
  documentation:'all',
  newOnly:false,
  range:'all',
  coverage:'all',
  briefLens:'all',
  threadTopic:'',
  structureFocus:'',
  structureInstrument:'',
  structureDirection:'any',
  structurePeriod:'all',
  structureSlope:'any',
  structureLevel:'any',
  structureMacro:false,
  structureAnchor:'',
  structurePassage:'',
  structureShareable:false,
  structureControlsOpen:false,
  sort:'newest',
  density:storedDensity,
  selected:'',
  limit:PAGE_SIZE.briefing,
  inspector:storedInspector
};

function hydrateFromHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const hashView = params.get('view') === 'saved' ? 'queue' : params.get('view');
  const legacySelected = String(params.get('selected') || '');
  state.view = ['briefing','ideas','research','queue','structure'].includes(hashView) ? hashView : 'structure';
  // Preserve article links created before Structure Desk became the default.
  // New stubs name briefing explicitly, while old #selected=… links are
  // canonicalized on first render instead of being discarded by the desk.
  if (!params.has('view') && ARTICLE_BY_ID.has(legacySelected)) state.view = 'briefing';
  state.query = String(params.get('q') || '').slice(0,300);
  state.sources = setFromParam(params,'src',VALID_SOURCES);
  state.revisions = setFromParam(params,'revision',VALID_BODY_REVISIONS);
  state.directions = setFromParam(params,'dir',VALID_DIRECTIONS);
  state.instruments = setFromParam(params,'inst',VALID_INSTRUMENTS);
  state.managers = setFromParam(params,'mgr',new Set(MANAGERS));
  state.quality = setFromParam(params,'evidence',VALID_QUALITY);
  state.publicationAccess = setFromParam(params,'access',VALID_PUBLICATION_ACCESS);
  state.content = setFromParam(params,'content',VALID_CONTENT);
  state.queueStatuses = setFromParam(params,'queue',VALID_QUEUE_STATUSES);
  state.documentation = VALID_DOCUMENTATION.has(params.get('doc')) ? params.get('doc') : 'all';
  state.newOnly = params.get('new') === '1';
  state.range = ['30d','90d','1y','all'].includes(params.get('range')) ? params.get('range') : 'all';
  state.coverage = ['all','ideas','research'].includes(params.get('coverage')) ? params.get('coverage') : 'all';
  state.briefLens = VALID_BRIEF_LENSES.has(params.get('lens')) ? params.get('lens') : 'all';
  // Desk inputs can contain a proprietary research question. They hydrate
  // only from a URL the reader explicitly shared; ordinary edits remain
  // memory-only and are removed from the address on the next render.
  state.structureFocus = String(params.get('focus') || '').slice(0,120);
  state.structureInstrument = VALID_INSTRUMENTS.has(params.get('sinst'))
    ? params.get('sinst') : '';
  state.structureDirection = VALID_DIRECTIONS.has(params.get('sdir'))
    ? params.get('sdir') : 'any';
  state.structurePeriod = /^[0-9]{4}$/.test(String(params.get('speriod') || ''))
    ? params.get('speriod') : 'all';
  state.structureSlope = VALID_RATE_BANDS.has(params.get('sslope'))
    ? params.get('sslope') : 'any';
  state.structureLevel = VALID_RATE_BANDS.has(params.get('slevel'))
    ? params.get('slevel') : 'any';
  state.structureMacro = params.get('smacro') === '1';
  if (!state.structureMacro) {
    state.structureSlope = 'any';
    state.structureLevel = 'any';
  }
  state.structureAnchor = ARTICLE_BY_ID.has(params.get('sanchor'))
    ? params.get('sanchor') : '';
  state.structurePassage = /^[A-Za-z0-9_-]{1,96}$/.test(String(params.get('spassage') || ''))
    ? params.get('spassage') : '';
  if (!state.structureAnchor) state.structurePassage = '';
  state.structureShareable = Boolean(
    state.structureFocus || state.structureInstrument ||
    state.structureDirection !== 'any' || state.structurePeriod !== 'all' ||
    state.structureMacro || state.structureAnchor || state.structurePassage
  );
  state.structureControlsOpen = Boolean(
    state.structureInstrument || state.structureDirection !== 'any' ||
    state.structurePeriod !== 'all' || state.structureMacro
  );
  state.sort = params.get('sort') || 'newest';
  state.density = ['compact','comfortable'].includes(params.get('density')) ? params.get('density') : storedDensity;
  state.selected = legacySelected;
  const requestedTopic = String(params.get('topic') || '').slice(0,120);
  const threadRow = THREAD_ARTICLES[state.selected];
  state.threadTopic = state.view === 'briefing' && threadRow && threadRow.topics.includes(requestedTopic)
    ? requestedTopic : '';
  state.limit = PAGE_SIZE[state.view];
}

let nextHistoryMode = 'replace';
let restoringHistory = false;
function markMeaningfulNavigation() {
  nextHistoryMode = 'push';
}
function updateHash(includeQuery) {
  const params = new URLSearchParams();
  if (state.view !== 'structure') params.set('view',state.view);
  if (includeQuery && state.query) params.set('q',state.query.slice(0,300));
  if (state.sources.size) params.set('src',Array.from(state.sources).join('|'));
  if (state.revisions.size) params.set('revision',Array.from(state.revisions).join('|'));
  if (state.directions.size) params.set('dir',Array.from(state.directions).join('|'));
  if (state.instruments.size) params.set('inst',Array.from(state.instruments).join('|'));
  if (state.managers.size) params.set('mgr',Array.from(state.managers).join('|'));
  if (state.quality.size) params.set('evidence',Array.from(state.quality).join('|'));
  if (state.publicationAccess.size) params.set('access',Array.from(state.publicationAccess).join('|'));
  if (state.content.size) params.set('content',Array.from(state.content).join('|'));
  if (state.queueStatuses.size) params.set('queue',Array.from(state.queueStatuses).join('|'));
  if (state.documentation !== 'all') params.set('doc',state.documentation);
  if (state.newOnly) params.set('new','1');
  if (state.range !== 'all') params.set('range',state.range);
  if (state.coverage !== 'all' && state.view === 'research') params.set('coverage',state.coverage);
  if (state.briefLens !== 'all' && state.view === 'briefing') params.set('lens',state.briefLens);
  if (state.view === 'structure' && (includeQuery || state.structureShareable)) {
    if (state.structureFocus) params.set('focus',state.structureFocus.slice(0,120));
    if (state.structureInstrument) params.set('sinst',state.structureInstrument);
    if (state.structureDirection !== 'any') params.set('sdir',state.structureDirection);
    if (state.structurePeriod !== 'all') params.set('speriod',state.structurePeriod);
    if (state.structureMacro) {
      params.set('smacro','1');
      if (state.structureSlope !== 'any') params.set('sslope',state.structureSlope);
      if (state.structureLevel !== 'any') params.set('slevel',state.structureLevel);
    }
    if (state.structureAnchor) params.set('sanchor',state.structureAnchor);
    if (state.structureAnchor && state.structurePassage) {
      params.set('spassage',state.structurePassage);
    }
  }
  const threadRow = THREAD_ARTICLES[state.selected];
  if (state.view === 'briefing' && threadRow && threadRow.topics.includes(state.threadTopic)) params.set('topic',state.threadTopic);
  if (state.sort !== 'newest') params.set('sort',state.sort);
  if (state.density !== 'compact') params.set('density',state.density);
  if (state.selected && state.view !== 'structure') params.set('selected',state.selected);
  const encoded = params.toString();
  const target = encoded ? '#' + encoded : location.pathname + location.search;
  const current = location.hash || location.pathname + location.search;
  if (!restoringHistory && target !== current) {
    history[nextHistoryMode === 'push' ? 'pushState' : 'replaceState'](null,'',target);
  }
  nextHistoryMode = 'replace';
}

let queryCacheKey = null;
let queryTokenCache = [];
let relevanceScoreCache = new WeakMap();
function queryTokens() {
  const aliases = {options:'option',equities:'equity',stocks:'equity',rv:'relative value',arb:'arbitrage'};
  const normalizedQuery = normalize(state.query);
  if (normalizedQuery !== queryCacheKey) {
    queryCacheKey = normalizedQuery;
    queryTokenCache = normalizedQuery.split(' ').filter(Boolean).map(function (token) { return aliases[token] || token; });
    relevanceScoreCache = new WeakMap();
  }
  return queryTokenCache;
}
function matchesSearch(haystack) {
  const tokens = queryTokens();
  return tokens.every(function (token) { return haystack.includes(token); });
}
function inDateRange(date) {
  if (state.range === 'all') return true;
  const newest = new Date(MAX_DATE + 'T00:00:00Z');
  const candidate = new Date(date + 'T00:00:00Z');
  const days = state.range === '30d' ? 30 : state.range === '90d' ? 90 : 365;
  return newest - candidate <= days * 86400000 && candidate <= newest;
}
function setMatches(selected, values) {
  if (!selected.size) return true;
  return values.some(function (value) { return selected.has(value); });
}
function qualityMatches(record, isArticle) {
  if (!state.quality.size) return true;
  const values = isArticle ? {
    quant:record.has_quant,thesis:record.has_thesis,outcome:record.has_outcome,
    manager:record.managers.length > 0
  } : {
    quant:hasValue(record.quant),thesis:hasValue(record.thesis),outcome:hasValue(record.outcome),
    manager:hasValue(record.manager)
  };
  return Array.from(state.quality).every(function (key) { return values[key]; });
}
function ideaMatches(idea, skip) {
  const article = idea._article;
  const workflow = workflowItems.get(idea.id);
  if (state.view === 'queue' && !workflow) return false;
  if (state.view === 'queue' && state.queueStatuses.size && !state.queueStatuses.has(workflow.status)) return false;
  if (skip !== 'source' && state.sources.size && !state.sources.has(article.source)) return false;
  if (skip !== 'revision' && state.revisions.size && !state.revisions.has(article.body_revision_status)) return false;
  if (!inDateRange(article.date)) return false;
  if (state.newOnly && !isNewArticle(article)) return false;
  if (skip !== 'direction' && state.directions.size && !state.directions.has(idea.direction)) return false;
  if (skip !== 'instrument' && !setMatches(state.instruments,idea.instruments)) return false;
  if (skip !== 'manager' && state.managers.size && !state.managers.has(idea.manager_key)) return false;
  if (skip !== 'quality' && !qualityMatches(idea,false)) return false;
  if (skip !== 'access' && state.publicationAccess.size && !state.publicationAccess.has(article.publication_access)) return false;
  if (skip !== 'content' && state.content.size && !state.content.has(article.content_status)) return false;
  if (skip !== 'documentation' && !documentationMatches(idea)) return false;
  return matchesSearch(idea._search);
}
function ideaMatchesResearchFacets(idea,skip) {
  if (skip !== 'direction' && state.directions.size && !state.directions.has(idea.direction)) return false;
  if (skip !== 'instrument' && !setMatches(state.instruments,idea.instruments)) return false;
  if (skip !== 'manager' && state.managers.size && !state.managers.has(idea.manager_key)) return false;
  if (skip !== 'quality' && !qualityMatches(idea,false)) return false;
  if (skip !== 'documentation' && !documentationMatches(idea)) return false;
  return matchesSearch(idea._search);
}
function articleMatches(article, skip) {
  if (skip !== 'source' && state.sources.size && !state.sources.has(article.source)) return false;
  if (skip !== 'revision' && state.revisions.size && !state.revisions.has(article.body_revision_status)) return false;
  if (skip !== 'access' && state.publicationAccess.size && !state.publicationAccess.has(article.publication_access)) return false;
  if (!inDateRange(article.date)) return false;
  if (state.newOnly && !isNewArticle(article)) return false;
  const hasTradeFilters =
    (skip !== 'direction' && state.directions.size) ||
    (skip !== 'instrument' && state.instruments.size) ||
    (skip !== 'manager' && state.managers.size) ||
    (skip !== 'quality' && state.quality.size) ||
    (skip !== 'documentation' && state.documentation !== 'all');
  if (hasTradeFilters && !article._ideas.some(function (idea) { return ideaMatchesResearchFacets(idea,skip); })) return false;
  if (skip !== 'content' && state.content.size && !state.content.has(article.content_status)) return false;
  if (state.coverage === 'ideas' && article.trade_count === 0) return false;
  if (state.coverage === 'research' && article.trade_count !== 0) return false;
  if (state.view === 'briefing' && !briefLensMatches(article)) return false;
  return matchesSearch(article._search);
}
function relevanceScore(record) {
  if (!state.query) return 0;
  queryTokens();
  if (relevanceScoreCache.has(record)) return relevanceScoreCache.get(record);
  const tokens = queryTokens();
  let score;
  if (isArticleView()) {
    const title = normalize(record.title);
    const claim = normalize(articleClaim(record));
    const lead = normalize(record.brief && record.brief.lead && record.brief.lead.text);
    const headings = normalize(record.brief && (record.brief.sections || []).map(function (section) { return section.heading; }).join(' '));
    const evidence = normalize(articleBriefSearch(record));
    const managers = normalize(record.managers.join(' '));
    score = tokens.reduce(function (value, token) {
      return value + (title.includes(token) ? 12 : 0) + (claim.includes(token) ? 9 : 0) +
        (lead.includes(token) ? 8 : 0) + (headings.includes(token) ? 7 : 0) +
        (evidence.includes(token) ? 5 : 0) + (managers.includes(token) ? 4 : 0);
    },0);
  } else {
    const article = record._article;
    const title = normalize(article.title);
    const manager = normalize(record.manager);
    const underlying = normalize(record.underlying);
    score = tokens.reduce(function (value, token) {
      return value + (manager.includes(token) ? 10 : 0) + (underlying.includes(token) ? 8 : 0) + (title.includes(token) ? 6 : 0);
    },0);
  }
  relevanceScoreCache.set(record,score);
  return score;
}
function sortedRecords(records) {
  return records.slice().sort(function (left,right) {
    const leftArticle = isArticleView() ? left : left._article;
    const rightArticle = isArticleView() ? right : right._article;
    if (state.sort === 'oldest') return leftArticle.date.localeCompare(rightArticle.date);
    if (state.sort === 'manager') return String(left.manager || '').localeCompare(String(right.manager || '')) || rightArticle.date.localeCompare(leftArticle.date);
    if (state.sort === 'market') return String((left.instruments || [])[0] || '').localeCompare(String((right.instruments || [])[0] || '')) || rightArticle.date.localeCompare(leftArticle.date);
    if (state.sort === 'direction') return String(left.direction || '').localeCompare(String(right.direction || '')) || rightArticle.date.localeCompare(leftArticle.date);
    if (state.sort === 'article') return leftArticle.title.localeCompare(rightArticle.title);
    if (state.sort === 'documented') return Number(right.documentation_score || 0) - Number(left.documentation_score || 0) || rightArticle.date.localeCompare(leftArticle.date);
    if (state.sort === 'queue-status') {
      const order = {diligence:0,review:1,monitor:2,archived:3};
      return (order[(workflowItems.get(left.id) || {}).status] ?? 9) - (order[(workflowItems.get(right.id) || {}).status] ?? 9) || rightArticle.date.localeCompare(leftArticle.date);
    }
    if (state.sort === 'priority') {
      const order = {high:0,normal:1,low:2};
      return (order[(workflowItems.get(left.id) || {}).priority] ?? 9) - (order[(workflowItems.get(right.id) || {}).priority] ?? 9) || rightArticle.date.localeCompare(leftArticle.date);
    }
    if (state.sort === 'review-date') {
      return String((workflowItems.get(left.id) || {}).review_date || '9999-12-31').localeCompare(String((workflowItems.get(right.id) || {}).review_date || '9999-12-31')) || rightArticle.date.localeCompare(leftArticle.date);
    }
    if (state.sort === 'updated') {
      return String((workflowItems.get(right.id) || {}).updated_at || '').localeCompare(String((workflowItems.get(left.id) || {}).updated_at || '')) || rightArticle.date.localeCompare(leftArticle.date);
    }
    if (state.sort === 'most-ideas') return right.trade_count - left.trade_count || right.date.localeCompare(left.date);
    if (state.sort === 'read-time') return right.read_minutes - left.read_minutes || right.date.localeCompare(left.date);
    if (state.sort === 'title') return left.title.localeCompare(right.title);
    if (state.sort === 'relevance') return relevanceScore(right) - relevanceScore(left) || rightArticle.date.localeCompare(leftArticle.date);
    return rightArticle.date.localeCompare(leftArticle.date);
  });
}
function filteredRecords(skip) {
  const records = isArticleView()
    ? ARTICLES.filter(function (article) { return articleMatches(article,skip); })
    : IDEAS.filter(function (idea) { return ideaMatches(idea,skip); });
  return sortedRecords(records);
}

function setSortOptions() {
  const select = document.getElementById('sort-select');
  const research = isArticleView();
  const options = research ? [
    ...(state.query ? [['relevance','Search relevance']] : []),
    ['newest','Newest first'],['oldest','Oldest first'],['most-ideas','Most observations'],
    ['read-time','Longest read'],['title','Title A–Z']
  ] : [
    ['newest','Newest first'],['oldest','Oldest first'],['manager','Manager A–Z'],
    ['market','Market A–Z'],['direction','Direction A–Z'],['article','Article A–Z'],
    ['documented','Most documented'],
    ...(state.view === 'queue' ? [['queue-status','Queue status'],['priority','Priority'],['review-date','Next review'],['updated','Packet updated']] : []),
    ['relevance','Search relevance']
  ];
  if (!options.some(function (option) { return option[0] === state.sort; })) state.sort = 'newest';
  select.innerHTML = options.map(function (option) {
    return '<option value="' + option[0] + '"' + (option[0] === state.sort ? ' selected' : '') + '>' + option[1] + '</option>';
  }).join('');
}
function ariaSort(key) {
  if (key === 'newest' && state.sort === 'oldest') return 'ascending';
  if (state.sort !== key) return 'none';
  return state.sort === 'oldest' || state.sort === 'manager' || state.sort === 'market' || state.sort === 'direction' || state.sort === 'article' || state.sort === 'title'
    ? 'ascending' : 'descending';
}
function renderTableHead() {
  const head = document.getElementById('table-head');
  if (state.view === 'research') {
    head.className = 'table-head research-grid';
    head.innerHTML =
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('newest') + '"><button class="head-sort" type="button" data-sort="newest">Date</button></div>' +
      '<div class="head-cell" role="columnheader">Source</div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('title') + '"><button class="head-sort" type="button" data-sort="title">Research note</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('most-ideas') + '"><button class="head-sort" type="button" data-sort="most-ideas">Observations</button></div>' +
      '<div class="head-cell" role="columnheader">Coverage</div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('read-time') + '"><button class="head-sort" type="button" data-sort="read-time">Read</button></div>' +
      '<div class="head-cell" role="columnheader"><span class="sr-only">Open</span></div>';
  } else {
    head.className = 'table-head idea-grid';
    head.innerHTML =
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('newest') + '"><button class="head-sort" type="button" data-sort="newest">Date</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('direction') + '"><button class="head-sort" type="button" data-sort="direction">Parsed stance</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('market') + '"><button class="head-sort" type="button" data-sort="market">Market</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('manager') + '"><button class="head-sort" type="button" data-sort="manager">Mentioned entity</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('article') + '"><button class="head-sort" type="button" data-sort="article">Source passage</button></div>' +
      '<div class="head-cell" role="columnheader">Captured</div>' +
      '<div class="head-cell" role="columnheader">Channel</div>' +
      '<div class="head-cell" role="columnheader"><span class="sr-only">Open</span></div>';
  }
  head.querySelectorAll('[data-sort]').forEach(function (button) {
    button.tabIndex = window.innerWidth < 760 ? -1 : 0;
  });
  document.getElementById('data-table').setAttribute('aria-colcount',state.view === 'research' ? '7' : '8');
}

function evidenceMarkup(idea) {
  const quant = hasValue(idea.quant);
  const thesis = hasValue(idea.thesis);
  const outcome = hasValue(idea.outcome);
  const label = 'Documentation coverage ' + idea.documentation_score + ' of 5; numeric context ' + (quant ? 'available' : 'unavailable') + '; thesis ' +
    (thesis ? 'available' : 'unavailable') + '; reported outcome ' + (outcome ? 'available' : 'unavailable');
  return '<span class="evidence-set" role="img" aria-label="' + label + '">' +
    '<span class="documentation-badge ' + (idea.documentation_score === 5 ? 'complete' : '') + '" aria-hidden="true" title="Documentation coverage">' + idea.documentation_score + '/5</span>' +
    '<span class="evidence-flag ' + (quant ? 'on' : '') + '" aria-hidden="true" title="Numeric context">N' + (quant ? '+' : '−') + '</span>' +
    '<span class="evidence-flag ' + (thesis ? 'on' : '') + '" aria-hidden="true" title="Edge or thesis">T' + (thesis ? '+' : '−') + '</span>' +
    '<span class="evidence-flag ' + (outcome ? 'on' : '') + '" aria-hidden="true" title="Reported outcome">O' + (outcome ? '+' : '−') + '</span>' +
    '</span>';
}
function passageText(idea) {
  const text = String(idea.description || 'No source passage extracted');
  return idea.description_truncated && !text.endsWith('…') ? text + '…' : text;
}
function ideaRow(idea) {
  const article = idea._article;
  const primaryInstrument = idea.instruments[0] || 'unspecified';
  const otherInstruments = idea.instruments.slice(1).map(instrumentLabel).join(', ');
  const selected = state.selected === idea.id;
  const rowLabel = formatDate(article.date) + ', ' + directionLabel(idea.direction) + ', ' +
    idea.instruments.map(instrumentLabel).join(', ') + ', ' + (idea.manager || 'manager not stated') + ', ' +
    (idea.description || 'no description extracted') + ', ' + sourceLabel(article.source);
  const workflow = workflowItems.get(idea.id);
  const newBadge = isNewArticle(article) ? '<span class="new-badge">New</span>' : '';
  const coverage = packetCoverage(workflow);
  const workflowBadge = workflow ? '<span class="workflow-badge">' + escapeHtml(workflow.status) + '</span>' +
    '<span class="workflow-badge coverage" title="Decision packet coverage; not approval">' + coverage.completed + '/' + coverage.total + '</span>' +
    (reviewIsOverdue(workflow) ? '<span class="review-flag">Overdue</span>' : '') : '';
  const review = reviewFlagged(idea) ? '<span class="review-flag" title="Source or extraction review recommended">Review</span>' : '';
  const sourceDetail = sourceAccessLabel(article) + (article.body_revision_status === 'current' ? '' : ' · ' + bodyRevisionLabel(article));
  return '<div class="data-row idea-grid" role="row" data-record-id="' + idea.id + '" tabindex="' + (selected ? '0' : '-1') + '" aria-selected="' + selected + '" aria-keyshortcuts="Enter Space ArrowUp ArrowDown Home End Alt+Shift+O Alt+Shift+S Alt+Shift+C" aria-label="' + escapeHtml(rowLabel) + '">' +
    '<div class="data-cell cell-date" role="gridcell"><time datetime="' + article.date + '">' + shortDate(article.date) + '</time>' + newBadge + '</div>' +
    '<div class="data-cell cell-bias" role="gridcell"><span class="direction-badge ' + directionClass(idea.direction) + '" title="' + escapeHtml(directionLabel(idea.direction) + ' in this source passage; not a verified position') + '">' + compactDirectionLabel(idea.direction) + '</span></div>' +
    '<div class="data-cell cell-market" role="gridcell"><div class="instrument-primary">' + escapeHtml(instrumentLabel(primaryInstrument)) + '</div>' +
      (otherInstruments ? '<div class="instrument-secondary">+' + escapeHtml(otherInstruments) + '</div>' : '') + '</div>' +
    '<div class="data-cell cell-manager" role="gridcell"><div class="manager-name ' + (idea.manager ? '' : 'missing') + '">' + escapeHtml(idea.manager || '—') + workflowBadge + '</div></div>' +
    '<div class="data-cell cell-idea" role="gridcell"><div class="idea-title">' + escapeHtml(passageText(idea)) + '</div>' +
      '<div class="idea-context">' + escapeHtml(idea.underlying || article.title) + (review ? ' · ' + review : '') + '</div></div>' +
    '<div class="data-cell cell-evidence" role="gridcell">' + evidenceMarkup(idea) + '</div>' +
    '<div class="data-cell cell-source" role="gridcell"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><div class="instrument-secondary">' + escapeHtml(sourceDetail) + '</div></div>' +
    '<div class="data-cell cell-open" role="gridcell"><a class="row-open" tabindex="-1" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer" aria-label="' + escapeHtml(sourceActionLabel(article).replace(' ↗','') + ': ' + article.title + ' in a new tab') + '">↗</a></div>' +
    '</div>';
}
function researchRow(article) {
  const selected = state.selected === article.id;
  const metadataOnlyMember = article.publication_access === 'member' && !hasIndexedMemberPreview(article);
  const coverage = metadataOnlyMember
    ? '<span class="coverage-badge coverage-excerpt">Metadata only</span>'
    : article.body_revision_status !== 'current'
    ? '<span class="coverage-badge coverage-revision">' + escapeHtml(bodyRevisionLabel(article)) + '</span>'
    : article.trade_count === 0
    ? '<span class="coverage-badge coverage-research">Research-only</span>'
    : article.content_status === 'full'
      ? '<span class="coverage-badge coverage-full">Full text</span>'
      : '<span class="coverage-badge coverage-excerpt">Excerpt</span>';
  const read = metadataOnlyMember
    ? 'Metadata'
    : isPaidSubstackArticle(article)
    ? 'Member'
    : article.content_status === 'excerpt'
    ? 'Preview'
    : article.read_minutes ? article.read_minutes + ' min' : '—';
  const rowLabel = formatDate(article.date) + ', ' + sourceLabel(article.source) + ', ' + article.title + ', ' +
    number(article.trade_count) + ' research observations, ' +
    sourceAccessLabel(article).toLowerCase() +
    ', ' + bodyRevisionLabel(article);
  return '<div class="data-row research-grid" role="row" data-record-id="' + article.id + '" tabindex="' + (selected ? '0' : '-1') + '" aria-selected="' + selected + '" aria-keyshortcuts="Enter Space ArrowUp ArrowDown Home End Alt+Shift+O Alt+Shift+C" aria-label="' + escapeHtml(rowLabel) + '">' +
    '<div class="data-cell cell-date" role="gridcell"><time datetime="' + article.date + '">' + shortDate(article.date) + '</time>' + (isNewArticle(article) ? '<span class="new-badge">New</span>' : '') + '</div>' +
    '<div class="data-cell cell-source" role="gridcell"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><div class="instrument-secondary">' + escapeHtml(publicationAccessLabel(article)) + '</div></div>' +
    '<div class="data-cell cell-article" role="gridcell"><div class="article-title">' + escapeHtml(article.title) + '</div><div class="article-subtitle">' + escapeHtml(article.subtitle || 'No abstract available') + '</div></div>' +
    '<div class="data-cell cell-count number-cell" role="gridcell">' + number(article.trade_count) + '</div>' +
    '<div class="data-cell cell-coverage" role="gridcell">' + coverage + '</div>' +
    '<div class="data-cell cell-read number-cell" role="gridcell">' + read + '</div>' +
    '<div class="data-cell cell-open" role="gridcell"><a class="row-open" tabindex="-1" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer" aria-label="' + escapeHtml(sourceActionLabel(article).replace(' ↗','') + ': ' + article.title + ' in a new tab') + '">↗</a></div>' +
    '</div>';
}
function renderRows(records) {
  const body = document.getElementById('table-body');
  const fragment = document.createDocumentFragment();
  let visible = records.slice(0,state.limit);
  let pinnedId = '';
  if (state.selected && !visible.some(function (record) { return record.id === state.selected; })) {
    const selectedRecord = records.find(function (record) { return record.id === state.selected; });
    if (selectedRecord) {
      visible = [selectedRecord].concat(visible.slice(0,Math.max(0,state.limit - 1)));
      pinnedId = selectedRecord.id;
    }
  }
  visible.forEach(function (record) {
    const template = document.createElement('template');
    template.innerHTML = state.view === 'research' ? researchRow(record) : ideaRow(record);
    const row = template.content.firstElementChild;
    if (record.id === pinnedId) {
      row.classList.add('pinned-selection');
      row.title = 'Selected record pinned above the current result window';
    }
    fragment.appendChild(row);
  });
  body.replaceChildren(fragment);
  body.querySelectorAll('[data-record-id]').forEach(function (row,index) {
    row.setAttribute('aria-rowindex',String(index + 2));
  });
  if (!body.querySelector('[aria-selected="true"]')) {
    const first = body.querySelector('[data-record-id]');
    if (first) first.tabIndex = 0;
  }
  document.getElementById('data-table').setAttribute('aria-rowcount',String(records.length + 1));

  const empty = document.getElementById('empty-state');
  empty.classList.toggle('visible',records.length === 0);
  const queueEmpty = state.view === 'queue' && workflowItems.size === 0;
  document.getElementById('empty-title').textContent = queueEmpty ? 'Decision queue is empty in this tab session' : 'No matching records';
  document.getElementById('empty-copy').textContent = queueEmpty
    ? 'Open a research observation and choose Add to review.'
    : 'Adjust the search or clear one of the active filters.';

  const more = document.getElementById('load-more-wrap');
  more.classList.toggle('visible',records.length > visible.length);
  document.getElementById('load-more').textContent = 'Show next ' + Math.min(PAGE_SIZE[state.view],records.length - visible.length) + ' of ' + number(records.length - visible.length) + ' remaining';
}

function renderOrphanedQueue() {
  const shell = document.getElementById('orphaned-queue');
  const list = document.getElementById('orphaned-list');
  const orphaned = Array.from(workflowItems.values()).filter(function (item) { return !IDEA_BY_ID.has(item.id); });
  const visible = state.view === 'queue' && orphaned.length > 0;
  shell.classList.toggle('visible',visible);
  if (!visible) {
    list.replaceChildren();
    return;
  }
  list.innerHTML = orphaned.slice(0,50).map(function (item) {
    const source = item.source_snapshot;
    const coverage = packetCoverage(item);
    return '<article class="orphaned-item"><time datetime="' + escapeHtml(source.date) + '">' + escapeHtml(source.date || 'Date unknown') + '</time><div><strong>' + escapeHtml(source.title) + '</strong><p>' + escapeHtml(source.passage || 'Passage snapshot unavailable') + '</p><small>' + escapeHtml(item.status) + ' · packet ' + coverage.completed + '/' + coverage.total + ' · retained from dataset ' + escapeHtml(String(source.data_checksum || '').slice(0,12) || 'unknown') + '</small></div><a href="' + escapeHtml(safeUrl(source.url)) + '" target="_blank" rel="noopener noreferrer">Open source ↗</a></article>';
  }).join('');
}

function renderContext(records) {
  let visibleIdeas;
  let visibleArticles;
  if (state.view === 'research') {
    visibleArticles = records;
    visibleIdeas = records.flatMap(function (article) {
      return article._ideas.filter(function (idea) { return ideaMatchesResearchFacets(idea); });
    });
  } else {
    visibleIdeas = records;
    visibleArticles = Array.from(new Set(records.map(function (idea) { return idea.article_id; }))).map(function (id) { return ARTICLE_BY_ID.get(id); });
  }
  const managers = new Set(visibleIdeas.map(function (idea) { return idea.manager; }).filter(Boolean));
  document.getElementById('visible-primary').textContent = number(records.length);
  document.getElementById('visible-primary-label').textContent = state.view === 'research' ? 'notes' : 'observations';
  document.getElementById('visible-articles').textContent = number(visibleArticles.length);
  document.getElementById('visible-secondary-label').textContent = state.view === 'research' ? 'observations' : 'notes';
  if (state.view === 'research') document.getElementById('visible-articles').textContent = number(visibleIdeas.length);
  document.getElementById('visible-managers').textContent = number(managers.size);

  const counts = {long:0,short:0,'arbitrage/relative value':0,'long/short':0,unspecified:0};
  visibleIdeas.forEach(function (idea) { counts[idea.direction] = (counts[idea.direction] || 0) + 1; });
  const total = visibleIdeas.length || 1;
  const segments = [
    ['long','mix-long'],['short','mix-short'],['arbitrage/relative value','mix-arb'],
    ['long/short','mix-ls'],['unspecified','mix-unspecified']
  ];
  const directionMix = document.getElementById('direction-mix');
  directionMix.innerHTML = segments.map(function (row) {
    const width = counts[row[0]] / total * 100;
    return '<span class="mix-segment ' + row[1] + '" style="width:' + width.toFixed(3) + '%" title="' + directionLabel(row[0]) + ': ' + number(counts[row[0]]) + '"></span>';
  }).join('');
  const directionSummary = 'Parsed passage language—not exposure · Long ' + number(counts.long) + ' · Short ' + number(counts.short) + ' · Relative value ' +
    number(counts['arbitrage/relative value']) + ' · L/S ' + number(counts['long/short']) +
    ' · No reliable stance ' + number(counts.unspecified);
  directionMix.setAttribute('aria-label',directionSummary);
  document.getElementById('mix-legend').textContent = directionSummary;
}

const BRIEF_KIND_LABELS = {
  evidence:'Contextual evidence passage', mechanism:'Mechanism', countercase:'Countercase / limitation',
  falsifier:'What would change the view', implementation:'Implementation / what to watch'
};
const BRIEF_SEQUENCE = [
  ['lead','Opening authored passage'],['evidence','Contextual evidence passage'],
  ['mechanism','Mechanism'],['countercase','Countercase / limitation'],
  ['falsifier','What would change the view'],['implementation','Implementation / what to watch']
];
function numberTokenRegex() {
  return /((?:\b(?:sharpe|sortino|rmse|r\s?[²2]|t-stat(?:istic)?|beta|alpha)\s*(?:of|=|:)?\s*)?(?:[$€£¥]\s*)?[+\-−]?\d+(?:,\d{3})*(?:\.\d+)?(?:\s*(?:-\s*to\s*-|[–—-]|to)\s*(?:[$€£¥]\s*)?[+\-−]?\d+(?:,\d{3})*(?:\.\d+)?)?(?:\s*(?:%|bp\b|bps\b|basis points?\b|[x×]\b|k\b|m\b|b\b|t\b|mn\b|bn\b|million\b|billion\b|trillion\b))?)/gi;
}
function extractNumberTokens(value) {
  const matches = String(value || '').match(numberTokenRegex()) || [];
  const seen = new Set();
  return matches.filter(function (token) {
    const key = normalize(token);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0,10);
}
function highlightArticleNumbers(value) {
  const text = String(value || '');
  const regex = numberTokenRegex();
  let markup = '';
  let cursor = 0;
  let match;
  while ((match = regex.exec(text)) !== null) {
    markup += escapeHtml(text.slice(cursor,match.index));
    markup += '<mark>' + escapeHtml(match[0]) + '</mark>';
    cursor = match.index + match[0].length;
  }
  return markup + escapeHtml(text.slice(cursor));
}
function articleBriefSpans(article) {
  const brief = article && article.brief || {lead:null,sections:[],fallback_evidence:null,checkpoints:[]};
  const rows = [];
  const byIdentity = new Map();
  function add(kind,label,heading,span) {
    if (!span || !span.text) return;
    const identity = String(span.sha256 || (String(span.start) + ':' + String(span.end) + ':' + span.text));
    let row = byIdentity.get(identity);
    if (!row) {
      row = {kind:kind,kinds:[],labels:[],headings:[],span:span,anchor:'brief-span-' + String(span.sha256 || (span.start + '-' + span.end)).slice(0,16)};
      byIdentity.set(identity,row);
      rows.push(row);
    }
    if (!row.kinds.includes(kind)) row.kinds.push(kind);
    if (!row.labels.includes(label)) row.labels.push(label);
    if (heading && !row.headings.includes(heading)) row.headings.push(heading);
    row.label = row.labels.join(' / ');
    row.heading = row.headings.join(' · ');
  }
  add('lead','Opening authored passage','First eligible prose passage',brief.lead);
  const evidence = articleEvidence(article);
  if (evidence && evidence.text) {
    const explicit = briefSection(article,'evidence');
    add('evidence',explicit ? 'Contextual evidence passage' : 'Contextual numerical passage',explicit && explicit.heading || 'First high-precision numerical context passage',evidence);
  }
  ['mechanism','countercase','falsifier','implementation'].forEach(function (kind) {
    const section = briefSection(article,kind);
    add(kind,BRIEF_KIND_LABELS[kind],section && section.heading,section);
  });
  return rows;
}
function articleEvidenceLedger(article) {
  return articleBriefSpans(article).map(function (row) {
    return Object.assign({},row,{values:extractNumberTokens(row.span.text)});
  }).filter(function (row) { return row.values.length; });
}
function articleExtractionMap(article) {
  if (!observationsReady) {
    const stateCopy = observationsFailed
      ? 'The release-bound observation asset could not be verified. This is a load failure, not evidence absence.'
      : 'Loading the release-bound parser observations for this article…';
    return '<section class="article-dossier-section"><h3>Instrument extraction map</h3><p class="missing">' + escapeHtml(stateCopy) + '</p></section>';
  }
  const buckets = new Map();
  (article._ideas || []).forEach(function (idea) {
    const instruments = new Set((idea.instruments || []).length ? idea.instruments : ['unspecified']);
    instruments.forEach(function (instrument) {
      if (!buckets.has(instrument)) buckets.set(instrument,{passages:0,numeric:0,directional:0,outcomes:0});
      const row = buckets.get(instrument);
      row.passages += 1;
      if (hasValue(idea.quant)) row.numeric += 1;
      if (idea.direction && idea.direction !== 'unspecified') row.directional += 1;
      if (hasValue(idea.outcome)) row.outcomes += 1;
    });
  });
  const rows = Array.from(buckets.entries()).sort(function (left,right) {
    return right[1].passages - left[1].passages || instrumentLabel(left[0]).localeCompare(instrumentLabel(right[0]));
  }).map(function (entry) {
    const value = entry[1];
    return '<tr><td>' + escapeHtml(instrumentLabel(entry[0])) + '</td><td>' + number(value.passages) + '</td><td>' + number(value.numeric) + '</td><td>' + number(value.directional) + '</td><td>' + number(value.outcomes) + '</td></tr>';
  }).join('');
  return '<section class="article-dossier-section"><h3>Instrument extraction map</h3>' +
    (rows ? '<table class="extraction-map"><caption class="sr-only">Parser-derived passages by instrument</caption><thead><tr><th scope="col">Instrument</th><th scope="col">Passages</th><th scope="col">Numeric</th><th scope="col">Directional</th><th scope="col">Outcomes</th></tr></thead><tbody>' + rows + '</tbody></table>' : '<p class="missing">No instrument-tagged parser observations were captured for this article.</p>') +
    '<p class="extraction-note">This maps parser-derived source passages, not positions, exposure, conviction, or portfolio risk. A multi-instrument passage is counted once in every applicable row; numeric and outcome fields are reported language, not independent verification.</p></section>';
}
function spanProvenance(span) {
  if (!span) return 'Exact source passage';
  const hash = String(span.sha256 || '');
  const offsets = Number.isInteger(span.start) && Number.isInteger(span.end) && span.end > span.start
    ? 'chars ' + number(span.start) + '–' + number(span.end) : '';
  const identity = hash ? 'SHA-256 ' + hash.slice(0,12) : '';
  return ['Exact source span',offsets,identity,span.truncated ? 'shortened for display' : 'complete captured span'].filter(Boolean).join(' · ');
}
function evidenceLedgerMarkup(article) {
  const ledger = articleEvidenceLedger(article);
  const rows = ledger.map(function (row) {
    const provenance = spanProvenance(row.span);
    return '<div class="ledger-row" role="row"><div class="ledger-cell ledger-role" role="cell"><b>' + escapeHtml(row.label) + '</b><span>' + escapeHtml(row.heading) + '</span></div>' +
      '<div class="ledger-cell" role="cell"><div class="ledger-values">' + row.values.map(function (value) { return '<span class="ledger-value">' + escapeHtml(value) + '</span>'; }).join('') + '</div></div>' +
      '<div class="ledger-cell" role="cell"><p class="ledger-passage">' + highlightArticleNumbers(row.span.text) + '</p><span class="ledger-provenance" title="' + escapeHtml(String(row.span.sha256 || '')) + '">' + escapeHtml(provenance) + '</span></div></div>';
  }).join('');
  return '<section class="evidence-ledger-section" id="brief-evidence-ledger" aria-labelledby="evidence-ledger-title"><div class="evidence-ledger-head"><div><div class="intel-label">Evidence ledger</div><h3 id="evidence-ledger-title">Detected numbers with their authored context</h3><p>Numeric tokens remain attached to the exact captured passage. Detection is lexical, deduplicated, and capped at ten unique tokens per passage; values are not normalized, made comparable, or independently verified.</p></div><span class="evidence-ledger-count">' + number(ledger.length) + ' unique source span' + (ledger.length === 1 ? '' : 's') + '</span></div>' +
    (rows ? '<div class="evidence-ledger" role="table" aria-label="Exact number-bearing source passages"><div class="ledger-row ledger-head" role="row"><div class="ledger-cell" role="columnheader">Research role</div><div class="ledger-cell" role="columnheader">Detected numeric tokens · max 10</div><div class="ledger-cell" role="columnheader">Exact authored context</div></div>' + rows + '</div>' : '<div class="intel-empty">No number-bearing passage was detected in the captured brief. This is an extraction boundary, not a claim that the article contains no quantitative evidence.</div>') + '</section>';
}
function researchMapMarkup(article) {
  const spans = articleBriefSpans(article);
  const checkpointCount = Number(article.brief && article.brief.checkpoints && article.brief.checkpoints.length || 0);
  const canonicalTargets = {
    lead:'brief-thesis',evidence:'brief-analysis',mechanism:'brief-analysis',checkpoint:'brief-checkpoints'
  };
  const steps = BRIEF_SEQUENCE.concat([['checkpoint','Public checkpoint']]).map(function (row,index) {
    const capturedSpan = spans.find(function (span) { return span.kinds.includes(row[0]); });
    const captured = row[0] === 'checkpoint' ? checkpointCount > 0 : Boolean(capturedSpan);
    const detail = captured ? (row[0] === 'checkpoint' ? number(checkpointCount) + ' dated cited checkpoint' + (checkpointCount === 1 ? '' : 's') : 'Exact passage captured') : 'Not identified by rules';
    const target = canonicalTargets[row[0]] || capturedSpan && capturedSpan.anchor;
    const inner = '<b>' + String(index + 1).padStart(2,'0') + ' · ' + escapeHtml(row[1]) + '</b><span>' + escapeHtml(detail) + '</span>';
    return captured ? '<button class="research-map-step captured" type="button" data-brief-jump="' + target + '">' + inner + '</button>' : '<div class="research-map-step not-captured">' + inner + '</div>';
  }).join('');
  return '<section class="research-map" aria-labelledby="research-map-title"><div class="research-map-head"><h3 id="research-map-title">Institutional diligence map</h3><p>Presence means an exact authored passage was captured—not that the argument is correct, complete, investable, or independently verified.</p></div><div class="research-map-track">' + steps + '</div></section>';
}
function archiveCoverageMarkup(records) {
  const definitions = [
    ['Contextual evidence','evidence'],['Mechanism','mechanism'],['Countercase','countercase'],
    ['Falsifier','falsifier'],['Implementation','implementation'],['Checkpoint','checkpoint']
  ];
  const denominator = records.length || 1;
  const bars = definitions.map(function (row) {
    const count = records.filter(function (article) {
      if (row[1] === 'checkpoint') return Boolean(article.brief_features && article.brief_features.checkpoint_count);
      return row[1] === 'evidence' ? articleHasEvidence(article) : articleHasBriefKind(article,row[1]);
    }).length;
    const percent = Math.round(count / denominator * 100);
    const visiblePercent = count ? Math.max(1,percent) : 0;
    return '<div class="coverage-bar-row"><span>' + row[0] + '</span><div class="coverage-bar-track" role="img" aria-label="' + escapeHtml(row[0] + ': ' + count + ' of ' + records.length + ' articles') + '"><i class="coverage-bar-fill" style="width:' + visiblePercent + '%"></i></div><b>' + number(count) + '</b></div>';
  }).join('');
  return '<section class="intel-side-card"><div class="intel-card-head"><h3>Dossier coverage in this lens</h3><span>' + number(records.length) + ' articles</span></div><div class="coverage-bars">' + bars + '</div><p class="coverage-caveat">High-precision section presence only; not research quality, confidence, or a recommendation score.</p></section>';
}
function relatedArticleRows(selected) {
  const selectedManagers = new Set(selected.manager_keys || []);
  const selectedUnderlyings = new Map((selected.underlyings || []).map(function (value) { return [normalize(value),value]; }));
  const selectedInstruments = new Set((selected.instruments || []).filter(function (value) { return value !== 'unspecified'; }));
  const ranked = ARTICLES.filter(function (article) { return article.id !== selected.id; }).map(function (article) {
    const managers = (article.manager_keys || []).filter(function (value) { return selectedManagers.has(value); });
    const underlyings = (article.underlyings || []).filter(function (value) { return selectedUnderlyings.has(normalize(value)); });
    const instruments = (article.instruments || []).filter(function (value) { return selectedInstruments.has(value) && value !== 'unspecified'; });
    const reasons = managers.slice(0,1).map(function (value) { return 'Same mentioned entity: ' + (MANAGER_LABELS.get(value) || value); })
      .concat(underlyings.slice(0,2).map(function (value) { return 'Same extracted underlying: ' + value; }));
    return {article:article,score:managers.length * 10 + underlyings.length * 8 + instruments.length * 0.25,reasons:reasons,qualified:Boolean(managers.length || underlyings.length)};
  }).filter(function (row) { return row.qualified; }).sort(function (left,right) {
    return right.score - left.score || right.article.date.localeCompare(left.article.date);
  });
  if (ranked.length) return {related:true,rows:ranked.slice(0,4)};
  return {related:false,rows:ARTICLES.filter(function (article) { return article.id !== selected.id; }).slice(0,4).map(function (article) { return {article:article,reasons:[]}; })};
}
function relatedResearchMarkup(selected) {
  const result = relatedArticleRows(selected);
  const items = result.rows.map(function (row) {
    const article = row.article;
    return '<button class="next-item" type="button" data-brief-article="' + article.id + '"><time datetime="' + article.date + '">' + escapeHtml(shortDate(article.date)) + ' · ' + sourceLabel(article.source) + '</time><span class="next-title">' + escapeHtml(article.title) + '</span><span class="next-summary">' + escapeHtml(articleClaim(article)) + '</span>' +
      (row.reasons.length ? '<span class="related-context">' + row.reasons.map(function (reason) { return '<span>' + escapeHtml(reason) + '</span>'; }).join('') + '</span>' : '') + '</button>';
  }).join('');
  return '<section class="intel-side-card"><div class="intel-card-head"><h3>' + (result.related ? 'Related archive context' : 'Recent research') + '</h3><span>' + (result.related ? 'Exact entity or underlying overlap' : 'No direct overlap found') + '</span></div><div class="next-list">' + items + '</div></section>';
}
function relatedPremiumRows(selected) {
  if (!selected || isPaidSubstackArticle(selected)) return [];
  const selectedManagers = new Set(selected.manager_keys || []);
  const selectedUnderlyings = new Map((selected.underlyings || []).map(function (value) { return [normalize(value),value]; }));
  const selectedTopics = new Set((THREAD_ARTICLES[selected.id] && THREAD_ARTICLES[selected.id].topics) || []);
  const selectedInstruments = new Set((selected.instruments || []).filter(function (value) { return value !== 'unspecified'; }));
  return ARTICLES.filter(function (article) {
    return article.id !== selected.id && isPaidSubstackArticle(article);
  }).map(function (article) {
    const managers = (article.manager_keys || []).filter(function (value) { return selectedManagers.has(value); });
    const underlyings = (article.underlyings || []).filter(function (value) { return selectedUnderlyings.has(normalize(value)); });
    const topics = (((THREAD_ARTICLES[article.id] && THREAD_ARTICLES[article.id].topics) || []).filter(function (value) { return selectedTopics.has(value); }));
    const instruments = (article.instruments || []).filter(function (value) { return selectedInstruments.has(value); });
    const reasons = topics.slice(0,1).map(function (value) {
      const topic = THREADS.topics && THREADS.topics[value];
      return 'Same research topic: ' + (topic && topic.label || value);
    }).concat(managers.slice(0,1).map(function (value) { return 'Same mentioned entity: ' + (MANAGER_LABELS.get(value) || value); }))
      .concat(underlyings.slice(0,2).map(function (value) { return 'Same extracted underlying: ' + value; }));
    if (instruments.length) reasons.push('Same market: ' + instrumentLabel(instruments[0]));
    return {article:article,reasons:reasons,score:topics.length * 14 + managers.length * 10 + underlyings.length * 8 + instruments.length * 3};
  }).filter(function (row) { return row.score > 0; }).sort(function (left,right) {
    return right.score - left.score || right.article.date.localeCompare(left.article.date);
  }).slice(0,3);
}
function premiumArchiveMarkup(selected) {
  const rows = relatedPremiumRows(selected);
  if (!rows.length) return '';
  const items = rows.map(function (row) {
    return '<button class="next-item" type="button" data-brief-article="' + row.article.id + '"><time datetime="' + row.article.date + '">' + escapeHtml(shortDate(row.article.date)) + ' · Subscriber research</time><span class="next-title">' + escapeHtml(row.article.title) + '</span><span class="next-summary">' + escapeHtml(articleClaim(row.article)) + '</span><span class="related-context">' + row.reasons.map(function (reason) { return '<span>' + escapeHtml(reason) + '</span>'; }).join('') + '</span></button>';
  }).join('');
  return '<section class="intel-side-card premium-archive-card"><div class="intel-card-head"><h3>Related subscriber research</h3><span>' + number(rows.length) + ' shown · exact-context overlap</span></div><p class="premium-archive-intro">These subscriber notes share an indexed research topic, normalized entity, extracted underlying, or market with the active dossier. The match organizes the archive; it does not imply a recommendation, position, or similar conclusion.</p><div class="next-list">' + items + '</div><div class="premium-archive-actions"><p>Open a dossier to inspect the bounded source material in this release, or review current subscription terms directly on Substack.</p><a class="secondary-action" href="' + escapeHtml(SUBSCRIPTION_URL) + '" target="_blank" rel="noopener noreferrer" aria-label="See Navnoor Research subscription plans (opens in a new tab)">See subscription plans ↗</a></div></section>';
}
function articleReasons(article) {
  const reasons = [];
  if (isNewArticle(article)) reasons.push(['New','accent']);
  reasons.push([sourceAccessLabel(article),article.content_status === 'full' ? '' : isPaidSubstackArticle(article) ? 'premium-badge' : 'evidence-gap']);
  if (article.body_revision_status !== 'current') {
    reasons.push([bodyRevisionLabel(article),'evidence-gap']);
  }
  if (articleHasEvidence(article)) reasons.push(['Contextual evidence passage','accent']);
  if (articleHasBriefKind(article,'countercase')) reasons.push(['Countercase','']);
  if (articleHasBriefKind(article,'falsifier')) reasons.push(['Explicit falsifier','']);
  if (articleHasBriefKind(article,'implementation')) reasons.push(['Implementation / capacity','']);
  if ((article.brief && article.brief.checkpoints.length) || (article.brief_features && article.brief_features.checkpoint_count)) reasons.push(['Dated checkpoint','accent']);
  const structures = new Set(article.directions.filter(function (value) { return value !== 'unspecified'; }));
  if (structures.size > 1) reasons.push(['Mixed structures in passages','']);
  return reasons;
}
function reasonChips(article,limit) {
  return articleReasons(article).slice(0,limit || 8).map(function (row) {
    return '<span class="intel-reason ' + row[1] + '">' + escapeHtml(row[0]) + '</span>';
  }).join('');
}
function exactPassageTail(span) {
  return '<span class="source-tail" title="' + escapeHtml(String(span && span.sha256 || '')) + '">' + escapeHtml(spanProvenance(span)) + '</span>';
}
function intelligenceSection(row,full) {
  if (!row || !row.span || !row.span.text) return '';
  return '<section class="intel-section' + (full ? ' full' : '') + '" id="' + escapeHtml(row.anchor) + '">' +
    '<div class="intel-label">' + escapeHtml(row.label) + '</div>' +
    (row.heading ? '<h3>' + escapeHtml(row.heading) + '</h3>' : '') +
    '<p class="intel-passage">' + highlightArticleNumbers(row.span.text) + '</p>' +
    exactPassageTail(row.span) + '</section>';
}
function articleBriefText(article) {
  const spans = articleBriefSpans(article);
  const checkpoints = article && article.brief && article.brief.checkpoints || [];
  const lines = [
    article.title,
    sourceLabel(article.source) + ' · published ' + formatDate(article.date) + ' · ' + (article.content_status === 'full' ? 'full text indexed' : 'excerpt indexed'),
    'Publication access: ' + publicationAccessLabel(article),
    'Source: ' + article.url,
    'Body revision: ' + bodyRevisionSummary(article),
    'Dataset: ' + String(SNAPSHOT.data_checksum || ''),
    'Dataset assembled: ' + formatReleaseCheckedAt(SNAPSHOT.checked_at),
    sourceCollectionSummary(article.source),
  ];
  spans.forEach(function (row) {
    lines.push(row.label.toUpperCase() + (row.heading ? ' — ' + row.heading : '') + '\n' + row.span.text + '\n[' + spanProvenance(row.span) + ']');
  });
  checkpoints.forEach(function (checkpoint) {
    lines.push('PUBLIC CHECKPOINT — ' + formatDate(checkpoint.date) + '\n' + checkpoint.text + '\n[' + spanProvenance(checkpoint) + ']');
  });
  lines.push('Boundary: exact published-source passages; not independently verified, not a live market as-of, and not a portfolio recommendation.');
  return lines.join('\n\n');
}
function intelligenceCard(article) {
  return '<button class="intel-article-card" type="button" data-brief-article="' + article.id + '">' +
    '<span class="intel-meta"><time datetime="' + article.date + '">' + escapeHtml(shortDate(article.date)) + '</time><span>·</span><span>' + sourceLabel(article.source) + '</span><span>·</span><span>' + escapeHtml(sourceAccessLabel(article)) + '</span></span>' +
    '<span class="intel-card-title">' + escapeHtml(article.title) + '</span><span class="intel-card-claim">' + escapeHtml(articleClaim(article)) + '</span>' +
    '<span class="intel-reasons">' + reasonChips(article,3) + '</span></button>';
}
function evidenceSpotlightMarkup(article) {
  const priority = {evidence:0,mechanism:1,lead:2,countercase:3,falsifier:4,implementation:5};
  const rows = articleEvidenceLedger(article).slice().sort(function (left,right) {
    const leftRank = Math.min.apply(null,left.kinds.map(function (kind) { return priority[kind] === undefined ? 9 : priority[kind]; }));
    const rightRank = Math.min.apply(null,right.kinds.map(function (kind) { return priority[kind] === undefined ? 9 : priority[kind]; }));
    return leftRank - rightRank;
  }).slice(0,2);
  const content = !rows.length
    ? '<div class="ic-evidence-empty"><strong>No number-bearing brief passage was identified.</strong> This is an extraction boundary, not a conclusion that the full article contains no quantitative evidence. Open the source for complete context.</div>'
    : rows.map(function (row) {
    const values = row.values.slice(0,5);
    return '<article class="ic-evidence-card"><div class="ic-evidence-overline">Exact authored passage</div><div class="ic-evidence-values">' +
      values.map(function (value,index) { return (index ? '<i>·</i>' : '') + '<span>' + escapeHtml(value) + '</span>'; }).join('') +
      '</div><h3>' + escapeHtml(row.label) + (row.heading ? ' · ' + escapeHtml(row.heading) : '') + '</h3><p>' + highlightArticleNumbers(row.span.text) + '</p>' +
      '<span class="source-tail" title="' + escapeHtml(String(row.span.sha256 || '')) + '">' + escapeHtml(spanProvenance(row.span)) + '</span></article>';
  }).join('');
  return '<section class="ic-evidence-strip" id="brief-key-evidence" aria-labelledby="brief-key-evidence-title"><h2 class="sr-only" id="brief-key-evidence-title">Source-backed numeric evidence</h2>' + content + '</section>';
}
const THREAD_ROLE_DEFINITIONS = [
  ['lead','Opening'],['mechanism','Mechanism'],['evidence','Evidence'],
  ['countercase','Countercase'],['falsifier','Falsifier'],
  ['implementation','Implementation'],['checkpoint','Checkpoint']
];
const THREAD_MATCH_LABELS = {
  t:'title',s:'subtitle',o:'opening',e:'evidence',m:'mechanism',
  c:'countercase',f:'falsifier',i:'implementation',x:'indexed section'
};
function threadArticleRow(article) {
  return article && THREAD_ARTICLES[article.id] || null;
}
function selectedThreadTopic(article) {
  const row = threadArticleRow(article);
  if (!row || !Array.isArray(row.topics) || !row.topics.length) {
    state.threadTopic = '';
    return '';
  }
  const topic = row.topics.includes(state.threadTopic) ? state.threadTopic : row.default_topic;
  state.threadTopic = topic;
  return topic;
}
function threadRoleCaptured(article,key) {
  const features = article && article.brief_features || {};
  if (key === 'checkpoint') return Number(features.checkpoint_count || 0) > 0;
  return Boolean(features[key]);
}
function threadRoleLabels(article) {
  return THREAD_ROLE_DEFINITIONS.filter(function (row) {
    return threadRoleCaptured(article,row[0]);
  }).map(function (row) { return row[1]; });
}
function threadPublishedLabel(article) {
  const raw = String(article && article.published_at || article && article.date || '');
  if (article && article.publication_precision === 'instant') {
    const zone = raw.endsWith('Z') ? 'UTC' : (raw.match(/[+-]\d{2}:\d{2}$/) || ['time'])[0];
    return shortDate(raw.slice(0,10)) + ' · ' + raw.slice(11,16) + ' ' + zone;
  }
  return shortDate(raw.slice(0,10)) + ' · publication day';
}
function threadMatchLabel(topic,articleId) {
  const position = topic.article_ids.indexOf(articleId);
  const codes = position >= 0 ? String(topic.match_codes[position] || '') : '';
  const labels = Array.from(codes).map(function (code) {
    return THREAD_MATCH_LABELS[code] || '';
  }).filter(Boolean);
  return labels.length ? labels.join(' / ') : 'high-precision topic index';
}
function threadWindow(articleIds,selectedId,limit) {
  const ids = articleIds.filter(function (id) { return ARTICLE_BY_ID.has(id); });
  if (ids.length <= limit) return ids;
  const position = Math.max(0,ids.indexOf(selectedId));
  const start = Math.max(0,Math.min(position - Math.floor(limit / 2),ids.length - limit));
  return ids.slice(start,start + limit);
}
function threadNumericTokens(article) {
  const lead = article && article.brief && article.brief.lead;
  return lead && lead.text ? extractNumberTokens(lead.text).slice(0,10) : [];
}
function threadExactPassageCard(article,label) {
  const lead = article && article.brief && article.brief.lead;
  const values = threadNumericTokens(article);
  return '<article class="thread-passage-card"><div class="thread-passage-meta"><span>' + escapeHtml(label) + '</span><time datetime="' + escapeHtml(String(article.published_at || article.date)) + '">' + escapeHtml(threadPublishedLabel(article)) + '</time></div><h4>' + escapeHtml(article.title) + '</h4>' +
    (lead && lead.text ? '<p>' + highlightArticleNumbers(lead.text) + '</p>' + exactPassageTail(lead) : '<p class="missing">No opening passage was identified by the high-precision brief rules. This is an extraction boundary.</p>') +
    '<div class="thread-token-row"><span>Opening-passage numeric tokens</span><div>' + (values.length ? values.map(function (value) { return '<i>' + escapeHtml(value) + '</i>'; }).join('') : '<em>None in the captured opening passage</em>') + '</div></div></article>';
}
function threadComparisonMarkup(current,topic) {
  const position = topic.article_ids.indexOf(current.id);
  if (position <= 0) {
    return '<section class="thread-comparison" aria-labelledby="thread-comparison-first-title"><div class="thread-subhead"><div><span>Capture comparison</span><h3 id="thread-comparison-first-title">Earliest indexed publication in this topic thread</h3></div><b>No prior topic match</b></div><p class="thread-first-note">There is no earlier body-backed article linked by this exact topic key. The bounded chronology below shows up to seven of ' + number(topic.article_count) + ' indexed publications.</p></section>';
  }
  const prior = ARTICLE_BY_ID.get(topic.article_ids[position - 1]);
  if (!prior) return '';
  const sameDayUnordered = current.date === prior.date && (current.publication_precision === 'day' || prior.publication_precision === 'day');
  const roleRows = THREAD_ROLE_DEFINITIONS.map(function (definition) {
    const before = threadRoleCaptured(prior,definition[0]);
    const now = threadRoleCaptured(current,definition[0]);
    const status = before === now ? (now ? 'Both captured' : 'Neither captured') : now ? 'Current capture only' : 'Prior capture only';
    return '<div class="thread-role-row" role="row"><span role="cell">' + escapeHtml(definition[1]) + '</span><i role="cell" class="' + (before ? 'captured' : '') + '">' + (before ? 'Yes' : '—') + '</i><i role="cell" class="' + (now ? 'captured' : '') + '">' + (now ? 'Yes' : '—') + '</i><b role="cell">' + status + '</b></div>';
  }).join('');
  const changedCount = THREAD_ROLE_DEFINITIONS.filter(function (definition) {
    return threadRoleCaptured(prior,definition[0]) !== threadRoleCaptured(current,definition[0]);
  }).length;
  let exactMarkup;
  if (prior.brief) {
    exactMarkup = '<div class="thread-passage-grid">' + threadExactPassageCard(prior,'Preceding indexed publication') + threadExactPassageCard(current,'Current dossier') + '</div><p class="thread-token-note">Opening-passage numeric tokens remain attached to the displayed source passage. They are not normalized, netted, or treated as comparable values.</p>';
  } else {
    const loading = Boolean(threadComparisonRequest && threadComparisonRequest.articleId === prior.id && threadComparisonRequest.selectedId === current.id && threadComparisonRequest.topic === state.threadTopic);
    exactMarkup = '<div class="thread-load-boundary"' + (loading ? ' aria-busy="true"' : '') + '><div><strong>' + (loading ? 'Validating exact prior dossier…' : 'Exact prior passage is deferred') + '</strong><p>' + (loading ? 'The release-bound archive is loading and its checksum and record identities will be verified before display.' : 'Load the release-bound prior dossier only when you want a side-by-side passage and numeric-token comparison.') + '</p></div><button class="secondary-action" type="button" data-thread-load="' + prior.id + '"' + (loading ? ' disabled aria-busy="true"' : '') + '>' + (loading ? 'Loading exact comparison…' : prior._briefLoadFailed ? 'Retry exact prior dossier' : 'Load exact passage comparison') + '</button></div>';
  }
  return '<section class="thread-comparison" aria-labelledby="thread-comparison-title"><div class="thread-subhead"><div><span>Capture comparison with preceding indexed publication</span><h3 id="thread-comparison-title">' + escapeHtml(sameDayUnordered ? 'Adjacent same-day capture; order unavailable' : 'Prior-to-current capture comparison') + '</h3></div><b>' + number(changedCount) + ' role field' + (changedCount === 1 ? '' : 's') + ' differ</b></div><div class="thread-role-table" role="table" aria-label="Captured research roles in prior and current publications"><div class="thread-role-row head" role="row"><span role="columnheader">Research role</span><i role="columnheader">Prior</i><i role="columnheader">Current</i><b role="columnheader">Coverage comparison</b></div>' + roleRows + '</div>' + exactMarkup + '<p class="thread-boundary compact">A field difference means the extraction rules captured different research roles in two publications. It does not establish a changed view, contradiction, conviction, or portfolio action.</p></section>';
}
function researchThreadMarkup(article) {
  const row = threadArticleRow(article);
  const topicKey = selectedThreadTopic(article);
  const topic = topicKey && THREADS.topics && THREADS.topics[topicKey];
  if (!row || !topic) return '';
  const attachedTopics = row.topics.slice();
  const topicButtons = Array.from(new Set(attachedTopics)).map(function (key) {
    const candidate = THREADS.topics[key];
    const active = key === topicKey;
    return '<button class="thread-topic' + (active ? ' active' : '') + '" type="button" data-thread-topic="' + escapeHtml(key) + '" aria-pressed="' + String(active) + '">' + escapeHtml(candidate.label) + '<span>' + number(candidate.article_count) + '</span></button>';
  }).join('');
  const topicArticles = topic.article_ids.map(function (id) { return ARTICLE_BY_ID.get(id); }).filter(Boolean);
  const firstArticle = ARTICLE_BY_ID.get(topic.article_ids[0]);
  const lastArticle = ARTICLE_BY_ID.get(topic.article_ids[topic.article_ids.length - 1]);
  const fullCount = topicArticles.filter(function (candidate) { return candidate.content_status === 'full'; }).length;
  const sourceCount = new Set(topicArticles.map(function (candidate) { return candidate.source; })).size;
  const timelineIds = threadWindow(topic.article_ids,article.id,7);
  const hiddenCount = topic.article_ids.length - timelineIds.length;
  const timeline = timelineIds.map(function (id) {
    const candidate = ARTICLE_BY_ID.get(id);
    const active = candidate.id === article.id;
    const roles = threadRoleLabels(candidate);
    const matchLabel = threadMatchLabel(topic,candidate.id);
    const framing = candidate.brief && candidate.brief.lead && candidate.brief.lead.text || candidate.subtitle || 'Open this dossier to inspect its exact captured passages.';
    return '<li class="thread-node' + (active ? ' active' : '') + '"><button type="button" data-thread-article="' + candidate.id + '"' + (active ? ' aria-current="true"' : '') + '><span class="thread-node-meta"><time datetime="' + escapeHtml(String(candidate.published_at || candidate.date)) + '">' + escapeHtml(threadPublishedLabel(candidate)) + '</time><i>' + sourceLabel(candidate.source) + (active ? ' · current dossier' : '') + '</i></span><strong>' + escapeHtml(candidate.title) + '</strong><span class="thread-node-match">Matched in: ' + escapeHtml(matchLabel) + '</span><span class="thread-node-framing">' + escapeHtml(framing) + '</span><span class="thread-node-roles">' + (roles.length ? roles.slice(0,5).map(function (label) { return '<i>' + escapeHtml(label) + '</i>'; }).join('') + (roles.length > 5 ? '<i>+' + number(roles.length - 5) + '</i>' : '') : '<i>No brief role captured</i>') + '</span></button></li>';
  }).join('');
  return '<section class="research-thread" id="brief-thread" aria-labelledby="research-thread-title"><div class="thread-header"><div><div class="ic-topic">Research threads · exact topic history</div><h2 id="research-thread-title">Research history across related publications</h2><p>Trace the published record for <strong>' + escapeHtml(topic.label) + '</strong> after reviewing the current dossier’s evidence and decision boundaries.</p></div><span class="thread-kind">' + escapeHtml(topic.kind) + '</span></div><div class="thread-topic-list" role="group" aria-label="Repeated topics captured in this article">' + topicButtons + '</div><div class="thread-facts"><div><b>' + number(topic.article_count) + '</b><span>Indexed publications</span></div><div><b>' + escapeHtml(shortDate(firstArticle.date)) + ' — ' + escapeHtml(shortDate(lastArticle.date)) + '</b><span>Published span</span></div><div><b>' + number(fullCount) + ' full · ' + number(topic.article_count - fullCount) + ' excerpt</b><span>Indexed text coverage</span></div><div><b>' + number(sourceCount) + '</b><span>Publication channel' + (sourceCount === 1 ? '' : 's') + '</span></div></div>' +
    threadComparisonMarkup(article,topic) +
    '<section class="thread-timeline" aria-labelledby="thread-timeline-title"><div class="thread-subhead"><div><span>Publication chronology</span><h3 id="thread-timeline-title">' + escapeHtml(topic.label) + ' research history</h3></div><b>' + number(timelineIds.length) + ' shown' + (hiddenCount > 0 ? ' · ' + number(hiddenCount) + ' outside window' : '') + '</b></div><ol>' + timeline + '</ol></section><p class="thread-boundary">Thread membership is an exact match from the release’s high-precision topic index. Chronology and captured fields organize published evidence; they do not infer the author’s current position, consistency, conviction, accuracy, performance, or portfolio suitability.</p></section>';
}
function analysisPanelMarkup(row,title,className) {
  if (!row || !row.span || !row.span.text) {
    return '<section class="ic-analysis-card missing ' + (className || '') + '"><h3>' + escapeHtml(title) + '<span class="ic-source-kind">Rule boundary</span></h3><p>No explicit ' + escapeHtml(title.toLowerCase()) + ' passage was identified by the high-precision section rules. This is not evidence that the argument is absent from the full source.</p></section>';
  }
  return '<section class="ic-analysis-card ' + (className || '') + '"><h3>' + escapeHtml(title) + '<span class="ic-source-kind">Authored · exact span</span></h3>' +
    (row.heading ? '<h4>' + escapeHtml(row.heading) + '</h4>' : '') + '<p>' + highlightArticleNumbers(row.span.text) + '</p>' + exactPassageTail(row.span) + '</section>';
}
function decisionSheetSectionMarkup(row,label) {
  if (!row || !row.span || !row.span.text) {
    return '<section class="ic-sheet-section"><div class="ic-sheet-label"><span>' + escapeHtml(label) + '</span><span class="ic-authored">Rule boundary</span></div><p class="missing">Not identified by the high-precision section rules. Absence cannot be inferred from this index.</p></section>';
  }
  return '<section class="ic-sheet-section"><div class="ic-sheet-label"><span>' + escapeHtml(label) + '</span><span class="ic-authored">Authored</span></div>' +
    (row.heading ? '<h3>' + escapeHtml(row.heading) + '</h3>' : '') + '<p>' + highlightArticleNumbers(row.span.text) + '</p>' + exactPassageTail(row.span) + '</section>';
}
function briefRailMarkup(lenses,article) {
  const sourceSnapshot = SNAPSHOT.sources || {};
  const substackCount = Number(sourceSnapshot.substack && sourceSnapshot.substack.included_count || ARTICLES.filter(function (article) { return article.source === 'substack'; }).length);
  const mediumCount = Number(sourceSnapshot.medium && sourceSnapshot.medium.included_count || ARTICLES.filter(function (article) { return article.source === 'medium'; }).length);
  const facts = [
    [number(Number(SNAPSHOT.article_count || ARTICLES.length)),'Published dossiers'],
    [number(Number(SNAPSHOT.observation_count || 0)),'Parser observations'],
    [number(substackCount),'Substack'],[number(mediumCount),'Unique Medium']
  ];
  const views = [
    ['01','structure','Structure Desk'],['02','briefing','Latest Brief'],
    ['03','ideas','Evidence Monitor'],['04','research','Research Library'],
    ['05','queue','Decision Queue']
  ];
  let jumpMarkup = '';
  if (article) {
    const spans = articleBriefSpans(article);
    const ledger = articleEvidenceLedger(article);
    const hasKind = function (kind) { return spans.some(function (row) { return row.kinds.includes(kind); }); };
    const hasThread = Boolean(threadArticleRow(article));
    const jumps = [
      ['01','brief-thesis','Opening thesis',true],
      ['02','brief-key-evidence','Key figures',ledger.length > 0],
      ['03','brief-analysis','Mechanism & evidence',hasKind('mechanism') || hasKind('evidence')],
      ['04','brief-dossier','Decision boundaries',hasKind('countercase') || hasKind('falsifier') || hasKind('implementation')],
      ['05','brief-evidence-ledger','Full evidence ledger',ledger.length > 0],
      ['06','brief-thread','Topic history',hasThread],
      ['07','brief-archive','Archive context',true]
    ];
    jumpMarkup = '<div class="ic-rail-rule"></div><div class="ic-rail-heading">In this brief</div><div class="ic-jump-list">' + jumps.map(function (row) {
      return row[3]
        ? '<button class="ic-jump" type="button" data-brief-jump="' + row[1] + '"><span class="ic-nav-index">' + row[0] + '</span><span>' + row[2] + '</span><i>Captured</i></button>'
        : '<div class="ic-jump unavailable"><span class="ic-nav-index">' + row[0] + '</span><span>' + row[2] + '</span><i>Not captured</i></div>';
    }).join('') + '</div>';
  }
  return '<aside class="ic-rail" aria-label="Research desk navigation"><div class="ic-rail-brand">Research desk</div><nav class="ic-nav">' +
    views.map(function (row) { return '<button class="ic-nav-button' + (row[1] === 'briefing' ? ' active' : '') + '" type="button" data-view="' + row[1] + '"' + (row[1] === 'briefing' ? ' aria-current="page"' : '') + '><span class="ic-nav-index">' + row[0] + '</span><span>' + row[2] + '</span></button>'; }).join('') +
    '</nav>' + jumpMarkup + '<div class="ic-rail-rule"></div><div class="ic-rail-heading">Archive lens</div><div class="ic-lens-list">' +
    lenses.map(function (row) { return '<button class="ic-lens' + (state.briefLens === row[0] ? ' active' : '') + '" type="button" data-brief-lens="' + row[0] + '" aria-pressed="' + String(state.briefLens === row[0]) + '">' + row[1] + '</button>'; }).join('') +
    '</div><div class="ic-rail-rule"></div><div class="ic-rail-heading">Library facts</div><div class="ic-library-facts">' +
    facts.map(function (row) { return '<div class="ic-library-fact"><span>' + row[1] + '</span><b>' + row[0] + '</b></div>'; }).join('') +
    '</div><p class="ic-standard">Exact published passages remain attached to dates, offsets, and hashes. Rules organize the source; they do not create a recommendation, confidence score, or live market view.</p></aside>';
}
function briefCompactNavMarkup(lenses) {
  const views = [
    ['structure','Structure Desk'],['briefing','Latest Brief'],
    ['ideas','Evidence Monitor'],['research','Research Library'],
    ['queue','Decision Queue']
  ];
  return '<nav class="ic-compact-nav" aria-label="Briefing navigation"><div class="ic-compact-group" role="group" aria-label="Research views"><span class="ic-compact-label" aria-hidden="true">Views</span><div class="ic-compact-scroll">' +
    views.map(function (row) {
      const current = row[0] === 'briefing';
      return '<button class="ic-compact-button' + (current ? ' active' : '') + '" type="button" data-view="' + row[0] + '"' + (current ? ' aria-current="page"' : '') + '>' + row[1] + '</button>';
    }).join('') + '</div></div><div class="ic-compact-group" role="group" aria-label="Archive lenses"><span class="ic-compact-label" aria-hidden="true">Lens</span><div class="ic-compact-scroll">' +
    lenses.map(function (row) {
      const active = state.briefLens === row[0];
      return '<button class="ic-compact-button lens' + (active ? ' active' : '') + '" type="button" data-brief-lens="' + row[0] + '" aria-pressed="' + String(active) + '">' + row[1] + '</button>';
    }).join('') + '</div></div></nav>';
}
let pendingBriefFocus = null;
let threadComparisonRequest = null;
function restorePendingBriefFocus(consumePending,preferStatusFocus) {
  if (!pendingBriefFocus) return;
  const pending = pendingBriefFocus;
  if (consumePending !== false) pendingBriefFocus = null;
  requestAnimationFrame(function () {
    let target = null;
    if (preferStatusFocus) {
      target = document.querySelector('[data-retry-briefs]') || document.getElementById('brief-status-title');
    } else if (pending.kind === 'lens') {
      const lensSelector = '[data-brief-lens="' + pending.value + '"]';
      target = window.innerWidth < 1440
        ? document.querySelector('.ic-compact-nav ' + lensSelector)
        : document.querySelector('.ic-rail ' + lensSelector);
      target = target || document.querySelector(lensSelector);
    } else if (pending.kind === 'thread') {
      target = document.querySelector('[data-thread-topic="' + pending.value + '"]') ||
        document.getElementById('brief-thread');
    } else if (pending.kind === 'thread-comparison') {
      target = document.querySelector('[data-thread-load="' + pending.value + '"]:not([disabled])') ||
        document.getElementById('thread-comparison-title') ||
        document.getElementById('thread-comparison-first-title');
    } else {
      target = document.getElementById('lead-article-title') ||
        document.getElementById('brief-status-title') ||
        document.querySelector('#inspector-content .record-title');
    }
    target = target || document.querySelector('[data-retry-briefs]');
    if (target) {
      if (!target.matches('button,a,input,select,textarea,[tabindex]')) target.tabIndex = -1;
      target.focus();
    }
  });
}
function renderIntelligenceBrief(records) {
  const shell = document.getElementById('briefing-shell');
  delete shell.dataset.statusAnnouncement;
  const lenses = BRIEF_LENSES;
  function renderBriefStatus(title,body,actionMarkup,preservePendingFocus,preferStatusFocus) {
    shell.innerHTML = '<div class="intel-wrap">' + briefRailMarkup(lenses) + briefCompactNavMarkup(lenses) +
      '<article class="intel-lead"><div class="intel-lead-inner"><div class="ic-topic">Investment committee brief · source-backed</div><h1 class="intel-title" id="brief-status-title">' + escapeHtml(title) + '</h1><p class="ic-dek">' + escapeHtml(body) + '</p>' + (actionMarkup || '') + '</div></article>' +
      '<aside class="intel-side ic-sheet"><div class="ic-sheet-inner"><div class="ic-sheet-eyebrow">Evidence boundary</div><h2 class="ic-sheet-title">Release integrity first</h2><p class="ic-sheet-intro">The terminal will not mix source passages from a different release or treat an unavailable asset as evidence absence.</p></div></aside></div>';
    shell.dataset.statusAnnouncement = title;
    restorePendingBriefFocus(!preservePendingFocus,preferStatusFocus);
  }
  if (state.briefLens !== 'all' && briefArchiveFailed && !briefArchiveReady) {
    renderBriefStatus('Older dossiers could not be verified','The release-bound article asset did not load. The terminal will not mix passages from a different release.','<div class="intel-actions"><button class="secondary-action" type="button" data-retry-briefs>Retry exact dossier load</button></div>',false,true);
    return;
  }
  if (state.briefLens !== 'all' && !briefArchiveReady && !briefArchiveFailed) {
    renderBriefStatus('Loading the complete article lens…','Retrieving deferred exact passages for older articles and checking them against this release. Preparing ' + lenses.find(function (row) { return row[0] === state.briefLens; })[1] + ' across the archive.','',true);
    loadBriefArchive().then(function () {
      if (state.view === 'briefing' && state.briefLens !== 'all') render();
    }).catch(function () {
      briefArchiveFailed = true;
      if (state.view === 'briefing') render();
    });
    return;
  }
  if (!records.length) {
    renderBriefStatus('No article matches this lens','Clear the search or return to the latest published research.','<div class="intel-actions"><button class="secondary-action" type="button" data-brief-lens="all">Show latest research</button></div>');
    return;
  }
  let selected = ARTICLE_BY_ID.get(state.selected);
  if (!selected || !records.some(function (article) { return article.id === selected.id; })) selected = records[0];
  state.selected = selected.id;
  if (!selected.brief && !selected._briefLoadFailed) {
    renderBriefStatus('Loading the exact article dossier…','The older dossier is stored as a deferred release asset. Validating it against release ' + String(SNAPSHOT.data_checksum || '').slice(0,12) + ' before display.','',true);
    ensureArticleBrief(selected).then(function (briefValue) {
      if (briefValue && state.view === 'briefing' && state.selected === selected.id) render();
      else if (!briefValue && state.view === 'briefing' && state.selected === selected.id) renderIntelligenceBrief(records);
    });
    return;
  }
  if (!selected.brief && selected._briefLoadFailed) {
    renderBriefStatus('This exact dossier could not be verified','The release-bound article asset did not load. No evidence-absence conclusion has been drawn.','<div class="intel-actions"><button class="secondary-action" type="button" data-retry-briefs>Retry exact dossier load</button></div>',false,true);
    return;
  }
  const brief = selected.brief || {lead:null,sections:[],fallback_evidence:null,checkpoints:[]};
  const sourceSpans = articleBriefSpans(selected);
  const ledger = articleEvidenceLedger(selected);
  const leadRow = sourceSpans.find(function (row) { return row.kinds.includes('lead'); });
  const evidenceRow = sourceSpans.find(function (row) { return row.kinds.includes('evidence'); });
  const mechanismRow = sourceSpans.find(function (row) { return row.kinds.includes('mechanism'); });
  const countercaseRow = sourceSpans.find(function (row) { return row.kinds.includes('countercase'); });
  const falsifierRow = sourceSpans.find(function (row) { return row.kinds.includes('falsifier'); });
  const implementationRow = sourceSpans.find(function (row) { return row.kinds.includes('implementation'); });
  const sectionMarkup = sourceSpans.filter(function (row) {
    return !row.kinds.some(function (kind) { return ['lead','evidence','mechanism'].includes(kind); });
  }).map(function (row) {
    return intelligenceSection(row,row.kinds.includes('implementation'));
  }).join('');
  const checkedDate = String(SNAPSHOT.checked_at || '').slice(0,10) || MAX_DATE;
  const checkpoints = (brief.checkpoints || []).slice().sort(function (left,right) { return left.date.localeCompare(right.date); });
  const checkpointMarkup = checkpoints.map(function (checkpoint) {
    const stateLabel = checkpoint.date < checkedDate ? 'Cited date passed · verification due' : checkpoint.date === checkedDate ? 'Cited date equals dataset check date · verify status' : 'Upcoming cited date';
    return '<div class="ic-sheet-checkpoint"><time datetime="' + checkpoint.date + '">' + escapeHtml(formatDate(checkpoint.date)) + ' · ' + escapeHtml(stateLabel) + '</time><p>' + escapeHtml(checkpoint.text) + '</p><span class="source-tail" title="' + escapeHtml(String(checkpoint.sha256 || '')) + '">' + escapeHtml(spanProvenance(checkpoint)) + '</span></div>';
  }).join('');
  const stream = records.filter(function (article) { return article.id !== selected.id; }).slice(0,6).map(intelligenceCard).join('');
  const readLabel = selected.content_status === 'excerpt' ? sourceAccessLabel(selected) : selected.read_minutes ? selected.read_minutes + ' min read' : 'Full text indexed';
  const exactSpanCount = sourceSpans.length + checkpoints.length;
  const articlePosition = Math.max(1,ARTICLES.findIndex(function (article) { return article.id === selected.id; }) + 1);
  const sourceRelease = SNAPSHOT.sources && SNAPSHOT.sources[selected.source] || {};
  const sourceHealthClass = sourceRelease.status === 'ok' ? ' ok' : sourceRelease.status === 'degraded' ? ' degraded' : ' unavailable';
  const articleIdeaIds = new Set(selected.idea_ids || []);
  const localPackets = Array.from(workflowItems.values()).filter(function (item) {
    return Boolean(item && ((item.source_snapshot && item.source_snapshot.article_id === selected.id) || articleIdeaIds.has(item.id)));
  });
  const activePackets = localPackets.filter(function (item) { return item.status !== 'archived'; }).length;
  const subtitleMarkup = selected.subtitle ? '<p class="ic-dek">' + escapeHtml(selected.subtitle) + '</p>' : '';
  const metadataOnlyMember = selected.publication_access === 'member' && !hasIndexedMemberPreview(selected);
  const openingText = leadRow && leadRow.span && leadRow.span.text || articleClaim(selected);
  const openingLabel = leadRow ? 'Author’s opening thesis' : metadataOnlyMember ? 'Published metadata' : 'Published article framing';
  const openingTail = leadRow ? exactPassageTail(leadRow.span) : metadataOnlyMember ? '<span class="source-tail">No anonymous article-body preview was available in this release</span>' : '<span class="source-tail">Captured article framing · open the original for full context</span>';
  const checkpointSection = '<section class="ic-sheet-section" id="brief-checkpoints"><div class="ic-sheet-label"><span>Public checkpoints</span><span class="ic-authored">Authored</span></div>' +
    (checkpointMarkup || '<p class="missing">No dated public checkpoint was identified by the high-precision rules.</p>') +
    '<p class="ic-boundary-note">Status is measured against the dataset check date. A passed cited date means verification is due; it does not assert that the event occurred.</p></section>';
  shell.innerHTML = '<div class="intel-wrap">' + briefRailMarkup(lenses,selected) + briefCompactNavMarkup(lenses) +
    '<article class="intel-lead" aria-labelledby="lead-article-title"><div class="intel-lead-inner">' +
      '<div class="ic-document-meta"><div class="ic-document-meta-left"><span class="source-badge source-' + selected.source + '">' + sourceLabel(selected.source) + '</span>' + accessBadgeMarkup(selected) + '<time datetime="' + selected.date + '">' + escapeHtml(formatDate(selected.date)) + '</time><span>·</span><span>' + escapeHtml(readLabel) + '</span><span>·</span><span class="coverage-badge ' + (selected.body_revision_status === 'current' ? 'coverage-full' : 'coverage-revision') + '">' + escapeHtml(bodyRevisionLabel(selected)) + '</span><span>·</span><span>Dossier ' + number(articlePosition) + ' of ' + number(ARTICLES.length) + '</span><span>·</span><span class="ic-checked-at">Dataset assembled <time datetime="' + escapeHtml(String(SNAPSHOT.checked_at || '')) + '">' + escapeHtml(formatReleaseCheckedAt(SNAPSHOT.checked_at)) + '</time></span><span>·</span><span class="ic-source-health' + sourceHealthClass + '">' + escapeHtml(sourceCollectionSummary(selected.source)) + '</span></div>' + (isPaidSubstackArticle(selected) ? '' : '<a class="ic-open-source" href="' + escapeHtml(safeUrl(selected.url)) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(sourceActionLabel(selected)) + '</a>') + '</div>' +
      '<div class="ic-topic">Investment committee brief · published information</div><h1 class="intel-title" id="lead-article-title">' + escapeHtml(selected.title) + '</h1>' + subtitleMarkup + bodyRevisionWarningMarkup(selected) +
      '<section class="ic-opening-claim" id="brief-thesis"><div class="ic-claim-label">' + openingLabel + '</div><p>' + highlightArticleNumbers(openingText) + '</p>' + openingTail + '</section>' + premiumAccessMarkup(selected,'brief') + '</div>' +
      evidenceSpotlightMarkup(selected) +
      '<section class="ic-analysis" id="brief-analysis" aria-labelledby="analysis-title"><div class="ic-section-header"><h2 id="analysis-title">How the argument works</h2><p>Exact authored passages, organized by research role. No analyst conclusion, score, or portfolio recommendation is inferred.</p></div><div class="ic-analysis-grid">' +
        analysisPanelMarkup(mechanismRow,'Mechanism','') + analysisPanelMarkup(evidenceRow,'Evidence','evidence') +
      '</div></section>' +
      '<section class="ic-dossier" id="brief-dossier"><div class="ic-dossier-head"><div class="ic-topic">Audit trail</div><h2>Source dossier and decision boundaries</h2><p>The evidence ledger retains detected values with their original context. Section coverage records what the rules captured; it is not a judgment of research quality.</p></div>' +
        researchMapMarkup(selected) + evidenceLedgerMarkup(selected) +
        '<div class="intel-section-grid">' + (sectionMarkup || '<div class="intel-empty">No additional countercase, falsifier, or implementation passage was identified. Open the original article for full context.</div>') + '</div>' +
      '</section>' + researchThreadMarkup(selected) + '<div class="intel-actions">' + (isPaidSubstackArticle(selected) ? '' : '<a class="primary-action" href="' + escapeHtml(safeUrl(selected.url)) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(sourceActionLabel(selected)) + '</a>') + '<button class="secondary-action" type="button" data-article-dossier="' + selected.id + '">Open source dossier</button><button class="secondary-action" type="button" data-copy-brief="' + selected.id + '">Copy IC brief</button><button class="secondary-action" type="button" data-print-brief>Print / PDF</button><button class="secondary-action" type="button" data-copy-article="' + selected.id + '">Copy citation</button><span class="intel-actions-note">' + number(exactSpanCount) + ' exact source spans · ' + number(ledger.length) + ' number-bearing spans · published-source research, not independently verified or a portfolio recommendation.</span></div></article>' +
    '<aside class="intel-side ic-sheet" aria-labelledby="decision-sheet-title"><div class="ic-sheet-inner"><div class="ic-sheet-eyebrow"><span class="screen-only">IC decision sheet · source + local</span><span class="print-only">IC decision sheet · published source</span></div><h2 class="ic-sheet-title" id="decision-sheet-title">What changes our mind</h2><p class="ic-sheet-intro"><span class="screen-only">The source-defined thesis, contrary case, falsifier, and public watch items remain separate from tab-session workflow.</span><span class="print-only">Source-defined thesis, contrary case, falsifier, and public watch items. Independent diligence remains required.</span></p>' +
      decisionSheetSectionMarkup(leadRow,'Author’s thesis') + decisionSheetSectionMarkup(countercaseRow,'Author’s countercase') + decisionSheetSectionMarkup(falsifierRow,'What would change the view') + decisionSheetSectionMarkup(implementationRow,'What to watch') + checkpointSection +
      '<section class="ic-sheet-section ic-sheet-local"><div class="ic-sheet-label"><span>Tab-session IC overlay</span><span class="ic-authored">Local · this tab</span></div><div class="ic-local-count">' + number(activePackets) + '</div><p class="ic-local-caption">Active source-passage packet' + (activePackets === 1 ? '' : 's') + ' for this article. Packets attach to individual observations; this brief never silently assigns an article-level recommendation.</p><div class="ic-sheet-actions"><button class="secondary-action" type="button" data-view="queue">Open decision queue</button><button class="secondary-action" type="button" data-copy-brief="' + selected.id + '">Copy brief</button></div></section>' +
      '<p class="ic-boundary-note">Evidence boundary: exact published-source passages; not independently verified, not a live market as-of, and not a portfolio recommendation. Full source context remains controlling.</p></div></aside>' +
    '<section class="ic-archive-grid" id="brief-archive">' + archiveCoverageMarkup(records) + relatedResearchMarkup(selected) + premiumArchiveMarkup(selected) + '</section>' +
    '<section class="intel-stream"><div class="intel-card-head"><h3>Recent article dossiers</h3><span>' + number(records.length) + ' in this lens</span></div><div class="intel-stream-list">' + (stream || '<div class="intel-empty">No additional articles in this lens.</div>') + '</div></section></div>';
  restorePendingBriefFocus();
}

function contextualRecords(skip) {
  if (isArticleView()) return ARTICLES.filter(function (article) { return articleMatches(article,skip); });
  return IDEAS.filter(function (idea) { return ideaMatches(idea,skip); });
}
function recordArticle(record) {
  return isArticleView() ? record : record._article;
}
function recordValues(record, facet) {
  if (isArticleView()) {
    if (facet === 'source') return [record.source];
    if (facet === 'revision') return [record.body_revision_status];
    if (facet === 'access') return [record.publication_access];
    if (facet === 'content') return [record.content_status];
    const matchingIdeas = record._ideas.filter(function (idea) { return ideaMatchesResearchFacets(idea,facet); });
    if (facet === 'direction') return matchingIdeas.map(function (idea) { return idea.direction; });
    if (facet === 'instrument') return matchingIdeas.flatMap(function (idea) { return idea.instruments; });
    if (facet === 'manager') return matchingIdeas.map(function (idea) { return idea.manager_key; }).filter(Boolean);
    if (facet === 'quality') return matchingIdeas.flatMap(function (idea) {
      return [
        hasValue(idea.quant) ? 'quant' : '', hasValue(idea.thesis) ? 'thesis' : '', hasValue(idea.outcome) ? 'outcome' : '', hasValue(idea.manager) ? 'manager' : ''
      ].filter(Boolean);
    });
  } else {
    if (facet === 'source') return [record._article.source];
    if (facet === 'revision') return [record._article.body_revision_status];
    if (facet === 'access') return [record._article.publication_access];
    if (facet === 'direction') return [record.direction];
    if (facet === 'instrument') return record.instruments;
    if (facet === 'manager') return record.manager_key ? [record.manager_key] : [];
    if (facet === 'content') return [record._article.content_status];
    if (facet === 'quality') return [
      hasValue(record.quant) ? 'quant' : '', hasValue(record.thesis) ? 'thesis' : '', hasValue(record.outcome) ? 'outcome' : '', hasValue(record.manager) ? 'manager' : ''
    ].filter(Boolean);
  }
  return [];
}
function updateFacetCounts() {
  ['source','revision','access','direction','instrument','manager','quality','content'].forEach(function (facet) {
    const records = contextualRecords(facet);
    const counts = new Map();
    records.forEach(function (record) {
      new Set(recordValues(record,facet)).forEach(function (value) {
        counts.set(value,(counts.get(value) || 0) + 1);
      });
    });
    document.querySelectorAll('[data-count-' + facet + ']').forEach(function (element) {
      const value = element.dataset['count' + facet.charAt(0).toUpperCase() + facet.slice(1)];
      element.textContent = number(counts.get(value) || 0);
    });
    const clear = document.querySelector('[data-count-clear="' + facet + '"]');
    if (clear) clear.textContent = number(records.length);
  });
}

function filterLabel(facet,value) {
  if (facet === 'source') return sourceLabel(value);
  if (facet === 'revision') return {
    current:'Current source capture',
    prior:'Prior revision capture',
    unverified:'Revision unverified'
  }[value];
  if (facet === 'direction') return directionLabel(value);
  if (facet === 'instrument') return instrumentLabel(value);
  if (facet === 'quality') return {quant:'Numeric context',thesis:'Has thesis',outcome:'Reported outcome',manager:'Mentioned entity'}[value];
  if (facet === 'access') return {public:'Public at source',member:'Member at source',unknown:'Access unverified'}[value];
  if (facet === 'content') return value === 'full' ? 'Full text' : 'Excerpt / metadata';
  if (facet === 'manager') return MANAGER_LABELS.get(value) || value;
  if (facet === 'range') return value.toUpperCase();
  if (facet === 'coverage') return value === 'ideas' ? 'With observations' : value === 'research' ? 'Research-only' : 'All research';
  if (facet === 'documentation') return {
    triage:'High-context triage',documented:'All 5 fields captured',strong:'At least 4 fields',
    'needs-context':'Needs context (1–2)',review:'Extraction review flag'
  }[value];
  if (facet === 'queue-status') return 'Queue: ' + value;
  return value;
}
function renderActiveFilters() {
  const container = document.getElementById('active-filters');
  const chips = [];
  [
    ['source',state.sources],['revision',state.revisions],['access',state.publicationAccess],['direction',state.directions],['instrument',state.instruments],
    ['manager',state.managers],['quality',state.quality],['content',state.content]
  ].forEach(function (entry) {
    entry[1].forEach(function (value) {
      chips.push('<button class="filter-chip" type="button" data-remove-filter="' + entry[0] + '" data-value="' + escapeHtml(value) + '">' +
        escapeHtml(filterLabel(entry[0],value)) + '<span class="chip-x">×</span></button>');
    });
  });
  if (state.range !== 'all') chips.push('<button class="filter-chip" type="button" data-remove-filter="range" data-value="' + state.range + '">' + filterLabel('range',state.range) + '<span class="chip-x">×</span></button>');
  if (state.view === 'research' && state.coverage !== 'all') chips.push('<button class="filter-chip" type="button" data-remove-filter="coverage" data-value="' + state.coverage + '">' + filterLabel('coverage',state.coverage) + '<span class="chip-x">×</span></button>');
  if (state.documentation !== 'all') chips.push('<button class="filter-chip" type="button" data-remove-filter="documentation" data-value="' + state.documentation + '">' + filterLabel('documentation',state.documentation) + '<span class="chip-x">×</span></button>');
  if (state.newOnly) chips.push('<button class="filter-chip" type="button" data-remove-filter="new" data-value="1">New since last review<span class="chip-x">×</span></button>');
  state.queueStatuses.forEach(function (value) {
    chips.push('<button class="filter-chip" type="button" data-remove-filter="queue-status" data-value="' + value + '">' + filterLabel('queue-status',value) + '<span class="chip-x">×</span></button>');
  });
  container.classList.toggle('empty',chips.length === 0);
  container.innerHTML = chips.length ? '<span class="active-label">Active</span>' + chips.join('') : '';
  container.querySelectorAll('[data-remove-filter]').forEach(function (button) {
    button.setAttribute('aria-label','Remove filter: ' + button.textContent.replace('×','').trim());
    const mark = button.querySelector('.chip-x');
    if (mark) mark.setAttribute('aria-hidden','true');
  });
}
function setPressedStates() {
  document.body.dataset.view = state.view;
  document.body.classList.toggle('density-compact',state.density === 'compact');
  document.body.classList.toggle('density-comfortable',state.density === 'comfortable');
  document.body.classList.toggle('inspector-hidden',!state.inspector);
  document.querySelectorAll('button[data-view]').forEach(function (button) {
    const active = button.dataset.view === state.view;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  document.querySelectorAll('[data-brief-lens]').forEach(function (button) {
    const active = button.dataset.briefLens === state.briefLens;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  const facetSets = {
    source:state.sources,revision:state.revisions,direction:state.directions,instrument:state.instruments,
    manager:state.managers,quality:state.quality,access:state.publicationAccess,content:state.content,'queue-status':state.queueStatuses
  };
  document.querySelectorAll('[data-filter]').forEach(function (button) {
    const facet = button.dataset.filter;
    let active = false;
    if (facetSets[facet]) active = facetSets[facet].has(button.dataset.value);
    if (facet === 'range') active = state.range === button.dataset.value;
    if (facet === 'coverage') active = state.coverage === button.dataset.value;
    if (facet === 'documentation') active = state.documentation === button.dataset.value;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  document.querySelectorAll('[data-clear-facet]').forEach(function (button) {
    const facet = button.dataset.clearFacet;
    const active = facetSets[facet] ? facetSets[facet].size === 0 : false;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  const clearDocumentation = document.querySelector('[data-clear-documentation]');
  clearDocumentation.classList.toggle('active',state.documentation === 'all');
  clearDocumentation.setAttribute('aria-pressed',String(state.documentation === 'all'));
  const clearQueue = document.querySelector('[data-clear-queue-status]');
  clearQueue.classList.toggle('active',state.queueStatuses.size === 0);
  clearQueue.setAttribute('aria-pressed',String(state.queueStatuses.size === 0));
  const inspectorControl = document.querySelector('[data-action="inspector"]');
  const inspectorActive = window.innerWidth <= 1240
    ? document.body.classList.contains('inspector-open')
    : state.inspector;
  inspectorControl.classList.toggle('active',inspectorActive);
  inspectorControl.setAttribute('aria-pressed',String(inspectorActive));
  inspectorControl.setAttribute('aria-expanded',String(inspectorActive));
  document.getElementById('density-label').textContent = state.density === 'compact' ? 'Compact' : 'Comfortable';
  document.getElementById('saved-count').textContent = savedIdeas.size ? '(' + savedIdeas.size + ')' : '';
  syncOverlayAccessibility();
}

function inspectorBadge(value,label) {
  return '<span class="direction-badge ' + directionClass(value) + '">' + escapeHtml(label || directionLabel(value)) + '</span>';
}
function detailSection(title,value,className) {
  return '<section class="inspector-section"><h3>' + title + '</h3>' +
    (value ? '<p class="' + (className || '') + '">' + escapeHtml(value) + '</p>' : '<p class="missing">—</p>') +
    '</section>';
}
function renderIdeaInspector(idea) {
  const article = idea._article;
  const alternate = article.alternate_urls && article.alternate_urls.medium;
  const workflow = workflowItems.get(idea.id);
  const badges = [inspectorBadge(idea.direction)];
  idea.instruments.forEach(function (instrument) {
    badges.push('<span class="coverage-badge">' + escapeHtml(instrumentLabel(instrument)) + '</span>');
  });
  if (idea.manager) badges.push('<span class="coverage-badge">' + escapeHtml(idea.manager) + '</span>');
  badges.push('<span class="documentation-badge ' + (idea.documentation_score === 5 ? 'complete' : '') + '">' + idea.documentation_score + '/5 fields</span>');
  badges.push('<span class="coverage-badge' + (isPaidSubstackArticle(article) ? ' premium-badge' : '') + '">' + escapeHtml(sourceAccessLabel(article)) + '</span>');
  badges.push('<span class="coverage-badge ' + (article.body_revision_status === 'current' ? '' : 'coverage-revision') + '">' + escapeHtml(bodyRevisionLabel(article)) + '</span>');
  const reviewReasons = [];
  if (idea.negation_risk) reviewReasons.push('directional language appears near a negation');
  if (idea.reference_line) reviewReasons.push('passage resembles a link or reference line');
  if (idea.description_truncated) reviewReasons.push('captured passage may be truncated');
  const capturedLabels = {market:'Market',stance:'Parsed stance',underlying:'Underlying',thesis:'Edge / thesis',numeric:'Numeric context'};
  const capturedDiligence = Object.keys(capturedLabels).map(function (key) {
    const captured = idea.documentation_fields[key];
    return '<div class="diligence-item ' + (captured ? 'captured' : '') + '"><span class="diligence-mark">' + (captured ? '✓' : '—') + '</span><span>' + capturedLabels[key] + (captured ? ' captured' : ' not captured') + '</span></div>';
  }).join('');
  const unassessed = ['Live price / valuation','Catalyst / horizon','Sizing','Liquidity / capacity','Downside / exit','Portfolio fit'];
  const unassessedDiligence = unassessed.map(function (label) {
    return '<div class="diligence-item"><span class="diligence-mark">!</span><span>' + label + ' not assessed by source extraction</span></div>';
  }).join('');
  const packet = packetCoverage(workflow);
  const gateControls = workflow ? DILIGENCE_GATES.map(function (row) {
    return '<label class="workflow-gate"><input type="checkbox" data-workflow-id="' + idea.id + '" data-workflow-gate="' + row[0] + '"' + (workflow.checks[row[0]] ? ' checked' : '') + '><span>' + escapeHtml(row[1]) + '</span></label>';
  }).join('') : '';
  const workflowPanel = workflow ?
    '<section class="workflow-panel"><div class="workflow-header"><h3>Human-entered IC decision packet</h3><span class="workflow-coverage" aria-label="Decision packet coverage ' + packet.completed + ' of ' + packet.total + '; not approval">' + packet.completed + '/' + packet.total + '</span></div>' +
      '<p class="workflow-warning">Packet coverage counts populated analyst fields and self-attested control gates. It is not a confidence score, approval, recommendation, or evidence that a control was performed.</p>' +
      '<div class="workflow-subhead">Decision control</div><div class="workflow-grid">' +
        '<label class="workflow-field">Status<select data-workflow-id="' + idea.id + '" data-workflow-select="status"><option value="review"' + (workflow.status === 'review' ? ' selected' : '') + '>Review</option><option value="diligence"' + (workflow.status === 'diligence' ? ' selected' : '') + '>Diligence</option><option value="monitor"' + (workflow.status === 'monitor' ? ' selected' : '') + '>Monitor</option><option value="archived"' + (workflow.status === 'archived' ? ' selected' : '') + '>Archived</option></select></label>' +
        '<label class="workflow-field">Priority<select data-workflow-id="' + idea.id + '" data-workflow-select="priority"><option value="low"' + (workflow.priority === 'low' ? ' selected' : '') + '>Low</option><option value="normal"' + (workflow.priority === 'normal' ? ' selected' : '') + '>Normal</option><option value="high"' + (workflow.priority === 'high' ? ' selected' : '') + '>High</option></select></label>' +
        '<label class="workflow-field">Decision owner<input data-workflow-id="' + idea.id + '" data-workflow-field="owner" value="' + escapeHtml(workflow.owner) + '" maxlength="120" autocomplete="off" placeholder="Initials or role"></label>' +
        '<label class="workflow-field">Next review<input type="date" data-workflow-id="' + idea.id + '" data-workflow-field="review_date" value="' + escapeHtml(workflow.review_date) + '"></label>' +
        '<label class="workflow-field">Analyst confidence<select data-workflow-id="' + idea.id + '" data-workflow-select="confidence"><option value="unrated"' + (workflow.confidence === 'unrated' ? ' selected' : '') + '>Unrated</option><option value="low"' + (workflow.confidence === 'low' ? ' selected' : '') + '>Low</option><option value="medium"' + (workflow.confidence === 'medium' ? ' selected' : '') + '>Medium</option><option value="high"' + (workflow.confidence === 'high' ? ' selected' : '') + '>High</option></select></label>' +
        '<label class="workflow-field">Tags<input data-workflow-id="' + idea.id + '" data-workflow-field="tags" value="' + escapeHtml(workflow.tags) + '" maxlength="500" autocomplete="off" placeholder="macro, RV, event"></label>' +
        '<label class="workflow-field wide">Next action<input data-workflow-id="' + idea.id + '" data-workflow-field="next_action" value="' + escapeHtml(workflow.next_action) + '" maxlength="700" autocomplete="off" placeholder="Evidence, call, model, or review required next"></label>' +
      '</div>' +
      '<div class="workflow-subhead">Investment case</div>' +
      '<label class="workflow-field">Variant thesis / edge<textarea class="compact" data-workflow-id="' + idea.id + '" data-workflow-field="thesis" maxlength="1800" placeholder="What is mispriced, and why can this process identify it?">' + escapeHtml(workflow.thesis) + '</textarea></label>' +
      '<label class="workflow-field">Contrary evidence<textarea class="compact" data-workflow-id="' + idea.id + '" data-workflow-field="contrary" maxlength="1600" placeholder="Strongest disconfirming evidence or alternative explanation">' + escapeHtml(workflow.contrary) + '</textarea></label>' +
      '<div class="workflow-grid"><label class="workflow-field">Catalyst / expected path<textarea class="compact" data-workflow-id="' + idea.id + '" data-workflow-field="catalyst" maxlength="1400" placeholder="What closes the gap?">' + escapeHtml(workflow.catalyst) + '</textarea></label>' +
      '<label class="workflow-field">Horizon<input data-workflow-id="' + idea.id + '" data-workflow-field="horizon" value="' + escapeHtml(workflow.horizon) + '" maxlength="160" autocomplete="off" placeholder="Days, months, event window"></label></div>' +
      '<label class="workflow-field">Valuation / entry / payoff<textarea class="compact" data-workflow-id="' + idea.id + '" data-workflow-field="payoff" maxlength="1800" placeholder="Market as-of, entry reference, base/bull/bear payoff">' + escapeHtml(workflow.payoff) + '</textarea></label>' +
      '<label class="workflow-field">Falsifier / downside / exit<textarea class="compact" data-workflow-id="' + idea.id + '" data-workflow-field="risk" maxlength="1800" placeholder="What proves the thesis wrong, and what ends the review?">' + escapeHtml(workflow.risk) + '</textarea></label>' +
      '<label class="workflow-field">Implementation / liquidity / funding<textarea class="compact" data-workflow-id="' + idea.id + '" data-workflow-field="implementation" maxlength="1800" placeholder="Instrument, borrow, spread, capacity, financing and exit-cost dependencies">' + escapeHtml(workflow.implementation) + '</textarea></label>' +
      '<label class="workflow-field">Mandate / portfolio fit<textarea class="compact" data-workflow-id="' + idea.id + '" data-workflow-field="portfolio" maxlength="1800" placeholder="Exposure, correlation, concentration, crowding and stress considerations">' + escapeHtml(workflow.portfolio) + '</textarea></label>' +
      '<div class="workflow-subhead">Self-attested control gates</div><fieldset class="workflow-gates"><legend>Check only after completing the work in controlled fund systems.</legend>' + gateControls + '</fieldset>' +
      '<label class="workflow-field">Research memo<textarea data-workflow-id="' + idea.id + '" data-workflow-field="note" maxlength="4000" placeholder="Public-source diligence and decision rationale only…">' + escapeHtml(workflow.note) + '</textarea></label>' +
      '<p class="workflow-warning">Case ' + packet.caseCount + '/8 · controls ' + packet.controlCount + '/6 · workflow ' + packet.workflowCount + '/4. Updated ' + escapeHtml(workflow.updated_at ? formatCheckedAt(workflow.updated_at) : 'not recorded') + (workflow.verified_at ? '; source marked reviewed ' + escapeHtml(formatCheckedAt(workflow.verified_at)) : '') + '.</p>' +
      '<div class="workflow-actions"><button class="secondary-action" type="button" data-copy-packet="' + idea.id + '">Copy decision packet</button><button class="secondary-action" type="button" data-save-idea="' + idea.id + '">' + (workflow.status === 'archived' ? 'Return to review' : 'Archive packet') + '</button></div>' +
      '<p class="workflow-warning">Stored only in this tab session unless exported; closing the tab session discards it. Backups are plaintext. Not an enterprise audit record. Do not enter confidential, personal, client, position, or regulated information.</p></section>' : '';
  return '<div class="inspector-content">' +
    '<div class="record-eyebrow"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><time datetime="' + article.date + '">' + formatDate(article.date) + '</time><span class="record-id">' + idea.id.toUpperCase() + '</span></div>' +
    '<h2 class="record-title">' + escapeHtml(article.title) + '</h2>' +
    (article.subtitle ? '<p class="record-subtitle">' + escapeHtml(article.subtitle) + '</p>' : '') +
    '<div class="record-actions">' +
      (isPaidSubstackArticle(article) ? '' : '<a class="primary-action" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(sourceActionLabel(article)) + '</a>') +
      (alternate ? '<a class="secondary-action" href="' + escapeHtml(safeUrl(alternate)) + '" target="_blank" rel="noopener noreferrer">Medium copy ↗</a>' : '') +
      (workflow ? '' : '<button class="secondary-action" type="button" data-save-idea="' + idea.id + '">☆ Add to review</button>') +
      '<button class="secondary-action" type="button" data-copy-citation="' + idea.id + '">Copy citation</button>' +
    '</div>' +
    '<div class="record-facts">' + badges.join('') + '</div>' +
    bodyRevisionWarningMarkup(article) +
    (reviewReasons.length ? '<div class="review-notice"><strong>Extraction review recommended:</strong> ' + escapeHtml(reviewReasons.join('; ')) + '. Verify the surrounding source context before interpreting stance.</div>' : '') +
    detailSection('Source passage',passageText(idea),'primary-text') +
    premiumAccessMarkup(article,'idea') +
    detailSection('Underlying',idea.underlying) +
    (idea.manager_raw && idea.manager_raw !== idea.manager ? detailSection('Original entity mention',idea.manager_raw) : '') +
    detailSection('Edge / thesis',idea.thesis) +
    '<section class="inspector-section"><h3>Numeric context</h3>' +
      (idea.quant ? '<div class="quant-block">' + escapeHtml(idea.quant) + '</div>' : '<p class="missing">—</p>') +
    '</section>' +
    '<section class="inspector-section"><h3>Reported outcome</h3>' +
      (idea.outcome ? '<p class="reported-outcome">' + escapeHtml(idea.outcome) + '</p>' : '<p class="missing">—</p>') +
    '</section>' +
    '<section class="inspector-section"><h3>Source-extracted coverage</h3><div class="diligence-grid">' + capturedDiligence + unassessedDiligence + '</div></section>' +
    workflowPanel +
    '<div class="provenance">Published ' + escapeHtml(formatDate(article.date)) + '; dataset assembled ' + escapeHtml(formatReleaseCheckedAt(SNAPSHOT.checked_at)) + '; ' + escapeHtml(sourceCollectionSummary(article.source)) + '; ' + escapeHtml(bodyRevisionSummary(article)) + ' This is not a live market as-of timestamp. Rules-based passage extracted from published research by one author. “No reliable stance” means the source did not express a direction the parser could safely classify. Mentions are not verified positions; reported outcomes are not independently verified. Review the original publication before any investment or execution decision.</div>' +
    '</div>';
}
function renderArticleInspector(article) {
  const alternate = article.alternate_urls && article.alternate_urls.medium;
  const brief = article.brief || {lead:null,sections:[],fallback_evidence:null,checkpoints:[]};
  const related = article._ideas.slice(0,8).map(function (idea) {
    return '<button class="related-idea" type="button" data-related-idea="' + idea.id + '">' +
      '<span class="direction-badge ' + directionClass(idea.direction) + '" title="' + escapeHtml(directionLabel(idea.direction) + '; not a verified position') + '">' + compactDirectionLabel(idea.direction) + '</span> ' +
      escapeHtml(passageText(idea)) + '</button>';
  }).join('');
  const dossierSections = articleBriefSpans(article).map(function (row) {
    return '<section class="article-dossier-section"><h3>' + escapeHtml(row.label) + '</h3><h4>' + escapeHtml(row.heading) + '</h4><p>' + highlightArticleNumbers(row.span.text) + '</p>' + exactPassageTail(row.span) + '</section>';
  }).join('');
  const checkpoints = (brief.checkpoints || []).map(function (checkpoint) {
    return '<div class="checkpoint-mini"><time datetime="' + checkpoint.date + '">' + escapeHtml(formatDate(checkpoint.date)) + '</time><span>' + escapeHtml(checkpoint.text) + '</span></div>';
  }).join('');
  const structures = new Set(article.directions.filter(function (value) { return value !== 'unspecified'; }));
  const gaps = [];
  const metadataOnlyMember = article.publication_access === 'member' && !hasIndexedMemberPreview(article);
  if (metadataOnlyMember) {
    gaps.push('No anonymous article-body preview was available to this release; body passages, evidence roles, and uncaptured sections are not assessable here.');
  } else if (article.content_status === 'excerpt') {
    gaps.push('Only an excerpt was available to the index; uncaptured sections are not assessable.');
    if (!articleEvidence(article)) gaps.push('Contextual-evidence coverage beyond the captured excerpt is not assessable; absence cannot be inferred.');
    if (!articleHasBriefKind(article,'countercase') && !articleHasBriefKind(article,'falsifier')) gaps.push('Countercase and falsifier coverage beyond the captured excerpt is not assessable; absence cannot be inferred.');
  } else {
    if (!articleEvidence(article)) gaps.push('No contextual evidence passage was identified by the high-precision brief rules; review the original before treating it as absent.');
    if (!articleHasBriefKind(article,'countercase') && !articleHasBriefKind(article,'falsifier')) gaps.push('No explicit countercase or falsifier section was identified by the high-precision rules; this is not proof of absence.');
  }
  if (structures.size > 1) gaps.push('Extracted passages describe mixed structures; no single article-level stance is assigned.');
  if (!brief.lead && !metadataOnlyMember) gaps.push(article.content_status === 'excerpt' ? 'An authored lead passage is not assessable from the captured excerpt.' : 'No authored lead passage was identified in the compact index; review the original for full context.');
  const openingInspectorLabel = metadataOnlyMember ? 'Published metadata' : 'Opening authored passage';
  const observationBoundary = metadataOnlyMember
    ? '<section class="article-dossier-section"><h3>Parser-derived observations</h3><p class="missing">None captured because this release contains no anonymous article-body preview. Metadata is not treated as an investment observation.</p></section>'
    : '<section class="article-dossier-section"><h3>Parser-derived observations</h3><p class="missing">None captured. The article dossier remains available because it is built from exact authored sections, not observation count.</p></section>';
  const provenanceBoundary = metadataOnlyMember
    ? 'This record contains published metadata only; it has no article-body span, inferred stance, holding, return, or recommendation.'
    : 'Every dossier passage is stored with source offsets and a SHA-256 hash and was validated against the captured article body before publication.';
  return '<div class="inspector-content">' +
    '<div class="record-eyebrow"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span>' + accessBadgeMarkup(article) + '<time datetime="' + article.date + '">' + formatDate(article.date) + '</time><span class="record-id">' + article.id.toUpperCase() + '</span></div>' +
    '<h2 class="record-title">' + escapeHtml(article.title) + '</h2>' +
    '<div class="intel-label" style="margin-top:10px">' + escapeHtml(openingInspectorLabel) + '</div><p class="record-subtitle primary-text">' + escapeHtml(articleClaim(article)) + '</p>' +
    '<div class="record-actions">' +
      (state.view === 'briefing' || isPaidSubstackArticle(article) ? '' : '<a class="primary-action" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(sourceActionLabel(article)) + '</a>') +
      (alternate ? '<a class="secondary-action" href="' + escapeHtml(safeUrl(alternate)) + '" target="_blank" rel="noopener noreferrer">Medium copy ↗</a>' : '') +
      '<button class="secondary-action" type="button" data-copy-brief="' + article.id + '">Copy institutional brief</button>' +
      '<button class="secondary-action" type="button" data-copy-article="' + article.id + '">Copy citation</button>' +
    '</div>' +
    '<div class="intel-reasons">' + reasonChips(article,8) + '</div>' +
    bodyRevisionWarningMarkup(article) +
    (state.view === 'briefing' ? '' : premiumAccessMarkup(article,'article')) +
    dossierSections +
    (checkpoints ? '<section class="article-dossier-section"><h3>Public checkpoints cited</h3>' + checkpoints + '</section>' : '') +
    (gaps.length ? '<section class="article-dossier-section"><h3>Evidence boundaries</h3><div class="review-notice">' + gaps.map(function (gap) { return '<div>• ' + escapeHtml(gap) + '</div>'; }).join('') + '</div></section>' : '') +
    articleExtractionMap(article) +
    (related ? '<details class="article-dossier-section"><summary>Parser-derived observations (' + number(article.trade_count) + ')</summary><div class="related-ideas" style="margin-top:9px">' + related + '</div></details>' : (observationsReady ? observationBoundary : '')) +
    '<div class="provenance">Published ' + escapeHtml(formatDate(article.date)) + '; dataset assembled ' + escapeHtml(formatReleaseCheckedAt(SNAPSHOT.checked_at)) + '; ' + escapeHtml(sourceCollectionSummary(article.source)) + '; ' + escapeHtml(bodyRevisionSummary(article)) + ' ' + escapeHtml(provenanceBoundary) + ' Structured direction fields classify passage language only; they do not identify the actor, a verified position, or a current view. The brief does not infer holdings, conviction, expected return, portfolio fit, or a live market view.</div>' +
    '</div>';
}
let renderedInspectorKey = '';
function renderInspector() {
  const container = document.getElementById('inspector-content');
  const inspectorKey = state.view + ':' + state.selected;
  const shouldResetScroll = inspectorKey !== renderedInspectorKey;
  if (!state.selected) {
    container.innerHTML = '<div class="inspector-empty"><div class="inspector-empty-mark">N/R</div><h2>Select a record</h2><p>Inspect the complete idea, evidence, provenance, and source without losing your position in the monitor.</p></div>';
  } else if (isArticleView()) {
    const article = ARTICLE_BY_ID.get(state.selected);
    if (article && !article.brief && !article._briefLoadFailed) {
      container.innerHTML = '<div class="inspector-empty"><div class="inspector-empty-mark">…</div><h2>Loading article dossier</h2><p>Checking the deferred dossier against this release.</p></div>';
      ensureArticleBrief(article).then(function () {
        if (state.selected === article.id && isArticleView()) renderInspector();
      });
    } else if (article && article._briefLoadFailed && !article.brief) {
      container.innerHTML = '<div class="inspector-empty"><div class="inspector-empty-mark">!</div><h2>Exact dossier unavailable</h2><p>The same-release article asset could not be verified. This is a load failure, not evidence absence.</p><button class="secondary-action" type="button" data-retry-briefs>Retry exact dossier load</button></div>';
      if (state.view !== 'briefing') restorePendingBriefFocus();
    } else {
      container.innerHTML = article ? renderArticleInspector(article) : '';
      if (state.view !== 'briefing') restorePendingBriefFocus();
    }
  } else {
    const idea = IDEA_BY_ID.get(state.selected);
    container.innerHTML = idea ? renderIdeaInspector(idea) : '';
  }
  if (shouldResetScroll) document.getElementById('inspector').scrollTop = 0;
  renderedInspectorKey = inspectorKey;
}

function currentStateNeedsObservations() {
  if (!isArticleView()) return true;
  return Boolean(
    state.directions.size || state.instruments.size || state.managers.size ||
    state.quality.size || state.documentation !== 'all'
  );
}
function queueObservationResultFocus(kind) {
  pendingObservationFocus = {view:state.view,query:state.query,kind:kind || 'entry'};
}
function focusViewEntry() {
  const target = state.view === 'briefing'
    ? document.getElementById('lead-article-title') || document.getElementById('brief-status-title') || document.getElementById('observation-gate-title')
    : state.view === 'structure'
      ? document.getElementById('structure-focus-input')
      : document.querySelector('[data-record-id][tabindex="0"]') || document.getElementById('empty-title');
  if (!target) return;
  if (!target.matches('button,a,input,select,textarea,[tabindex]')) target.tabIndex = -1;
  target.focus();
}
function focusObservationGate(consumePending) {
  const retry = observationsFailed ? document.querySelector('[data-retry-observations]') : null;
  const target = retry || (
    state.view === 'briefing' ? document.getElementById('observation-gate-title')
      : state.view === 'structure' ? document.getElementById('structure-focus-input')
        : document.getElementById('empty-title')
  );
  if (target) {
    if (!target.matches('button,a,input,select,textarea,[tabindex]')) target.tabIndex = -1;
    target.focus();
  }
  if (consumePending) pendingObservationFocus = null;
}
function restoreObservationResultFocus() {
  const pending = pendingObservationFocus;
  pendingObservationFocus = null;
  if (!pending || pending.view !== state.view || pending.query !== state.query) return;
  if (pending.kind === 'inspector') openInspector(true);
  else focusViewEntry();
}
function renderObservationAwareNavigation(focusKind) {
  const waiting = !observationsReady && currentStateNeedsObservations();
  if (waiting) queueObservationResultFocus(focusKind);
  render();
  if (waiting) focusObservationGate();
  else if (focusKind === 'inspector') openInspector(true);
  else focusViewEntry();
}
function requestObservationsForCurrentState(forceRetry) {
  if (observationsReady || !currentStateNeedsObservations()) return Promise.resolve(IDEAS);
  if (observationGatePromise) return observationGatePromise;
  if (observationsFailed && !forceRetry) return Promise.resolve(null);
  const request = forceRetry ? retryObservations() : loadObservations();
  const gatePromise = request.then(function (rows) {
    if (observationGatePromise === gatePromise) observationGatePromise = null;
    if (currentStateNeedsObservations()) {
      render();
      restoreObservationResultFocus();
    } else {
      pendingObservationFocus = null;
    }
    return rows;
  }).catch(function () {
    if (observationGatePromise === gatePromise) observationGatePromise = null;
    if (currentStateNeedsObservations()) {
      render();
      focusObservationGate(true);
      showToast('Deferred evidence archive could not be verified');
    } else {
      pendingObservationFocus = null;
    }
    return null;
  });
  observationGatePromise = gatePromise;
  return gatePromise;
}
function renderObservationGate() {
  setSortOptions();
  setPressedStates();
  const title = observationsFailed ? 'Evidence archive unavailable' : 'Loading release-bound evidence…';
  const copy = observationsFailed
    ? 'The release-bound observation asset could not be verified against this release. Article dossiers remain available, and no evidence-absence conclusion has been drawn.'
    : 'Validating observation identities, article ownership, and source fields before showing observation-dependent results.';
  const action = observationsFailed ? '<button class="secondary-action" type="button" data-retry-observations>Retry evidence archive</button>' : '';
  if (state.view === 'briefing') {
    document.getElementById('briefing-shell').innerHTML = '<div class="intel-wrap">' + briefRailMarkup(BRIEF_LENSES) + briefCompactNavMarkup(BRIEF_LENSES) +
      '<article class="intel-lead"><div class="intel-lead-inner"><div class="ic-topic">Release integrity check</div><h1 class="intel-title" id="observation-gate-title">' + escapeHtml(title) + '</h1><p class="ic-dek">' + escapeHtml(copy) + '</p>' + (action ? '<div class="intel-actions">' + action + '</div>' : '') + '</div></article>' +
      '<aside class="intel-side ic-sheet"><div class="ic-sheet-inner"><div class="ic-sheet-eyebrow">Evidence boundary</div><h2 class="ic-sheet-title">Fail closed</h2><p class="ic-sheet-intro">Article dossiers remain separate from the deferred parser archive. An unavailable asset is never presented as missing evidence.</p></div></aside></div>';
  } else if (state.view === 'structure') {
    // The desk is the landing view, so it draws its full chrome from
    // build-time counts and reports the archive state inside the panels
    // instead of replacing the page with a notice.
    renderStructureDesk([], {title:title, copy:copy, action:action});
  } else {
    renderTableHead();
    document.getElementById('table-body').replaceChildren();
    document.getElementById('data-table').setAttribute('aria-rowcount','1');
    document.getElementById('empty-title').textContent = title;
    document.getElementById('empty-copy').textContent = copy;
    document.querySelector('#empty-state .empty-actions').hidden = true;
    if (action) document.getElementById('empty-copy').insertAdjacentHTML('afterend','<div class="empty-actions observation-retry">' + action + '</div>');
    document.getElementById('empty-state').classList.add('visible');
    document.getElementById('load-more-wrap').classList.remove('visible');
  }
  renderActiveFilters();
  document.getElementById('orphaned-queue').classList.remove('visible');
  document.getElementById('inspector-content').innerHTML = '<div class="inspector-empty"><div class="inspector-empty-mark">' + (observationsFailed ? '!' : '…') + '</div><h2>' + escapeHtml(title) + '</h2><p>' + escapeHtml(copy) + '</p>' + action + '</div>';
  document.getElementById('result-summary').textContent = observationsFailed ? 'Evidence archive unavailable' : 'Validating evidence archive';
  document.getElementById('announcer').textContent = title;
  updateHash();
}
function syncExportAvailability() {
  const exportButton = document.querySelector('[data-action="export"]');
  const unavailable = !isArticleView() && !observationsReady;
  exportButton.disabled = unavailable;
  exportButton.title = unavailable
    ? 'Available after the release-bound evidence archive is verified'
    : '';
}
function underlyingParts(idea) {
  return String((idea && idea.underlying) || '').split(';').map(function (part) {
    return part.trim();
  }).filter(function (part) { return part && part !== '—' && part !== '-'; });
}
function outcomeTotal() {
  if (!IDEAS.length) return Number(DESK_FACETS.outcome_count || 0);
  return IDEAS.filter(function (idea) { return Boolean(idea.outcome); }).length;
}
function deskUniverseTotal() {
  return IDEAS.length || Number(DESK_FACETS.observation_count || 0);
}
// Most passages never state how a position resolved, but the record often
// returns to the same subject later. Those later notes are the only
// source-grounded answer to "what happened next" this archive can give, so
// resolve them from the published thread chronology rather than inferring one.
const followUpCache = new Map();
// A subject broad enough to cover much of the archive links everything to
// everything: threading a VIX passage to an unrelated note because both touch
// "Options" would assert a continuity the record does not contain. Only
// subjects narrow enough to mean something are followed, narrowest first.
const FOLLOW_UP_MAX_TOPIC_SHARE = 0.07;
const FOLLOW_UP_MAX_TOPIC_ARTICLES = Math.max(
  2,Math.round(Number(THREADS.article_count || 0) * FOLLOW_UP_MAX_TOPIC_SHARE)
);
function observationFollowUps(idea) {
  const article = idea && idea._article;
  if (!article) return [];
  if (followUpCache.has(article.id)) return followUpCache.get(article.id);
  const row = THREAD_ARTICLES[article.id];
  const collected = new Map();
  if (row) {
    row.topics.forEach(function (topicKey) {
      const topic = THREADS.topics[topicKey];
      if (!topic || topic.article_count > FOLLOW_UP_MAX_TOPIC_ARTICLES) return;
      (topic.article_ids || []).forEach(function (otherId) {
        if (otherId === article.id) return;
        const other = ARTICLE_BY_ID.get(otherId);
        if (!other || !(other.date > article.date)) return;
        const existing = collected.get(otherId);
        if (existing) {
          if (existing.topics.indexOf(topic.label) === -1) existing.topics.push(topic.label);
          existing.breadth = Math.min(existing.breadth,topic.article_count);
          return;
        }
        collected.set(otherId,{
          article:other,topics:[topic.label],breadth:topic.article_count
        });
      });
    });
  }
  const ordered = Array.from(collected.values()).sort(function (first,second) {
    if (first.breadth !== second.breadth) return first.breadth - second.breadth;
    if (first.article.date !== second.article.date) {
      return second.article.date.localeCompare(first.article.date);
    }
    return first.article.id.localeCompare(second.article.id);
  });
  followUpCache.set(article.id,ordered);
  return ordered;
}
// Rate conditions come from the tracked official daily curve. A publication
// calendar date on a weekend or holiday resolves to the last available dated
// observation at or before it. Publication timestamps and daily observation
// times are not aligned, so a same-day observation may post-date the article.
const RATE_BANDS = Object.freeze({
  slope:[['low','Flattest third'],['mid','Middle third'],['high','Steepest third']],
  level:[['low','Lowest 10Y third'],['mid','Middle 10Y third'],['high','Highest 10Y third']]
});
function rateReading(idea) {
  const article = idea && idea._article;
  const row = article && RATE_CONTEXT.days ? RATE_CONTEXT.days[article.date] : null;
  if (!row) return null;
  return {
    asOf:row[0], y2:row[1], y10:row[2], y30:row[3],
    slope:Math.round((row[2] - row[1]) * 100) / 100,
    slopeBand:row[4], levelBand:row[5]
  };
}
function rateBandLabel(kind,value) {
  const row = (RATE_BANDS[kind] || []).filter(function (entry) { return entry[0] === value; })[0];
  return row ? row[1] : value;
}
// The citation is rendered as text: safeUrl deliberately admits only owned
// article URLs, and widening it for an outbound link would weaken the control
// that keeps every other link in the terminal pointing at this research.
function rateSourceText() {
  const thresholds = RATE_CONTEXT.thresholds || {};
  const slope = thresholds.slope || [];
  const level = thresholds.level || [];
  const source = RATE_CONTEXT.source || {};
  const sourceName = String(source.name || '');
  const sourceUrl = String(source.page_url || '');
  const cuts = slope.length === 2 && level.length === 2
    ? ' Thirds are cut at a 10Y−2Y of ' + Number(slope[0]).toFixed(2) + ' and ' +
      Number(slope[1]).toFixed(2) + ', and at a 10Y of ' + Number(level[0]).toFixed(2) +
      '% and ' + Number(level[1]).toFixed(2) + '%.'
    : '';
  return 'Optional publication-date provenance only. Bands use fixed cut points from the complete ' +
    'extracted observation-date set, not the selected evidence set, and are not absolute regimes.' +
    cuts + ' Each reading is an official observation dated on or before the publication calendar date. ' +
    'It is not timestamp-aligned and may post-date a same-day article; it is not an event, entry, or ' +
    'position date. Source: ' + sourceName + (sourceUrl ? ' (' + sourceUrl + ')' : '') + '.';
}
function rateSourceNote() {
  return escapeHtml(rateSourceText());
}
function structureWords(value) {
  return normalize(value).match(/[\p{L}\p{N}]+/gu) || [];
}
function structurePhrase(value) {
  return structureWords(value).join(' ');
}
function structureFocusTokens() {
  return Array.from(new Set(structureWords(state.structureFocus)));
}
function structureSetupDefined() {
  return Boolean(
    structureFocusTokens().length || state.structureInstrument ||
    state.structureDirection !== 'any' || state.structurePeriod !== 'all'
  );
}
function structureInstrumentOptions() {
  // Before the deferred archive verifies, the controls are drawn from counts
  // fixed at build time so the landing view is never an empty frame.
  if (!IDEAS.length) return (DESK_FACETS.instruments || []).map(function (row) {
    return [row[0],row[1],row[2] || row[1]];
  });
  const counts = new Map();
  IDEAS.forEach(function (idea) {
    const seen = new Set();
    idea.instruments.forEach(function (instrument) {
      if (!instrument || instrument === 'unspecified' || seen.has(instrument)) return;
      seen.add(instrument);
      const row = counts.get(instrument) || {passages:0,articleIds:new Set()};
      row.passages += 1;
      if (idea._article && idea._article.id) row.articleIds.add(idea._article.id);
      counts.set(instrument,row);
    });
  });
  return Array.from(counts.entries()).map(function (entry) {
    return [entry[0],entry[1].passages,entry[1].articleIds.size];
  }).sort(function (first,second) {
    return second[2] - first[2] || second[1] - first[1] || first[0].localeCompare(second[0]);
  });
}
function structureUnderlyingOptions() {
  if (!IDEAS.length) return (DESK_FACETS.underlyings || []).map(function (row) {
    return {label:row[0],count:row[1],notes:row[2] || row[1]};
  });
  const counts = new Map();
  IDEAS.forEach(function (idea) {
    const seen = new Set();
    underlyingParts(idea).forEach(function (part) {
      const key = normalize(part);
      if (!key || seen.has(key)) return;
      seen.add(key);
      const row = counts.get(key) || {label:part,count:0,articleIds:new Set()};
      row.count += 1;
      if (idea._article && idea._article.id) row.articleIds.add(idea._article.id);
      counts.set(key,row);
    });
  });
  return Array.from(counts.values()).map(function (row) {
    return {label:row.label,count:row.count,notes:row.articleIds.size};
  }).filter(function (row) {
    return row.count >= 2;
  }).sort(function (first,second) {
    return second.notes - first.notes || second.count - first.count ||
      first.label.localeCompare(second.label);
  });
}
function structurePeriodOptions() {
  const years = new Set();
  if (!IDEAS.length) (DESK_FACETS.periods || []).forEach(function (year) {
    years.add(String(year));
  });
  IDEAS.forEach(function (idea) {
    const date = (idea._article && idea._article.date) || '';
    if (date.length >= 4) years.add(date.slice(0,4));
  });
  return [['all','Whole record']].concat(Array.from(years).sort().reverse().map(function (year) {
    return [year,year + ' record'];
  }));
}
// Rank one observation against the desk inputs. Returns null when a hard
// filter excludes it, so an empty result set stays honestly empty instead of
// padding the comparison with unrelated passages.
function structureMatch(idea) {
  const reasons = [];
  let score = 0;
  if (state.structureInstrument) {
    if (!idea.instruments.includes(state.structureInstrument)) return null;
    score += 40;
    reasons.push('Same instrument: ' + instrumentLabel(state.structureInstrument));
  }
  if (state.structureDirection !== 'any') {
    if (idea.direction !== state.structureDirection) return null;
    score += 20;
    reasons.push('Same parsed stance: ' + state.structureDirection);
  }
  const date = (idea._article && idea._article.date) || '';
  if (state.structurePeriod !== 'all' && date.slice(0,4) !== state.structurePeriod) return null;
  if (state.structureMacro && (state.structureSlope !== 'any' || state.structureLevel !== 'any')) {
    const rates = rateReading(idea);
    if (!rates) return null;
    if (state.structureSlope !== 'any') {
      if (rates.slopeBand !== state.structureSlope) return null;
      score += 15;
      reasons.push('Curve shape: ' + rateBandLabel('slope',rates.slopeBand).toLowerCase());
    }
    if (state.structureLevel !== 'any') {
      if (rates.levelBand !== state.structureLevel) return null;
      score += 15;
      reasons.push('10Y level: ' + rateBandLabel('level',rates.levelBand).toLowerCase());
    }
  }
  const tokens = structureFocusTokens();
  if (tokens.length) {
    const parts = underlyingParts(idea);
    const normalizedParts = parts.map(structurePhrase);
    const focus = structurePhrase(state.structureFocus);
    const exact = normalizedParts.indexOf(focus) !== -1;
    const structuredWords = new Set(structureWords([
      parts.join(' '),idea.manager_raw,
      idea.instruments.map(function (value) { return value + ' ' + instrumentLabel(value); }).join(' '),
      idea.direction,directionLabel(idea.direction)
    ].join(' ')));
    const article = idea._article || {};
    const authoredTitle = structurePhrase(article.title);
    const titleSubject = !exact && Boolean(focus) &&
      (' ' + authoredTitle + ' ').includes(' ' + focus + ' ');
    const allWords = new Set(structureWords([
      article.title,article.subtitle,idea.description,idea.quant,idea.thesis,
      idea.outcome,Array.from(structuredWords).join(' ')
    ].join(' ')));
    const related = !exact && !titleSubject && tokens.every(function (token) {
      return structuredWords.has(token);
    });
    const mention = !exact && !titleSubject && !related && tokens.every(function (token) {
      return allWords.has(token);
    });
    if (exact) {
      score += 300;
      reasons.push('Same underlying: ' + parts[normalizedParts.indexOf(focus)]);
      return {score:score,reasons:reasons,tier:'exact'};
    }
    if (titleSubject) {
      score += 250;
      reasons.push('Authored headline subject: ' + state.structureFocus.trim());
      return {score:score,reasons:reasons,tier:'subject'};
    }
    if (related) {
      score += 200;
      reasons.push('Structured-field match: ' + tokens.join(' + '));
      return {score:score,reasons:reasons,tier:'related'};
    }
    if (mention) {
      score += 100;
      reasons.push('Article-text mention only: ' + tokens.join(' + '));
      return {score:score,reasons:reasons,tier:'mention'};
    }
    return null;
  }
  return {score:score,reasons:reasons,tier:'filtered'};
}
function structureMatchSets() {
  if (!structureSetupDefined()) {
    return {primary:[],related:[],tier:'none',all:[]};
  }
  const buckets = {exact:[],subject:[],related:[],mention:[],filtered:[]};
  IDEAS.forEach(function (idea) {
    const match = structureMatch(idea);
    if (!match) return;
    buckets[match.tier].push({idea:idea,score:match.score,reasons:match.reasons,tier:match.tier});
  });
  Object.keys(buckets).forEach(function (key) {
    buckets[key].sort(function (first,second) {
      if (second.score !== first.score) return second.score - first.score;
      const firstDate = (first.idea._article && first.idea._article.date) || '';
      const secondDate = (second.idea._article && second.idea._article.date) || '';
      if (firstDate !== secondDate) return secondDate.localeCompare(firstDate);
      return first.idea.id.localeCompare(second.idea.id);
    });
  });
  if (!structureFocusTokens().length) {
    return {primary:buckets.filtered,related:[],tier:'filtered',all:buckets.filtered};
  }
  const tier = buckets.exact.length ? 'exact' : buckets.subject.length ? 'subject' :
    buckets.related.length ? 'related' : 'none';
  const primary = tier === 'exact' ? buckets.exact : tier === 'subject' ? buckets.subject :
    tier === 'related' ? buckets.related : [];
  const related = tier === 'exact'
    ? buckets.subject.concat(buckets.related,buckets.mention)
    : tier === 'subject' ? buckets.related.concat(buckets.mention) : buckets.mention;
  return {primary:primary,related:related,tier:tier,all:primary.concat(related)};
}
function structureMatches() {
  return structureMatchSets().primary;
}
function structureArticleGroups(rows) {
  const groups = new Map();
  rows.forEach(function (row) {
    const article = row.idea._article || {};
    const key = article.id || row.idea.article_id || article.url || row.idea.id;
    const group = groups.get(key) || {
      id:key,article:article,rows:[],score:row.score,tier:row.tier,reasons:new Set()
    };
    group.rows.push(row);
    group.score = Math.max(group.score,row.score);
    row.reasons.forEach(function (reason) { group.reasons.add(reason); });
    groups.set(key,group);
  });
  return Array.from(groups.values()).sort(function (first,second) {
    if (second.score !== first.score) return second.score - first.score;
    const firstDate = first.article.date || '';
    const secondDate = second.article.date || '';
    if (firstDate !== secondDate) return secondDate.localeCompare(firstDate);
    return String(first.id).localeCompare(String(second.id));
  });
}
function structureGroupFacts(group) {
  const instruments = new Set();
  const directions = new Set();
  const underlyings = new Set();
  let withQuant = false;
  let withThesis = false;
  let withOutcome = false;
  let truncated = false;
  let review = false;
  let documentation = 0;
  group.rows.forEach(function (row) {
    const idea = row.idea;
    idea.instruments.forEach(function (value) {
      if (value && value !== 'unspecified') instruments.add(value);
    });
    if (idea.direction && idea.direction !== 'unspecified') directions.add(idea.direction);
    underlyingParts(idea).forEach(function (value) { underlyings.add(value); });
    withQuant = withQuant || Boolean(idea.quant);
    withThesis = withThesis || Boolean(idea.thesis);
    withOutcome = withOutcome || Boolean(idea.outcome);
    truncated = truncated || Boolean(idea.description_truncated);
    review = review || reviewFlagged(idea);
    documentation = Math.max(documentation,Number(idea.documentation_score || 0));
  });
  return {
    instruments:Array.from(instruments).sort(),
    directions:Array.from(directions).sort(),
    underlyings:Array.from(underlyings).sort(),
    withQuant:withQuant,withThesis:withThesis,withOutcome:withOutcome,
    truncated:truncated,review:review,documentation:documentation
  };
}
function structurePattern(rows) {
  const groups = structureArticleGroups(rows);
  const instruments = new Map();
  const directions = new Map();
  const managers = new Map();
  const periods = new Map();
  const slopeBands = new Map();
  const levelBands = new Map();
  let priced = 0;
  let levelLow = null;
  let levelHigh = null;
  let slopeLow = null;
  let slopeHigh = null;
  let withQuant = 0;
  let withThesis = 0;
  let withOutcome = 0;
  let withCurrent = 0;
  let withFull = 0;
  let withTruncation = 0;
  let withReview = 0;
  groups.forEach(function (group) {
    const facts = structureGroupFacts(group);
    const list = facts.instruments;
    list.forEach(function (value) {
      instruments.set(value,(instruments.get(value) || 0) + 1);
    });
    if (facts.directions.length) facts.directions.forEach(function (value) {
      directions.set(value,(directions.get(value) || 0) + 1);
    });
    else directions.set('unspecified',(directions.get('unspecified') || 0) + 1);
    const year = (group.article.date || '').slice(0,4);
    if (year) periods.set(year,(periods.get(year) || 0) + 1);
    const rates = group.rows.length ? rateReading(group.rows[0].idea) : null;
    if (state.structureMacro && rates) {
      slopeBands.set(rates.slopeBand,(slopeBands.get(rates.slopeBand) || 0) + 1);
      levelBands.set(rates.levelBand,(levelBands.get(rates.levelBand) || 0) + 1);
      priced += 1;
      levelLow = levelLow === null ? rates.y10 : Math.min(levelLow,rates.y10);
      levelHigh = levelHigh === null ? rates.y10 : Math.max(levelHigh,rates.y10);
      slopeLow = slopeLow === null ? rates.slope : Math.min(slopeLow,rates.slope);
      slopeHigh = slopeHigh === null ? rates.slope : Math.max(slopeHigh,rates.slope);
    }
    const named = new Set(group.rows.map(function (row) {
      return row.idea.manager_raw;
    }).filter(Boolean));
    named.forEach(function (value) { managers.set(value,(managers.get(value) || 0) + 1); });
    if (facts.withQuant) withQuant += 1;
    if (facts.withThesis) withThesis += 1;
    if (facts.withOutcome) withOutcome += 1;
    if (group.article.body_revision_status === 'current') withCurrent += 1;
    if (group.article.content_status === 'full') withFull += 1;
    if (facts.truncated) withTruncation += 1;
    if (facts.review) withReview += 1;
  });
  function ranked(map) {
    return Array.from(map.entries()).sort(function (first,second) {
      return second[1] - first[1] || first[0].localeCompare(second[0]);
    });
  }
  return {
    total:rows.length,
    noteTotal:groups.length,
    groups:groups,
    instruments:ranked(instruments),
    directions:ranked(directions),
    periods:Array.from(periods.entries()).sort(function (first,second) {
      return second[0].localeCompare(first[0]);
    }),
    slopeBands:RATE_BANDS.slope.filter(function (entry) {
      return slopeBands.has(entry[0]);
    }).map(function (entry) { return [entry[1],slopeBands.get(entry[0])]; }),
    levelBands:RATE_BANDS.level.filter(function (entry) {
      return levelBands.has(entry[0]);
    }).map(function (entry) { return [entry[1],levelBands.get(entry[0])]; }),
    managers:ranked(managers),
    withQuant:withQuant,
    withThesis:withThesis,
    withOutcome:withOutcome,
    withCurrent:withCurrent,
    withFull:withFull,
    withTruncation:withTruncation,
    withReview:withReview,
    priced:priced,
    levelRange:levelLow === null ? null : [levelLow,levelHigh],
    slopeRange:slopeLow === null ? null : [slopeLow,slopeHigh],
    firstDate:groups.length ? groups.map(function (group) {
      return group.article.date || '';
    }).filter(Boolean).sort()[0] : '',
    lastDate:groups.length ? groups.map(function (group) {
      return group.article.date || '';
    }).filter(Boolean).sort().slice(-1)[0] : ''
  };
}
function structureChipRow(label,attribute,options,active,allOption) {
  const buttons = (allOption ? [allOption] : []).concat(options).map(function (option) {
    const value = option[0];
    const text = option[1];
    const count = option.length > 2 ? option[2] : null;
    const pressed = String(value) === String(active);
    return '<button class="structure-chip' + (pressed ? ' active' : '') + '" type="button" data-' +
      attribute + '="' + escapeHtml(value) + '" aria-pressed="' + (pressed ? 'true' : 'false') + '">' +
      escapeHtml(text) + (count === null ? '' : '<span class="structure-chip-count">' +
        escapeHtml(typeof count === 'number' ? number(count) : String(count)) + '</span>') +
      '</button>';
  }).join('');
  return '<div class="structure-control"><span class="structure-control-label">' + escapeHtml(label) +
    '</span><div class="structure-chips">' + buttons + '</div></div>';
}
function structurePassageWarnings(idea) {
  const article = (idea && idea._article) || {};
  const warnings = [];
  if (idea && idea.negation_risk) warnings.push('negation risk');
  if (idea && idea.reference_line) warnings.push('reference line');
  if (idea && idea.description_truncated) warnings.push('passage truncated');
  if (article.body_revision_status !== 'current') {
    warnings.push(bodyRevisionLabel(article));
  }
  if (article.content_status !== 'full') warnings.push('indexed excerpt only');
  return warnings;
}
function structurePassageMarkup(row,index) {
  const idea = row.idea;
  const rates = state.structureMacro ? rateReading(idea) : null;
  const followUps = observationFollowUps(idea).slice(0,2);
  const warnings = structurePassageWarnings(idea);
  const selected = state.structurePassage === idea.id;
  return '<div class="structure-passage-item"><div class="structure-passage-head"><span>Passage ' +
    number(index) + '</span><button class="secondary-action structure-use-passage' +
    (selected ? ' active' : '') + '" type="button" data-structure-passage="' +
    escapeHtml(idea.id) + '" aria-pressed="' + (selected ? 'true' : 'false') +
    '" aria-label="' + (selected ? 'Selected' : 'Use') + ' passage ' + number(index) +
    ' from ' + escapeHtml(String((idea._article || {}).title || 'source note')) +
    ' as the diligence packet anchor">' + (selected ? 'Anchored passage' : 'Anchor this passage') +
    '</button></div><p>' + escapeHtml(passageText(idea)) + '</p>' +
    '<div class="structure-passage-meta"><span>Stance: ' + escapeHtml(directionLabel(idea.direction)) + '</span>' +
    '<span>Documentation: ' + number(idea.documentation_score) + '/5</span>' +
    (idea.quant ? '<span>Numeric context: ' + escapeHtml(idea.quant) + '</span>' : '') +
    (idea.thesis ? '<span>Source-stated edge captured</span>' : '') +
    (idea.outcome ? '<span>Source-stated outcome captured</span>' : '') +
    warnings.map(function (warning) {
      return '<span class="structure-passage-warning">Review: ' +
        escapeHtml(warning) + '</span>';
    }).join('') +
    ((idea._article || {}).content_status === 'full'
      ? '<span>Indexed coverage: full text</span>' : '') + '</div>' +
    (rates ? '<p class="structure-rates"><span>U.S. curve observation by publication date — not timestamp-aligned or an event, entry, or position date</span>' +
      '2Y ' + rates.y2.toFixed(2) + '% · 10Y ' + rates.y10.toFixed(2) + '% · 30Y ' +
      rates.y30.toFixed(2) + '% · official observation dated ' + escapeHtml(formatDate(rates.asOf)) + '</p>' : '') +
    (followUps.length ? '<div class="structure-followup"><span>Related subsequent notes via article-level topic — not an outcome or result</span><ul>' +
      followUps.map(function (follow) {
        return '<li><time datetime="' + escapeHtml(follow.article.date) + '">' +
          escapeHtml(shortDate(follow.article.date)) + '</time><a href="' +
          escapeHtml(safeUrl(follow.article.url)) + '" target="_blank" rel="noopener noreferrer">' +
          escapeHtml(follow.article.title) + '</a><em>via ' +
          escapeHtml(follow.topics.slice(0,2).join(', ')) + '</em></li>';
      }).join('') + '</ul></div>' : '') + '</div>';
}
function structureComparableRow(group,rank) {
  const facts = structureGroupFacts(group);
  const article = group.article;
  const stance = facts.directions.length
    ? facts.directions.map(directionLabel).join(' · ') : 'No stance in matching passages';
  const structure = facts.instruments.length
    ? facts.instruments.map(instrumentLabel).join(' · ') : 'No instrument stated';
  const tierLabel = group.tier === 'exact' ? 'Direct underlying' :
    group.tier === 'subject' ? 'Authored headline subject' :
    group.tier === 'related' ? 'Structured field' : 'Filtered set';
  const flags = [
    ['N','Numeric context',facts.withQuant],
    ['T','Source-stated edge',facts.withThesis],
    ['O','Source-stated outcome',facts.withOutcome]
  ].map(function (row) {
    return '<span class="structure-evidence-flag' + (row[2] ? ' on' : '') +
      '"><span aria-hidden="true">' + row[0] + (row[2] ? '+' : '−') + '</span>' +
      '<span class="sr-only">' + escapeHtml(row[1] + (row[2] ? ' captured' : ' absent')) +
      '</span></span>';
  }).join('');
  const gaps = [];
  if (!facts.directions.length) gaps.push('stance absent');
  if (!facts.instruments.length) gaps.push('instrument absent');
  if (!facts.underlyings.length) gaps.push('underlying absent');
  if (!facts.withQuant) gaps.push('numeric context absent');
  if (!facts.withThesis) gaps.push('edge absent');
  if (!facts.withOutcome) gaps.push('outcome absent');
  if (facts.truncated) gaps.push('passage truncated');
  if (facts.review) gaps.push('extraction review required');
  if (article.content_status !== 'full') gaps.push('indexed excerpt only');
  if (article.body_revision_status !== 'current') gaps.push(bodyRevisionLabel(article));
  const selected = state.structureAnchor === group.id && group.rows.some(function (row) {
    return row.idea.id === state.structurePassage;
  });
  return '<div class="structure-evidence-group" role="rowgroup" data-structure-note="' +
    escapeHtml(group.id) + '"><div class="structure-evidence-row" role="row">' +
    '<div class="structure-evidence-cell" role="cell"><span class="structure-anchor-status' +
      (selected ? ' active' : '') + '">' + (selected ? 'Passage anchored' : 'Choose below') + '</span></div>' +
    '<div class="structure-evidence-cell" role="cell"><strong>' +
      escapeHtml(article.title || 'Untitled source note') + '</strong><small>' +
      escapeHtml(formatDate(article.date)) + ' · ' + escapeHtml(sourceLabel(article.source)) +
      ' · ' + escapeHtml(sourceAccessLabel(article)) + ' · indexed coverage: ' +
      escapeHtml(article.content_status === 'full' ? 'full text' : 'excerpt only') + '</small></div>' +
    '<div class="structure-evidence-cell" role="cell"><span class="structure-tier ' +
      escapeHtml(group.tier) + '">' + escapeHtml(tierLabel) + '</span><small>' +
      escapeHtml(Array.from(group.reasons).join(' · ') || 'Explicit filters') + '</small></div>' +
    '<div class="structure-evidence-cell" role="cell"><strong>' + escapeHtml(structure) +
      '</strong><small>' + escapeHtml(stance) + ' · parsed mentions, not validated legs</small></div>' +
    '<div class="structure-evidence-cell" role="cell"><strong>' + number(group.rows.length) +
      ' passage' + (group.rows.length === 1 ? '' : 's') + '</strong><div class="structure-evidence-flags">' +
      flags + '</div><small>Best documentation ' + number(facts.documentation) + '/5</small></div>' +
    '<div class="structure-evidence-cell" role="cell"><strong>' +
      escapeHtml(gaps.length ? gaps.join(' · ') : 'Inspect exact passage; no automated gap label') +
      '</strong><small>Retrieval relevance and documentation are separate.</small>' +
      '<a class="structure-source" href="' + escapeHtml(safeUrl(article.url)) +
      '" target="_blank" rel="noopener noreferrer">Open source ↗</a></div></div>' +
    '<div class="structure-row-detail" role="row"><div class="structure-row-detail-cell" ' +
      'role="cell" aria-colspan="6"><details' + (selected ? ' open' : '') +
      '><summary>Inspect ' + number(group.rows.length) +
      ' captured source passage' + (group.rows.length === 1 ? '' : 's') + '</summary>' +
      '<div class="structure-passages">' + group.rows.map(function (row,index) {
        return structurePassageMarkup(row,index + 1);
      }).join('') + '</div></details></div></div></div>';
}
function countLabel(value,singular,plural) {
  return number(value) + ' ' + (Number(value) === 1 ? singular : (plural || singular + 's'));
}
function structureRelatedPanel(rows) {
  const groups = structureArticleGroups(rows);
  if (!groups.length) return '';
  return '<details class="structure-related"><summary><span>Related mentions — excluded from the evidence set</span><span>' +
    countLabel(groups.length,'source note') + ' · ' + countLabel(rows.length,'passage') + '</span></summary>' +
    '<div class="structure-related-list">' + groups.slice(0,12).map(function (group) {
      return '<div class="structure-related-row"><span>' + escapeHtml(formatDate(group.article.date)) +
        '</span><span>' + escapeHtml(group.article.title || 'Untitled source note') +
        '<small>' + escapeHtml(Array.from(group.reasons).join(' · ')) + '</small></span>' +
        '<a href="' + escapeHtml(safeUrl(group.article.url)) +
        '" target="_blank" rel="noopener noreferrer">Open source ↗</a></div>';
    }).join('') + '</div></details>';
}
function deskShare(total) {
  return total ? Math.round((total[0] / total[1]) * 100) : 0;
}
function deskReferenceClass() {
  if (!structureSetupDefined()) return 'no research evidence set defined';
  const parts = [];
  const tier = structureMatchSets().tier;
  if (state.structureFocus) {
    const prefix = tier === 'exact' ? 'direct underlying ' :
      tier === 'subject' ? 'authored headline subject ' : 'structured fields for ';
    parts.push(prefix + '"' + state.structureFocus + '"');
  }
  if (state.structureInstrument) parts.push(instrumentLabel(state.structureInstrument));
  if (state.structureDirection !== 'any') parts.push(directionLabel(state.structureDirection));
  if (state.structurePeriod !== 'all') parts.push(state.structurePeriod);
  if (state.structureMacro && state.structureSlope !== 'any') {
    parts.push(rateBandLabel('slope',state.structureSlope).toLowerCase() + ' curve');
  }
  if (state.structureMacro && state.structureLevel !== 'any') {
    parts.push(rateBandLabel('level',state.structureLevel).toLowerCase());
  }
  return parts.join(' · ');
}
// Build the note as data first so the panel and the copied memo cannot drift.
function deskNoteLines(pattern) {
  const notes = pattern.noteTotal;
  if (!notes) return [];
  const tier = structureMatchSets().tier;
  function coverage(count) {
    const base = number(count) + ' of ' + countLabel(notes,'source note');
    return notes >= 3 ? base + ' (' + deskShare([count,notes]) + '%)' : base;
  }
  const lines = [];
  lines.push(['Evidence set',
    number(notes) + ' source ' + (notes === 1 ? 'note' : 'notes') + ' containing ' +
    number(pattern.total) + (tier === 'subject'
      ? ' captured ' + (pattern.total === 1 ? 'passage' : 'passages') + ' from headline-matched notes'
      : ' matching extracted ' + (pattern.total === 1 ? 'passage' : 'passages')) +
    (notes === 1 ? ' matches ' : ' match ') + deskReferenceClass() +
    (pattern.firstDate && pattern.lastDate
      ? ', published between ' + formatDate(pattern.firstDate) + ' and ' + formatDate(pattern.lastDate)
      : '') + '.']);
  const top = pattern.instruments.filter(function (row) {
    return row[0] !== state.structureInstrument;
  })[0];
  lines.push(['Parsed instrument context',top
    ? instrumentLabel(top[0]) + ' is mentioned in ' + coverage(top[1]) +
      '. These are archive co-occurrences, not validated position legs.'
    : 'No additional instrument mention is repeated across the selected source notes.']);
  lines.push(['Captured evidence',coverage(pattern.withQuant) +
    (notes === 1 ? ' contains' : ' contain') + ' numeric context; ' + coverage(pattern.withThesis) +
    (notes === 1 ? ' contains' : ' contain') + ' a source-stated edge; ' + coverage(pattern.withFull) +
    (notes === 1 ? ' uses' : ' use') + ' a full-text capture; ' + coverage(notes - pattern.withFull) +
    (notes === 1 ? ' is' : ' are') + ' excerpt-backed; ' + coverage(pattern.withCurrent) +
    (notes === 1 ? ' uses' : ' use') + ' a current source capture.']);
  lines.push(['Extraction cautions',coverage(pattern.withReview) +
    (notes === 1 ? ' carries' : ' carry') + ' an automated extraction-review flag; ' +
    coverage(pattern.withTruncation) +
    (notes === 1 ? ' contains' : ' contain') + ' a truncated captured passage. Zero flags do not independently verify the source.']);
  const outcomeGap = pattern.withOutcome
    ? coverage(pattern.withOutcome) + (notes === 1 ? ' contains' : ' contain') + ' a source-stated outcome.'
    : 'No selected source note contains a source-stated outcome.';
  lines.push(['Primary diligence gap',outcomeGap +
    (notes < 3
      ? ' The set is too thin for distributional interpretation.'
      : ' Passage frequency is descriptive archive coverage, not a performance base rate.')]);
  return lines;
}
function deskNoteMarkdown(pattern, rows) {
  const lines = deskNoteLines(pattern);
  if (!lines.length) return '';
  const body = lines.map(function (row) {
    return '**' + row[0] + '.** ' + row[1];
  }).join('\n\n');
  const cited = structureArticleGroups(rows).map(function (group, index) {
    const article = group.article || {};
    const rates = state.structureMacro && group.rows.length ? rateReading(group.rows[0].idea) : null;
    return (index + 1) + '. [' + article.title + '](' + safeUrl(article.url) + ') — ' +
      formatDate(article.date) +
      (rates ? ' · U.S. curve as of ' + formatDate(rates.asOf) + ': 10Y ' +
        rates.y10.toFixed(2) + '%, 10Y−2Y ' + rates.slope.toFixed(2) +
        ' · publication-calendar-date provenance; not timestamp-aligned and may post-date a same-day article' : '') +
      (group.reasons.size ? ' · ' + Array.from(group.reasons).join('; ') : '');
  }).join('\n');
  const macro = state.structureMacro
    ? '\n\n## Optional macro provenance\n\n' + rateSourceText() : '';
  return '# Research evidence memo — ' + deskReferenceClass() + '\n\n' + body +
    '\n\n## Primary retrieved source notes\n\n' + cited + macro +
    '\n\n---\n\nGenerated from snapshot ' + String(SNAPSHOT.data_checksum || '') +
    '. Published-research evidence only; no live holdings, pricing, sizing, P&L, or recommendation.\n';
}
function renderStructureDesk(rows, gate) {
  const shell = document.getElementById('structure-shell');
  const defined = structureSetupDefined();
  const sets = gate ? {primary:[],related:[],tier:'none'} : structureMatchSets();
  rows = sets.primary;
  const pattern = structurePattern(rows);
  const instrumentOptions = structureInstrumentOptions().map(function (entry) {
    return [entry[0],instrumentLabel(entry[0]),number(entry[2]) +
      (entry[2] === 1 ? ' note · ' : ' notes · ') + number(entry[1]) +
      (entry[1] === 1 ? ' passage' : ' passages')];
  });
  const underlyingOptions = structureUnderlyingOptions();
  // Answer first, controls on demand. A reader who opens this wants the
  // synthesis, not six rows of filters standing between them and it.
  const controls =
    structureChipRow('Instrument','structure-instrument',instrumentOptions,state.structureInstrument,['','Any instrument']) +
    structureChipRow('Stance','structure-direction',[
      ['long','Long'],['short','Short'],['long/short','Long / short'],
      ['arbitrage/relative value','Arbitrage / RV'],['unspecified','Not stated']
    ],state.structureDirection,['any','Any stance']) +
    structureChipRow('Period','structure-period',structurePeriodOptions().slice(1),state.structurePeriod,['all','Any publication year']) +
    '<div class="structure-control"><span class="structure-control-label">Optional macro provenance</span>' +
    '<div class="structure-chips"><button class="structure-chip' + (state.structureMacro ? ' active' : '') +
    '" type="button" data-structure-macro="' + (state.structureMacro ? '0' : '1') +
    '" aria-pressed="' + (state.structureMacro ? 'true' : 'false') + '">' +
    (state.structureMacro ? 'U.S. publication-date curve shown' : 'Add U.S. publication-date curve') +
    '</button></div></div>' +
    (state.structureMacro
      ? structureChipRow('U.S. curve band','structure-slope',RATE_BANDS.slope,state.structureSlope,['any','Any fixed-universe band']) +
        structureChipRow('U.S. 10Y band','structure-level',RATE_BANDS.level,state.structureLevel,['any','Any fixed-universe band'])
      : '');
  // The recurring underlyings are how most readers begin, so they sit with
  // the question rather than inside the refinements.
  const startChips = underlyingOptions.length
    ? '<div class="desk-starts"><span class="desk-starts-label">Start with</span>' +
      underlyingOptions.slice(0,8).map(function (row) {
        const active = structurePhrase(row.label) === structurePhrase(state.structureFocus);
        return '<button class="desk-start' + (active ? ' active' : '') + '" type="button" ' +
          'data-structure-focus="' + escapeHtml(row.label) + '" aria-pressed="' +
          (active ? 'true' : 'false') + '" aria-label="' + escapeHtml(row.label) + ': ' +
          countLabel(row.notes,'source note') + ' and ' + countLabel(row.count,'passage') + '">' +
          escapeHtml(row.label) + '<span class="desk-start-count">' + number(row.notes) + 'N · ' +
          number(row.count) + 'P</span></button>';
      }).join('') +
      (state.structureFocus
        ? '<button class="desk-start clear" type="button" data-structure-focus="">Clear</button>'
        : '') + '</div>'
    : '';
  const activeFilters = [
    state.structureInstrument
      ? ['structure-instrument','',instrumentLabel(state.structureInstrument)] : null,
    state.structureDirection !== 'any'
      ? ['structure-direction','any',directionLabel(state.structureDirection)] : null,
    state.structurePeriod !== 'all'
      ? ['structure-period','all',state.structurePeriod + ' record'] : null,
    state.structureSlope !== 'any'
      ? ['structure-slope','any',rateBandLabel('slope',state.structureSlope)] : null,
    state.structureLevel !== 'any'
      ? ['structure-level','any',rateBandLabel('level',state.structureLevel)] : null,
    state.structureMacro ? ['structure-macro','0','U.S. publication-date curve'] : null
  ].filter(Boolean);
  const refineBar = '<div class="desk-refine">' +
    '<button class="desk-refine-toggle" type="button" data-structure-refine="1" ' +
    'aria-expanded="' + (state.structureControlsOpen ? 'true' : 'false') +
    '" aria-controls="desk-refinements">Refine' +
    (activeFilters.length ? '<span class="desk-refine-count">' + number(activeFilters.length) + '</span>' : '') +
    '</button>' +
    (activeFilters.length
      ? '<div class="desk-active-filters">' + activeFilters.map(function (row) {
          return '<button class="desk-active-filter" type="button" data-' + row[0] +
            '="' + escapeHtml(row[1]) + '" aria-label="Remove ' + escapeHtml(row[2]) +
            '">' + escapeHtml(row[2]) + '<span aria-hidden="true">&times;</span></button>';
        }).join('') + '</div>'
      : '<span class="desk-refine-hint">Add instrument, stance, period, or optional macro provenance</span>') +
    '</div>' +
    '<div class="structure-controls" id="desk-refinements"' +
    (state.structureControlsOpen ? '' : ' hidden') + '>' + controls + '</div>';
  if (!gate) {
    const selectedGroup = state.structureAnchor && pattern.groups.filter(function (group) {
      return group.id === state.structureAnchor;
    })[0];
    const passageIsPresent = selectedGroup && state.structurePassage &&
      selectedGroup.rows.some(function (row) {
        return row.idea.id === state.structurePassage;
      });
    if (!passageIsPresent && (state.structureAnchor || state.structurePassage)) {
      state.structureAnchor = '';
      state.structurePassage = '';
    }
  }
  const packetAnchorGroup = state.structureAnchor && pattern.groups.filter(function (group) {
    return group.id === state.structureAnchor;
  })[0];
  const packetAnchorRow = packetAnchorGroup && packetAnchorGroup.rows.filter(function (row) {
    return row.idea.id === state.structurePassage;
  })[0];
  const packetAnchor = packetAnchorRow
    ? '<p class="desk-packet-anchor"><strong>Packet anchor</strong><span>' +
      escapeHtml(packetAnchorGroup.article.title || 'Untitled source note') +
      ' · exact passage selected</span></p>'
    : '';
  const anchorStats = pattern.noteTotal ? [
    [number(pattern.noteTotal),'source ' + (pattern.noteTotal === 1 ? 'note' : 'notes')],
    [number(pattern.total),sets.tier === 'subject'
      ? 'captured passages in matched notes' : 'matching source passages'],
    [number(pattern.withQuant),'notes with numeric context'],
    [number(pattern.withOutcome),'notes with source outcome']
  ] : [];
  const anchorRow = anchorStats.length
    ? '<div class="desk-anchors">' + anchorStats.map(function (row) {
        return '<div class="desk-anchor"><b>' + escapeHtml(row[0]) + '</b><span>' +
          escapeHtml(row[1]) + '</span></div>';
      }).join('') + '</div>'
    : '';
  const noteLines = deskNoteLines(pattern);
  const notePanel = noteLines.length
    ? '<section class="desk-note"><header class="desk-note-head">' +
      '<h3>Decision snapshot</h3><div class="desk-note-actions">' +
      '<button class="secondary-action" type="button" ' +
      'data-structure-open-packet="1"' + (state.structurePassage ? '' : ' disabled') +
      '>' + (state.structurePassage ? 'Create diligence packet' : 'Anchor a source passage first') + '</button>' +
      '<button class="secondary-action" type="button" ' +
      'data-structure-copy-note="1">Copy evidence memo</button></div></header>' +
      packetAnchor + anchorRow + '<dl>' +
      noteLines.map(function (row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl></section>'
    : '';
  const summary = pattern.noteTotal
    ? '<p class="structure-summary">Counts are clustered by source note: <b>' +
      number(pattern.noteTotal) + '</b> ' + (pattern.noteTotal === 1 ? 'note contains' : 'notes contain') + ' <b>' + number(pattern.total) +
      '</b> ' + (sets.tier === 'subject' ? 'captured passages in headline-matched notes' :
        'matching passages') + '. Repeated passages from one note never receive separate votes.</p>'
    : '<p class="structure-summary">No primary exact, headline, or structured-field evidence matches these inputs.</p>';
  function bars(entries,total,label) {
    if (!entries.length) return '<p class="structure-none">Nothing recorded.</p>';
    const name = label || function (value) { return value; };
    return '<ul class="structure-bars">' + entries.map(function (entry) {
      const share = total ? Math.round((entry[1] / total) * 100) : 0;
      const value = number(entry[1]) + ' note' + (entry[1] === 1 ? '' : 's') +
        (total >= 3 ? ' · ' + share + '%' : '');
      return '<li aria-label="' + escapeHtml(name(entry[0])) + ': ' + value + ' of ' +
        number(total) + '"><span class="structure-bar-label">' + escapeHtml(name(entry[0])) +
        '</span><span class="structure-bar" aria-hidden="true"><i style="width:' + share + '%"></i></span>' +
        '<span class="structure-bar-value">' + escapeHtml(value) + '</span></li>';
    }).join('') + '</ul>';
  }
  const patternPanel = '<section class="structure-panel"><div class="structure-panel-head"><h3>Coverage audit</h3>' +
    '<span class="structure-summary">source-note clustered</span></div>' +
    (gate ? '<p class="structure-none">' + escapeHtml(gate.title) + '</p>' : summary) +
    (pattern.noteTotal ? '<p class="structure-note">Presence counts are note-clustered; one source note may contain more than one parsed instrument or stance.</p>' : '') +
    '<h4>Instrument mentions by source note</h4>' + bars(pattern.instruments,pattern.noteTotal,instrumentLabel) +
    '<h4>Parsed stance-language presence</h4>' + bars(pattern.directions,pattern.noteTotal,directionLabel) +
    (pattern.periods.length > 1
      ? '<h4>Publication year</h4>' + bars(pattern.periods,pattern.noteTotal)
      : '') +
    (state.structureMacro && pattern.slopeBands.length
      ? '<h4>U.S. curve observation by publication date</h4>' + bars(pattern.slopeBands,pattern.noteTotal)
      : '') +
    (state.structureMacro && pattern.levelBands.length
      ? '<h4>U.S. 10Y observation by publication date</h4>' + bars(pattern.levelBands,pattern.noteTotal) +
        '<p class="structure-macro-note">' + rateSourceNote() + '</p>'
      : '') +
    (pattern.managers.length
      ? '<h4>Managers named in these passages</h4><p class="structure-managers">' +
        pattern.managers.slice(0,8).map(function (entry) {
          return escapeHtml(entry[0]) + ' (' + number(entry[1]) + ')';
        }).join(' · ') + '</p>'
      : '') +
    '</section>';
  const orderedGroups = packetAnchorGroup
    ? [packetAnchorGroup].concat(pattern.groups.filter(function (group) {
        return group.id !== packetAnchorGroup.id;
      }))
    : pattern.groups;
  const shown = orderedGroups.slice(0,state.limit);
  const comparablePanel = '<section class="structure-panel structure-comparables">' +
    '<div class="structure-panel-head"><h3>Primary retrieved source notes</h3><span class="structure-summary">' +
    countLabel(pattern.noteTotal,'note') + ' · ' + countLabel(pattern.total,'passage') + '</span></div>' +
    (gate
      ? '<p class="structure-none">' + escapeHtml(gate.copy) + '</p>' +
        (gate.action ? '<div class="empty-actions observation-retry">' + gate.action + '</div>' : '')
      : pattern.groups.length
      ? '<div class="structure-matrix" role="table" aria-label="Retrieved research source notes">' +
        '<div class="structure-matrix-head" role="row"><span role="columnheader">Anchor</span><span role="columnheader">Source note</span><span role="columnheader">Match</span><span role="columnheader">Parsed instrument mentions / stance</span><span role="columnheader">Coverage</span><span role="columnheader">Gaps / source</span></div>' +
        shown.map(function (group) {
          return structureComparableRow(group,pattern.groups.indexOf(group) + 1);
        }).join('') + '</div>' +
        (pattern.groups.length > shown.length
          ? '<button class="secondary-action structure-more" type="button" data-structure-more="1">Show ' +
            number(Math.min(PAGE_SIZE.structure,pattern.groups.length - shown.length)) + ' more of ' +
            number(pattern.groups.length) + ' source notes</button>'
          : '')
      : '<p class="structure-none">No primary evidence set can be formed. Related article-text mentions, if any, remain separated below and do not enter the snapshot.</p>') +
    '</section>';
  const startState = !gate && !defined
    ? '<section class="desk-start-state"><div class="desk-start-copy"><div class="structure-kicker">No setup defined</div>' +
      '<h3>Build an evidence-bound research set</h3><p>Choose an underlying, named entity, instrument, stance, or publication window. The desk will cluster captured passages by source note, separate primary evidence from loose mentions, expose what is missing, and let you choose the exact passage that anchors a local diligence packet.</p></div>' +
      '<div class="desk-start-steps"><div class="desk-start-step"><b>01 Define</b><span>This app keeps the question in memory and does not transmit it.</span></div><div class="desk-start-step"><b>02 Challenge</b><span>Inspect source-note breadth, missing fields, and revision limits.</span></div><div class="desk-start-step"><b>03 Decide</b><span>Anchor one exact passage; analyst thesis and risk remain blank.</span></div></div></section>'
    : '';
  shell.innerHTML = '<div class="structure-wrap">' +
    '<header class="structure-head"><div class="structure-title-row"><div><div class="structure-kicker">Pre-trade research workflow</div><h2>Research Structuring Desk</h2></div>' +
    '<p class="structure-boundary">Published-research evidence only · no live holdings, prices, P&amp;L, sizing, exposure, liquidity, compliance approval, or recommendation</p></div>' +
    '<p class="structure-lede">Define a research question, compare captured passages and parsed instrument/stance fields, challenge missing evidence, and carry one explicitly anchored passage into a local diligence packet.</p>' +
    '<div class="structure-search"><label class="sr-only" for="structure-focus-input">Research subject or question</label>' +
    '<div class="structure-search-row"><input id="structure-focus-input" type="search" maxlength="120" spellcheck="false" autocorrect="off" autocapitalize="off" placeholder="Underlying, entity, strategy, or research question — e.g. VIX, S&amp;P 500, JGB" value="' +
    escapeHtml(state.structureFocus) + '" autocomplete="off"><span class="structure-search-help">This app keeps it in memory · included in the URL only after Copy view</span></div></div>' +
    startChips + '</header>' + notePanel + refineBar +
    startState + (defined || gate ? '<div class="structure-grid">' + comparablePanel + patternPanel + '</div>' : '') +
    (!gate && defined ? structureRelatedPanel(sets.related) : '') +
    '<p class="structure-disclosure"><strong>Evidence boundary.</strong> Structure Desk covers ' +
    number(deskUniverseTotal()) + ' extracted passages clustered into ' +
    number(DESK_FACETS.source_note_count || 0) + ' source notes; the broader library contains ' +
    number(ARTICLES.length) + ' indexed records. Instrument fields are lexical source mentions, not validated legs. Related subsequent notes are article-topic links, not outcomes. Only ' +
    number(outcomeTotal()) + ' passages contain a source-stated outcome. This is not a backtest, realised P&amp;L, or a recommendation.</p></div>';
  shell.dataset.statusAnnouncement = gate
    ? gate.title
    : !defined
      ? 'Define a setup to build a research evidence set'
      : pattern.noteTotal
        ? countLabel(pattern.noteTotal,'source note') + ' and ' +
          (sets.tier === 'subject'
            ? countLabel(pattern.total,'captured passage') + ' in headline-matched notes'
            : countLabel(pattern.total,'matching passage'))
        : 'No primary retrieved source notes for these inputs';
}
function render() {
  if (state.view !== 'briefing') pendingBriefFocus = null;
  document.querySelectorAll('.observation-retry').forEach(function (element) { element.remove(); });
  document.querySelector('#empty-state .empty-actions').hidden = false;
  syncExportAvailability();
  if (!observationsReady && currentStateNeedsObservations()) {
    renderObservationGate();
    requestObservationsForCurrentState(false);
    return;
  }
  setSortOptions();
  const records = filteredRecords();
  const ids = new Set(records.map(function (record) { return record.id; }));
  if (state.view === 'structure') state.selected = '';
  else if (!ids.has(state.selected)) state.selected = records.length ? records[0].id : '';
  setPressedStates();
  if (state.view === 'briefing') {
    renderIntelligenceBrief(records);
  } else if (state.view === 'structure') {
    renderStructureDesk(structureMatches());
  } else {
    renderTableHead();
    renderRows(records);
    renderContext(records);
  }
  renderActiveFilters();
  updateFacetCounts();
  renderOrphanedQueue();
  renderInspector();
  const orphanedCount = state.view === 'queue' ? Array.from(workflowItems.keys()).filter(function (id) { return !IDEA_BY_ID.has(id); }).length : 0;
  const briefStatusAnnouncement = state.view === 'briefing'
    ? document.getElementById('briefing-shell').dataset.statusAnnouncement || ''
    : state.view === 'structure'
      ? document.getElementById('structure-shell').dataset.statusAnnouncement || ''
      : '';
  document.getElementById('result-summary').textContent = briefStatusAnnouncement ||
    number(records.length) + ' ' + (isArticleView() ? 'article dossiers' : state.view === 'queue' ? 'current queued observations' : 'research observations') + (orphanedCount ? ' + ' + number(orphanedCount) + ' retained source snapshots' : '');
  updateHash();
  document.getElementById('announcer').textContent = briefStatusAnnouncement ||
    number(records.length) + ' results in ' + (state.view === 'briefing' ? 'Latest Brief' : state.view === 'research' ? 'Research Library' : state.view === 'queue' ? 'Decision Queue' : state.view === 'structure' ? 'Structure Desk' : 'Evidence Monitor');
}

function resetFilters() {
  state.query = '';
  state.sources.clear();
  state.revisions.clear();
  state.directions.clear();
  state.instruments.clear();
  state.managers.clear();
  state.quality.clear();
  state.publicationAccess.clear();
  state.content.clear();
  state.queueStatuses.clear();
  state.documentation = 'all';
  state.newOnly = false;
  state.range = 'all';
  state.coverage = 'all';
  state.briefLens = 'all';
  state.threadTopic = '';
  state.structureFocus = '';
  state.structureInstrument = '';
  state.structureDirection = 'any';
  state.structurePeriod = 'all';
  state.structureSlope = 'any';
  state.structureLevel = 'any';
  state.structureMacro = false;
  state.structureAnchor = '';
  state.structurePassage = '';
  state.structureShareable = false;
  state.structureControlsOpen = false;
  state.limit = PAGE_SIZE[state.view];
  document.getElementById('search').value = '';
  document.getElementById('manager-search').value = '';
  document.querySelectorAll('.manager-option').forEach(function (button) { button.hidden = false; });
  render();
}
function toggleSet(set,value) {
  if (set.has(value)) set.delete(value); else set.add(value);
}
function setInertState(element,hidden) {
  if (!element) return;
  element.inert = hidden;
  if (hidden) element.setAttribute('aria-hidden','true');
  else element.removeAttribute('aria-hidden');
}
function syncOverlayAccessibility() {
  const filtersNarrow = window.innerWidth <= 1020;
  const inspectorNarrow = window.innerWidth <= 1240;
  const filtersOpen = filtersNarrow && document.body.classList.contains('filters-open');
  const inspectorOpen = inspectorNarrow && document.body.classList.contains('inspector-open');
  const overlayOpen = filtersOpen || inspectorOpen;
  setInertState(document.querySelector('.app-header'),overlayOpen);
  setInertState(document.querySelector('.kpi-strip'),overlayOpen);
  setInertState(document.getElementById('main-panel'),overlayOpen);
  setInertState(
    document.getElementById('filter-rail'),
    filtersOpen ? false : filtersNarrow || inspectorOpen
  );
  setInertState(
    document.getElementById('inspector'),
    inspectorOpen ? false : inspectorNarrow || !state.inspector || filtersOpen
  );
  const filterRail = document.getElementById('filter-rail');
  const inspector = document.getElementById('inspector');
  [[filterRail,filtersOpen,'Research filters'],[inspector,inspectorOpen,'Research evidence']].forEach(function (entry) {
    if (entry[1]) {
      entry[0].setAttribute('role','dialog');
      entry[0].setAttribute('aria-modal','true');
      entry[0].setAttribute('aria-label',entry[2]);
    } else {
      entry[0].removeAttribute('role');
      entry[0].removeAttribute('aria-modal');
      entry[0].setAttribute('aria-label',entry[2]);
    }
  });
  document.getElementById('mobile-filter-button').setAttribute('aria-expanded',String(filtersOpen));
  document.getElementById('drawer-backdrop').setAttribute('aria-hidden',String(!overlayOpen));
  const inspectorControl = document.querySelector('[data-action="inspector"]');
  const inspectorActive = inspectorNarrow ? inspectorOpen : state.inspector;
  inspectorControl.classList.toggle('active',inspectorActive);
  inspectorControl.setAttribute('aria-pressed',String(inspectorActive));
  inspectorControl.setAttribute('aria-expanded',String(inspectorActive));
}
function closeDrawers() {
  document.body.classList.remove('filters-open','inspector-open');
  syncOverlayAccessibility();
}
function focusSelectedRow() {
  const selected = document.querySelector('[data-record-id="' + CSS.escape(state.selected) + '"]');
  const fallback = document.querySelector('[data-record-id][tabindex="0"]');
  (selected || fallback || document.querySelector('[data-action="inspector"]')).focus();
}
function openInspector(focusInside) {
  state.inspector = true;
  try { localStorage.setItem('nrt-inspector','visible'); } catch (_error) {}
  if (window.innerWidth <= 1240) {
    document.body.classList.remove('filters-open');
    document.body.classList.add('inspector-open');
  }
  setPressedStates();
  if (focusInside) {
    setTimeout(function () {
      const heading = document.querySelector('#inspector-content .record-title');
      if (heading) {
        heading.tabIndex = -1;
        heading.focus();
      } else document.getElementById('inspector-close').focus();
    },0);
  }
}
function selectRecord(id,focusRow,openDetails) {
  state.selected = id;
  document.querySelectorAll('[data-record-id]').forEach(function (row) {
    const selected = row.dataset.recordId === id;
    row.classList.toggle('selected',selected);
    row.setAttribute('aria-selected',String(selected));
    row.tabIndex = selected ? 0 : -1;
    if (selected && focusRow) row.focus({preventScroll:false});
  });
  renderInspector();
  updateHash();
  if (openDetails) openInspector(true);
}
function selectedArticle() {
  if (!state.selected) return null;
  if (isArticleView()) return ARTICLE_BY_ID.get(state.selected);
  const idea = IDEA_BY_ID.get(state.selected);
  return idea ? idea._article : null;
}
function moveSelection(delta) {
  let rows = Array.from(document.querySelectorAll('[data-record-id]'));
  if (!rows.length) return;
  let index = rows.findIndex(function (row) { return row.dataset.recordId === state.selected; });
  if (delta > 0 && index === rows.length - 1) {
    const records = filteredRecords();
    if (rows.length < records.length) {
      state.limit += PAGE_SIZE[state.view];
      renderRows(records);
      rows = Array.from(document.querySelectorAll('[data-record-id]'));
      index = rows.findIndex(function (row) { return row.dataset.recordId === state.selected; });
    }
  }
  index = Math.max(0,Math.min(rows.length - 1,(index < 0 ? 0 : index + delta)));
  selectRecord(rows[index].dataset.recordId,true,false);
}
function showToast(message) {
  if (/could not|unavailable|failed|invalid|missing|too large|limit reached|does not match/i.test(String(message || ''))) {
    showPersistentNotice(message);
  }
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(function () { toast.classList.remove('show'); },1800);
}
function showPersistentNotice(message,actionLabel,actionName) {
  const notice = document.getElementById('persistent-notice');
  document.getElementById('persistent-notice-text').textContent = String(message || 'Action required');
  const action = document.getElementById('persistent-notice-action');
  action.hidden = !actionLabel;
  action.textContent = actionLabel || '';
  action.dataset.noticeAction = actionName || '';
  notice.hidden = false;
}
function dismissPersistentNotice() {
  document.getElementById('persistent-notice').hidden = true;
  const action = document.getElementById('persistent-notice-action');
  action.hidden = true;
  action.dataset.noticeAction = '';
}
function showManualCopyDialog(value) {
  const dialog = document.getElementById('manual-copy-dialog');
  const textarea = document.getElementById('manual-copy-text');
  textarea.value = String(value || '');
  if (!dialog.open) dialog.showModal();
  textarea.focus();
  textarea.select();
}
async function copyText(value,message) {
  let copied = false;
  try {
    await navigator.clipboard.writeText(value);
    copied = true;
  } catch (_error) {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try { copied = document.execCommand('copy'); } catch (_copyError) { copied = false; }
    textarea.remove();
  }
  if (copied) showToast(message || 'Copied');
  else showManualCopyDialog(value);
  return copied;
}
function ideaCitation(idea) {
  const article = idea._article;
  return 'Navnoor Bawa, “' + article.title + ',” ' + sourceLabel(article.source) +
    ', ' + article.date + '. Observation ' + idea.id.toUpperCase() +
    '; parsed stance: ' + directionLabel(idea.direction) + '; dataset ' +
    String(SNAPSHOT.data_checksum || '').slice(0,12) + '; ' +
    bodyRevisionSummary(article) + ' ' + article.url;
}
function articleCitation(article) {
  return 'Navnoor Bawa, “' + article.title + ',” ' + sourceLabel(article.source) +
    ', ' + article.date + '; dataset ' + String(SNAPSHOT.data_checksum || '').slice(0,12) +
    '; ' + bodyRevisionSummary(article) + ' ' + article.url;
}
function decisionPacketText(idea,item) {
  const article = idea._article;
  const packet = packetCoverage(item);
  const checks = DILIGENCE_GATES.map(function (row) {
    return '- [' + (item.checks[row[0]] ? 'x' : ' ') + '] ' + row[1];
  }).join('\n');
  return [
    'NAVNOOR RESEARCH TERMINAL — HUMAN-ENTERED DECISION PACKET',
    'Packet coverage: ' + packet.completed + '/' + packet.total + ' (not approval or confidence)',
    'Status: ' + item.status + ' | Priority: ' + item.priority + ' | Analyst confidence: ' + item.confidence,
    'Decision owner: ' + (item.owner || 'Not assigned') + ' | Next review: ' + (item.review_date || 'Not set'),
    'Next action: ' + (item.next_action || 'Not recorded'),
    '',
    'SOURCE',
    ideaCitation(idea),
    'Published passage: ' + passageText(idea),
    'Publication access: ' + publicationAccessLabel(article),
    'Indexed coverage: ' + (article.content_status === 'full' ? 'Full text indexed' : 'Excerpt indexed'),
    'Body revision: ' + bodyRevisionSummary(article),
    '',
    'INVESTMENT CASE (analyst-entered)',
    'Variant thesis / edge: ' + (item.thesis || 'Not recorded'),
    'Contrary evidence: ' + (item.contrary || 'Not recorded'),
    'Catalyst / expected path: ' + (item.catalyst || 'Not recorded'),
    'Horizon: ' + (item.horizon || 'Not recorded'),
    'Valuation / entry / payoff: ' + (item.payoff || 'Not recorded'),
    'Falsifier / downside / exit: ' + (item.risk || 'Not recorded'),
    'Implementation / liquidity / funding: ' + (item.implementation || 'Not recorded'),
    'Mandate / portfolio fit: ' + (item.portfolio || 'Not recorded'),
    '',
    'SELF-ATTESTED CONTROL GATES',
    checks,
    '',
    'Tags: ' + (item.tags || 'None'),
    'Research memo: ' + (item.note || 'Not recorded'),
    'Updated: ' + (item.updated_at || 'Not recorded'),
    'Terminal boundary: no live positions, pricing, P&L, sizing, execution, portfolio risk, liquidity, counterparty, investor, or compliance data.'
  ].join('\n');
}
function confirmQueueStorageBoundary() {
  try {
    if (sessionStorage.getItem(QUEUE_BOUNDARY_ACK_KEY) === 'acknowledged') return true;
  } catch (_error) {}
  const accepted = window.confirm(
    'Tab-session queue warning\n\nDecision packets use plaintext storage scoped to this browser tab session and are discarded when the tab session closes. Do not enter confidential, personal, client, position, or regulated information. Exported queue backups are plaintext files.\n\nContinue and enable the tab-session queue?'
  );
  if (accepted) {
    try { sessionStorage.setItem(QUEUE_BOUNDARY_ACK_KEY,'acknowledged'); } catch (_error) {}
  }
  return accepted;
}
function toggleSaved(id) {
  if (!IDEA_BY_ID.has(id)) return;
  const active = document.activeElement;
  const restoreSave = active && active.closest && active.closest('[data-save-idea="' + CSS.escape(id) + '"]');
  const restoreRow = active && active.closest && active.closest('[data-record-id]');
  const previous = workflowItems.get(id);
  if (!previous && workflowItems.size >= MAX_QUEUE_ITEMS) {
    showToast('Decision queue limit reached; back up and archive older packets');
    return;
  }
  if (!previous && !confirmQueueStorageBoundary()) return;
  if (previous) {
    previous.status = previous.status === 'archived' ? 'review' : 'archived';
    previous.updated_at = new Date().toISOString();
  } else {
    const item = newWorkflowItem(id);
    if (!item) return;
    workflowItems.set(id,item);
  }
  const stored = persistWorkflow();
  if (!stored) {
    if (previous) {
      previous.status = previous.status === 'archived' ? 'review' : 'archived';
    } else workflowItems.delete(id);
    savedIdeas = new Set(workflowItems.keys());
    render();
    return;
  }
  showToast(previous ? (previous.status === 'archived' ? 'Decision packet archived' : 'Decision packet returned to review') : 'Added to review in this tab session');
  render();
  if (restoreSave) {
    const replacement = document.querySelector('[data-save-idea="' + CSS.escape(id) + '"]');
    if (replacement) replacement.focus();
    else document.querySelector('button[data-view="queue"]').focus();
  } else if (restoreRow) {
    focusSelectedRow();
  }
}
function clampWorkflowText(field,value) {
  const limit = WORKFLOW_TEXT_LIMITS[field] || 1000;
  const text = String(value || '');
  return text.length <= limit ? text : text.slice(0,limit - 1).trimEnd() + '…';
}
function boundedPacketNote(head,middle,tail) {
  const limit = WORKFLOW_TEXT_LIMITS.note;
  const full = head + middle + tail;
  if (full.length <= limit) return full;
  const marker = '\n\n[Additional retrieved-source detail omitted from this local packet; use the desk or CSV for the complete set.]';
  const room = Math.max(0,limit - head.length - tail.length - marker.length);
  return head + middle.slice(0,room).trimEnd() + marker + tail;
}
function deskPreMortem(pattern) {
  const notes = pattern.noteTotal;
  function coverage(count) {
    return number(count) + ' of ' + countLabel(notes,'source note') +
      (notes >= 3 ? ' (' + deskShare([count,notes]) + '%)' : '');
  }
  const gaps = [
    'stance was not stated in ' + coverage(
      (pattern.directions.filter(function (row) { return row[0] === 'unspecified'; })[0] || ['',0])[1]
    ),
    coverage(pattern.withOutcome) + (notes === 1 ? ' contains' : ' contain') + ' a source-stated outcome',
    coverage(pattern.withQuant) + (notes === 1 ? ' contains' : ' contain') + ' numeric context',
    coverage(pattern.withThesis) + (notes === 1 ? ' contains' : ' contain') + ' a source-stated edge'
  ];
  return 'EVIDENCE CHALLENGE — ' + countLabel(notes,'source note') + ' / ' +
    countLabel(pattern.total,'passage') + ' (' + deskReferenceClass() + '):\n' +
    gaps.map(function (line) { return '- ' + line; }).join('\n') +
    '\n\nAnalyst questions left deliberately unanswered: What would falsify the thesis? ' +
    'What live price, liquidity, funding, portfolio, and compliance checks are required? ' +
    'What evidence would distinguish a real analogue from a narrative resemblance?';
}
function openDecisionPacketFromDesk() {
  const ranked = structureMatches();
  if (!ranked.length) {
    showToast('Define a setup with primary retrieved evidence before creating a packet');
    return;
  }
  const pattern = structurePattern(ranked);
  const selectedGroup = pattern.groups.filter(function (group) {
    return group.id === state.structureAnchor;
  })[0];
  const selectedRow = selectedGroup && selectedGroup.rows.filter(function (row) {
    return row.idea.id === state.structurePassage;
  })[0];
  if (!selectedGroup || !selectedRow) {
    showToast('Anchor the exact source passage that should open this diligence packet');
    return;
  }
  const anchor = selectedRow.idea;
  const previous = workflowItems.get(anchor.id);
  if (!previous && workflowItems.size >= MAX_QUEUE_ITEMS) {
    showToast('Decision queue limit reached; back up and archive older packets');
    return;
  }
  if (!previous && !confirmQueueStorageBoundary()) return;
  const item = previous || newWorkflowItem(anchor.id);
  if (!item) return;
  const restore = previous
    ? {note:item.note,tags:item.tags,updated_at:item.updated_at}
    : null;
  // Generated evidence belongs only in the non-counted note field. Thesis,
  // falsifier/risk, catalyst, payoff, implementation, portfolio fit, owner,
  // review date, and diligence gates remain entirely analyst-authored.
  let seeded = 0;
  if (!hasValue(item.note)) {
    const packetGroups = [selectedGroup].concat(pattern.groups.filter(function (group) {
      return group.id !== selectedGroup.id;
    })).slice(0,5);
    const anchorWarnings = structurePassageWarnings(anchor);
    const anchorRates = state.structureMacro ? rateReading(anchor) : null;
    const anchorRateText = anchorRates
      ? '\nSelected anchor U.S. curve as of ' + formatDate(anchorRates.asOf) +
        ': 2Y ' + anchorRates.y2.toFixed(2) + '%, 10Y ' + anchorRates.y10.toFixed(2) +
        '%, 30Y ' + anchorRates.y30.toFixed(2) + '%, 10Y−2Y ' +
        anchorRates.slope.toFixed(2) + ' percentage points; official observation dated on or before ' +
        'the publication calendar date; not timestamp-aligned and may post-date a same-day article; ' +
        'not event/entry/position date'
      : '';
    const packetSources = packetGroups.map(function (group, index) {
      const article = group.article || {};
      return (index + 1) + '. ' + (group.id === selectedGroup.id ? '[ANCHOR] ' : '') +
        article.title + ' — ' + formatDate(article.date) +
        ' — ' + safeUrl(article.url);
    }).join('\n');
    const packetHead =
      'RESEARCH EVIDENCE SET opened from Structure Desk — ' + deskReferenceClass() +
      '\nSnapshot checksum: ' + String(SNAPSHOT.data_checksum || '') +
      '\nSelected source anchor: ' + String(selectedGroup.article.title || '') +
      '\nSelected source URL: ' + safeUrl(selectedGroup.article.url) +
      '\nSelected anchor publication access: ' + publicationAccessLabel(selectedGroup.article) +
      '\nSelected anchor indexed coverage: ' +
        (selectedGroup.article.content_status === 'full' ? 'Full text indexed' : 'Excerpt indexed') +
      '\nSelected anchor body revision: ' + bodyRevisionSummary(selectedGroup.article) +
      '\nSelected anchor review flags: ' +
        (anchorWarnings.length ? anchorWarnings.join('; ') : 'No automated flag; inspect exact passage') +
      '\nSelected exact passage: ' + passageText(anchor) +
      anchorRateText +
      '\n\nPRIMARY RETRIEVED SOURCE NOTES — ' + number(packetGroups.length) + ' OF ' +
        number(pattern.noteTotal) + '; ANCHOR FIRST; FULL SET REMAINS IN DESK / CSV\n';
    const packetMiddle = packetSources +
      '\n\n' + deskNoteLines(pattern).map(function (row) {
        return row[0].toUpperCase() + '\n' + row[1];
      }).join('\n\n');
    const packetTail = (state.structureMacro
        ? '\n\nOPTIONAL MACRO PROVENANCE\n' + rateSourceText() : '') +
      '\n\nPACKET EVIDENCE SCOPE\nThis packet cites ' + number(packetGroups.length) +
        ' of ' + number(pattern.noteTotal) +
        ' retrieved ' + (pattern.noteTotal === 1 ? 'source note' : 'source notes') +
        '; inspect the desk or CSV for the complete set.\n\n' +
      deskPreMortem(pattern);
    item.note = boundedPacketNote(packetHead,packetMiddle,packetTail);
    seeded += 1;
  }
  if (!hasValue(item.tags)) {
    item.tags = clampWorkflowText('tags','research evidence: ' + deskReferenceClass());
    seeded += 1;
  }
  item.updated_at = new Date().toISOString();
  workflowItems.set(anchor.id,item);
  if (!persistWorkflow()) {
    if (restore) Object.assign(item,restore);
    else workflowItems.delete(anchor.id);
    savedIdeas = new Set(workflowItems.keys());
    render();
    return;
  }
  savedIdeas = new Set(workflowItems.keys());
  markMeaningfulNavigation();
  state.view = 'queue';
  state.selected = anchor.id;
  state.limit = PAGE_SIZE.queue;
  render();
  showToast(seeded
    ? 'Diligence packet opened with source evidence; analyst thesis and risk remain blank'
    : 'Decision packet already open; existing entries were left untouched');
}
function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\s]*[=+\-@]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g,'""') + '"';
}
function exportCsv() {
  if (!isArticleView() && !observationsReady) {
    showToast('Export waits for the verified evidence archive');
    requestObservationsForCurrentState(false);
    return;
  }
  const records = state.view === 'structure'
    ? structureArticleGroups(structureMatches())
    : filteredRecords();
  let rows;
  if (state.view === 'structure') {
    // The interface clusters passages by source note; the export must preserve
    // that same unit of evidence instead of quietly reverting to passage-level
    // rows that imply a larger independent sample.
    const ranked = records;
    rows = [[
      'Retrieval order (same tier then publication date)','Retrieval tier',
      'Why it matched','Date','Included passage count',
      'Instrument mentions','Parsed stances','Underlyings',
      'Numeric anchors','Source-stated edges','Outcomes recorded at source',
      'Body revision','Indexed coverage','Review required','Passage truncated',
      'Related later notes via article topic (not outcome)',
      'U.S. 2Y observation by publication date','U.S. 10Y observation by publication date',
      'U.S. 30Y observation by publication date','U.S. 10Y-2Y observation by publication date',
      'Curve as of','Macro source URL','Fixed-universe curve band','Fixed-universe 10Y band',
      'Authored entity mentions','Included source passages','Article','Publication channel','URL'
    ]].concat(ranked.map(function (group, index) {
      const article = group.article;
      const facts = structureGroupFacts(group);
      const firstIdea = group.rows[0].idea;
      const rates = state.structureMacro ? rateReading(firstIdea) : null;
      function unique(field) {
        return Array.from(new Set(group.rows.map(function (row) {
          return String(row.idea[field] || '').trim();
        }).filter(Boolean))).join('\n');
      }
      return [
        index + 1,
        group.tier,
        Array.from(group.reasons).join('; '),
        article.date,
        group.rows.length,
        facts.instruments.map(instrumentLabel).join('; '),
        facts.directions.map(directionLabel).join('; ') || 'Not stated',
        facts.underlyings.join('; '),
        unique('quant'),
        unique('thesis'),
        unique('outcome'),
        bodyRevisionLabel(article),
        article.content_status,
        facts.review ? 'yes' : 'no',
        facts.truncated ? 'yes' : 'no',
        observationFollowUps(firstIdea).length,
        rates ? rates.y2 : '',
        rates ? rates.y10 : '',
        rates ? rates.y30 : '',
        rates ? rates.slope : '',
        rates ? rates.asOf : '',
        rates ? String((RATE_CONTEXT.source || {}).page_url || '') : '',
        rates ? rateBandLabel('slope',rates.slopeBand) : '',
        rates ? rateBandLabel('level',rates.levelBand) : '',
        unique('manager_raw'),
        group.rows.map(function (row) { return passageText(row.idea); }).join('\n\n'),
        article.title,
        sourceLabel(article.source),
        article.url
      ];
    }));
  } else if (state.view === 'research') {
    rows = [['Date','Publication channel','Article','Subtitle','Research observations','Publication access','Indexed coverage','Body revision','Body source revision','Observed source revision','URL']].concat(records.map(function (article) {
      return [article.date,sourceLabel(article.source),article.title,article.subtitle,article.trade_count,article.publication_access,article.content_status,article.body_revision_status,article.source_updated_at,article.observed_source_updated_at,article.url];
    }));
  } else {
    rows = [['Date','Parsed stance','Instruments','Underlying','Mentioned entity','Original entity mention','Source passage','Extracted edge / thesis','Numeric context','Reported outcome','Documentation coverage','Review flags','Publication access','Indexed coverage','Body revision','Body source revision','Observed source revision','Queue status','Priority','Decision owner','Next review','Analyst confidence','Next action','Variant thesis','Contrary evidence','Catalyst','Horizon','Valuation / payoff','Falsifier / downside / exit','Implementation / liquidity / funding','Mandate / portfolio fit','Source reviewed','Independent evidence','Live market / valuation checked','Liquidity / funding checked','Portfolio risk checked','Compliance checked','Packet coverage','Local tags','Local memo','Packet updated','Article','Publication channel','URL']].concat(records.map(function (idea) {
      const article = idea._article;
      const workflow = workflowItems.get(idea.id) || {};
      const packet = packetCoverage(workflowItems.get(idea.id));
      const flags = [idea.negation_risk ? 'negation-risk' : '',idea.reference_line ? 'reference-line' : '',idea.description_truncated ? 'truncated' : '',article.body_revision_status === 'prior' ? 'prior-revision' : '',article.body_revision_status === 'unverified' ? 'unverified-revision' : ''].filter(Boolean).join('; ');
      const checks = workflow.checks || {};
      return [article.date,directionLabel(idea.direction),idea.instruments.map(instrumentLabel).join('; '),idea.underlying,idea.manager,idea.manager_raw,passageText(idea),idea.thesis,idea.quant,idea.outcome,idea.documentation_score + '/5',flags,article.publication_access,article.content_status,article.body_revision_status,article.source_updated_at,article.observed_source_updated_at,workflow.status || '',workflow.priority || '',workflow.owner || '',workflow.review_date || '',workflow.confidence || '',workflow.next_action || '',workflow.thesis || '',workflow.contrary || '',workflow.catalyst || '',workflow.horizon || '',workflow.payoff || '',workflow.risk || '',workflow.implementation || '',workflow.portfolio || '',checks.source ? 'yes' : '',checks.independent ? 'yes' : '',checks.market ? 'yes' : '',checks.liquidity ? 'yes' : '',checks.portfolio ? 'yes' : '',checks.compliance ? 'yes' : '',workflow.id ? packet.completed + '/' + packet.total : '',workflow.tags || '',workflow.note || '',workflow.updated_at || '',article.title,sourceLabel(article.source),article.url];
    }));
  }
  const csv = '\uFEFF' + rows.map(function (row) { return row.map(csvCell).join(','); }).join('\r\n');
  const blob = new Blob([csv],{type:'text/csv;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'navnoor-research-' + state.view + '-' + new Date().toISOString().slice(0,10) + '.csv';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(function () { URL.revokeObjectURL(url); },0);
  showToast(number(records.length) + ' records exported');
}

function applyPreset(name) {
  state.view = 'ideas';
  state.sources.clear();
  state.revisions.clear();
  state.directions.clear();
  state.instruments.clear();
  state.managers.clear();
  state.quality.clear();
  state.publicationAccess.clear();
  state.content.clear();
  state.queueStatuses.clear();
  state.query = '';
  state.range = 'all';
  state.coverage = 'all';
  state.documentation = 'all';
  state.newOnly = false;
  if (name === 'recent') { state.range = '90d'; state.documentation = 'triage'; }
  if (name === 'new') state.newOnly = true;
  if (name === 'rv') {
    state.directions.add('arbitrage/relative value');
    state.quality.add('quant');
    state.documentation = 'strong';
  }
  if (name === 'entity') state.quality.add('manager');
  if (name === 'directional') ['long','short','arbitrage/relative value','long/short'].forEach(function (value) { state.directions.add(value); });
  if (name === 'documented') state.documentation = 'documented';
  state.sort = 'newest';
  state.selected = '';
  state.threadTopic = '';
  state.limit = PAGE_SIZE.ideas;
  document.getElementById('search').value = '';
  renderObservationAwareNavigation('entry');
}
function markReviewedThroughLatest() {
  try {
    const currentIds = ARTICLES.map(function (article) { return article.id; });
    localStorage.setItem(REVIEWED_ARTICLE_IDS_KEY,JSON.stringify(currentIds));
    reviewedArticleIds = new Set(currentIds);
    reviewBaselineExists = true;
    state.newOnly = false;
    render();
    showToast(number(currentIds.length) + ' current research notes marked reviewed');
  } catch (_error) {
    showToast('Review baseline could not be saved in this browser');
  }
}
function downloadLocalFile(blob,filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(function () { URL.revokeObjectURL(url); },0);
}
function backupUnreadableWorkflow() {
  if (!unreadableWorkflowRaw) {
    showToast('No readable raw queue record is available to back up');
    return;
  }
  downloadLocalFile(
    new Blob([unreadableWorkflowRaw],{type:'text/plain;charset=utf-8'}),
    'navnoor-unreadable-queue-' + new Date().toISOString().slice(0,10) + '.txt'
  );
  showToast('Unreadable stored queue record backed up as plaintext');
}
function clearQueueStorageKeys() {
  sessionStorage.removeItem(WORKFLOW_KEY);
  sessionStorage.removeItem(RESTORE_ROLLBACK_KEY);
  try {
    clearLegacyLocalWorkflowKeys();
    return true;
  } catch (_error) {
    legacyStorageCheckUnavailable = true;
    return false;
  }
}
function clearUnreadableWorkflow() {
  if (!window.confirm('Discard the unreadable stored queue record? This cannot be undone unless you backed up the raw record first.')) return;
  try {
    if (unreadableWorkflowLocation === 'session') sessionStorage.removeItem(WORKFLOW_KEY);
    else clearLegacyLocalWorkflowKeys();
    workflowLoadBlocked = false;
    workflowStorageUnavailable = false;
    workflowStorageDirty = false;
    unreadableWorkflowRaw = '';
    unreadableWorkflowLocation = '';
    workflowItems = new Map();
    savedIdeas = new Set();
    lastPersistedWorkflow = '[]';
    persistWorkflow();
    syncWorkflowStorageAlert();
    dismissPersistentNotice();
    render();
    showToast('Unreadable stored queue discarded');
  } catch (_error) {
    showToast('Unreadable queue record could not be cleared');
  }
}
function clearTabQueue() {
  if (!window.confirm('Clear every decision packet in this tab session and any legacy persistent queue? Export a backup first if any record must be retained.')) return;
  try {
    const legacyCleared = clearQueueStorageKeys();
    sessionStorage.removeItem(QUEUE_BOUNDARY_ACK_KEY);
    workflowItems = new Map();
    savedIdeas = new Set();
    lastRestoreWorkflowItems = null;
    workflowLoadBlocked = false;
    workflowStorageUnavailable = false;
    workflowStorageDirty = false;
    unreadableWorkflowRaw = '';
    unreadableWorkflowLocation = '';
    lastPersistedWorkflow = '[]';
    persistWorkflow();
    syncWorkflowStorageAlert();
    render();
    if (legacyCleared) showToast('Tab-session decision queue and legacy storage cleared');
    else showPersistentNotice('The tab-session queue was cleared, but legacy origin-wide storage could not be accessed. Clear old site data in browser settings.');
  } catch (_error) {
    showToast('Tab-session decision queue could not be cleared');
  }
}
function backupQueue() {
  const payload = {
    schema_version:2,
    exported_at:new Date().toISOString(),
    data_checksum:String(SNAPSHOT.data_checksum || ''),
    items:Array.from(workflowItems.values())
  };
  downloadLocalFile(
    new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}),
    'navnoor-decision-queue-' + new Date().toISOString().slice(0,10) + '.json'
  );
  showToast(number(workflowItems.size) + ' queue records backed up');
}
function cloneWorkflowMap(source) {
  const clone = new Map();
  source.forEach(function (value,key) {
    const item = normalizeWorkflowItem(JSON.parse(JSON.stringify(value)));
    if (item) clone.set(key,item);
  });
  return clone;
}
function undoLastQueueRestore() {
  if (!lastRestoreWorkflowItems) {
    showToast('No queue restore is available to undo');
    return;
  }
  const currentItems = workflowItems;
  workflowItems = cloneWorkflowMap(lastRestoreWorkflowItems);
  if (!persistWorkflow()) {
    workflowItems = currentItems;
    savedIdeas = new Set(workflowItems.keys());
    return;
  }
  lastRestoreWorkflowItems = null;
  try { sessionStorage.removeItem(RESTORE_ROLLBACK_KEY); } catch (_error) {}
  dismissPersistentNotice();
  render();
  showToast('Queue restore undone');
}
function restoreQueueFile(file) {
  if (!file || file.size > 2000000) { showToast('Queue backup is missing or too large'); return; }
  if (!confirmQueueStorageBoundary()) return;
  const reader = new FileReader();
  reader.onload = function () {
    try {
      const payload = JSON.parse(String(reader.result || ''));
      if (!payload || ![1,2].includes(payload.schema_version) || !Array.isArray(payload.items)) throw new Error('invalid schema');
      const previousItems = cloneWorkflowMap(workflowItems);
      const restored = cloneWorkflowMap(workflowItems);
      let added = 0;
      let updated = 0;
      let skipped = 0;
      payload.items.slice(0,MAX_QUEUE_ITEMS).forEach(function (value) {
        const item = normalizeWorkflowItem(value);
        if (!item) { skipped += 1; return; }
        const existing = restored.get(item.id);
        if (!existing && restored.size >= MAX_QUEUE_ITEMS) { skipped += 1; return; }
        if (!existing) {
          restored.set(item.id,item);
          added += 1;
        } else if (item.updated_at && (!existing.updated_at || item.updated_at > existing.updated_at)) {
          restored.set(item.id,item);
          updated += 1;
        } else {
          skipped += 1;
        }
      });
      if (!added && !updated) {
        showToast('Queue backup contains no newer records to import');
        return;
      }
      const snapshotDiffers = Boolean(payload.data_checksum && payload.data_checksum !== String(SNAPSHOT.data_checksum || ''));
      if (snapshotDiffers && !window.confirm(
        'Source snapshot mismatch\n\nThis backup was created against a different research dataset. Cancel is safest. Continue only if you intend to retain its bounded source snapshots.'
      )) {
        showToast('Queue import cancelled because the source snapshot differs');
        return;
      }
      const preview = 'Queue import preview\n\nNew packets: ' + added + '\nUpdated packets: ' + updated + '\nSkipped or older packets: ' + skipped + (snapshotDiffers ? '\nSource snapshot: DIFFERENT' : '\nSource snapshot: matching') + '\n\nThe current queue will be retained as a tab-scoped rollback across reloads. Export a separate plaintext backup for long-term retention. Continue?';
      if (!window.confirm(preview)) {
        showToast('Queue import cancelled after preview');
        return;
      }
      try {
        sessionStorage.setItem(
          RESTORE_ROLLBACK_KEY,
          JSON.stringify(Array.from(previousItems.values()))
        );
      } catch (_error) {
        showToast('Queue import stopped because a durable tab rollback could not be preserved');
        return;
      }
      workflowItems = restored;
      lastRestoreWorkflowItems = previousItems;
      if (persistWorkflow()) {
        render();
        showPersistentNotice(number(added + updated) + ' queue packets imported. The previous tab queue can be restored after reload until the next import or clear.','Undo import','undo-restore');
      } else {
        workflowItems = previousItems;
        lastRestoreWorkflowItems = null;
        savedIdeas = new Set(workflowItems.keys());
      }
    } catch (_error) {
      showToast('Queue backup could not be validated');
    }
  };
  reader.onerror = function () { showToast('Queue backup could not be read'); };
  reader.readAsText(file);
}

document.getElementById('table-body').addEventListener('click',function (event) {
  if (event.target.closest('a')) return;
  const row = event.target.closest('[data-record-id]');
  if (row) {
    markMeaningfulNavigation();
    selectRecord(row.dataset.recordId,false,true);
  }
});
document.getElementById('table-body').addEventListener('dblclick',function (event) {
  const row = event.target.closest('[data-record-id]');
  if (!row || event.target.closest('a')) return;
  const article = selectedArticle();
  if (article) window.open(safeUrl(article.url),'_blank','noopener,noreferrer');
});
document.getElementById('table-body').addEventListener('keydown',function (event) {
  const row = event.target.closest('[data-record-id]');
  if (!row) return;
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    event.stopPropagation();
    markMeaningfulNavigation();
    selectRecord(row.dataset.recordId,false,true);
  }
});
document.getElementById('table-head').addEventListener('click',function (event) {
  const button = event.target.closest('[data-sort]');
  if (!button) return;
  state.sort = button.dataset.sort === 'newest'
    ? (state.sort === 'newest' ? 'oldest' : 'newest')
    : button.dataset.sort;
  state.limit = PAGE_SIZE[state.view];
  render();
  const replacement = document.querySelector('[data-sort="' + button.dataset.sort + '"]');
  if (replacement) replacement.focus();
});

document.addEventListener('click',function (event) {
  if (event.target.closest('[data-dismiss-notice]')) {
    dismissPersistentNotice();
    return;
  }
  const noticeAction = event.target.closest('[data-notice-action]');
  if (noticeAction && noticeAction.dataset.noticeAction === 'undo-restore') {
    undoLastQueueRestore();
    return;
  }
  if (noticeAction && noticeAction.dataset.noticeAction === 'backup-queue') {
    backupQueue();
    return;
  }
  const retryObservationButton = event.target.closest('[data-retry-observations]');
  if (retryObservationButton) {
    queueObservationResultFocus();
    document.querySelectorAll('[data-retry-observations]').forEach(function (button) { button.disabled = true; });
    requestObservationsForCurrentState(true);
    render();
    focusObservationGate();
    return;
  }
  const briefJump = event.target.closest('[data-brief-jump]');
  if (briefJump) {
    const target = document.getElementById(briefJump.dataset.briefJump);
    if (target) {
      target.scrollIntoView({behavior:'smooth',block:'start'});
      target.tabIndex = -1;
      target.focus({preventScroll:true});
    }
    return;
  }
  const copyBrief = event.target.closest('[data-copy-brief]');
  if (copyBrief) {
    const article = ARTICLE_BY_ID.get(copyBrief.dataset.copyBrief);
    if (article && article.brief) copyText(articleBriefText(article),'Institutional brief copied with source provenance');
    return;
  }
  if (event.target.closest('[data-print-brief]')) {
    window.print();
    return;
  }
  const retryBriefs = event.target.closest('[data-retry-briefs]');
  if (retryBriefs) {
    retryBriefs.disabled = true;
    pendingBriefFocus = {kind:state.view === 'briefing' && state.briefLens !== 'all' ? 'lens' : 'article',value:state.view === 'briefing' && state.briefLens !== 'all' ? state.briefLens : state.selected};
    retryBriefArchive().then(function () { render(); }).catch(function () {
      showToast('Exact article dossiers are still unavailable');
      render();
    });
    return;
  }
  const threadTopic = event.target.closest('[data-thread-topic]');
  if (threadTopic) {
    const article = ARTICLE_BY_ID.get(state.selected);
    const row = threadArticleRow(article);
    if (!row || !row.topics.includes(threadTopic.dataset.threadTopic)) return;
    markMeaningfulNavigation();
    state.threadTopic = threadTopic.dataset.threadTopic;
    pendingBriefFocus = {kind:'thread',value:state.threadTopic};
    render();
    return;
  }
  const threadLoad = event.target.closest('[data-thread-load]');
  if (threadLoad) {
    const prior = ARTICLE_BY_ID.get(threadLoad.dataset.threadLoad);
    if (!prior) return;
    const request = {
      articleId:prior.id,selectedId:state.selected,topic:state.threadTopic
    };
    threadComparisonRequest = request;
    pendingBriefFocus = {kind:'thread-comparison',value:prior.id};
    render();
    document.getElementById('announcer').textContent = 'Loading and verifying the exact prior article dossier';
    ensureArticleBrief(prior).then(function (briefValue) {
      if (threadComparisonRequest !== request) return;
      threadComparisonRequest = null;
      const contextMatches = state.view === 'briefing' &&
        state.selected === request.selectedId && state.threadTopic === request.topic;
      if (!contextMatches) return;
      pendingBriefFocus = {kind:'thread-comparison',value:prior.id};
      render();
      const completion = briefValue
        ? 'Exact prior passage comparison loaded and verified'
        : 'Exact prior dossier could not be verified; retry is available';
      document.getElementById('announcer').textContent = completion;
      if (!briefValue) showToast('Exact prior dossier could not be verified');
    });
    return;
  }
  const threadArticle = event.target.closest('[data-thread-article]');
  if (threadArticle) {
    const topic = THREADS.topics && THREADS.topics[state.threadTopic];
    if (!topic || !topic.article_ids.includes(threadArticle.dataset.threadArticle)) return;
    markMeaningfulNavigation();
    state.view = 'briefing';
    state.briefLens = 'all';
    state.selected = threadArticle.dataset.threadArticle;
    pendingBriefFocus = {kind:'article',value:state.selected};
    render();
    document.getElementById('briefing-shell').scrollTop = 0;
    return;
  }
  const briefLens = event.target.closest('[data-brief-lens]');
  if (briefLens) {
    markMeaningfulNavigation();
    state.view = 'briefing';
    state.briefLens = VALID_BRIEF_LENSES.has(briefLens.dataset.briefLens) ? briefLens.dataset.briefLens : 'all';
    pendingBriefFocus = {kind:'lens',value:state.briefLens};
    state.selected = '';
    state.threadTopic = '';
    state.sort = 'newest';
    state.limit = PAGE_SIZE.briefing;
    render();
    document.getElementById('briefing-shell').scrollTop = 0;
    return;
  }
  const briefArticle = event.target.closest('[data-brief-article]');
  if (briefArticle) {
    markMeaningfulNavigation();
    state.view = 'briefing';
    state.selected = briefArticle.dataset.briefArticle;
    state.threadTopic = '';
    pendingBriefFocus = {kind:'article',value:state.selected};
    render();
    document.getElementById('briefing-shell').scrollTop = 0;
    return;
  }
  const articleDossier = event.target.closest('[data-article-dossier]');
  if (articleDossier) {
    markMeaningfulNavigation();
    state.view = 'research';
    state.selected = articleDossier.dataset.articleDossier;
    state.threadTopic = '';
    state.sort = 'newest';
    state.limit = PAGE_SIZE.research;
    renderObservationAwareNavigation('inspector');
    return;
  }
  const structureChip = event.target.closest(
    '[data-structure-instrument],[data-structure-direction],[data-structure-period],'
    + '[data-structure-focus],[data-structure-slope],[data-structure-level],[data-structure-macro]'
  );
  if (structureChip) {
    // Desk inputs can be proprietary. Local refinements replace any shared
    // hash instead of leaving the old question one Back navigation away.
    nextHistoryMode = 'replace';
    state.view = 'structure';
    if (structureChip.hasAttribute('data-structure-instrument')) {
      state.structureInstrument = structureChip.dataset.structureInstrument;
    }
    if (structureChip.hasAttribute('data-structure-direction')) {
      state.structureDirection = structureChip.dataset.structureDirection || 'any';
    }
    if (structureChip.hasAttribute('data-structure-period')) {
      state.structurePeriod = structureChip.dataset.structurePeriod || 'all';
    }
    if (structureChip.hasAttribute('data-structure-focus')) {
      state.structureFocus = structureChip.dataset.structureFocus;
    }
    if (structureChip.hasAttribute('data-structure-slope')) {
      state.structureSlope = structureChip.dataset.structureSlope || 'any';
    }
    if (structureChip.hasAttribute('data-structure-level')) {
      state.structureLevel = structureChip.dataset.structureLevel || 'any';
    }
    if (structureChip.hasAttribute('data-structure-macro')) {
      state.structureMacro = structureChip.dataset.structureMacro === '1';
      if (!state.structureMacro) {
        state.structureSlope = 'any';
        state.structureLevel = 'any';
      }
    }
    state.structureAnchor = '';
    state.structurePassage = '';
    state.structureShareable = false;
    state.limit = PAGE_SIZE.structure;
    renderObservationAwareNavigation('entry');
    return;
  }
  const structurePassage = event.target.closest('[data-structure-passage]');
  if (structurePassage) {
    const idea = IDEA_BY_ID.get(structurePassage.dataset.structurePassage);
    const article = idea && idea._article;
    if (!idea || !article || !article.id) return;
    state.structureAnchor = article.id;
    state.structurePassage = idea.id;
    state.structureShareable = false;
    render();
    const replacement = document.querySelector('[data-structure-passage="' +
      CSS.escape(state.structurePassage) + '"]');
    if (replacement) replacement.focus();
    return;
  }
  const openPacket = event.target.closest('[data-structure-open-packet]');
  if (openPacket) {
    openDecisionPacketFromDesk();
    return;
  }
  const copyNote = event.target.closest('[data-structure-copy-note]');
  if (copyNote) {
    const ranked = structureMatches();
    const markdown = deskNoteMarkdown(structurePattern(ranked),ranked);
    if (markdown) copyText(markdown,'Desk note copied as a memo');
    return;
  }
  const refine = event.target.closest('[data-structure-refine]');
  if (refine) {
    state.structureControlsOpen = !state.structureControlsOpen;
    render();
    const reopened = document.querySelector('[data-structure-refine]');
    if (reopened) reopened.focus();
    return;
  }
  const structureMore = event.target.closest('[data-structure-more]');
  if (structureMore) {
    state.limit += PAGE_SIZE.structure;
    render();
    const replacement = document.querySelector('[data-structure-more]');
    if (replacement) replacement.focus();
    return;
  }
  const view = event.target.closest('button[data-view]');
  if (view) {
    markMeaningfulNavigation();
    state.view = view.dataset.view;
    state.sort = 'newest';
    state.selected = '';
    state.threadTopic = '';
    state.limit = PAGE_SIZE[state.view];
    renderObservationAwareNavigation('entry');
    return;
  }
  const kpiView = event.target.closest('[data-kpi-view]');
  if (kpiView) {
    markMeaningfulNavigation();
    state.view = kpiView.dataset.kpiView;
    state.sort = 'newest';
    state.selected = '';
    state.threadTopic = '';
    state.limit = PAGE_SIZE[state.view];
    renderObservationAwareNavigation('entry');
    return;
  }
  const preset = event.target.closest('[data-preset],[data-kpi-preset]');
  if (preset) {
    applyPreset(preset.dataset.preset || preset.dataset.kpiPreset);
    return;
  }
  const briefRecord = event.target.closest('[data-brief-record]');
  if (briefRecord) {
    markMeaningfulNavigation();
    state.view = 'ideas';
    state.selected = briefRecord.dataset.briefRecord;
    state.limit = PAGE_SIZE.ideas;
    renderObservationAwareNavigation('inspector');
    return;
  }
  const kpiQuality = event.target.closest('[data-kpi-quality]');
  if (kpiQuality) {
    state.view = 'ideas';
    state.quality.add(kpiQuality.dataset.kpiQuality);
    state.limit = PAGE_SIZE.ideas;
    renderObservationAwareNavigation('entry');
    return;
  }
  const facet = event.target.closest('[data-filter]');
  if (facet) {
    const name = facet.dataset.filter;
    const value = facet.dataset.value;
    if (name === 'range') state.range = value;
    else if (name === 'coverage') state.coverage = value;
    else if (name === 'documentation') state.documentation = value;
    else {
      const map = {
        source:state.sources,revision:state.revisions,direction:state.directions,instrument:state.instruments,
        manager:state.managers,quality:state.quality,access:state.publicationAccess,content:state.content,'queue-status':state.queueStatuses
      };
      if (map[name]) toggleSet(map[name],value);
    }
    state.limit = PAGE_SIZE[state.view];
    render();
    return;
  }
  const clearFacet = event.target.closest('[data-clear-facet]');
  if (clearFacet) {
    const map = {
      source:state.sources,revision:state.revisions,direction:state.directions,instrument:state.instruments,
      manager:state.managers,quality:state.quality,access:state.publicationAccess,content:state.content
    };
    if (map[clearFacet.dataset.clearFacet]) map[clearFacet.dataset.clearFacet].clear();
    state.limit = PAGE_SIZE[state.view];
    render();
    return;
  }
  if (event.target.closest('[data-clear-documentation]')) {
    state.documentation = 'all';
    state.limit = PAGE_SIZE[state.view];
    render();
    return;
  }
  if (event.target.closest('[data-clear-queue-status]')) {
    state.queueStatuses.clear();
    state.limit = PAGE_SIZE[state.view];
    render();
    return;
  }
  const remove = event.target.closest('[data-remove-filter]');
  if (remove) {
    const chipIndex = Array.from(document.querySelectorAll('[data-remove-filter]')).indexOf(remove);
    const name = remove.dataset.removeFilter;
    const map = {
      source:state.sources,revision:state.revisions,direction:state.directions,instrument:state.instruments,
      manager:state.managers,quality:state.quality,access:state.publicationAccess,content:state.content,'queue-status':state.queueStatuses
    };
    if (map[name]) map[name].delete(remove.dataset.value);
    if (name === 'range') state.range = 'all';
    if (name === 'coverage') state.coverage = 'all';
    if (name === 'documentation') state.documentation = 'all';
    if (name === 'new') state.newOnly = false;
    state.limit = PAGE_SIZE[state.view];
    render();
    const remainingChips = document.querySelectorAll('[data-remove-filter]');
    if (remainingChips.length) remainingChips[Math.min(chipIndex,remainingChips.length - 1)].focus();
    else document.getElementById('clear-filters').focus();
    return;
  }
  const related = event.target.closest('[data-related-idea]');
  if (related) {
    markMeaningfulNavigation();
    state.view = 'ideas';
    state.directions.clear();
    state.instruments.clear();
    state.managers.clear();
    state.quality.clear();
    state.publicationAccess.clear();
    state.documentation = 'all';
    state.selected = related.dataset.relatedIdea;
    state.sort = 'newest';
    state.limit = PAGE_SIZE.ideas;
    render();
    openInspector(false);
    const heading = document.querySelector('#inspector-content .record-title');
    if (heading) {
      heading.tabIndex = -1;
      heading.focus();
    }
    return;
  }
  const emptyAction = event.target.closest('[data-empty-action]');
  if (emptyAction) {
    if (emptyAction.dataset.emptyAction === 'clear') resetFilters();
    else {
      state.view = 'ideas';
      resetFilters();
    }
    return;
  }
  const save = event.target.closest('[data-save-idea]');
  if (save) {
    toggleSaved(save.dataset.saveIdea);
    return;
  }
  const copyPacket = event.target.closest('[data-copy-packet]');
  if (copyPacket) {
    const idea = IDEA_BY_ID.get(copyPacket.dataset.copyPacket);
    const item = workflowItems.get(copyPacket.dataset.copyPacket);
    if (idea && item) copyText(decisionPacketText(idea,item),'Decision packet copied');
    return;
  }
  const copyIdea = event.target.closest('[data-copy-citation]');
  if (copyIdea) {
    const idea = IDEA_BY_ID.get(copyIdea.dataset.copyCitation);
    if (idea) copyText(ideaCitation(idea),'Idea citation copied');
    return;
  }
  const copyArticle = event.target.closest('[data-copy-article]');
  if (copyArticle) {
    const article = ARTICLE_BY_ID.get(copyArticle.dataset.copyArticle);
    if (article) copyText(articleCitation(article),'Article citation copied');
    return;
  }
  const action = event.target.closest('[data-action]');
  if (action) {
    if (action.dataset.action === 'density') {
      state.density = state.density === 'compact' ? 'comfortable' : 'compact';
      try { localStorage.setItem('nrt-density',state.density); } catch (_error) {}
      render();
    } else if (action.dataset.action === 'copy-view') {
      if (state.view === 'structure') state.structureShareable = true;
      updateHash(true);
      copyText(location.href,state.view === 'structure' && state.structureFocus
        ? 'Shareable view copied with the non-confidential desk setup'
        : state.query ? 'Shareable view copied with search phrase' : 'Shareable view copied');
    } else if (action.dataset.action === 'export') {
      exportCsv();
    } else if (action.dataset.action === 'backup-queue') {
      backupQueue();
    } else if (action.dataset.action === 'restore-queue') {
      if (confirmQueueStorageBoundary()) document.getElementById('queue-restore-input').click();
    } else if (action.dataset.action === 'backup-raw-storage') {
      backupUnreadableWorkflow();
    } else if (action.dataset.action === 'clear-unreadable-storage') {
      clearUnreadableWorkflow();
    } else if (action.dataset.action === 'clear-queue') {
      clearTabQueue();
    } else if (action.dataset.action === 'retry-storage') {
      if (persistWorkflow()) showToast('Decision queue saved in this tab session');
    } else if (action.dataset.action === 'mark-reviewed') {
      markReviewedThroughLatest();
    } else if (action.dataset.action === 'inspector') {
      if (window.innerWidth <= 1240) {
        const wasOpen = document.body.classList.contains('inspector-open');
        if (wasOpen) {
          closeDrawers();
          action.focus();
        } else {
          openInspector(true);
        }
      } else {
        state.inspector = !state.inspector;
        try { localStorage.setItem('nrt-inspector',state.inspector ? 'visible' : 'hidden'); } catch (_error) {}
        if (!state.inspector) document.body.classList.remove('inspector-open');
        render();
      }
    }
  }
});

document.addEventListener('change',function (event) {
  const selectControl = event.target.closest('[data-workflow-select]');
  const gateControl = event.target.closest('[data-workflow-gate]');
  const dateControl = event.target.closest('[data-workflow-field="review_date"]');
  const control = selectControl || gateControl || dateControl;
  if (!control) return;
  const item = workflowItems.get(control.dataset.workflowId);
  if (!item) return;
  const previous = JSON.parse(JSON.stringify(item));
  if (selectControl) {
    const field = selectControl.dataset.workflowSelect;
    const valid = field === 'status' ? VALID_QUEUE_STATUSES : field === 'priority' ? VALID_PRIORITIES : field === 'confidence' ? VALID_CONFIDENCE : null;
    if (!valid || !valid.has(selectControl.value)) return;
    item[field] = selectControl.value;
  } else if (gateControl) {
    const gate = gateControl.dataset.workflowGate;
    if (!DILIGENCE_GATES.some(function (row) { return row[0] === gate; })) return;
    item.checks[gate] = gateControl.checked;
    if (gate === 'source') item.verified_at = gateControl.checked ? new Date().toISOString() : '';
  } else if (dateControl) {
    item.review_date = validDateInput(dateControl.value);
  }
  item.updated_at = new Date().toISOString();
  if (!persistWorkflow()) workflowItems.set(item.id,previous);
  render();
  let selector = '[data-workflow-id="' + CSS.escape(item.id) + '"]';
  if (selectControl) selector += '[data-workflow-select="' + CSS.escape(selectControl.dataset.workflowSelect) + '"]';
  if (gateControl) selector += '[data-workflow-gate="' + CSS.escape(gateControl.dataset.workflowGate) + '"]';
  if (dateControl) selector += '[data-workflow-field="review_date"]';
  const replacement = document.querySelector(selector);
  if (replacement) replacement.focus();
});
let workflowInputTimer;
document.addEventListener('input',function (event) {
  const control = event.target.closest('[data-workflow-field]');
  if (!control || control.dataset.workflowField === 'review_date') return;
  const field = control.dataset.workflowField;
  const limit = WORKFLOW_TEXT_LIMITS[field];
  const item = workflowItems.get(control.dataset.workflowId);
  if (!item || !limit) return;
  item[field] = control.value.slice(0,limit);
  item.updated_at = new Date().toISOString();
  workflowStorageDirty = workflowSerialization() !== lastPersistedWorkflow;
  clearTimeout(workflowInputTimer);
  workflowInputTimer = setTimeout(persistWorkflow,250);
});
document.addEventListener('focusout',function (event) {
  const control = event.target.closest('[data-workflow-field]');
  if (!control) return;
  const item = workflowItems.get(control.dataset.workflowId);
  if (!item) return;
  clearTimeout(workflowInputTimer);
  persistWorkflow();
});
window.addEventListener('beforeunload',function (event) {
  clearTimeout(workflowInputTimer);
  if (workflowStorageDirty && !persistWorkflow()) {
    event.preventDefault();
    event.returnValue = '';
  }
});
window.addEventListener('pagehide',function () {
  clearTimeout(workflowInputTimer);
  if (workflowStorageDirty) persistWorkflow();
});
document.getElementById('queue-restore-input').addEventListener('change',function (event) {
  restoreQueueFile(event.target.files && event.target.files[0]);
  event.target.value = '';
});

let searchTimer;
let articleSearchGeneration = 0;
function renderArticleAwareSearch(focusResult) {
  const generation = ++articleSearchGeneration;
  render();
  const finish = function () {
    if (!focusResult) return;
    if (!observationsReady && currentStateNeedsObservations()) {
      queueObservationResultFocus();
      return;
    }
    if (state.view === 'briefing') {
      const leadTitle = document.getElementById('lead-article-title');
      if (leadTitle) { leadTitle.tabIndex = -1; leadTitle.focus(); }
    } else focusSelectedRow();
  };
  if (isArticleView() && state.query && !briefArchiveReady && !briefArchiveFailed) {
    loadBriefArchive().then(function () {
      if (generation !== articleSearchGeneration) return;
      render();
      finish();
    }).catch(function () {
      if (generation !== articleSearchGeneration) return;
      showToast('Older article passages could not be searched; showing verified local results');
      render();
      finish();
    });
    return;
  }
  finish();
}
let structureFocusTimer = null;
document.addEventListener('input',function (event) {
  const input = event.target.closest && event.target.closest('#structure-focus-input');
  if (!input) return;
  clearTimeout(structureFocusTimer);
  const value = input.value.slice(0,120);
  const caret = input.selectionStart;
  structureFocusTimer = setTimeout(function () {
    state.structureFocus = value;
    state.structureAnchor = '';
    state.structurePassage = '';
    state.structureShareable = false;
    state.limit = PAGE_SIZE.structure;
    render();
    const restored = document.getElementById('structure-focus-input');
    if (!restored) return;
    restored.focus();
    const position = Math.min(caret === null ? value.length : caret,restored.value.length);
    restored.setSelectionRange(position,position);
  },200);
});
document.getElementById('search').addEventListener('input',function (event) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(function () {
    state.query = event.target.value;
    state.limit = PAGE_SIZE[state.view];
    if (state.query && state.sort === 'newest') state.sort = 'relevance';
    if (!state.query && state.sort === 'relevance') state.sort = 'newest';
    renderArticleAwareSearch(false);
  },120);
});
document.getElementById('search').addEventListener('keydown',function (event) {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  clearTimeout(searchTimer);
  state.query = event.target.value;
  state.sort = state.query ? 'relevance' : 'newest';
  state.limit = PAGE_SIZE[state.view];
  renderArticleAwareSearch(true);
});
document.getElementById('manager-search').addEventListener('input',function (event) {
  const query = normalize(event.target.value);
  document.querySelectorAll('.manager-option').forEach(function (button) {
    button.hidden = Boolean(query) && !normalize(button.dataset.value).includes(query);
  });
});
document.getElementById('sort-select').addEventListener('change',function (event) {
  state.sort = event.target.value;
  state.limit = PAGE_SIZE[state.view];
  render();
});
document.getElementById('clear-filters').addEventListener('click',resetFilters);
document.getElementById('load-more').addEventListener('click',function () {
  state.limit += PAGE_SIZE[state.view];
  renderRows(filteredRecords());
});
document.getElementById('mobile-filter-button').addEventListener('click',function () {
  const open = !document.body.classList.contains('filters-open');
  document.body.classList.toggle('filters-open',open);
  document.body.classList.remove('inspector-open');
  syncOverlayAccessibility();
  if (open) document.getElementById('filter-close').focus();
  else this.focus();
});
document.getElementById('filter-close').addEventListener('click',function () {
  closeDrawers();
  document.getElementById('mobile-filter-button').focus();
});
document.getElementById('drawer-backdrop').addEventListener('click',function () {
  const filtersWereOpen = document.body.classList.contains('filters-open');
  const inspectorWasOpen = document.body.classList.contains('inspector-open');
  closeDrawers();
  if (filtersWereOpen) document.getElementById('mobile-filter-button').focus();
  else if (inspectorWasOpen) focusSelectedRow();
});
document.getElementById('inspector-close').addEventListener('click',function () {
  if (window.innerWidth <= 1240) {
    closeDrawers();
    focusSelectedRow();
  } else {
    state.inspector = false;
    try { localStorage.setItem('nrt-inspector','hidden'); } catch (_error) {}
    render();
    document.querySelector('[data-action="inspector"]').focus();
  }
});
document.getElementById('theme-button').addEventListener('click',function () {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  document.getElementById('theme-color').content = next === 'light' ? '__LIGHT_THEME_BG__' : '__DARK_THEME_BG__';
  try { localStorage.setItem('nrt-theme',next); } catch (_error) {}
  this.textContent = next === 'light' ? 'Dark' : 'Light';
  this.setAttribute('aria-label','Switch to ' + (next === 'light' ? 'dark' : 'light') + ' theme');
});
const shortcutDialog = document.getElementById('shortcut-dialog');
const manualCopyDialog = document.getElementById('manual-copy-dialog');
document.getElementById('shortcut-button').addEventListener('click',function () { shortcutDialog.showModal(); });
document.getElementById('method-button').addEventListener('click',function () { shortcutDialog.showModal(); });
document.querySelector('[data-close-dialog]').addEventListener('click',function () { shortcutDialog.close(); });
document.getElementById('manual-copy-select').addEventListener('click',function () {
  const textarea = document.getElementById('manual-copy-text');
  textarea.focus();
  textarea.select();
});
document.getElementById('manual-copy-close').addEventListener('click',function () { manualCopyDialog.close(); });
document.getElementById('manual-copy-done').addEventListener('click',function () { manualCopyDialog.close(); });

// Command palette. Every entry delegates to the control that already owns the
// behaviour, so the palette can never drift from the visible terminal actions.
const commandPalette = document.getElementById('command-palette');
const paletteInput = document.getElementById('command-palette-input');
const paletteList = document.getElementById('command-palette-list');
const paletteEmpty = document.getElementById('command-palette-empty');
const PALETTE_LIMIT = 40;
let paletteCommands = null;
let paletteMatches = [];
let paletteActive = 0;

function clickCommandTarget(selector) {
  const target = document.querySelector(selector);
  if (target) target.click();
}
function gotoView(view) {
  markMeaningfulNavigation();
  state.view = view;
  state.sort = 'newest';
  state.selected = '';
  state.threadTopic = '';
  state.limit = PAGE_SIZE[view];
  renderObservationAwareNavigation('entry');
}
function buildPaletteCommands() {
  const commands = [
    ['structure','Structure Desk'],['briefing','Latest Brief'],
    ['ideas','Evidence Monitor'],['research','Research Library'],
    ['queue','Decision Queue']
  ].map(function (row) {
    return {kind:'View',label:'Go to ' + row[1],meta:'',run:function () { gotoView(row[0]); }};
  });
  [
    ['Toggle light / dark theme','#theme-button'],
    ['Toggle row density','[data-action="density"]'],
    ['Toggle evidence inspector','[data-action="inspector"]'],
    ['Copy current view','[data-action="copy-view"]'],
    ['Export current view as CSV','[data-action="export"]'],
    ['Clear all filters','#clear-filters']
  ].forEach(function (row) {
    commands.push({kind:'Action',label:row[0],meta:'',run:function () { clickCommandTarget(row[1]); }});
  });
  commands.push({kind:'Action',label:'Keyboard workflow & data method',meta:'',
    run:function () { shortcutDialog.showModal(); }});
  ARTICLES.forEach(function (article) {
    commands.push({
      kind:'Dossier',
      label:article.title,
      meta:shortDate(article.date),
      run:function () {
        markMeaningfulNavigation();
        state.view = 'briefing';
        state.selected = article.id;
        state.threadTopic = '';
        state.limit = PAGE_SIZE.briefing;
        renderObservationAwareNavigation('entry');
      }
    });
  });
  return commands;
}
function paletteScore(label, query) {
  const haystack = label.toLowerCase();
  const direct = haystack.indexOf(query);
  if (direct >= 0) return direct;
  let cursor = 0;
  for (let index = 0; index < query.length; index++) {
    cursor = haystack.indexOf(query.charAt(index),cursor);
    if (cursor < 0) return -1;
    cursor++;
  }
  return 900;
}
function renderPalette() {
  const query = paletteInput.value.trim().toLowerCase();
  const scored = [];
  paletteCommands.forEach(function (command,order) {
    if (!query) { scored.push([order,order,command]); return; }
    const score = paletteScore(command.label,query);
    if (score >= 0) scored.push([score,order,command]);
  });
  scored.sort(function (left,right) { return left[0] - right[0] || left[1] - right[1]; });
  paletteMatches = scored.slice(0,PALETTE_LIMIT).map(function (row) { return row[2]; });
  if (paletteActive >= paletteMatches.length) paletteActive = 0;
  paletteEmpty.hidden = paletteMatches.length > 0;
  paletteList.innerHTML = paletteMatches.map(function (command,index) {
    return '<li class="palette-option" role="option" id="palette-option-' + index + '"' +
      ' aria-selected="' + (index === paletteActive) + '" data-palette-index="' + index + '">' +
      '<span class="palette-kind">' + escapeHtml(command.kind) + '</span>' +
      '<span class="palette-label">' + escapeHtml(command.label) + '</span>' +
      (command.meta ? '<span class="palette-meta">' + escapeHtml(command.meta) + '</span>' : '') +
      '</li>';
  }).join('');
  paletteInput.setAttribute('aria-activedescendant',
    paletteMatches.length ? 'palette-option-' + paletteActive : '');
}
function movePalette(step) {
  if (!paletteMatches.length) return;
  paletteActive = (paletteActive + step + paletteMatches.length) % paletteMatches.length;
  renderPalette();
  const option = paletteList.querySelector('[aria-selected="true"]');
  if (option) option.scrollIntoView({block:'nearest'});
}
function runPalette(index) {
  const command = paletteMatches[index];
  if (!command) return;
  commandPalette.close();
  command.run();
}
function openPalette() {
  if (commandPalette.open) return;
  if (!paletteCommands) paletteCommands = buildPaletteCommands();
  paletteInput.value = '';
  paletteActive = 0;
  renderPalette();
  commandPalette.showModal();
  paletteInput.focus();
}
paletteInput.addEventListener('input',function () { paletteActive = 0; renderPalette(); });
paletteInput.addEventListener('keydown',function (event) {
  if (event.key === 'ArrowDown') { event.preventDefault(); movePalette(1); }
  else if (event.key === 'ArrowUp') { event.preventDefault(); movePalette(-1); }
  else if (event.key === 'Enter') { event.preventDefault(); runPalette(paletteActive); }
});
paletteList.addEventListener('click',function (event) {
  const option = event.target.closest('[data-palette-index]');
  if (option) runPalette(Number(option.dataset.paletteIndex));
});
document.getElementById('palette-button').addEventListener('click',openPalette);
document.addEventListener('keydown',function (event) {
  if ((event.metaKey || event.ctrlKey) && !event.altKey && event.code === 'KeyK') {
    event.preventDefault();
    if (commandPalette.open) commandPalette.close();
    else openPalette();
  }
});

document.addEventListener('keydown',function (event) {
  const target = event.target;
  const editable = target.matches('input,textarea,select,[contenteditable="true"]');
  const interactive = target.closest && target.closest('button,a,[role="button"]');
  if (event.key === 'Escape') {
    if (manualCopyDialog.open) { manualCopyDialog.close(); return; }
    if (shortcutDialog.open) { shortcutDialog.close(); return; }
    const filtersWereOpen = document.body.classList.contains('filters-open');
    const inspectorWasOpen = document.body.classList.contains('inspector-open');
    if (filtersWereOpen || inspectorWasOpen) {
      closeDrawers();
      if (filtersWereOpen) document.getElementById('mobile-filter-button').focus();
      else focusSelectedRow();
      return;
    }
    if (target === document.getElementById('search') && (target.value || state.query)) {
      clearTimeout(searchTimer);
      target.value = '';
      state.query = '';
      state.sort = 'newest';
      render();
    }
    if (target === document.getElementById('structure-focus-input') &&
        (target.value || state.structureFocus)) {
      clearTimeout(structureFocusTimer);
      state.structureFocus = '';
      state.structureAnchor = '';
      state.structurePassage = '';
      state.structureShareable = false;
      render();
      const restored = document.getElementById('structure-focus-input');
      if (restored) restored.focus();
    }
    return;
  }
  if (shortcutDialog.open || manualCopyDialog.open || editable || interactive || event.metaKey || event.ctrlKey) return;
  if (!event.altKey && (event.key === 'Home' || event.key === 'End') && document.querySelector('[data-record-id]')) {
    event.preventDefault();
    const rows = document.querySelectorAll('[data-record-id]');
    const row = event.key === 'Home' ? rows[0] : rows[rows.length - 1];
    if (row) selectRecord(row.dataset.recordId,true,false);
    return;
  }
  if (!event.altKey && event.key === 'ArrowDown' && target.closest('[data-record-id]')) {
    event.preventDefault();
    moveSelection(1);
    return;
  }
  if (!event.altKey && event.key === 'ArrowUp' && target.closest('[data-record-id]')) {
    event.preventDefault();
    moveSelection(-1);
    return;
  }
  if (!event.altKey && event.key === 'Enter' && state.selected) {
    if (state.view === 'briefing') {
      markMeaningfulNavigation();
      state.view = 'research';
      renderObservationAwareNavigation('inspector');
    } else openInspector(true);
    return;
  }
  if (event.altKey && !event.shiftKey && event.code === 'Slash') {
    event.preventDefault();
    document.getElementById(state.view === 'structure' ? 'structure-focus-input' : 'search').focus();
    return;
  }
  // Unmodified desk keys. The guard above already proved focus is not in an
  // editable or interactive control, so these cannot swallow typed input.
  if (state.view !== 'structure' && !event.altKey && !event.shiftKey && event.code === 'KeyJ') {
    event.preventDefault();
    moveSelection(1);
    return;
  }
  if (state.view !== 'structure' && !event.altKey && !event.shiftKey && event.code === 'KeyK') {
    event.preventDefault();
    moveSelection(-1);
    return;
  }
  if (!event.altKey && !event.shiftKey && event.code === 'Slash') {
    event.preventDefault();
    document.getElementById(state.view === 'structure' ? 'structure-focus-input' : 'search').focus();
    return;
  }
  if (!event.altKey && event.shiftKey && event.code === 'Slash') {
    event.preventDefault();
    shortcutDialog.showModal();
    return;
  }
  if (!event.altKey || !event.shiftKey) return;
  if (event.code === 'Slash') {
    event.preventDefault();
    shortcutDialog.showModal();
  } else if (event.code === 'KeyG') {
    event.preventDefault();
    if (state.view === 'briefing') {
      const leadTitle = document.getElementById('lead-article-title');
      if (leadTitle) { leadTitle.tabIndex = -1; leadTitle.focus(); }
    } else focusSelectedRow();
  } else if (event.code === 'KeyJ') {
    event.preventDefault();
    moveSelection(1);
  } else if (event.code === 'KeyK') {
    event.preventDefault();
    moveSelection(-1);
  } else if (event.code === 'KeyO') {
    event.preventDefault();
    const article = selectedArticle();
    if (article) window.open(safeUrl(article.url),'_blank','noopener,noreferrer');
  } else if (event.code === 'KeyS' && (state.view === 'ideas' || state.view === 'queue') && state.selected) {
    event.preventDefault();
    toggleSaved(state.selected);
  } else if (event.code === 'KeyC' && state.selected) {
    event.preventDefault();
    if (isArticleView()) {
      const article = ARTICLE_BY_ID.get(state.selected);
      if (article) copyText(articleCitation(article),'Article citation copied');
    } else {
      const idea = IDEA_BY_ID.get(state.selected);
      if (idea) copyText(ideaCitation(idea),'Idea citation copied');
    }
  } else if (event.code === 'KeyF') {
    event.preventDefault();
    if (window.innerWidth > 1020) {
      document.getElementById('manager-search').focus();
    } else {
      const button = document.getElementById('mobile-filter-button');
      const open = !document.body.classList.contains('filters-open');
      document.body.classList.toggle('filters-open',open);
      document.body.classList.remove('inspector-open');
      syncOverlayAccessibility();
      if (open) document.getElementById('filter-close').focus();
      else button.focus();
    }
  } else if (['Digit1','Digit2','Digit3','Digit4','Digit5'].includes(event.code)) {
    event.preventDefault();
    const viewNumber = event.code.slice(-1);
    markMeaningfulNavigation();
    state.view = viewNumber === '1' ? 'structure' : viewNumber === '2' ? 'briefing'
      : viewNumber === '3' ? 'ideas' : viewNumber === '4' ? 'research' : 'queue';
    state.sort = 'newest';
    state.selected = '';
    state.threadTopic = '';
    state.limit = PAGE_SIZE[state.view];
    renderObservationAwareNavigation('entry');
  }
});

window.addEventListener('popstate',function () {
  restoringHistory = true;
  hydrateFromHash();
  if (state.view === 'briefing' && state.threadTopic) {
    pendingBriefFocus = {kind:'thread',value:state.threadTopic};
  }
  document.getElementById('search').value = state.query;
  const waiting = !observationsReady && currentStateNeedsObservations();
  if (waiting) queueObservationResultFocus('entry');
  render();
  restoringHistory = false;
  updateHash();
  if (waiting) focusObservationGate();
  else focusViewEntry();
});

window.addEventListener('resize',function () {
  if (window.innerWidth > 1240) document.body.classList.remove('inspector-open');
  if (window.innerWidth > 1020) document.body.classList.remove('filters-open');
  document.querySelectorAll('#table-head [data-sort]').forEach(function (button) {
    button.tabIndex = window.innerWidth < 760 ? -1 : 0;
  });
  syncOverlayAccessibility();
});

function formatCheckedAt(value) {
  const date = new Date(String(value || ''));
  if (Number.isNaN(date.getTime())) return 'time not recorded';
  return date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});
}
function formatReleaseCheckedAt(value) {
  const date = new Date(String(value || ''));
  if (Number.isNaN(date.getTime())) return 'time not recorded';
  const iso = date.toISOString();
  return iso.slice(0,10) + ' ' + iso.slice(11,16) + ' UTC';
}
function renderStaticStats() {
  const evidenceArticles = ARTICLES.filter(function (article) { return articleHasEvidence(article); }).length;
  const boundaryArticles = ARTICLES.filter(function (article) {
    return articleHasBriefKind(article,'countercase') || articleHasBriefKind(article,'falsifier');
  }).length;
  const implementationArticles = ARTICLES.filter(function (article) { return articleHasBriefKind(article,'implementation'); }).length;
  const checkpointCount = ARTICLES.reduce(function (total,article) {
    return total + Number(article.brief_features && article.brief_features.checkpoint_count || 0);
  },0);
  const provenanceArticles = {current:0,prior:0,unverified:0};
  const provenanceObservations = {current:0,prior:0,unverified:0};
  ARTICLES.forEach(function (article) {
    const status = VALID_BODY_REVISIONS.has(article.body_revision_status)
      ? article.body_revision_status : 'unverified';
    provenanceArticles[status] += 1;
    provenanceObservations[status] += Number(article.trade_count || 0);
  });
  document.getElementById('kpi-latest').textContent = shortDate(String(SNAPSHOT.latest_publication || MAX_DATE).slice(0,10));
  document.getElementById('kpi-evidence').textContent = number(evidenceArticles);
  document.getElementById('kpi-countercase').textContent = number(boundaryArticles);
  document.getElementById('kpi-implementation').textContent = number(implementationArticles);
  document.getElementById('kpi-checkpoints').textContent = number(checkpointCount);
  document.getElementById('kpi-provenance-articles').textContent =
    number(provenanceArticles.current) + ' current · ' +
    number(provenanceArticles.prior) + ' prior · ' +
    number(provenanceArticles.unverified) + ' unverified';
  document.getElementById('kpi-provenance-observations').textContent =
    number(provenanceObservations.current) + ' current · ' +
    number(provenanceObservations.prior) + ' prior · ' +
    number(provenanceObservations.unverified) + ' unverified observations';
  const provenanceSummary = document.getElementById('kpi-provenance');
  const provenanceText =
    'Body revision provenance. Articles: ' +
    number(provenanceArticles.current) + ' current, ' +
    number(provenanceArticles.prior) + ' prior, ' +
    number(provenanceArticles.unverified) + ' unverified. Observations: ' +
    number(provenanceObservations.current) + ' current, ' +
    number(provenanceObservations.prior) + ' prior, ' +
    number(provenanceObservations.unverified) + ' unverified.';
  provenanceSummary.title = provenanceText;
  provenanceSummary.setAttribute('aria-label',provenanceText);
  const sourceHealth = Object.values(SNAPSHOT.sources || {});
  const checked = new Date(String(SNAPSHOT.checked_at || ''));
  const nowMs = Date.now();
  const futureToleranceMs = 10 * 60 * 1000;
  const checkedMs = checked.getTime();
  const manifestClockInvalid = Number.isNaN(checkedMs) || checkedMs > nowMs + futureToleranceMs;
  const sourceClockInvalid = sourceHealth.some(function (source) {
    const sourceCheckedMs = new Date(String(source && source.checked_at || '')).getTime();
    return Number.isNaN(sourceCheckedMs) || sourceCheckedMs > nowMs + futureToleranceMs;
  });
  const ageHours = manifestClockInvalid ? Infinity : (nowMs - checkedMs) / 3600000;
  const allHealthy = sourceHealth.length >= 2 && sourceHealth.every(function (source) { return source.status === 'ok'; });
  const freshnessClass = manifestClockInvalid || sourceClockInvalid || ageHours > 16 ? 'stale' : allHealthy ? 'fresh' : sourceHealth.length ? 'degraded' : '';
  const freshnessStatus = freshnessClass === 'stale' ? 'Stale' : freshnessClass === 'fresh' ? 'Current' : freshnessClass === 'degraded' ? 'Degraded' : 'Unknown';
  const dot = document.getElementById('freshness-dot');
  dot.className = 'status-dot' + (freshnessClass ? ' ' + freshnessClass : '');
  document.getElementById('freshness-state').textContent = freshnessStatus;
  const label = document.getElementById('freshness-label');
  label.textContent = (manifestClockInvalid || sourceClockInvalid ? 'Refresh clock invalid · ' : '') + 'Research through ' + formatDate(String(SNAPSHOT.latest_publication || MAX_DATE).slice(0,10)) + ' · checked ' + formatCheckedAt(SNAPSHOT.checked_at);
  const healthDetail = ['substack','medium'].map(function (source) {
    const info = SNAPSHOT.sources && SNAPSHOT.sources[source] || {};
    return sourceLabel(source) + ': ' + (info.status || 'unknown') + ', ' + number(info.included_count || 0) + ' included, ' + (info.mode || 'mode unknown');
  }).join(' | ');
  const freshnessSummary = document.getElementById('freshness-summary');
  freshnessSummary.title = healthDetail + ' | Next scheduled checks: 9 AM, 1 PM, and 10 PM Asia/Kolkata';
  freshnessSummary.setAttribute('aria-label',freshnessStatus + '. ' + label.textContent + '. ' + healthDetail);
  const theme = document.documentElement.dataset.theme || 'light';
  const themeButton = document.getElementById('theme-button');
  themeButton.textContent = theme === 'light' ? 'Dark' : 'Light';
  themeButton.setAttribute('aria-label','Switch to ' + (theme === 'light' ? 'dark' : 'light') + ' theme');
}

syncWorkflowStorageAlert();
if (workflowLoadBlocked) {
  showPersistentNotice('Stored decision-queue data could not be read. Saving is blocked until you back up or explicitly discard the unreadable record.');
} else if (lastRestoreWorkflowItems) {
  showPersistentNotice('A pre-import tab queue is available as a rollback from the most recent restore.','Undo import','undo-restore');
} else if (workflowLegacyMigrated) {
  showPersistentNotice('A legacy persistent queue was moved into this safer tab session and removed from origin-wide storage. Export a plaintext backup before closing the tab if it must be retained.','Back up queue','backup-queue');
} else if (legacyCleanupPending || legacyStorageCheckUnavailable) {
  showPersistentNotice('Legacy origin-wide queue storage could not be checked or cleared. This tab queue remains session-scoped; clear old site data in browser settings if needed.');
}
hydrateFromHash();
document.getElementById('search').value = state.query;
state.inspector = storedInspector;
renderStaticStats();
if (state.query && isArticleView()) renderArticleAwareSearch(false);
else render();
</script>
</body>
</html>
"""

HTML = (HTML_TEMPLATE
        .replace('__ARTICLE_WIRE_SCHEMA_VERSION__', str(ARTICLE_WIRE_SCHEMA_VERSION))
        .replace('__SUBSCRIPTION_URL_JSON__', json_for_script(SUBSCRIPTION_URL))
        .replace('__ARTICLES_JSON__', articles_json)
        .replace('__THREADS_JSON__', threads_json)
        .replace('__RATE_CONTEXT_JSON__', rate_context_json)
        .replace('__DESK_FACETS_JSON__', desk_facets_json)
        .replace('__MANAGER_LABELS_JSON__', manager_labels_json)
        .replace('__SNAPSHOT_JSON__', snapshot_json)
        .replace('__MANAGER_BUTTONS__', manager_html)
        .replace('__THEME_REVISION__', THEME_REVISION)
        .replace('__LIGHT_THEME_BG__', LIGHT_THEME_BG)
        .replace('__DARK_THEME_BG__', DARK_THEME_BG)
        .replace('__BRIEF_ARCHIVE_SHA256__', brief_archive_sha256)
        .replace('__OBSERVATION_ARCHIVE_SHA256__', observation_archive_sha256)
        .replace('__REVISION__', revision_meta)
        .replace('__ARTICLE_COUNT__', str(len(client_articles)))
        .replace('__OBSERVATION_COUNT__', str(len(client_ideas)))
        .replace('__DATA_CHECKSUM__', checksum_meta))

inline_scripts = [
    script_body for _script_type, script_body in extract_inline_scripts(HTML)
]
if not inline_scripts:
    raise ValueError('Generated terminal contains no inline application scripts')
script_sources = []
for script_body in inline_scripts:
    digest = base64.b64encode(
        hashlib.sha256(script_body.encode('utf-8')).digest()
    ).decode('ascii')
    script_sources.append(f"'sha256-{digest}'")
csp = '; '.join((
    "default-src 'none'",
    f"script-src {' '.join(script_sources)}",
    "style-src 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "font-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-src 'none'",
    "media-src 'none'",
    "worker-src 'none'",
    "manifest-src 'self'",
    'upgrade-insecure-requests',
))
HTML = HTML.replace('__CSP__', csp)
if '__CSP__' in HTML:
    raise ValueError('Content Security Policy placeholder was not fully replaced')

last_modified = clean_date(
    str(snapshot_manifest.get('checked_at')
        or snapshot_manifest.get('latest_publication') or '')[:10]
)
robots_text = (
    'User-agent: *\n'
    'Allow: /\n'
    f'Sitemap: {SITE_URL}sitemap.xml\n'
)
sitemap_rows = [
    '  <url>\n'
    f'    <loc>{SITE_URL}</loc>\n'
    f'    <lastmod>{last_modified}</lastmod>\n'
    '  </url>'
]
for article in catalog_index:
    slug = quote(str(article.get('slug') or ''), safe='-')
    location = html_lib.escape(f'{SITE_URL}a/{slug}.html', quote=False)
    publication_date = clean_date(article.get('post_date'))
    sitemap_rows.append(
        '  <url>\n'
        f'    <loc>{location}</loc>\n'
        f'    <lastmod>{publication_date}</lastmod>\n'
        '  </url>'
    )
sitemap_xml = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + '\n'.join(sitemap_rows)
    + '\n</urlset>\n'
)
web_manifest = json.dumps({
    'name': 'Navnoor Research Terminal',
    'short_name': 'Navnoor Research',
    'description': (
        'Source-backed institutional research dossiers with exact passages, '
        'evidence ledgers, checkpoints, and decision boundaries.'
    ),
    'start_url': './',
    'scope': './',
    'display': 'standalone',
    'background_color': LIGHT_THEME_BG,
    'theme_color': LIGHT_THEME_BG,
    'icons': [{
        'src': 'favicon.svg',
        'sizes': 'any',
        'type': 'image/svg+xml',
        'purpose': 'any',
    }],
}, ensure_ascii=False, indent=2) + '\n'
favicon_svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="6" fill="{DARK_THEME_BG}"/>
<rect x="2" y="2" width="60" height="60" rx="4" fill="none" stroke="#ffb000" stroke-width="2"/>
<text x="32" y="39" fill="#f4f6f7" font-family="Arial,sans-serif" font-size="19" font-weight="700" text-anchor="middle">N/R</text>
</svg>
'''

if not SOCIAL_IMAGE_SOURCE.is_file():
    raise FileNotFoundError(f'Missing social preview asset: {SOCIAL_IMAGE_SOURCE}')
social_image_bytes = SOCIAL_IMAGE_SOURCE.read_bytes()
if (
        len(social_image_bytes) < 10_000
        or len(social_image_bytes) > 500_000
        or not social_image_bytes.startswith(b'\xff\xd8')
        or not social_image_bytes.rstrip().endswith(b'\xff\xd9')):
    raise ValueError('Social preview must be a valid, optimized 10–500 KB JPEG')


def jpeg_dimensions(payload):
    """Read JPEG dimensions without adding an image-library dependency."""
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset:offset + 2], 'big')
        if segment_length < 2 or offset + segment_length > len(payload):
            break
        if marker in sof_markers and segment_length >= 7:
            height = int.from_bytes(payload[offset + 3:offset + 5], 'big')
            width = int.from_bytes(payload[offset + 5:offset + 7], 'big')
            return width, height
        offset += segment_length
    raise ValueError('Social preview JPEG dimensions could not be read')


if jpeg_dimensions(social_image_bytes) != (1200, 630):
    raise ValueError('Social preview JPEG must be exactly 1200×630 pixels')

out = DOCS_DIR / 'index.html'
with open(out, 'w', encoding='utf-8') as handle:
    handle.write(HTML)

brief_out = DOCS_DIR / 'article_briefs.json'
with open(brief_out, 'w', encoding='utf-8') as handle:
    handle.write(brief_archive_json)

observations_out = DOCS_DIR / 'observations.json'
with open(observations_out, 'w', encoding='utf-8') as handle:
    handle.write(observation_archive_json)

support_assets = {
    'robots.txt': robots_text,
    'sitemap.xml': sitemap_xml,
    'site.webmanifest': web_manifest,
    'favicon.svg': favicon_svg,
}
for asset_name, asset_text in support_assets.items():
    (DOCS_DIR / asset_name).write_text(asset_text, encoding='utf-8')
shutil.copyfile(SOCIAL_IMAGE_SOURCE, DOCS_DIR / 'og.jpg')

share_summary = emit_share_assets(share_articles, DOCS_DIR, SITE_URL)
write_data_layer(
    DOCS_DIR,
    article_index_path,
    snapshot_manifest,
    search_index,
    related_graph,
    families_index,
)
data_summary = validate_data_layer(
    DOCS_DIR,
    article_index_path,
    snapshot_manifest,
)

print(
    f'Built {out} ({len(HTML) // 1024} KB + '
    f'{brief_out.stat().st_size // 1024} KB deferred dossiers + '
    f'{observations_out.stat().st_size // 1024} KB deferred observations, '
    f'{len(support_assets) + 1} support assets, '
    f'{len(client_articles)} research notes, {len(client_ideas)} extracted ideas, '
    f'{data_summary["article_count"]} public catalogue records, '
    f'{share_summary["count"]} share cards/stubs)'
)
