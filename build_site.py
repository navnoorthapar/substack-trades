#!/usr/bin/env python3
"""Build the institutional research terminal at docs/index.html."""
import hashlib
import html as html_lib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from article_briefs import is_boilerplate_text
from extract_trades import has_negated_trade_signal


ROOT = Path(__file__).parent
DOCS_DIR = Path(os.environ.get('SITE_OUTPUT_DIR', ROOT / 'docs')).expanduser()
DOCS_DIR.mkdir(parents=True, exist_ok=True)

with open(ROOT / 'trades_extracted.json', encoding='utf-8') as handle:
    trades = json.load(handle)

article_index_path = ROOT / 'articles_index.json'
article_index = []
if article_index_path.exists():
    with open(article_index_path, encoding='utf-8') as handle:
        payload = json.load(handle)
    article_index = payload.get('articles', []) if isinstance(payload, dict) else payload
    if not isinstance(article_index, list) or not all(
            isinstance(article, dict) for article in article_index):
        raise ValueError('articles_index.json must contain a list of article objects')
else:
    print('Warning: articles_index.json missing; building from trade metadata only')


def clean_date(value):
    """Return a sortable ISO date, sending malformed values to the bottom."""
    date = str(value or '')[:10]
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return '1970-01-01'
    return date


def clean_source(value, url=''):
    source = str(value or '').strip().casefold()
    if source in {'substack', 'medium'}:
        return source
    return 'medium' if 'medium.com/' in str(url).casefold() else 'substack'


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
    """Keep only fields required to render a validated exact source passage."""
    if not isinstance(value, dict) or not value.get('text'):
        return None
    return {
        'text': value['text'],
        'truncated': bool(value.get('truncated')),
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
    articles.append({
        'title': metadata.get('title') or first.get('article_title') or url,
        'subtitle': subtitle,
        'date': clean_date(metadata.get('post_date') or first.get('article_date')),
        'url': url,
        'source': clean_source(metadata.get('source'), url),
        'alternate_urls': metadata.get('alternate_urls') or {},
        'wordcount': wordcount,
        'content_status': metadata.get('content_status') or 'full',
        'brief': client_brief(metadata.get('brief')),
        'trades': article_trades,
    })

# Keep trade-bearing articles additive if metadata is ever incomplete.
for url, article_trades in trades_by_url.items():
    if url in seen_urls:
        continue
    first = article_trades[0]
    articles.append({
        'title': first.get('article_title') or url,
        'subtitle': '',
        'date': clean_date(first.get('article_date')),
        'url': url,
        'source': clean_source(None, url),
        'alternate_urls': {},
        'wordcount': 0,
        'content_status': 'full',
        'brief': client_brief(None),
        'trades': article_trades,
    })

articles.sort(key=lambda article: article['date'], reverse=True)

manager_variants = defaultdict(Counter)
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
# being repeated inside every extracted idea, materially reducing parse/heap
# cost while keeping the deployment a single static file.
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
        'url': article['url'],
        'source': article['source'],
        'alternate_urls': article['alternate_urls'],
        'wordcount': article['wordcount'],
        'read_minutes': read_minutes,
        'content_status': article['content_status'],
        'brief': brief_value if article_position < INLINE_BRIEF_COUNT else None,
        'brief_features': brief_features,
        'idea_ids': idea_ids,
        'trade_count': len(idea_ids),
        'directions': sorted(directions),
        'instruments': sorted(instruments),
        'managers': sorted(managers, key=str.casefold),
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
    key=lambda row: (-row[1], manager_labels[row[0]].casefold()),
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

articles_json = json_for_script(client_articles)
ideas_json = json_for_script(client_ideas)
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

site_revision = str(os.environ.get('SITE_REVISION') or 'local')
snapshot_json = json_for_script(snapshot_manifest)
revision_meta = html_lib.escape(site_revision, quote=True)
checksum_meta = html_lib.escape(
    str(snapshot_manifest.get('data_checksum') or ''), quote=True,
)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Institutional research intelligence across hedge funds, systematic strategies, derivatives, and market structure.">
<meta name="color-scheme" content="dark light">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none'; media-src 'none'; worker-src 'none'">
<meta name="nrt-revision" content="__REVISION__">
<meta name="nrt-article-count" content="__ARTICLE_COUNT__">
<meta name="nrt-observation-count" content="__OBSERVATION_COUNT__">
<meta name="nrt-data-checksum" content="__DATA_CHECKSUM__">
<title>Navnoor Research Terminal</title>
<script>
(function () {
  try {
    var stored = localStorage.getItem('nrt-theme');
    var theme = stored || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.dataset.theme = theme;
  } catch (_error) {
    document.documentElement.dataset.theme = 'dark';
  }
})();
</script>
<style>
*,*::before,*::after{box-sizing:border-box}
html,body,h1,h2,h3,p,ul,ol,dl,dd,figure{margin:0}
button,input,select{font:inherit}
:root{
  --header-h:58px;
  --kpi-h:44px;
  --rail-w:232px;
  --inspector-w:420px;
  --bg:#0b0d0e;
  --surface-1:#111416;
  --surface-2:#171b1e;
  --surface-3:#1e2428;
  --surface-raised:#242b30;
  --line:#2b3237;
  --line-strong:#3d474e;
  --control-line:#606a72;
  --text:#f3f0e8;
  --text-secondary:#c3c4c0;
  --text-muted:#969c9d;
  --accent:#d2b06a;
  --accent-strong:#b88935;
  --accent-soft:#2a2418;
  --on-accent:#101214;
  --positive:#58b98d;
  --positive-soft:#14251e;
  --positive-line:#2f7457;
  --negative:#e07379;
  --negative-soft:#2b191c;
  --negative-line:#874148;
  --warning:#d2a25a;
  --warning-soft:#2a2114;
  --warning-line:#73592f;
  --relative:#e38e42;
  --relative-soft:#2c1f15;
  --relative-line:#825328;
  --long-short:#ae9adb;
  --long-short-soft:#221e2e;
  --long-short-line:#675884;
  --long:#58b98d;
  --long-soft:#14251e;
  --long-line:#2f7457;
  --short:#e07379;
  --short-soft:#2b191c;
  --short-line:#874148;
  --quant:#73b5b1;
  --quant-soft:#142426;
  --quant-line:#3b6d6b;
  --number:#8fc0bc;
  --number-soft:#162526;
  --number-line:#3a6663;
  --checkpoint:#d8b36a;
  --source-substack:#d47d47;
  --source-medium:#a3aaa8;
  --focus:#f0c875;
  --selected:#211d16;
  --selected-line:#d2b06a;
  --backdrop:rgba(5,6,7,.78);
  --shadow:0 20px 54px rgba(0,0,0,.52);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
  --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#f1efea;
  --surface-1:#fbfaf7;
  --surface-2:#f5f2ec;
  --surface-3:#eae6de;
  --surface-raised:#ffffff;
  --line:#d8d2c8;
  --line-strong:#b9b0a4;
  --control-line:#81796f;
  --text:#202326;
  --text-secondary:#4b5153;
  --text-muted:#5f676b;
  --accent:#77551d;
  --accent-strong:#b88935;
  --accent-soft:#f1e6cf;
  --on-accent:#101214;
  --positive:#176846;
  --positive-soft:#e8f3ee;
  --positive-line:#7eb29c;
  --negative:#9e303c;
  --negative-soft:#f8e9eb;
  --negative-line:#c98f96;
  --warning:#76541a;
  --warning-soft:#f4ecda;
  --warning-line:#c7aa70;
  --relative:#824b12;
  --relative-soft:#f7ebdd;
  --relative-line:#c79b68;
  --long-short:#61488f;
  --long-short-soft:#efeaf6;
  --long-short-line:#a99ac4;
  --long:#1f6a4b;
  --long-soft:#e8f3ee;
  --long-line:#7eb29c;
  --short:#9e3d47;
  --short-soft:#f8e9eb;
  --short-line:#c98f96;
  --quant:#286b6b;
  --quant-soft:#e8f2f1;
  --quant-line:#8ab3b0;
  --number:#2c6866;
  --number-soft:#e8f2f1;
  --number-line:#89b3af;
  --checkpoint:#76541a;
  --source-substack:#a65329;
  --source-medium:#626b6e;
  --focus:#77551d;
  --selected:#eee4d0;
  --selected-line:#8d6828;
  --backdrop:rgba(42,38,32,.35);
  --shadow:0 18px 45px rgba(51,46,39,.14);
}
html[data-theme="dark"]{color-scheme:dark}
html{background:var(--bg);color:var(--text);font:13px/1.45 var(--sans);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body{height:100vh;height:100dvh;overflow:hidden;background:var(--bg)}
button,a,input,select{outline:none}
button{color:inherit}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{
  outline:2px solid var(--focus);outline-offset:2px
}
a{color:var(--accent)}
.skip-link{
  position:fixed;left:12px;top:8px;z-index:1000;padding:9px 12px;
  background:var(--accent-strong);color:var(--on-accent);border-radius:4px;text-decoration:none;
  transform:translateY(-150%)
}
.skip-link:focus{transform:translateY(0)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}

/* Global command header */
.app-header{
  height:var(--header-h);display:grid;grid-template-columns:auto minmax(240px,640px) auto;
  align-items:center;gap:20px;padding:0 16px;border-bottom:1px solid var(--line);
  background:var(--surface-1);position:relative;z-index:50
}
.brand{display:flex;align-items:center;gap:10px;min-width:205px}
.brand-mark{
  width:34px;height:34px;display:grid;place-items:center;border:1px solid var(--line-strong);
  border-radius:4px;color:var(--accent);font:700 11px var(--mono);letter-spacing:.04em;background:var(--surface-2)
}
.brand-name{font-weight:650;font-size:12px;letter-spacing:.015em;white-space:nowrap}
.brand-sub{font:10px var(--mono);color:var(--text-muted);letter-spacing:.08em;text-transform:uppercase;margin-top:2px}
.global-search{position:relative;width:100%}
.search-glyph{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--text-muted);font-size:15px;pointer-events:none}
#search{
  width:100%;height:36px;border:1px solid var(--control-line);border-radius:4px;
  background:var(--surface-2);color:var(--text);padding:0 52px 0 36px
}
#search::placeholder{color:var(--text-muted)}
#search:focus{border-color:var(--control-line);background:var(--surface-1)}
#search:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
.search-key{position:absolute;right:9px;top:50%;transform:translateY(-50%);font:10px var(--mono);color:var(--text-muted);border:1px solid var(--control-line);border-radius:3px;padding:1px 5px}
.header-right{display:flex;align-items:center;justify-content:flex-end;gap:8px;min-width:0}
.freshness{display:flex;align-items:center;gap:7px;max-width:350px;overflow:hidden;color:var(--text-secondary);font:10px var(--mono);white-space:nowrap;margin-right:4px}
.freshness>span:last-child{overflow:hidden;text-overflow:ellipsis}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--text-muted);box-shadow:0 0 0 3px var(--surface-3)}
.status-dot.fresh{background:var(--positive);box-shadow:0 0 0 3px var(--positive-soft)}
.status-dot.degraded{background:var(--warning);box-shadow:0 0 0 3px var(--warning-soft)}
.status-dot.stale{background:var(--negative);box-shadow:0 0 0 3px var(--negative-soft)}
.utility-button{
  min-height:34px;padding:0 10px;border:1px solid var(--control-line);border-radius:4px;
  background:var(--surface-2);color:var(--text-secondary);cursor:pointer
}
.utility-button:hover{background:var(--surface-3);color:var(--text);border-color:var(--line-strong)}
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
button.kpi-item{cursor:pointer}
button.kpi-item:hover{background:var(--surface-3)}
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
.date-option:hover{border-color:var(--line-strong);color:var(--text)}
.date-option.active{background:var(--selected);border-color:var(--line-strong);color:var(--text);box-shadow:inset 0 -2px var(--selected-line)}
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
.preset-button:hover{background:var(--surface-3);color:var(--text);border-color:var(--line-strong)}

.main-panel{min-width:0;background:var(--bg);display:flex;flex-direction:column;overflow:hidden}
.command-bar{
  min-height:50px;display:flex;align-items:center;gap:10px;padding:7px 12px;
  border-bottom:1px solid var(--line);background:var(--surface-1);flex-wrap:wrap
}
.view-tabs{display:flex;align-items:center;background:var(--surface-2);border:1px solid var(--control-line);border-radius:4px;padding:2px}
.view-tab{
  min-height:30px;border:0;border-radius:3px;background:transparent;color:var(--text-secondary);
  padding:0 11px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap
}
.view-tab.active{background:var(--surface-raised);color:var(--text);box-shadow:inset 0 -2px var(--selected-line)}
.result-summary{font:10px var(--mono);color:var(--text-muted);white-space:nowrap}
.command-spacer{flex:1}
.select-control{
  height:32px;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-2);
  color:var(--text-secondary);padding:0 28px 0 8px;font-size:11px;cursor:pointer
}
.command-button{
  min-height:32px;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-2);
  color:var(--text-secondary);padding:0 9px;cursor:pointer;font-size:10px;white-space:nowrap
}
.command-button:hover{background:var(--surface-3);border-color:var(--line-strong);color:var(--text)}
.command-button.active{background:var(--selected);border-color:var(--line-strong);color:var(--text);box-shadow:inset 0 -2px var(--selected-line)}
body[data-view="briefing"] .table-command{display:none}
.queue-command{display:none}
body[data-view="queue"] .queue-command{display:inline-flex}
.active-filters{
  min-height:35px;display:flex;align-items:center;gap:6px;padding:5px 12px;border-bottom:1px solid var(--line);
  background:var(--surface-2);overflow-x:auto
}
.active-filters.empty{display:none}
.active-label{font:9px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-right:3px}
.filter-chip{
  display:inline-flex;align-items:center;gap:5px;min-height:24px;border:1px solid var(--control-line);
  background:var(--surface-1);color:var(--text-secondary);border-radius:3px;padding:0 7px;
  font-size:10px;white-space:nowrap;cursor:pointer
}
.filter-chip:hover{border-color:var(--line-strong);color:var(--text)}
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
.mix-segment{height:100%;min-width:0}
.mix-long{background:var(--long)}
.mix-short{background:var(--short)}
.mix-arb{background:var(--relative)}
.mix-ls{background:var(--long-short)}
.mix-unspecified{background:var(--line-strong)}
.mix-legend{font:10px var(--mono);color:var(--text-muted);white-space:nowrap}

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
.brief-record:hover{background:var(--surface-2)}
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
.owner-workflow-item span{display:block;margin-top:3px;font-size:9.5px;line-height:1.35;color:var(--text-muted)}
.operating-boundary{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
.operating-boundary div{padding:11px 12px;background:var(--surface-1)}
.operating-boundary h4{font:650 9.5px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-secondary);margin-bottom:5px}
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
.intel-lens:hover{background:var(--surface-3);color:var(--text)}
.intel-lens.active{border-color:var(--selected-line);background:var(--accent-soft);color:var(--accent)}
.intel-grid{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(300px,.75fr);gap:12px;align-items:start}
.intel-lead,.intel-side-card,.intel-stream{border:1px solid var(--line);border-radius:6px;background:var(--surface-1);overflow:hidden}
.intel-lead{box-shadow:inset 0 2px var(--selected-line)}
.intel-lead-inner{padding:19px 21px 17px}
.intel-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--text-muted);font:10px var(--mono);text-transform:uppercase;letter-spacing:.045em}
.intel-meta .source-badge{text-transform:none;letter-spacing:0}
.intel-title{font-size:28px;line-height:1.16;letter-spacing:-.025em;margin-top:12px;max-width:980px;overflow-wrap:anywhere}
.intel-claim{font-size:14px;line-height:1.52;color:var(--text-secondary);margin-top:10px;max-width:980px}
.intel-reasons{display:flex;gap:5px;flex-wrap:wrap;margin-top:13px}
.intel-reason{display:inline-flex;align-items:center;min-height:23px;border:1px solid var(--line-strong);border-radius:3px;background:var(--surface-2);color:var(--text-secondary);padding:0 7px;font:9.5px var(--mono)}
.intel-reason.accent{border-color:var(--quant-line);background:var(--quant-soft);color:var(--quant)}
.intel-section-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.intel-section{padding:15px 18px;background:var(--surface-1);min-height:145px}
.intel-section.full{grid-column:1/-1;min-height:0}
.intel-label{font:650 9.5px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:6px}
.intel-section h3{font-size:12px;line-height:1.35;color:var(--text);margin-bottom:7px}
.intel-passage{font-size:12.5px;line-height:1.62;color:var(--text-secondary);overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:5;overflow:hidden}
.intel-passage mark{background:var(--number-soft);color:var(--number);border-bottom:1px solid var(--number-line);border-radius:2px;padding:0 1px}
.source-tail{color:var(--text-muted);font:9px var(--mono);white-space:nowrap}
.intel-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap;padding:13px 18px;background:var(--surface-2)}
.intel-actions-note{margin-left:auto;color:var(--text-muted);font-size:9.5px;line-height:1.4;text-align:right;max-width:340px}
.intel-side{display:grid;gap:12px}
.intel-card-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--surface-2)}
.intel-card-head h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.075em;color:var(--text-secondary)}
.intel-card-head span{font:9.5px var(--mono);color:var(--text-muted)}
.checkpoint-list,.next-list{display:grid}
.checkpoint{padding:11px 12px;border-bottom:1px solid var(--line)}
.checkpoint:last-child,.next-item:last-child{border-bottom:0}
.checkpoint time{display:block;font:650 11px var(--mono);color:var(--checkpoint);margin-bottom:4px}
.checkpoint .next-title{display:block;font-size:11.5px;line-height:1.4;color:var(--text)}
.checkpoint .next-summary{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden;margin-top:4px;font-size:10.5px;line-height:1.48;color:var(--text-muted)}
.next-item{width:100%;display:block;padding:10px 12px;border:0;border-bottom:1px solid var(--line);background:transparent;text-align:left;cursor:pointer;color:var(--text-secondary)}
.next-item:hover,.next-item.selected{background:var(--surface-2)}
.next-item time{font:9.5px var(--mono);color:var(--text-muted)}
.next-item .next-title{display:block;margin-top:3px;font-size:11.5px;font-weight:650;line-height:1.38;color:var(--text)}
.next-item .next-summary{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;margin-top:4px;font-size:10px;line-height:1.42;color:var(--text-muted)}
.intel-stream{grid-column:1/-1;margin-top:12px}
.intel-stream-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line)}
.intel-article-card{min-width:0;padding:14px;background:var(--surface-1);border:0;text-align:left;color:var(--text-secondary);cursor:pointer}
.intel-article-card:hover{background:var(--surface-2)}
.intel-article-card .intel-meta{font-size:9px}
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
.checkpoint-mini{display:grid;grid-template-columns:92px 1fr;gap:8px;padding:8px 0;border-top:1px solid var(--line);font-size:10.5px;color:var(--text-secondary)}
.checkpoint-mini time{font:650 10px var(--mono);color:var(--checkpoint)}

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
.data-row:hover{background:var(--surface-2)}
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
  border-radius:3px;color:var(--text-muted);font:600 9px var(--mono)
}
.evidence-flag.on{border-color:var(--quant-line);color:var(--quant);background:var(--quant-soft)}
.documentation-badge{min-width:34px;height:19px;display:grid;place-items:center;border:1px solid var(--line-strong);border-radius:3px;color:var(--text-secondary);font:650 9px var(--mono);background:var(--surface-2)}
.documentation-badge.complete{color:var(--positive);border-color:var(--positive-line);background:var(--positive-soft)}
.review-flag{color:var(--warning);font:650 9px var(--mono)}
.new-badge{display:inline-flex;margin-left:6px;color:var(--accent);font:650 9px var(--mono);text-transform:uppercase}
.workflow-badge{display:inline-flex;margin-left:6px;padding:1px 5px;border:1px solid var(--line-strong);border-radius:3px;color:var(--text-secondary);font:650 9px var(--mono);text-transform:uppercase}
.workflow-badge.coverage{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
.pinned-selection{box-shadow:inset 3px 0 var(--selected-line)}
.row-open{
  width:27px;height:27px;display:grid;place-items:center;border:1px solid transparent;border-radius:3px;
  text-decoration:none;color:var(--text-muted);font-size:13px
}
.row-open:hover{border-color:var(--line);background:var(--surface-3);color:var(--accent)}
.article-title{font-size:12px;font-weight:600;color:var(--text);line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.article-subtitle{font-size:10px;color:var(--text-muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.number-cell{font:11px var(--mono);color:var(--text);text-align:right}
.coverage-full{color:var(--positive);border-color:var(--positive-line)}
.coverage-excerpt{color:var(--warning);border-color:var(--warning-line)}
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
.load-more:hover{background:var(--surface-3);color:var(--text)}

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
.inspector-close{border:0;background:transparent;color:var(--text-muted);cursor:pointer;padding:8px}
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
.primary-action:hover{background:color-mix(in srgb,var(--accent-strong) 88%,var(--text));border-color:transparent}
.secondary-action{border:1px solid var(--control-line);background:var(--surface-2);color:var(--text-secondary)}
.secondary-action:hover{background:var(--surface-3);color:var(--text)}
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
.article-stat span{font:9px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted)}
.related-ideas{display:grid;gap:5px}
.related-idea{
  border:1px solid var(--line);border-radius:3px;background:var(--surface-2);padding:8px;
  color:var(--text-secondary);text-align:left;cursor:pointer;font-size:10.5px;line-height:1.4
}
.related-idea:hover{background:var(--surface-3);border-color:var(--line-strong);color:var(--text)}
.review-notice{margin:8px 0 13px;padding:9px;border:1px solid var(--warning-line);border-radius:3px;background:var(--warning-soft);color:var(--warning);font-size:10.5px;line-height:1.5}
.diligence-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.diligence-item{display:flex;align-items:flex-start;gap:6px;padding:6px;border:1px solid var(--line);border-radius:3px;background:var(--surface-2);font-size:10px;color:var(--text-muted)}
.diligence-item.captured{color:var(--text-secondary)}
.diligence-mark{font:700 10px var(--mono);color:var(--text-muted)}
.diligence-item.captured .diligence-mark{color:var(--positive)}
.workflow-panel{margin:13px 0;padding:11px;border:1px solid var(--line-strong);border-radius:4px;background:var(--surface-2)}
.workflow-header{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.workflow-panel h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.07em}
.workflow-coverage{display:inline-flex;padding:3px 6px;border:1px solid var(--accent);border-radius:3px;background:var(--accent-soft);color:var(--accent);font:650 9px var(--mono)}
.workflow-subhead{margin:12px 0 3px;padding-top:10px;border-top:1px solid var(--line);font:650 9.5px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-secondary)}
.workflow-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 8px}
.workflow-field{display:grid;gap:4px;margin-top:8px;color:var(--text-muted);font-size:10px}
.workflow-field.wide{grid-column:1/-1}
.workflow-field select,.workflow-field input,.workflow-field textarea{width:100%;border:1px solid var(--control-line);border-radius:3px;background:var(--surface-1);color:var(--text);padding:7px;font-size:11px}
.workflow-field textarea{min-height:72px;resize:vertical;line-height:1.45}
.workflow-field textarea.compact{min-height:56px}
.workflow-gates{display:grid;gap:5px;margin-top:8px;border:0;padding:0}
.workflow-gates legend{font-size:10px;color:var(--text-muted);margin-bottom:3px}
.workflow-gate{display:flex;align-items:flex-start;gap:7px;padding:7px;border:1px solid var(--line);border-radius:3px;background:var(--surface-1);color:var(--text-secondary);font-size:10px;line-height:1.4;cursor:pointer}
.workflow-gate input{width:16px;height:16px;flex:0 0 auto;margin:0;accent-color:var(--accent)}
.workflow-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.workflow-warning{font-size:9.5px;color:var(--text-muted);line-height:1.45;margin-top:8px}
.orphaned-queue{display:none;border-bottom:1px solid var(--line);background:var(--surface-1);padding:10px 12px}
.orphaned-queue.visible{display:block}
.orphaned-queue h2{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--warning)}
.orphaned-queue>p{margin:4px 0 8px;color:var(--text-muted);font-size:10px;line-height:1.45}
.orphaned-list{display:grid;gap:6px}
.orphaned-item{display:grid;grid-template-columns:88px minmax(0,1fr) auto;gap:8px;padding:8px;border:1px solid var(--line);border-radius:3px;background:var(--surface-2);font-size:10px;color:var(--text-secondary)}
.orphaned-item time,.orphaned-item small{font:9.5px var(--mono);color:var(--text-muted)}
.orphaned-item strong{display:block;color:var(--text);font-size:10.5px}
.orphaned-item p{margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text-muted)}
.orphaned-item a{color:var(--accent);text-decoration:none}
body.density-comfortable .idea-title{font-size:12.5px;line-height:1.5}
body.density-comfortable .idea-context,body.density-comfortable .instrument-secondary,body.density-comfortable .article-subtitle{font-size:11px}
body.density-comfortable .manager-name,body.density-comfortable .article-title{font-size:12px}
body.density-comfortable .data-cell{padding-top:9px;padding-bottom:9px}

/* Overlays and feedback */
.drawer-backdrop{display:none}
.toast{
  position:fixed;left:50%;bottom:22px;z-index:200;transform:translate(-50%,20px);
  padding:9px 13px;border:1px solid var(--line-strong);border-radius:4px;
  background:var(--surface-raised);color:var(--text);box-shadow:var(--shadow);
  font-size:11px;opacity:0;pointer-events:none;transition:opacity .16s,transform .16s
}
.toast.show{opacity:1;transform:translate(-50%,0)}
dialog{
  width:min(720px,calc(100vw - 32px));border:1px solid var(--line-strong);border-radius:5px;
  background:var(--surface-raised);color:var(--text);padding:0;box-shadow:var(--shadow)
}
dialog::backdrop{background:var(--backdrop)}
.dialog-header{display:flex;align-items:center;justify-content:space-between;padding:13px 15px;border-bottom:1px solid var(--line)}
.dialog-header h2{font-size:13px}
.shortcut-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);margin:15px;border:1px solid var(--line)}
.shortcut-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px;background:var(--surface-2);font-size:10.5px;color:var(--text-secondary)}
kbd{font:9px var(--mono);border:1px solid var(--line-strong);background:var(--surface-1);border-radius:3px;padding:2px 6px;color:var(--text)}
.dialog-foot{padding:0 15px 15px;color:var(--text-muted);font-size:10px}
.method-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:15px;border-top:1px solid var(--line)}
.method-card{border:1px solid var(--line);border-radius:3px;background:var(--surface-2);padding:11px}
.method-card h3{font:650 10px var(--mono);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}
.method-card p,.method-card li{font-size:10.5px;line-height:1.55;color:var(--text-secondary)}
.method-card ul{margin:6px 0 0;padding-left:17px}
.method-card a{color:var(--accent);text-underline-offset:2px}
noscript{position:fixed;inset:0;z-index:1000;display:grid;place-items:center;background:var(--bg);color:var(--text);padding:30px}

::-webkit-scrollbar{width:7px;height:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line-strong);border-radius:4px}
*{scrollbar-color:var(--line-strong) transparent}

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
  .app-header{grid-template-columns:auto minmax(220px,1fr) auto;gap:10px}
  .brand{min-width:0}
  .brand-sub{display:none}
  .freshness{display:none}
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
@media(max-width:760px){
  :root{--header-h:54px;--kpi-h:42px}
  .app-header{grid-template-columns:auto minmax(0,1fr) auto;padding:0 9px;gap:7px}
  .brand-name{display:none}
  .brand{min-width:auto}
  .brand-mark{width:32px;height:32px}
  .search-key,#method-button,.button-label{display:none}
  #search{height:44px;padding-right:9px;font-size:16px}
  .header-right{gap:5px}
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
  .filter-chip,.primary-action,.secondary-action,.inspector-close,.load-more{min-height:44px}
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
  .intel-section-grid{grid-template-columns:1fr}
  .intel-section,.intel-section.full{grid-column:1;min-height:0;padding:13px 14px}
  .intel-passage{font-size:12px}
  .intel-actions{padding:12px 14px}
  .intel-actions-note{flex:1 0 100%;margin-left:0;text-align:left}
  .intel-side{grid-template-columns:1fr}
  .intel-stream-list{grid-template-columns:1fr}
  .intel-article-card{min-height:44px}
  .brief-hero{display:block}
  .brief-actions{justify-content:flex-start;margin-top:10px}
  .brief-metrics{grid-template-columns:1fr 1fr}
  .brief-grid{grid-template-columns:1fr}
  .brief-record{grid-template-columns:68px 80px minmax(0,1fr)}
  .brief-record .documentation-badge{display:none}
  .diligence-grid{grid-template-columns:1fr}
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
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}
}
@media(forced-colors:active){
  .status-dot,.facet-option.active::before,.mix-segment{forced-color-adjust:none}
}
</style>
</head>
<body class="density-compact" data-view="briefing">
<a class="skip-link" href="#main-panel">Skip to research results</a>
<h1 class="sr-only">Navnoor Research Terminal</h1>

<header class="app-header">
  <div class="brand" aria-label="Navnoor Research Terminal">
    <div class="brand-mark" aria-hidden="true">N/R</div>
    <div>
      <div class="brand-name">Navnoor Research Terminal</div>
      <div class="brand-sub">Source-backed article intelligence</div>
    </div>
  </div>
  <div class="global-search">
    <label class="sr-only" for="search">Search claim, entity, market, evidence, or article</label>
    <span class="search-glyph" aria-hidden="true">⌕</span>
    <input id="search" type="search" autocomplete="off" spellcheck="false"
      placeholder="Search claim, entity, market, evidence…" aria-keyshortcuts="/">
    <span class="search-key" aria-hidden="true">/</span>
  </div>
  <div class="header-right">
    <div class="freshness" id="freshness-summary"><span class="status-dot" id="freshness-dot"></span><span id="freshness-label">Research health unknown</span></div>
    <button class="utility-button" id="method-button" type="button" aria-label="Show data methodology">Method</button>
    <button class="utility-button" id="theme-button" type="button" aria-label="Switch color theme">Light</button>
    <button class="utility-button" id="shortcut-button" type="button" aria-label="Show keyboard shortcuts">?</button>
    <button class="utility-button" id="mobile-filter-button" type="button" aria-expanded="false" aria-controls="filter-rail">Filters</button>
  </div>
</header>

<section class="kpi-strip" aria-label="Article intelligence coverage">
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-latest">—</span><span class="kpi-label">Research<br>published through</span>
  </div>
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-evidence">0</span><span class="kpi-label">Articles with<br>contextual evidence</span>
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
      <div class="filter-heading"><h2 id="direction-filter-label">Parsed stance / structure</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="direction"><span>Any direction</span><span class="facet-count" data-count-clear="direction"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="long"><span>Long</span><span class="facet-count" data-count-direction="long"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="short"><span>Short</span><span class="facet-count" data-count-direction="short"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="arbitrage/relative value"><span>Arbitrage / RV</span><span class="facet-count" data-count-direction="arbitrage/relative value"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="long/short"><span>Long / short</span><span class="facet-count" data-count-direction="long/short"></span></button>
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
      <p class="filter-note">Five fields: market, parsed stance, underlying, thesis, and numeric context. Coverage is not investment quality.</p>
    </section>

    <section class="filter-group" aria-labelledby="content-filter-label">
      <div class="filter-heading"><h2 id="content-filter-label">Content access</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="content"><span>Full + excerpt</span><span class="facet-count" data-count-clear="content"></span></button>
        <button class="facet-option" type="button" data-filter="content" data-value="full"><span>Full text indexed</span><span class="facet-count" data-count-content="full"></span></button>
        <button class="facet-option" type="button" data-filter="content" data-value="excerpt"><span>Excerpt indexed</span><span class="facet-count" data-count-content="excerpt"></span></button>
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
        <button class="view-tab active" type="button" data-view="briefing">Intelligence Brief</button>
        <button class="view-tab" type="button" data-view="ideas">Evidence Explorer</button>
        <button class="view-tab" type="button" data-view="research">Article Library</button>
        <button class="view-tab" type="button" data-view="queue">Review Queue <span id="saved-count"></span></button>
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
      <button class="command-button active" type="button" data-action="inspector" aria-pressed="true" aria-expanded="true" aria-controls="inspector">Inspector</button>
    </div>

    <div class="active-filters empty" id="active-filters" aria-label="Active filters"></div>

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
      <div class="direction-mix" id="direction-mix" aria-label="Visible direction distribution"></div>
      <span class="mix-legend" id="mix-legend"></span>
    </section>

    <section class="briefing-shell" id="briefing-shell" aria-label="Article intelligence brief"></section>

    <section class="table-shell" id="table-shell" aria-label="Research results">
      <div class="data-table" id="data-table" role="grid" aria-rowcount="0" aria-multiselectable="false">
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

<input class="sr-only" id="queue-restore-input" type="file" accept="application/json,.json" tabindex="-1">

<div class="toast" id="toast" role="status" aria-live="polite"></div>
<div class="sr-only" id="announcer" aria-live="polite" aria-atomic="true"></div>

<dialog id="shortcut-dialog" aria-labelledby="shortcut-title">
  <div class="dialog-header">
    <h2 id="shortcut-title">Keyboard workflow &amp; data method</h2>
    <button class="inspector-close" type="button" data-close-dialog aria-label="Close method and keyboard reference">×</button>
  </div>
  <div class="shortcut-grid">
    <div class="shortcut-item"><span>Focus global search</span><kbd>/</kbd></div>
    <div class="shortcut-item"><span>Jump to result grid</span><kbd>G</kbd></div>
    <div class="shortcut-item"><span>Move through rows</span><span><kbd>J</kbd> <kbd>K</kbd> <kbd>↑</kbd> <kbd>↓</kbd></span></div>
    <div class="shortcut-item"><span>First / last visible row</span><span><kbd>Home</kbd> <kbd>End</kbd></span></div>
    <div class="shortcut-item"><span>Open evidence inspector</span><kbd>Enter</kbd></div>
    <div class="shortcut-item"><span>Open original research</span><kbd>O</kbd></div>
    <div class="shortcut-item"><span>Add or archive selected decision packet</span><kbd>S</kbd></div>
    <div class="shortcut-item"><span>Copy selected citation</span><kbd>C</kbd></div>
    <div class="shortcut-item"><span>Toggle filters</span><kbd>F</kbd></div>
    <div class="shortcut-item"><span>Brief / Monitor / Library / Queue</span><span><kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> <kbd>4</kbd></span></div>
    <div class="shortcut-item"><span>Close panel</span><kbd>Esc</kbd></div>
    <div class="shortcut-item"><span>Show this reference</span><kbd>?</kbd></div>
  </div>
  <div class="method-grid">
    <section class="method-card">
      <h3>Scope &amp; refresh</h3>
      <p>This is a research index covering one author's Substack and Medium publication channels. Cross-posted articles are deduplicated. Scheduled checks run at 9 AM, 1 PM, and 10 PM Asia/Kolkata.</p>
    </section>
    <section class="method-card">
      <h3>Article dossier method</h3>
      <ul>
        <li>Author framing uses the validated exact lead passage, with a screened source subtitle only when no lead is available.</li>
        <li>Evidence, mechanism, countercase, falsifier, and implementation are exact passages under authored section headings.</li>
        <li>Every dossier span is validated against the article body with offsets and a SHA-256 hash before publication.</li>
        <li>Full and Excerpt describe indexed access, never research quality.</li>
      </ul>
    </section>
    <section class="method-card">
      <h3>Decision boundary</h3>
      <p>Records are research observations—not verified trades, current holdings, or recommendations. This terminal supports published-source intake and a human-entered decision packet. It does not contain live prices, positions, P&amp;L, sizing, execution, portfolio risk, liquidity, financing, counterparties, investor records, or compliance approvals.</p>
    </section>
    <section class="method-card">
      <h3>Local decision queue</h3>
      <p>Queue packets and self-attested diligence gates stay in this browser unless you export a backup. They are not an authenticated or immutable enterprise audit record. Do not enter confidential, personal, client, position, or regulated information.</p>
    </section>
    <section class="method-card">
      <h3>Institutional basis</h3>
      <p>The workflow is informed by the <a href="https://www.sec.gov/newsroom/press-releases/2024-17" target="_blank" rel="noopener noreferrer">SEC’s private-fund risk dimensions</a>, <a href="https://www.aima.org/article/presenting-the-2025-edition.html" target="_blank" rel="noopener noreferrer">AIMA’s 2025 manager DDQ</a>, and <a href="https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a" target="_blank" rel="noopener noreferrer">CFA Institute’s reasonable-basis standard</a>. These references guide questions; they do not certify a packet.</p>
    </section>
    <section class="method-card">
      <h3>Owner operating stack</h3>
      <p>Daily P&amp;L, exposure, leverage, concentration, stress, cash and margin require an OMS/PMS, market data, administrator, prime-broker and risk feeds. Operations, governance and investor oversight require controlled compliance, accounting and CRM systems.</p>
    </section>
  </div>
  <p class="dialog-foot">Shortcuts are disabled while typing in a form control. Always review the original publication and perform independent diligence.</p>
</dialog>

<noscript><div>This research terminal requires JavaScript to filter and inspect the embedded dataset.</div></noscript>

<script>
const ARTICLES = __ARTICLES_JSON__;
const IDEAS = __IDEAS_JSON__;
const SNAPSHOT = __SNAPSHOT_JSON__;

let briefArchivePromise = null;
let briefArchiveReady = false;
let briefArchiveFailed = false;
function loadBriefArchive() {
  if (briefArchivePromise) return briefArchivePromise;
  briefArchiveFailed = false;
  const archiveUrl = 'article_briefs.json?v=' + encodeURIComponent(String(SNAPSHOT.data_checksum || ''));
  briefArchivePromise = fetch(archiveUrl,{credentials:'same-origin',cache:'no-cache'}).then(function (response) {
    if (!response.ok) throw new Error('Deferred article dossiers are unavailable');
    return response.json();
  }).then(function (payload) {
    if (!payload || payload.schema_version !== 1 || payload.data_checksum !== SNAPSHOT.data_checksum || !payload.briefs || typeof payload.briefs !== 'object') {
      throw new Error('Deferred article dossiers do not match this release');
    }
    Object.keys(payload.briefs).forEach(function (id) {
      const article = ARTICLE_BY_ID.get(id);
      if (article) {
        article.brief = payload.briefs[id];
        refreshArticleSearch(article);
      }
    });
    briefArchiveReady = true;
    briefArchiveFailed = false;
    relevanceScoreCache = new WeakMap();
    return payload.briefs;
  }).catch(function (error) {
    briefArchiveFailed = true;
    briefArchivePromise = null;
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
    article.brief = briefs[article.id] || {lead:null,sections:[],fallback_evidence:null,checkpoints:[]};
    return article.brief;
  }).catch(function () {
    article._briefLoadFailed = true;
    return null;
  });
}

const ARTICLE_BY_ID = new Map(ARTICLES.map(function (article) { return [article.id, article]; }));
const IDEA_BY_ID = new Map(IDEAS.map(function (idea) { return [idea.id, idea]; }));
const MANAGER_LABELS = new Map(IDEAS.filter(function (idea) { return idea.manager_key; }).map(function (idea) { return [idea.manager_key,idea.manager]; }));
const MANAGERS = Array.from(MANAGER_LABELS.keys()).sort(function (a, b) { return MANAGER_LABELS.get(a).localeCompare(MANAGER_LABELS.get(b)); });
const MAX_DATE = ARTICLES.reduce(function (latest, article) { return article.date > latest ? article.date : latest; }, '1970-01-01');
const VALID_SOURCES = new Set(['substack','medium']);
const VALID_DIRECTIONS = new Set(['long','short','arbitrage/relative value','long/short','unspecified']);
const VALID_INSTRUMENTS = new Set(['equity','option','volatility','bond','futures','commodity','FX','swap','CDS','repo','prediction_market','weather_derivative','unspecified']);
const VALID_QUALITY = new Set(['quant','thesis','outcome','manager']);
const VALID_CONTENT = new Set(['full','excerpt']);
const VALID_DOCUMENTATION = new Set(['triage','documented','strong','needs-context','review']);
const VALID_QUEUE_STATUSES = new Set(['review','diligence','monitor','archived']);
const VALID_PRIORITIES = new Set(['low','normal','high']);
const VALID_CONFIDENCE = new Set(['unrated','low','medium','high']);
const VALID_BRIEF_LENSES = new Set(['all','checkpoint','evidence','countercase','falsifier','implementation']);
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
const PAGE_SIZE = {briefing:24,ideas:100,research:80,queue:100};
const WORKFLOW_KEY = 'nrt-decision-queue-v2';
const LEGACY_WORKFLOW_KEY = 'nrt-decision-queue-v1';
const LAST_SEEN_KEY = 'nrt-last-seen-publication';

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
    long:'Long', short:'Short', 'arbitrage/relative value':'Arb / RV',
    'long/short':'Long / short', unspecified:'No reliable stance'
  };
  return labels[value] || 'No reliable stance';
}
function compactDirectionLabel(value) {
  if (value === 'unspecified') return 'No stance';
  if (value === 'long/short') return 'L / S';
  return directionLabel(value);
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
function escapeHtml(value) {
  return String(value || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}
function safeUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const validHost = host === 'navnoorbawa.substack.com' || host === 'medium.com' || host.endsWith('.medium.com');
    return url.protocol === 'https:' && validHost ? url.href : '#';
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
  const lead = article && article.brief && article.brief.lead;
  return String((lead && lead.text) || (article && article.subtitle) || 'No authored framing excerpt is available in this index.');
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
function isNewDate(date) {
  return Boolean(date && date > NEW_SINCE_DATE);
}
function reviewFlagged(idea) {
  return Boolean(idea.reference_line || idea.negation_risk || idea.description_truncated);
}
function validDateInput(value) {
  const text = String(value || '');
  return /^\d{4}-\d{2}-\d{2}$/.test(text) && !Number.isNaN(new Date(text + 'T00:00:00Z').getTime()) ? text : '';
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
    passage:String(passageText(idea) || '').slice(0,1200),
    direction:String(idea.direction || 'unspecified').slice(0,40),
    instruments:(idea.instruments || []).slice(0,20).map(function (value) { return String(value).slice(0,80); }),
    underlying:String(idea.underlying || '').slice(0,500),
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
    passage:String(value.passage || '').slice(0,1200),
    direction:VALID_DIRECTIONS.has(value.direction) ? value.direction : 'unspecified',
    instruments:Array.isArray(value.instruments) ? value.instruments.slice(0,20).map(function (item) { return String(item).slice(0,80); }) : [],
    underlying:String(value.underlying || '').slice(0,500),
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
    verified_at:String(value.verified_at || '').slice(0,40),
    checks:checks,
    source_snapshot:snapshot,
    updated_at:String(value.updated_at || '').slice(0,40)
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
function persistWorkflow() {
  savedIdeas = new Set(workflowItems.keys());
  try {
    localStorage.setItem(WORKFLOW_KEY,JSON.stringify(Array.from(workflowItems.values()).slice(0,MAX_QUEUE_ITEMS)));
    return true;
  } catch (_error) {
    showToast('Queue could not be saved in this browser');
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
let workflowNeedsMigration = false;
try {
  const currentStored = JSON.parse(localStorage.getItem(WORKFLOW_KEY) || 'null');
  const legacyStored = JSON.parse(localStorage.getItem(LEGACY_WORKFLOW_KEY) || 'null');
  const stored = Array.isArray(currentStored) ? currentStored : Array.isArray(legacyStored) ? legacyStored : [];
  if (Array.isArray(stored)) {
    stored.slice(0,MAX_QUEUE_ITEMS).forEach(function (value) {
      const item = normalizeWorkflowItem(value);
      if (item) workflowItems.set(item.id,item);
    });
    if (!Array.isArray(currentStored) && workflowItems.size) workflowNeedsMigration = true;
  }
  if (!workflowItems.size) {
    const legacy = JSON.parse(localStorage.getItem('nrt-saved-ideas') || '[]');
    if (Array.isArray(legacy)) legacy.forEach(function (id) {
      if (IDEA_BY_ID.has(id) && workflowItems.size < MAX_QUEUE_ITEMS) {
        const item = newWorkflowItem(id);
        if (item) workflowItems.set(id,item);
      }
    });
    if (workflowItems.size) workflowNeedsMigration = true;
  }
} catch (_error) {}
let savedIdeas = new Set(workflowItems.keys());
if (workflowNeedsMigration) persistWorkflow();

let lastSeenPublication = '';
try { lastSeenPublication = localStorage.getItem(LAST_SEEN_KEY) || ''; } catch (_error) {}
const firstVisitCutoff = (function () {
  const newest = new Date(MAX_DATE + 'T00:00:00Z');
  newest.setUTCDate(newest.getUTCDate() - 7);
  return newest.toISOString().slice(0,10);
})();
let NEW_SINCE_DATE = lastSeenPublication
  ? (lastSeenPublication > MAX_DATE ? MAX_DATE : lastSeenPublication)
  : firstVisitCutoff;

let storedDensity = 'compact';
let storedInspector = true;
try {
  storedDensity = localStorage.getItem('nrt-density') === 'comfortable' ? 'comfortable' : 'compact';
  storedInspector = localStorage.getItem('nrt-inspector') !== 'hidden';
} catch (_error) {}

const state = {
  view:'briefing',
  query:'',
  sources:new Set(),
  directions:new Set(),
  instruments:new Set(),
  managers:new Set(),
  quality:new Set(),
  content:new Set(),
  queueStatuses:new Set(),
  documentation:'all',
  newOnly:false,
  range:'all',
  coverage:'all',
  briefLens:'all',
  sort:'newest',
  density:storedDensity,
  selected:'',
  limit:PAGE_SIZE.briefing,
  inspector:storedInspector
};

function hydrateFromHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const hashView = params.get('view') === 'saved' ? 'queue' : params.get('view');
  if (['briefing','ideas','research','queue'].includes(hashView)) state.view = hashView;
  state.query = params.get('q') || '';
  state.sources = setFromParam(params,'src',VALID_SOURCES);
  state.directions = setFromParam(params,'dir',VALID_DIRECTIONS);
  state.instruments = setFromParam(params,'inst',VALID_INSTRUMENTS);
  state.managers = setFromParam(params,'mgr',new Set(MANAGERS));
  state.quality = setFromParam(params,'evidence',VALID_QUALITY);
  state.content = setFromParam(params,'content',VALID_CONTENT);
  state.queueStatuses = setFromParam(params,'queue',VALID_QUEUE_STATUSES);
  if (VALID_DOCUMENTATION.has(params.get('doc'))) state.documentation = params.get('doc');
  state.newOnly = params.get('new') === '1';
  if (['30d','90d','1y','all'].includes(params.get('range'))) state.range = params.get('range');
  if (['all','ideas','research'].includes(params.get('coverage'))) state.coverage = params.get('coverage');
  if (VALID_BRIEF_LENSES.has(params.get('lens'))) state.briefLens = params.get('lens');
  if (params.get('sort')) state.sort = params.get('sort');
  if (['compact','comfortable'].includes(params.get('density'))) state.density = params.get('density');
  state.selected = params.get('selected') || '';
  state.limit = PAGE_SIZE[state.view];
}

function updateHash() {
  const params = new URLSearchParams();
  if (state.view !== 'briefing') params.set('view',state.view);
  if (state.query) params.set('q',state.query);
  if (state.sources.size) params.set('src',Array.from(state.sources).join('|'));
  if (state.directions.size) params.set('dir',Array.from(state.directions).join('|'));
  if (state.instruments.size) params.set('inst',Array.from(state.instruments).join('|'));
  if (state.managers.size) params.set('mgr',Array.from(state.managers).join('|'));
  if (state.quality.size) params.set('evidence',Array.from(state.quality).join('|'));
  if (state.content.size) params.set('content',Array.from(state.content).join('|'));
  if (state.queueStatuses.size) params.set('queue',Array.from(state.queueStatuses).join('|'));
  if (state.documentation !== 'all') params.set('doc',state.documentation);
  if (state.newOnly) params.set('new','1');
  if (state.range !== 'all') params.set('range',state.range);
  if (state.coverage !== 'all' && state.view === 'research') params.set('coverage',state.coverage);
  if (state.briefLens !== 'all' && state.view === 'briefing') params.set('lens',state.briefLens);
  if (state.sort !== 'newest') params.set('sort',state.sort);
  if (state.density !== 'compact') params.set('density',state.density);
  if (state.selected) params.set('selected',state.selected);
  const encoded = params.toString();
  history.replaceState(null,'',encoded ? '#' + encoded : location.pathname + location.search);
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
  if (!inDateRange(article.date)) return false;
  if (state.newOnly && !isNewDate(article.date)) return false;
  if (skip !== 'direction' && state.directions.size && !state.directions.has(idea.direction)) return false;
  if (skip !== 'instrument' && !setMatches(state.instruments,idea.instruments)) return false;
  if (skip !== 'manager' && state.managers.size && !state.managers.has(idea.manager_key)) return false;
  if (skip !== 'quality' && !qualityMatches(idea,false)) return false;
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
  if (!inDateRange(article.date)) return false;
  if (state.newOnly && !isNewDate(article.date)) return false;
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
    button.tabIndex = window.innerWidth <= 760 ? -1 : 0;
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
  const newBadge = isNewDate(article.date) ? '<span class="new-badge">New</span>' : '';
  const coverage = packetCoverage(workflow);
  const workflowBadge = workflow ? '<span class="workflow-badge">' + escapeHtml(workflow.status) + '</span>' +
    '<span class="workflow-badge coverage" title="Decision packet coverage; not approval">' + coverage.completed + '/' + coverage.total + '</span>' +
    (reviewIsOverdue(workflow) ? '<span class="review-flag">Overdue</span>' : '') : '';
  const review = reviewFlagged(idea) ? '<span class="review-flag" title="Extraction review recommended">Review</span>' : '';
  return '<div class="data-row idea-grid" role="row" data-record-id="' + idea.id + '" tabindex="' + (selected ? '0' : '-1') + '" aria-selected="' + selected + '" aria-keyshortcuts="Enter Space ArrowUp ArrowDown Home End O S C" aria-label="' + escapeHtml(rowLabel) + '">' +
    '<div class="data-cell cell-date" role="gridcell"><time datetime="' + article.date + '">' + shortDate(article.date) + '</time>' + newBadge + '</div>' +
    '<div class="data-cell cell-bias" role="gridcell"><span class="direction-badge ' + directionClass(idea.direction) + '">' + directionLabel(idea.direction) + '</span></div>' +
    '<div class="data-cell cell-market" role="gridcell"><div class="instrument-primary">' + escapeHtml(instrumentLabel(primaryInstrument)) + '</div>' +
      (otherInstruments ? '<div class="instrument-secondary">+' + escapeHtml(otherInstruments) + '</div>' : '') + '</div>' +
    '<div class="data-cell cell-manager" role="gridcell"><div class="manager-name ' + (idea.manager ? '' : 'missing') + '">' + escapeHtml(idea.manager || '—') + workflowBadge + '</div></div>' +
    '<div class="data-cell cell-idea" role="gridcell"><div class="idea-title">' + escapeHtml(passageText(idea)) + '</div>' +
      '<div class="idea-context">' + escapeHtml(idea.underlying || article.title) + (review ? ' · ' + review : '') + '</div></div>' +
    '<div class="data-cell cell-evidence" role="gridcell">' + evidenceMarkup(idea) + '</div>' +
    '<div class="data-cell cell-source" role="gridcell"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><div class="instrument-secondary">' + (article.content_status === 'full' ? 'Full' : 'Excerpt') + '</div></div>' +
    '<div class="data-cell cell-open" role="gridcell"><a class="row-open" tabindex="-1" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer" aria-label="Open ' + escapeHtml(article.title) + ' in a new tab">↗</a></div>' +
    '</div>';
}
function researchRow(article) {
  const selected = state.selected === article.id;
  const coverage = article.trade_count === 0
    ? '<span class="coverage-badge coverage-research">Research-only</span>'
    : article.content_status === 'full'
      ? '<span class="coverage-badge coverage-full">Full text</span>'
      : '<span class="coverage-badge coverage-excerpt">Excerpt</span>';
  const read = article.content_status === 'excerpt'
    ? 'Preview'
    : article.read_minutes ? article.read_minutes + ' min' : '—';
  const rowLabel = formatDate(article.date) + ', ' + sourceLabel(article.source) + ', ' + article.title + ', ' +
    number(article.trade_count) + ' research observations, ' + (article.content_status === 'full' ? 'full text indexed' : 'excerpt indexed');
  return '<div class="data-row research-grid" role="row" data-record-id="' + article.id + '" tabindex="' + (selected ? '0' : '-1') + '" aria-selected="' + selected + '" aria-keyshortcuts="Enter Space ArrowUp ArrowDown Home End O C" aria-label="' + escapeHtml(rowLabel) + '">' +
    '<div class="data-cell cell-date" role="gridcell"><time datetime="' + article.date + '">' + shortDate(article.date) + '</time>' + (isNewDate(article.date) ? '<span class="new-badge">New</span>' : '') + '</div>' +
    '<div class="data-cell cell-source" role="gridcell"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span></div>' +
    '<div class="data-cell cell-article" role="gridcell"><div class="article-title">' + escapeHtml(article.title) + '</div><div class="article-subtitle">' + escapeHtml(article.subtitle || 'No abstract available') + '</div></div>' +
    '<div class="data-cell cell-count number-cell" role="gridcell">' + number(article.trade_count) + '</div>' +
    '<div class="data-cell cell-coverage" role="gridcell">' + coverage + '</div>' +
    '<div class="data-cell cell-read number-cell" role="gridcell">' + read + '</div>' +
    '<div class="data-cell cell-open" role="gridcell"><a class="row-open" tabindex="-1" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer" aria-label="Open ' + escapeHtml(article.title) + ' in a new tab">↗</a></div>' +
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
  document.getElementById('empty-title').textContent = queueEmpty ? 'Decision queue is empty on this device' : 'No matching records';
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
  document.getElementById('direction-mix').innerHTML = segments.map(function (row) {
    const width = counts[row[0]] / total * 100;
    return '<span class="mix-segment ' + row[1] + '" style="width:' + width.toFixed(3) + '%" title="' + directionLabel(row[0]) + ': ' + number(counts[row[0]]) + '"></span>';
  }).join('');
  document.getElementById('mix-legend').textContent =
    'L ' + number(counts.long) + ' · S ' + number(counts.short) + ' · RV ' +
    number(counts['arbitrage/relative value']) + ' · L/S ' + number(counts['long/short']) +
    ' · No reliable stance ' + number(counts.unspecified);
}

const BRIEF_KIND_LABELS = {
  evidence:'Contextual evidence', mechanism:'Mechanism', countercase:'Countercase / limitation',
  falsifier:'What would change the view', implementation:'Implementation / what to watch'
};
function highlightArticleNumbers(value) {
  return escapeHtml(value).replace(
    /([$€£¥]\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:%|bp\b|bps\b|basis points?\b|[x×]\b|million\b|billion\b|trillion\b))/gi,
    '<mark>$1</mark>'
  );
}
function articleReasons(article) {
  const reasons = [];
  if (isNewDate(article.date)) reasons.push(['New','accent']);
  reasons.push([article.content_status === 'full' ? 'Full source' : 'Excerpt only',article.content_status === 'full' ? '' : 'evidence-gap']);
  if (articleHasEvidence(article)) reasons.push(['Contextual evidence','accent']);
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
  return '<span class="source-tail">Exact article passage' + (span && span.truncated ? ' · shortened for display' : '') + '</span>';
}
function intelligenceSection(label,heading,span,full) {
  if (!span || !span.text) return '';
  return '<section class="intel-section' + (full ? ' full' : '') + '">' +
    '<div class="intel-label">' + escapeHtml(label) + '</div>' +
    (heading ? '<h3>' + escapeHtml(heading) + '</h3>' : '') +
    '<p class="intel-passage">' + highlightArticleNumbers(span.text) + '</p>' +
    exactPassageTail(span) + '</section>';
}
function intelligenceCard(article) {
  return '<button class="intel-article-card" type="button" data-brief-article="' + article.id + '">' +
    '<span class="intel-meta"><time datetime="' + article.date + '">' + escapeHtml(shortDate(article.date)) + '</time><span>·</span><span>' + sourceLabel(article.source) + '</span><span>·</span><span>' + (article.content_status === 'full' ? (article.read_minutes ? article.read_minutes + ' min' : 'Full text') : 'Excerpt') + '</span></span>' +
    '<span class="intel-card-title">' + escapeHtml(article.title) + '</span><span class="intel-card-claim">' + escapeHtml(articleClaim(article)) + '</span>' +
    '<span class="intel-reasons">' + reasonChips(article,3) + '</span></button>';
}
let pendingBriefFocus = null;
function restorePendingBriefFocus() {
  if (!pendingBriefFocus) return;
  const pending = pendingBriefFocus;
  pendingBriefFocus = null;
  requestAnimationFrame(function () {
    let target = null;
    if (pending.kind === 'lens') {
      target = document.querySelector('[data-brief-lens="' + pending.value + '"]');
    } else {
      target = document.getElementById('lead-article-title') ||
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
  const lenses = [
    ['all','Latest'],['checkpoint','Public checkpoints'],['evidence','Contextual evidence'],
    ['countercase','Countercase'],['falsifier','Falsifiers'],['implementation','Implementation / capacity']
  ];
  if (state.briefLens !== 'all' && briefArchiveFailed && !briefArchiveReady) {
    shell.innerHTML = '<div class="intel-wrap"><div class="intel-head"><div class="intel-head-copy"><div class="brief-kicker">Research intelligence brief</div><h2>Older dossiers could not be verified</h2><p>The release-bound article asset did not load. The terminal will not mix passages from a different release.</p></div></div><div class="intel-empty"><button class="secondary-action" type="button" data-retry-briefs>Retry exact dossier load</button></div></div>';
    restorePendingBriefFocus();
    return;
  }
  if (state.briefLens !== 'all' && !briefArchiveReady && !briefArchiveFailed) {
    shell.innerHTML = '<div class="intel-wrap"><div class="intel-head"><div class="intel-head-copy"><div class="brief-kicker">Research intelligence brief · source-backed</div><h2>Loading the complete article lens…</h2><p>Retrieving deferred exact passages for older articles and checking them against this release.</p></div></div><div class="intel-side-card"><div class="intel-empty">Preparing ' + escapeHtml(lenses.find(function (row) { return row[0] === state.briefLens; })[1]) + ' across the archive…</div></div></div>';
    loadBriefArchive().then(function () {
      if (state.view === 'briefing' && state.briefLens !== 'all') render();
    }).catch(function () {
      briefArchiveFailed = true;
      if (state.view === 'briefing') render();
    });
    return;
  }
  if (!records.length) {
    shell.innerHTML = '<div class="intel-wrap"><div class="intel-head"><div class="intel-head-copy"><div class="brief-kicker">Research intelligence brief</div><h2>No article matches this lens</h2><p>Clear the search or return to the latest-article lens.</p></div><div class="intel-lenses">' + lenses.map(function (row) { return '<button class="intel-lens' + (state.briefLens === row[0] ? ' active' : '') + '" type="button" data-brief-lens="' + row[0] + '" aria-pressed="' + String(state.briefLens === row[0]) + '">' + row[1] + '</button>'; }).join('') + '</div></div><div class="intel-empty"><button class="secondary-action" type="button" data-brief-lens="all">Show latest research</button></div></div>';
    restorePendingBriefFocus();
    return;
  }
  let selected = ARTICLE_BY_ID.get(state.selected);
  if (!selected || !records.some(function (article) { return article.id === selected.id; })) selected = records[0];
  state.selected = selected.id;
  if (!selected.brief && !selected._briefLoadFailed) {
    shell.innerHTML = '<div class="intel-wrap"><div class="intel-head"><div class="intel-head-copy"><div class="brief-kicker">Research intelligence brief · source-backed</div><h2>Loading the exact article dossier…</h2><p>The older dossier is stored as a deferred release asset so the latest research opens immediately.</p></div></div><div class="intel-side-card"><div class="intel-empty">Validating this dossier against release ' + escapeHtml(String(SNAPSHOT.data_checksum || '').slice(0,12)) + '…</div></div></div>';
    ensureArticleBrief(selected).then(function (briefValue) {
      if (briefValue && state.view === 'briefing' && state.selected === selected.id) render();
      else if (!briefValue && state.view === 'briefing' && state.selected === selected.id) renderIntelligenceBrief(records);
    });
    return;
  }
  if (!selected.brief && selected._briefLoadFailed) {
    shell.innerHTML = '<div class="intel-wrap"><div class="intel-head"><div class="intel-head-copy"><div class="brief-kicker">Research intelligence brief</div><h2>This exact dossier could not be verified</h2><p>The release-bound article asset did not load. No evidence-absence conclusion has been drawn.</p></div></div><div class="intel-empty"><button class="secondary-action" type="button" data-retry-briefs>Retry exact dossier load</button></div></div>';
    restorePendingBriefFocus();
    return;
  }
  const brief = selected.brief || {lead:null,sections:[],fallback_evidence:null,checkpoints:[]};
  const authoredSections = (brief.sections || []).map(function (section,index) {
    return intelligenceSection(
      BRIEF_KIND_LABELS[section.kind] || 'Authored section',
      section.heading,
      section,
      section.kind === 'implementation'
    );
  }).join('');
  const fallbackEvidence = !briefSection(selected,'evidence') && brief.fallback_evidence
    ? intelligenceSection('Contextual evidence','Exact numerical passage identified in the article',brief.fallback_evidence,false)
    : '';
  const sectionMarkup = intelligenceSection('Author framing','Why the article says this matters',brief.lead,true) + fallbackEvidence + authoredSections;
  const today = new Date().toISOString().slice(0,10);
  const checkpoints = records.flatMap(function (article) {
    return ((article.brief && article.brief.checkpoints) || []).map(function (checkpoint) {
      return {article:article,checkpoint:checkpoint};
    });
  }).filter(function (row) { return row.checkpoint.date >= today; }).sort(function (a,b) {
    return a.checkpoint.date.localeCompare(b.checkpoint.date) || b.article.date.localeCompare(a.article.date);
  }).slice(0,5);
  const checkpointMarkup = checkpoints.map(function (row) {
    return '<button class="next-item checkpoint" type="button" data-brief-article="' + row.article.id + '"><time datetime="' + row.checkpoint.date + '">' + escapeHtml(formatDate(row.checkpoint.date)) + '</time><span class="next-title">' + escapeHtml(row.article.title) + '</span><span class="next-summary">' + escapeHtml(row.checkpoint.text) + '</span></button>';
  }).join('') || '<div class="intel-empty">No explicit future dated checkpoint is available in this view. Dates in articles are not treated as live market events unless the authored section names an event.</div>';
  const nextArticles = records.filter(function (article) { return article.id !== selected.id; }).slice(0,4);
  const nextMarkup = nextArticles.map(function (article) {
    return '<button class="next-item" type="button" data-brief-article="' + article.id + '"><time datetime="' + article.date + '">' + escapeHtml(shortDate(article.date)) + ' · ' + sourceLabel(article.source) + '</time><span class="next-title">' + escapeHtml(article.title) + '</span><span class="next-summary">' + escapeHtml(articleClaim(article)) + '</span></button>';
  }).join('');
  const stream = records.filter(function (article) { return article.id !== selected.id; }).slice(0,9).map(intelligenceCard).join('');
  const readLabel = selected.content_status === 'excerpt' ? 'Excerpt indexed' : selected.read_minutes ? selected.read_minutes + ' min read' : 'Full text indexed';
  shell.innerHTML = '<div class="intel-wrap">' +
    '<div class="intel-head"><div class="intel-head-copy"><div class="brief-kicker">Research intelligence brief · source-backed</div><h2>Start with the article. Test it against its evidence.</h2><p>Authored framing, contextual evidence, mechanism, limitations, falsifiers, and public checkpoints—shown as exact source passages, never converted into a synthetic recommendation.</p></div>' +
      '<div class="intel-lenses" aria-label="Article intelligence lenses">' + lenses.map(function (row) { return '<button class="intel-lens' + (state.briefLens === row[0] ? ' active' : '') + '" type="button" data-brief-lens="' + row[0] + '" aria-pressed="' + String(state.briefLens === row[0]) + '">' + row[1] + '</button>'; }).join('') + '</div></div>' +
    '<div class="intel-grid"><article class="intel-lead" aria-labelledby="lead-article-title"><div class="intel-lead-inner">' +
      '<div class="intel-meta"><span class="source-badge source-' + selected.source + '">' + sourceLabel(selected.source) + '</span><time datetime="' + selected.date + '">' + escapeHtml(formatDate(selected.date)) + '</time><span>·</span><span>' + escapeHtml(readLabel) + '</span><span>·</span><span>' + number(selected.trade_count) + ' raw observation' + (selected.trade_count === 1 ? '' : 's') + '</span></div>' +
      '<h2 class="intel-title" id="lead-article-title">' + escapeHtml(selected.title) + '</h2><div class="intel-label" style="margin-top:13px">Author\'s framing</div><p class="intel-claim">' + escapeHtml(articleClaim(selected)) + '</p><div class="intel-reasons" aria-label="Why this article is surfaced">' + reasonChips(selected,8) + '</div></div>' +
      '<div class="intel-section-grid">' + (sectionMarkup || '<div class="intel-empty">No exact authored section passage is available in this index. Open the original article for full context.</div>') + '</div>' +
      '<div class="intel-actions"><a class="primary-action" href="' + escapeHtml(safeUrl(selected.url)) + '" target="_blank" rel="noopener noreferrer">Open original ↗</a><button class="secondary-action" type="button" data-article-dossier="' + selected.id + '">Open full dossier</button><button class="secondary-action" type="button" data-copy-article="' + selected.id + '">Copy citation</button><span class="intel-actions-note">Published research, not a live market as-of or a portfolio recommendation.</span></div></article>' +
      '<aside class="intel-side"><section class="intel-side-card"><div class="intel-card-head"><h3>Upcoming checkpoints cited</h3><span>' + number(checkpoints.length) + ' explicit</span></div><div class="checkpoint-list">' + checkpointMarkup + '</div></section><section class="intel-side-card"><div class="intel-card-head"><h3>Continue reading</h3><span>Newest next</span></div><div class="next-list">' + nextMarkup + '</div></section></aside>' +
      '<section class="intel-stream"><div class="intel-card-head"><h3>Recent article dossiers</h3><span>' + number(records.length) + ' in this lens</span></div><div class="intel-stream-list">' + (stream || '<div class="intel-empty">No additional articles in this lens.</div>') + '</div></section></div></div>';
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
  ['source','direction','instrument','manager','quality','content'].forEach(function (facet) {
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
  if (facet === 'direction') return directionLabel(value);
  if (facet === 'instrument') return instrumentLabel(value);
  if (facet === 'quality') return {quant:'Numeric context',thesis:'Has thesis',outcome:'Reported outcome',manager:'Mentioned entity'}[value];
  if (facet === 'content') return value === 'full' ? 'Full text' : 'Excerpt';
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
    ['source',state.sources],['direction',state.directions],['instrument',state.instruments],
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
    source:state.sources,direction:state.directions,instrument:state.instruments,
    manager:state.managers,quality:state.quality,content:state.content,'queue-status':state.queueStatuses
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
  badges.push('<span class="coverage-badge">' + (article.content_status === 'full' ? 'Full text indexed' : 'Excerpt indexed') + '</span>');
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
      '<p class="workflow-warning">Stored only in this browser unless backed up. Not an enterprise audit record. Do not enter confidential, personal, client, position, or regulated information.</p></section>' : '';
  return '<div class="inspector-content">' +
    '<div class="record-eyebrow"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><time datetime="' + article.date + '">' + formatDate(article.date) + '</time><span class="record-id">' + idea.id.toUpperCase() + '</span></div>' +
    '<h2 class="record-title">' + escapeHtml(article.title) + '</h2>' +
    (article.subtitle ? '<p class="record-subtitle">' + escapeHtml(article.subtitle) + '</p>' : '') +
    '<div class="record-actions">' +
      '<a class="primary-action" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer">Open original ↗</a>' +
      (alternate ? '<a class="secondary-action" href="' + escapeHtml(safeUrl(alternate)) + '" target="_blank" rel="noopener noreferrer">Medium copy ↗</a>' : '') +
      (workflow ? '' : '<button class="secondary-action" type="button" data-save-idea="' + idea.id + '">☆ Add to review</button>') +
      '<button class="secondary-action" type="button" data-copy-citation="' + idea.id + '">Copy citation</button>' +
    '</div>' +
    '<div class="record-facts">' + badges.join('') + '</div>' +
    (reviewReasons.length ? '<div class="review-notice"><strong>Extraction review recommended:</strong> ' + escapeHtml(reviewReasons.join('; ')) + '. Verify the surrounding source context before interpreting stance.</div>' : '') +
    detailSection('Source passage',passageText(idea),'primary-text') +
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
    '<div class="provenance">Published ' + escapeHtml(formatDate(article.date)) + '; source collection checked ' + escapeHtml(formatCheckedAt(SNAPSHOT.checked_at)) + '. This is not a live market as-of timestamp. Rules-based passage extracted from published research by one author. “No reliable stance” means the source did not express a direction the parser could safely classify. Mentions are not verified positions; reported outcomes are not independently verified. Review the original publication before any investment or execution decision.</div>' +
    '</div>';
}
function renderArticleInspector(article) {
  const alternate = article.alternate_urls && article.alternate_urls.medium;
  const brief = article.brief || {lead:null,sections:[],fallback_evidence:null,checkpoints:[]};
  const related = article._ideas.slice(0,8).map(function (idea) {
    return '<button class="related-idea" type="button" data-related-idea="' + idea.id + '">' +
      '<span class="direction-badge ' + directionClass(idea.direction) + '">' + directionLabel(idea.direction) + '</span> ' +
      escapeHtml(passageText(idea)) + '</button>';
  }).join('');
  const dossierSections = (brief.sections || []).map(function (section) {
    return '<section class="article-dossier-section"><h3>' + escapeHtml(BRIEF_KIND_LABELS[section.kind] || 'Authored section') + '</h3><h4>' + escapeHtml(section.heading) + '</h4><p>' + highlightArticleNumbers(section.text) + '</p>' + exactPassageTail(section) + '</section>';
  }).join('');
  const fallbackEvidence = !briefSection(article,'evidence') && brief.fallback_evidence
    ? '<section class="article-dossier-section"><h3>Contextual evidence</h3><h4>Exact numerical passage identified in the article</h4><p>' + highlightArticleNumbers(brief.fallback_evidence.text) + '</p>' + exactPassageTail(brief.fallback_evidence) + '</section>'
    : '';
  const checkpoints = (brief.checkpoints || []).map(function (checkpoint) {
    return '<div class="checkpoint-mini"><time datetime="' + checkpoint.date + '">' + escapeHtml(formatDate(checkpoint.date)) + '</time><span>' + escapeHtml(checkpoint.text) + '</span></div>';
  }).join('');
  const structures = new Set(article.directions.filter(function (value) { return value !== 'unspecified'; }));
  const gaps = [];
  if (article.content_status === 'excerpt') gaps.push('Only an excerpt was available to the index.');
  if (!articleEvidence(article)) gaps.push('No contextual numerical passage was identified by the high-precision brief rules.');
  if (!articleHasBriefKind(article,'countercase') && !articleHasBriefKind(article,'falsifier')) gaps.push('No explicit countercase or falsifier section was identified.');
  if (structures.size > 1) gaps.push('Extracted passages describe mixed structures; no single article-level stance is assigned.');
  if (!brief.lead) gaps.push('No authored lead passage is available in the compact index.');
  return '<div class="inspector-content">' +
    '<div class="record-eyebrow"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><time datetime="' + article.date + '">' + formatDate(article.date) + '</time><span class="record-id">' + article.id.toUpperCase() + '</span></div>' +
    '<h2 class="record-title">' + escapeHtml(article.title) + '</h2>' +
    '<div class="intel-label" style="margin-top:10px">Author\'s framing</div><p class="record-subtitle primary-text">' + escapeHtml(articleClaim(article)) + '</p>' +
    '<div class="record-actions">' +
      '<a class="primary-action" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener noreferrer">Open original ↗</a>' +
      (alternate ? '<a class="secondary-action" href="' + escapeHtml(safeUrl(alternate)) + '" target="_blank" rel="noopener noreferrer">Medium copy ↗</a>' : '') +
      '<button class="secondary-action" type="button" data-copy-article="' + article.id + '">Copy citation</button>' +
    '</div>' +
    '<div class="intel-reasons">' + reasonChips(article,8) + '</div>' +
    (brief.lead ? '<section class="article-dossier-section"><h3>Author framing</h3><h4>Why the article says this matters</h4><p>' + highlightArticleNumbers(brief.lead.text) + '</p>' + exactPassageTail(brief.lead) + '</section>' : '') +
    fallbackEvidence + dossierSections +
    (checkpoints ? '<section class="article-dossier-section"><h3>Public checkpoints cited</h3>' + checkpoints + '</section>' : '') +
    (gaps.length ? '<section class="article-dossier-section"><h3>Evidence boundaries</h3><div class="review-notice">' + gaps.map(function (gap) { return '<div>• ' + escapeHtml(gap) + '</div>'; }).join('') + '</div></section>' : '') +
    (related ? '<details class="article-dossier-section"><summary>Raw extracted observations (' + number(article.trade_count) + ')</summary><div class="related-ideas" style="margin-top:9px">' + related + '</div></details>' : '<section class="article-dossier-section"><h3>Raw extracted observations</h3><p class="missing">None. The article dossier remains available because it is built from exact authored sections, not observation count.</p></section>') +
    '<div class="provenance">Published ' + escapeHtml(formatDate(article.date)) + '; source collection checked ' + escapeHtml(formatCheckedAt(SNAPSHOT.checked_at)) + '. Every dossier passage is stored with source offsets and a SHA-256 hash and was validated against the article body before publication. The brief preserves authored language; it does not infer current holdings, conviction, expected return, portfolio fit, or a live market view.</div>' +
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
      restorePendingBriefFocus();
    } else {
      container.innerHTML = article ? renderArticleInspector(article) : '';
      restorePendingBriefFocus();
    }
  } else {
    const idea = IDEA_BY_ID.get(state.selected);
    container.innerHTML = idea ? renderIdeaInspector(idea) : '';
  }
  if (shouldResetScroll) document.getElementById('inspector').scrollTop = 0;
  renderedInspectorKey = inspectorKey;
}

function render() {
  setSortOptions();
  const records = filteredRecords();
  const ids = new Set(records.map(function (record) { return record.id; }));
  if (!ids.has(state.selected)) state.selected = records.length ? records[0].id : '';
  setPressedStates();
  if (state.view === 'briefing') {
    renderIntelligenceBrief(records);
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
  document.getElementById('result-summary').textContent =
    number(records.length) + ' ' + (isArticleView() ? 'article dossiers' : state.view === 'queue' ? 'current queued observations' : 'research observations') + (orphanedCount ? ' + ' + number(orphanedCount) + ' retained source snapshots' : '');
  updateHash();
  document.getElementById('announcer').textContent =
    number(records.length) + ' results in ' + (state.view === 'briefing' ? 'Intelligence Brief' : state.view === 'research' ? 'Article Library' : state.view === 'queue' ? 'Review Queue' : 'Evidence Explorer');
}

function resetFilters() {
  state.query = '';
  state.sources.clear();
  state.directions.clear();
  state.instruments.clear();
  state.managers.clear();
  state.quality.clear();
  state.content.clear();
  state.queueStatuses.clear();
  state.documentation = 'all';
  state.newOnly = false;
  state.range = 'all';
  state.coverage = 'all';
  state.briefLens = 'all';
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
  if (focusInside && window.innerWidth <= 1240) {
    setTimeout(function () { document.getElementById('inspector-close').focus(); },0);
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
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(function () { toast.classList.remove('show'); },1800);
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
  showToast(copied ? (message || 'Copied') : 'Copy failed—select and copy manually');
  return copied;
}
function ideaCitation(idea) {
  const article = idea._article;
  return 'Navnoor Bawa, “' + article.title + ',” ' + sourceLabel(article.source) +
    ', ' + article.date + '. Observation ' + idea.id.toUpperCase() +
    '; parsed stance: ' + directionLabel(idea.direction) + '; dataset ' +
    String(SNAPSHOT.data_checksum || '').slice(0,12) + '. ' + article.url;
}
function articleCitation(article) {
  return 'Navnoor Bawa, “' + article.title + ',” ' + sourceLabel(article.source) +
    ', ' + article.date + '; dataset ' + String(SNAPSHOT.data_checksum || '').slice(0,12) +
    '. ' + article.url;
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
    'Content access: ' + (article.content_status === 'full' ? 'Full text indexed' : 'Excerpt indexed'),
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
  showToast(previous ? (previous.status === 'archived' ? 'Decision packet archived' : 'Decision packet returned to review') : 'Added to review on this device');
  render();
  if (restoreSave) {
    const replacement = document.querySelector('[data-save-idea="' + CSS.escape(id) + '"]');
    if (replacement) replacement.focus();
    else document.querySelector('button[data-view="queue"]').focus();
  } else if (restoreRow) {
    focusSelectedRow();
  }
}
function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\s]*[=+\-@]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g,'""') + '"';
}
function exportCsv() {
  const records = filteredRecords();
  let rows;
  if (state.view === 'research') {
    rows = [['Date','Publication channel','Article','Subtitle','Research observations','Content access','URL']].concat(records.map(function (article) {
      return [article.date,sourceLabel(article.source),article.title,article.subtitle,article.trade_count,article.content_status,article.url];
    }));
  } else {
    rows = [['Date','Parsed stance','Instruments','Underlying','Mentioned entity','Original entity mention','Source passage','Extracted edge / thesis','Numeric context','Reported outcome','Documentation coverage','Review flags','Content access','Queue status','Priority','Decision owner','Next review','Analyst confidence','Next action','Variant thesis','Contrary evidence','Catalyst','Horizon','Valuation / payoff','Falsifier / downside / exit','Implementation / liquidity / funding','Mandate / portfolio fit','Source reviewed','Independent evidence','Live market / valuation checked','Liquidity / funding checked','Portfolio risk checked','Compliance checked','Packet coverage','Local tags','Local memo','Packet updated','Article','Publication channel','URL']].concat(records.map(function (idea) {
      const article = idea._article;
      const workflow = workflowItems.get(idea.id) || {};
      const packet = packetCoverage(workflowItems.get(idea.id));
      const flags = [idea.negation_risk ? 'negation-risk' : '',idea.reference_line ? 'reference-line' : '',idea.description_truncated ? 'truncated' : ''].filter(Boolean).join('; ');
      const checks = workflow.checks || {};
      return [article.date,directionLabel(idea.direction),idea.instruments.map(instrumentLabel).join('; '),idea.underlying,idea.manager,idea.manager_raw,passageText(idea),idea.thesis,idea.quant,idea.outcome,idea.documentation_score + '/5',flags,article.content_status,workflow.status || '',workflow.priority || '',workflow.owner || '',workflow.review_date || '',workflow.confidence || '',workflow.next_action || '',workflow.thesis || '',workflow.contrary || '',workflow.catalyst || '',workflow.horizon || '',workflow.payoff || '',workflow.risk || '',workflow.implementation || '',workflow.portfolio || '',checks.source ? 'yes' : '',checks.independent ? 'yes' : '',checks.market ? 'yes' : '',checks.liquidity ? 'yes' : '',checks.portfolio ? 'yes' : '',checks.compliance ? 'yes' : '',workflow.id ? packet.completed + '/' + packet.total : '',workflow.tags || '',workflow.note || '',workflow.updated_at || '',article.title,sourceLabel(article.source),article.url];
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
  state.directions.clear();
  state.instruments.clear();
  state.managers.clear();
  state.quality.clear();
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
  state.limit = PAGE_SIZE.ideas;
  document.getElementById('search').value = '';
  render();
}
function markReviewedThroughLatest() {
  try {
    localStorage.setItem(LAST_SEEN_KEY,MAX_DATE);
    lastSeenPublication = MAX_DATE;
    NEW_SINCE_DATE = MAX_DATE;
    state.newOnly = false;
    render();
    showToast('Research marked reviewed through ' + formatDate(MAX_DATE));
  } catch (_error) {
    showToast('Review baseline could not be saved in this browser');
  }
}
function backupQueue() {
  const payload = {
    schema_version:2,
    exported_at:new Date().toISOString(),
    data_checksum:String(SNAPSHOT.data_checksum || ''),
    items:Array.from(workflowItems.values())
  };
  const blob = new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'navnoor-decision-queue-' + new Date().toISOString().slice(0,10) + '.json';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(function () { URL.revokeObjectURL(url); },0);
  showToast(number(workflowItems.size) + ' queue records backed up');
}
function restoreQueueFile(file) {
  if (!file || file.size > 2000000) { showToast('Queue backup is missing or too large'); return; }
  const reader = new FileReader();
  reader.onload = function () {
    try {
      const payload = JSON.parse(String(reader.result || ''));
      if (!payload || ![1,2].includes(payload.schema_version) || !Array.isArray(payload.items)) throw new Error('invalid schema');
      const restored = new Map(workflowItems);
      let imported = 0;
      payload.items.slice(0,MAX_QUEUE_ITEMS).forEach(function (value) {
        const item = normalizeWorkflowItem(value);
        if (!item) return;
        const existing = restored.get(item.id);
        if (!existing && restored.size >= MAX_QUEUE_ITEMS) return;
        if (!existing || !existing.updated_at || item.updated_at >= existing.updated_at) {
          restored.set(item.id,item);
          imported += 1;
        }
      });
      const previousItems = workflowItems;
      workflowItems = restored;
      const snapshotDiffers = Boolean(payload.data_checksum && payload.data_checksum !== String(SNAPSHOT.data_checksum || ''));
      if (persistWorkflow()) showToast(number(imported) + ' packets merged' + (snapshotDiffers ? '; backup source snapshot differs' : ''));
      else {
        workflowItems = previousItems;
        savedIdeas = new Set(workflowItems.keys());
      }
      render();
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
  if (row) selectRecord(row.dataset.recordId,false,true);
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
  const briefLens = event.target.closest('[data-brief-lens]');
  if (briefLens) {
    state.view = 'briefing';
    state.briefLens = VALID_BRIEF_LENSES.has(briefLens.dataset.briefLens) ? briefLens.dataset.briefLens : 'all';
    pendingBriefFocus = {kind:'lens',value:state.briefLens};
    state.selected = '';
    state.sort = 'newest';
    state.limit = PAGE_SIZE.briefing;
    render();
    document.getElementById('briefing-shell').scrollTop = 0;
    return;
  }
  const briefArticle = event.target.closest('[data-brief-article]');
  if (briefArticle) {
    state.view = 'briefing';
    state.selected = briefArticle.dataset.briefArticle;
    pendingBriefFocus = {kind:'article',value:state.selected};
    render();
    document.getElementById('briefing-shell').scrollTop = 0;
    return;
  }
  const articleDossier = event.target.closest('[data-article-dossier]');
  if (articleDossier) {
    state.view = 'research';
    state.selected = articleDossier.dataset.articleDossier;
    state.sort = 'newest';
    state.limit = PAGE_SIZE.research;
    render();
    openInspector(true);
    return;
  }
  const view = event.target.closest('button[data-view]');
  if (view) {
    state.view = view.dataset.view;
    state.sort = 'newest';
    state.selected = '';
    state.limit = PAGE_SIZE[state.view];
    render();
    return;
  }
  const kpiView = event.target.closest('[data-kpi-view]');
  if (kpiView) {
    state.view = kpiView.dataset.kpiView;
    state.sort = 'newest';
    state.selected = '';
    state.limit = PAGE_SIZE[state.view];
    render();
    return;
  }
  const preset = event.target.closest('[data-preset],[data-kpi-preset]');
  if (preset) {
    applyPreset(preset.dataset.preset || preset.dataset.kpiPreset);
    return;
  }
  const briefRecord = event.target.closest('[data-brief-record]');
  if (briefRecord) {
    state.view = 'ideas';
    state.selected = briefRecord.dataset.briefRecord;
    state.limit = PAGE_SIZE.ideas;
    render();
    openInspector(true);
    return;
  }
  const kpiQuality = event.target.closest('[data-kpi-quality]');
  if (kpiQuality) {
    state.view = 'ideas';
    state.quality.add(kpiQuality.dataset.kpiQuality);
    state.limit = PAGE_SIZE.ideas;
    render();
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
        source:state.sources,direction:state.directions,instrument:state.instruments,
        manager:state.managers,quality:state.quality,content:state.content,'queue-status':state.queueStatuses
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
      source:state.sources,direction:state.directions,instrument:state.instruments,
      manager:state.managers,quality:state.quality,content:state.content
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
      source:state.sources,direction:state.directions,instrument:state.instruments,
      manager:state.managers,quality:state.quality,content:state.content,'queue-status':state.queueStatuses
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
    state.view = 'ideas';
    state.directions.clear();
    state.instruments.clear();
    state.managers.clear();
    state.quality.clear();
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
      updateHash();
      copyText(location.href,'Shareable view copied');
    } else if (action.dataset.action === 'export') {
      exportCsv();
    } else if (action.dataset.action === 'backup-queue') {
      backupQueue();
    } else if (action.dataset.action === 'restore-queue') {
      document.getElementById('queue-restore-input').click();
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
window.addEventListener('pagehide',function () { clearTimeout(workflowInputTimer); persistWorkflow(); });
document.getElementById('queue-restore-input').addEventListener('change',function (event) {
  restoreQueueFile(event.target.files && event.target.files[0]);
  event.target.value = '';
});

let searchTimer;
function renderArticleAwareSearch(focusResult) {
  const finish = function () {
    render();
    if (!focusResult) return;
    if (state.view === 'briefing') {
      const leadTitle = document.getElementById('lead-article-title');
      if (leadTitle) { leadTitle.tabIndex = -1; leadTitle.focus(); }
    } else focusSelectedRow();
  };
  if (isArticleView() && state.query && !briefArchiveReady && !briefArchiveFailed) {
    loadBriefArchive().then(finish).catch(function () {
      showToast('Older article passages could not be searched; showing verified local results');
      finish();
    });
    return;
  }
  finish();
}
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
  try { localStorage.setItem('nrt-theme',next); } catch (_error) {}
  this.textContent = next === 'light' ? 'Dark' : 'Light';
});
const shortcutDialog = document.getElementById('shortcut-dialog');
document.getElementById('shortcut-button').addEventListener('click',function () { shortcutDialog.showModal(); });
document.getElementById('method-button').addEventListener('click',function () { shortcutDialog.showModal(); });
document.querySelector('[data-close-dialog]').addEventListener('click',function () { shortcutDialog.close(); });

document.addEventListener('keydown',function (event) {
  const target = event.target;
  const editable = target.matches('input,textarea,select,[contenteditable="true"]');
  const interactive = target.closest && target.closest('button,a,[role="button"]');
  if (event.key === 'Escape') {
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
    return;
  }
  if (shortcutDialog.open || editable || interactive || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === '/') {
    event.preventDefault();
    document.getElementById('search').focus();
  } else if (event.key === '?') {
    event.preventDefault();
    shortcutDialog.showModal();
  } else if (event.key.toLowerCase() === 'g') {
    event.preventDefault();
    if (state.view === 'briefing') {
      const leadTitle = document.getElementById('lead-article-title');
      if (leadTitle) { leadTitle.tabIndex = -1; leadTitle.focus(); }
    } else focusSelectedRow();
  } else if ((event.key === 'Home' || event.key === 'End') && document.querySelector('[data-record-id]')) {
    event.preventDefault();
    const rows = document.querySelectorAll('[data-record-id]');
    const row = event.key === 'Home' ? rows[0] : rows[rows.length - 1];
    if (row) selectRecord(row.dataset.recordId,true,false);
  } else if (event.key === 'j' || (event.key === 'ArrowDown' && target.closest('[data-record-id]'))) {
    event.preventDefault();
    moveSelection(1);
  } else if (event.key === 'k' || (event.key === 'ArrowUp' && target.closest('[data-record-id]'))) {
    event.preventDefault();
    moveSelection(-1);
  } else if (event.key === 'Enter' && state.selected) {
    if (state.view === 'briefing') {
      state.view = 'research';
      render();
      openInspector(true);
    } else openInspector(true);
  } else if (event.key.toLowerCase() === 'o') {
    const article = selectedArticle();
    if (article) window.open(safeUrl(article.url),'_blank','noopener,noreferrer');
  } else if (event.key.toLowerCase() === 's' && (state.view === 'ideas' || state.view === 'queue') && state.selected) {
    toggleSaved(state.selected);
  } else if (event.key.toLowerCase() === 'c' && state.selected) {
    if (isArticleView()) {
      const article = ARTICLE_BY_ID.get(state.selected);
      if (article) copyText(articleCitation(article),'Article citation copied');
    } else {
      const idea = IDEA_BY_ID.get(state.selected);
      if (idea) copyText(ideaCitation(idea),'Idea citation copied');
    }
  } else if (event.key.toLowerCase() === 'f') {
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
  } else if (event.key === '1' || event.key === '2' || event.key === '3' || event.key === '4') {
    state.view = event.key === '1' ? 'briefing' : event.key === '2' ? 'ideas' : event.key === '3' ? 'research' : 'queue';
    state.sort = 'newest';
    state.selected = '';
    state.limit = PAGE_SIZE[state.view];
    render();
    if (state.view !== 'briefing') focusSelectedRow();
  }
});

window.addEventListener('resize',function () {
  if (window.innerWidth > 1240) document.body.classList.remove('inspector-open');
  if (window.innerWidth > 1020) document.body.classList.remove('filters-open');
  document.querySelectorAll('#table-head [data-sort]').forEach(function (button) {
    button.tabIndex = window.innerWidth <= 760 ? -1 : 0;
  });
  syncOverlayAccessibility();
});

function formatCheckedAt(value) {
  const date = new Date(String(value || ''));
  if (Number.isNaN(date.getTime())) return 'time not recorded';
  return date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});
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
  document.getElementById('kpi-latest').textContent = shortDate(String(SNAPSHOT.latest_publication || MAX_DATE).slice(0,10));
  document.getElementById('kpi-evidence').textContent = number(evidenceArticles);
  document.getElementById('kpi-countercase').textContent = number(boundaryArticles);
  document.getElementById('kpi-implementation').textContent = number(implementationArticles);
  document.getElementById('kpi-checkpoints').textContent = number(checkpointCount);
  const sourceHealth = Object.values(SNAPSHOT.sources || {});
  const checked = new Date(String(SNAPSHOT.checked_at || ''));
  const ageHours = Number.isNaN(checked.getTime()) ? Infinity : Math.max(0,(Date.now() - checked.getTime()) / 3600000);
  const allHealthy = sourceHealth.length >= 2 && sourceHealth.every(function (source) { return source.status === 'ok'; });
  const freshnessClass = ageHours > 36 ? 'stale' : allHealthy ? 'fresh' : sourceHealth.length ? 'degraded' : '';
  const dot = document.getElementById('freshness-dot');
  dot.className = 'status-dot' + (freshnessClass ? ' ' + freshnessClass : '');
  const label = document.getElementById('freshness-label');
  label.textContent = 'Research through ' + formatDate(String(SNAPSHOT.latest_publication || MAX_DATE).slice(0,10)) + ' · checked ' + formatCheckedAt(SNAPSHOT.checked_at);
  const healthDetail = ['substack','medium'].map(function (source) {
    const info = SNAPSHOT.sources && SNAPSHOT.sources[source] || {};
    return sourceLabel(source) + ': ' + (info.status || 'unknown') + ', ' + number(info.included_count || 0) + ' included, ' + (info.mode || 'mode unknown');
  }).join(' | ');
  document.getElementById('freshness-summary').title = healthDetail + ' | Next scheduled checks: 9 AM, 1 PM, and 10 PM Asia/Kolkata';
  const theme = document.documentElement.dataset.theme || 'dark';
  document.getElementById('theme-button').textContent = theme === 'light' ? 'Dark' : 'Light';
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
        .replace('__ARTICLES_JSON__', articles_json)
        .replace('__IDEAS_JSON__', ideas_json)
        .replace('__SNAPSHOT_JSON__', snapshot_json)
        .replace('__MANAGER_BUTTONS__', manager_html)
        .replace('__REVISION__', revision_meta)
        .replace('__ARTICLE_COUNT__', str(len(client_articles)))
        .replace('__OBSERVATION_COUNT__', str(len(client_ideas)))
        .replace('__DATA_CHECKSUM__', checksum_meta))

out = DOCS_DIR / 'index.html'
with open(out, 'w', encoding='utf-8') as handle:
    handle.write(HTML)

brief_out = DOCS_DIR / 'article_briefs.json'
with open(brief_out, 'w', encoding='utf-8') as handle:
    json.dump({
        'schema_version': 1,
        'data_checksum': snapshot_manifest.get('data_checksum') or '',
        'briefs': brief_archive,
    }, handle, ensure_ascii=False, separators=(',', ':'))

print(
    f'Built {out} ({len(HTML) // 1024} KB + '
    f'{brief_out.stat().st_size // 1024} KB deferred dossiers, '
    f'{len(client_articles)} research notes, {len(client_ideas)} extracted ideas)'
)
