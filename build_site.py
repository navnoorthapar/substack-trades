#!/usr/bin/env python3
"""Build the institutional research terminal at docs/index.html."""
import hashlib
import html as html_lib
import json
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).parent
DOCS_DIR = ROOT / 'docs'
DOCS_DIR.mkdir(exist_ok=True)

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
    articles.append({
        'title': metadata.get('title') or first.get('article_title') or url,
        'subtitle': metadata.get('subtitle') or '',
        'date': clean_date(metadata.get('post_date') or first.get('article_date')),
        'url': url,
        'source': clean_source(metadata.get('source'), url),
        'alternate_urls': metadata.get('alternate_urls') or {},
        'wordcount': wordcount,
        'content_status': metadata.get('content_status') or 'full',
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
        'trades': article_trades,
    })

articles.sort(key=lambda article: article['date'], reverse=True)

manager_variants = defaultdict(Counter)
for trade in trades:
    raw_manager = ' '.join(unicodedata.normalize(
        'NFKC', str(trade.get('fund_name_if_mentioned') or '')
    ).split())
    if raw_manager:
        manager_variants[normalize_identity_text(raw_manager)][raw_manager] += 1


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
for article in articles:
    article_id = stable_id('a', canonical_url_identity(article['url']))
    idea_ids = []
    directions = set()
    instruments = set()
    managers = set()
    manager_keys = set()

    for trade in article['trades']:
        description = str(trade.get('trade_description') or '').strip()
        idea_id = stable_id(
            'i',
            canonical_url_identity(article['url']) + '\0' + normalize_identity_text(description),
        )
        idea_ids.append(idea_id)
        direction = str(trade.get('direction') or 'unspecified')
        idea_instruments = [
            str(value) for value in (trade.get('instruments') or ['unspecified'])
            if value
        ] or ['unspecified']
        manager_key = normalize_identity_text(trade.get('fund_name_if_mentioned'))
        manager = manager_labels.get(manager_key, '')
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
            'underlying': trade.get('underlying') or '',
            'thesis': trade.get('edge_or_thesis') or '',
            'quant': trade.get('any_quant_detail') or '',
            'outcome': trade.get('outcome_if_mentioned') or '',
            'manager': manager,
            'manager_key': manager_key,
        })

    read_minutes = max(1, round(article['wordcount'] / 220)) if article['wordcount'] else 0
    client_articles.append({
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
        'idea_ids': idea_ids,
        'trade_count': len(idea_ids),
        'directions': sorted(directions),
        'instruments': sorted(instruments),
        'managers': sorted(managers, key=str.casefold),
        'manager_keys': sorted(manager_keys),
        'has_quant': any(bool(trade.get('any_quant_detail')) for trade in article['trades']),
        'has_thesis': any(bool(trade.get('edge_or_thesis')) for trade in article['trades']),
        'has_outcome': any(bool(trade.get('outcome_if_mentioned')) for trade in article['trades']),
    })

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

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Institutional research intelligence across hedge funds, systematic strategies, derivatives, and market structure.">
<meta name="color-scheme" content="dark light">
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
  --bg:#0a0e13;
  --surface-1:#0f141b;
  --surface-2:#151b23;
  --surface-3:#1b232d;
  --surface-raised:#202a35;
  --line:#273340;
  --line-strong:#3b4858;
  --control-line:#607080;
  --text:#f1f5f9;
  --text-secondary:#b7c1cc;
  --text-muted:#94a3b2;
  --accent:#67a2f8;
  --accent-strong:#2f6fcf;
  --accent-soft:#172a42;
  --on-accent:#ffffff;
  --positive:#42c58a;
  --positive-soft:#10261d;
  --positive-line:#28684e;
  --negative:#f36a72;
  --negative-soft:#291619;
  --negative-line:#773a42;
  --relative:#e7b353;
  --relative-soft:#261f11;
  --relative-line:#715b2e;
  --long-short:#a98df6;
  --long-short-soft:#211b31;
  --long-short-line:#5d4e81;
  --quant:#77b6c5;
  --quant-soft:#102329;
  --quant-line:#315b65;
  --source-substack:#c4773f;
  --source-medium:#98a5b1;
  --focus:#a8c7ff;
  --selected:#17212b;
  --selected-line:#67a2f8;
  --backdrop:rgba(5,8,12,.74);
  --shadow:0 20px 54px rgba(0,0,0,.46);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
  --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#f4f6f8;
  --surface-1:#ffffff;
  --surface-2:#f7f9fb;
  --surface-3:#edf1f5;
  --surface-raised:#ffffff;
  --line:#d7dee6;
  --line-strong:#aeb9c4;
  --control-line:#7a8999;
  --text:#18232f;
  --text-secondary:#46586a;
  --text-muted:#56687a;
  --accent:#175caa;
  --accent-strong:#175caa;
  --accent-soft:#e6f0fc;
  --on-accent:#ffffff;
  --positive:#08754e;
  --positive-soft:#ecf8f3;
  --positive-line:#81b9a3;
  --negative:#b42334;
  --negative-soft:#fdf0f1;
  --negative-line:#d6a0a6;
  --relative:#7a5000;
  --relative-soft:#fbf5e5;
  --relative-line:#cdb474;
  --long-short:#67419c;
  --long-short-soft:#f4f0fb;
  --long-short-line:#b2a0d0;
  --quant:#246d7a;
  --quant-soft:#edf7f8;
  --quant-line:#8ab6be;
  --source-substack:#b86532;
  --source-medium:#6f7e8c;
  --focus:#175caa;
  --selected:#eef3f8;
  --selected-line:#175caa;
  --backdrop:rgba(14,21,29,.38);
  --shadow:0 18px 45px rgba(24,35,47,.16);
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
.brand-sub{font:9px var(--mono);color:var(--text-muted);letter-spacing:.11em;text-transform:uppercase;margin-top:2px}
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
.header-right{display:flex;align-items:center;justify-content:flex-end;gap:8px;min-width:310px}
.freshness{display:flex;align-items:center;gap:7px;color:var(--text-secondary);font:10px var(--mono);white-space:nowrap;margin-right:4px}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--positive);box-shadow:0 0 0 3px var(--positive-soft)}
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
  min-width:150px;display:flex;align-items:center;gap:10px;padding:0 16px;
  border:0;border-right:1px solid var(--line);background:transparent;text-align:left;
  color:var(--text-secondary)
}
button.kpi-item{cursor:pointer}
button.kpi-item:hover{background:var(--surface-3)}
.kpi-value{font:600 15px var(--mono);color:var(--text)}
.kpi-label{font:9px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);line-height:1.3}
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
.filter-heading h2{font:600 9px var(--mono);text-transform:uppercase;letter-spacing:.11em;color:var(--text-muted)}
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
.facet-count{margin-left:auto;color:var(--text-muted);font:9px var(--mono)}
.facet-option.active .facet-count{color:var(--text-secondary)}
.date-options{display:grid;grid-template-columns:repeat(4,1fr);gap:3px}
.date-option{
  min-height:30px;border:1px solid var(--control-line);background:var(--surface-2);color:var(--text-secondary);
  border-radius:3px;font:9px var(--mono);cursor:pointer
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
.filter-chip:hover{border-color:var(--negative);color:var(--text)}
.chip-x{color:var(--text-muted)}

.context-bar{
  min-height:42px;display:grid;grid-template-columns:auto minmax(160px,1fr) auto;align-items:center;
  gap:14px;padding:6px 12px;border-bottom:1px solid var(--line);background:var(--surface-2)
}
.context-metrics{display:flex;align-items:center;gap:15px;white-space:nowrap}
.context-metric{display:flex;align-items:baseline;gap:5px}
.context-metric b{font:600 12px var(--mono);color:var(--text)}
.context-metric span{font:9px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted)}
.direction-mix{height:6px;display:flex;overflow:hidden;border-radius:2px;background:var(--surface-3)}
.mix-segment{height:100%;min-width:0}
.mix-long{background:var(--positive)}
.mix-short{background:var(--negative)}
.mix-arb{background:var(--relative)}
.mix-ls{background:var(--long-short)}
.mix-unspecified{background:var(--line-strong)}
.mix-legend{font:9px var(--mono);color:var(--text-muted);white-space:nowrap}

/* Dense master tables */
.command-bar,.active-filters,.context-bar{flex:0 0 auto}
.table-shell{flex:1 1 auto;min-height:0;overflow:auto;position:relative;scrollbar-width:thin;background:var(--surface-1)}
.data-table{min-width:760px}
.table-head{
  position:sticky;top:0;z-index:5;display:grid;align-items:center;min-height:34px;
  border-bottom:1px solid var(--line-strong);background:var(--surface-2);
  color:var(--text-muted);font:600 9px var(--mono);text-transform:uppercase;letter-spacing:.07em
}
.idea-grid{grid-template-columns:82px 86px 122px 130px minmax(270px,1fr) 78px 76px 34px}
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
  border-radius:3px;font:600 9px var(--mono);white-space:nowrap
}
.dir-long{color:var(--positive);border-color:var(--positive-line);background:var(--positive-soft)}
.dir-short{color:var(--negative);border-color:var(--negative-line);background:var(--negative-soft)}
.dir-arb{color:var(--relative);border-color:var(--relative-line);background:var(--relative-soft)}
.dir-ls{color:var(--long-short);border-color:var(--long-short-line);background:var(--long-short-soft)}
.dir-unspecified{color:var(--text-muted);border-color:var(--line-strong);background:var(--surface-2)}
.source-badge{gap:5px;color:var(--text-secondary);border-color:var(--line-strong);background:var(--surface-2)}
.source-badge::before{content:"";width:4px;height:4px;border-radius:50%;flex:0 0 auto}
.source-substack::before{background:var(--source-substack)}
.source-medium::before{background:var(--source-medium)}
.instrument-primary{font:600 10px var(--mono);color:var(--text);text-transform:capitalize}
.instrument-secondary{font-size:9px;color:var(--text-muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.manager-name{font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.missing{color:var(--text-muted)}
.idea-title{font-size:11.5px;color:var(--text);line-height:1.4;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.idea-context{font-size:9.5px;color:var(--text-muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.evidence-set{display:flex;gap:3px;align-items:center}
.evidence-flag{
  min-width:24px;height:19px;display:grid;place-items:center;border:1px solid var(--line);
  border-radius:3px;color:var(--text-muted);font:600 8px var(--mono)
}
.evidence-flag.on{border-color:var(--quant-line);color:var(--quant);background:var(--quant-soft)}
.row-open{
  width:27px;height:27px;display:grid;place-items:center;border:1px solid transparent;border-radius:3px;
  text-decoration:none;color:var(--text-muted);font-size:13px
}
.row-open:hover{border-color:var(--line);background:var(--surface-3);color:var(--accent)}
.article-title{font-size:12px;font-weight:600;color:var(--text);line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.article-subtitle{font-size:9.5px;color:var(--text-muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.number-cell{font:11px var(--mono);color:var(--text);text-align:right}
.coverage-full{color:var(--positive);border-color:var(--positive-line)}
.coverage-excerpt{color:var(--relative);border-color:var(--relative-line)}
.coverage-research{color:var(--text-muted);border-color:var(--line-strong)}
body.density-compact .data-row{min-height:48px}
body.density-comfortable .data-row{min-height:66px}
body.density-compact .idea-title{-webkit-line-clamp:1}
.empty-state{display:none;padding:80px 24px;text-align:center;color:var(--text-muted)}
.empty-state.visible{display:block}
.empty-state h2{font-size:14px;color:var(--text);margin-bottom:6px}
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
.inspector-label{font:600 9px var(--mono);text-transform:uppercase;letter-spacing:.11em;color:var(--text-muted)}
.inspector-close{border:0;background:transparent;color:var(--text-muted);cursor:pointer;padding:8px}
.inspector-content{padding:16px}
.inspector-empty{padding:70px 18px;text-align:center;color:var(--text-muted)}
.inspector-empty-mark{
  width:42px;height:42px;margin:0 auto 14px;display:grid;place-items:center;border:1px solid var(--line);
  border-radius:4px;background:var(--surface-2);font:600 11px var(--mono);color:var(--accent)
}
.inspector-empty h2{font-size:13px;color:var(--text);margin-bottom:6px}
.record-eyebrow{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:10px}
.record-id{font:9px var(--mono);color:var(--text-muted)}
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
.inspector-section h3{font:600 9px var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--text-muted);margin-bottom:7px}
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
  background:var(--surface-2);font-size:9.5px;line-height:1.55;color:var(--text-muted)
}
.article-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin:14px 0}
.article-stat{padding:10px;background:var(--surface-2)}
.article-stat b{display:block;font:600 12px var(--mono);color:var(--text)}
.article-stat span{font:8px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted)}
.related-ideas{display:grid;gap:5px}
.related-idea{
  border:1px solid var(--line);border-radius:3px;background:var(--surface-2);padding:8px;
  color:var(--text-secondary);text-align:left;cursor:pointer;font-size:10.5px;line-height:1.4
}
.related-idea:hover{background:var(--surface-3);border-color:var(--line-strong);color:var(--text)}

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
  width:min(520px,calc(100vw - 32px));border:1px solid var(--line-strong);border-radius:5px;
  background:var(--surface-raised);color:var(--text);padding:0;box-shadow:var(--shadow)
}
dialog::backdrop{background:var(--backdrop)}
.dialog-header{display:flex;align-items:center;justify-content:space-between;padding:13px 15px;border-bottom:1px solid var(--line)}
.dialog-header h2{font-size:13px}
.shortcut-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);margin:15px;border:1px solid var(--line)}
.shortcut-item{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px;background:var(--surface-2);font-size:10.5px;color:var(--text-secondary)}
kbd{font:9px var(--mono);border:1px solid var(--line-strong);background:var(--surface-1);border-radius:3px;padding:2px 6px;color:var(--text)}
.dialog-foot{padding:0 15px 15px;color:var(--text-muted);font-size:10px}
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
}
@media(max-width:760px){
  :root{--header-h:54px;--kpi-h:42px}
  .app-header{grid-template-columns:auto minmax(0,1fr) auto;padding:0 9px;gap:7px}
  .brand-name{display:none}
  .brand{min-width:auto}
  .brand-mark{width:32px;height:32px}
  .search-key,#shortcut-button,.button-label{display:none}
  #search{height:42px;padding-right:9px}
  .header-right{gap:5px}
  .utility-button{min-width:42px;min-height:42px;padding:0 8px}
  .facet-option,.facet-clear,.date-option,.manager-search{min-height:44px}
  .kpi-item{min-width:128px;padding:0 11px}
  .kpi-value{font-size:13px}
  .command-bar{padding:6px 8px;gap:6px}
  .view-tab{padding:0 8px;min-height:34px}
  .result-summary{order:5;flex:1 0 100%}
  .command-spacer{display:none}
  .select-control,.command-button{min-height:42px}
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
}
@media(max-width:430px){
  .command-button[data-action="copy-view"],.command-button[data-action="density"]{display:none}
  .view-tab{font-size:10px}
  .context-metric:nth-child(n+3){display:none}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}
}
@media(forced-colors:active){
  .status-dot,.facet-option.active::before,.mix-segment{forced-color-adjust:none}
}
</style>
</head>
<body class="density-compact" data-view="ideas">
<a class="skip-link" href="#main-panel">Skip to research results</a>
<h1 class="sr-only">Navnoor Research Terminal</h1>

<header class="app-header">
  <div class="brand" aria-label="Navnoor Research Terminal">
    <div class="brand-mark" aria-hidden="true">N/R</div>
    <div>
      <div class="brand-name">Navnoor Research Terminal</div>
      <div class="brand-sub">Institutional trade intelligence</div>
    </div>
  </div>
  <div class="global-search">
    <label class="sr-only" for="search">Search manager, market, underlying, thesis, or article</label>
    <span class="search-glyph" aria-hidden="true">⌕</span>
    <input id="search" type="search" autocomplete="off" spellcheck="false"
      placeholder="Search manager, market, underlying, thesis…">
    <span class="search-key" aria-hidden="true">/</span>
  </div>
  <div class="header-right">
    <div class="freshness"><span class="status-dot"></span><span>Data through <b id="data-through">—</b></span></div>
    <button class="utility-button" id="theme-button" type="button" aria-label="Switch color theme">Light</button>
    <button class="utility-button" id="shortcut-button" type="button" aria-label="Show keyboard shortcuts">?</button>
    <button class="utility-button" id="mobile-filter-button" type="button" aria-expanded="false" aria-controls="filter-rail">Filters</button>
  </div>
</header>

<section class="kpi-strip" aria-label="Dataset coverage">
  <button class="kpi-item" type="button" data-kpi-view="ideas">
    <span class="kpi-value" id="kpi-ideas">0</span><span class="kpi-label">Extracted<br>ideas</span>
  </button>
  <button class="kpi-item" type="button" data-kpi-view="research">
    <span class="kpi-value" id="kpi-research">0</span><span class="kpi-label">Research<br>notes</span>
  </button>
  <div class="kpi-item">
    <span class="kpi-value" id="kpi-managers">0</span><span class="kpi-label">Managers<br>/ firms</span>
  </div>
  <button class="kpi-item" type="button" data-kpi-quality="quant">
    <span class="kpi-value" id="kpi-quantified">0</span><span class="kpi-label">Quantified<br>observations</span>
  </button>
  <div class="kpi-item">
    <span class="kpi-detail" id="kpi-sources">—</span><span class="kpi-label">Source<br>coverage</span>
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

    <section class="filter-group" aria-labelledby="source-filter-label">
      <div class="filter-heading"><h2 id="source-filter-label">Source</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="source"><span>Any source</span><span class="facet-count" data-count-clear="source"></span></button>
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
      <div class="filter-heading"><h2 id="direction-filter-label">Direction / structure</h2></div>
      <div class="facet-list">
        <button class="facet-clear active" type="button" data-clear-facet="direction"><span>Any direction</span><span class="facet-count" data-count-clear="direction"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="long"><span>Long</span><span class="facet-count" data-count-direction="long"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="short"><span>Short</span><span class="facet-count" data-count-direction="short"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="arbitrage/relative value"><span>Arbitrage / RV</span><span class="facet-count" data-count-direction="arbitrage/relative value"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="long/short"><span>Long / short</span><span class="facet-count" data-count-direction="long/short"></span></button>
        <button class="facet-option" type="button" data-filter="direction" data-value="unspecified"><span>Not stated / research</span><span class="facet-count" data-count-direction="unspecified"></span></button>
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
        <h2 id="manager-filter-label">Manager / firm</h2>
        <button class="text-button" type="button" data-clear-facet="manager">Any</button>
      </div>
      <label class="sr-only" for="manager-search">Search managers and firms</label>
      <input class="manager-search" id="manager-search" type="search" autocomplete="off" placeholder="Find manager or firm…">
      <div class="facet-list manager-options" id="manager-options">
__MANAGER_BUTTONS__
      </div>
    </section>

    <section class="filter-group" aria-labelledby="evidence-filter-label">
      <div class="filter-heading"><h2 id="evidence-filter-label">Evidence available</h2></div>
      <div class="facet-list">
        <button class="facet-option" type="button" data-filter="quality" data-value="quant"><span>Quantified</span><span class="facet-count" data-count-quality="quant"></span></button>
        <button class="facet-option" type="button" data-filter="quality" data-value="thesis"><span>Has edge / thesis</span><span class="facet-count" data-count-quality="thesis"></span></button>
        <button class="facet-option" type="button" data-filter="quality" data-value="outcome"><span>Reported outcome</span><span class="facet-count" data-count-quality="outcome"></span></button>
      </div>
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
        <button class="date-option" type="button" data-filter="coverage" data-value="ideas">With extracted ideas</button>
        <button class="date-option" type="button" data-filter="coverage" data-value="research">Research-only</button>
      </div>
    </section>

    <p class="rail-disclaimer"><strong>Research-derived intelligence.</strong> Extracted ideas are not execution records or investment recommendations. Verify context in the original publication.</p>
  </aside>

  <main class="main-panel" id="main-panel" tabindex="-1">
    <div class="command-bar">
      <nav class="view-tabs" aria-label="Terminal views">
        <button class="view-tab active" type="button" data-view="ideas">Idea Monitor</button>
        <button class="view-tab" type="button" data-view="research">Research Library</button>
        <button class="view-tab" type="button" data-view="saved">Saved <span id="saved-count"></span></button>
      </nav>
      <span class="result-summary" id="result-summary"></span>
      <span class="command-spacer"></span>
      <label class="sr-only" for="sort-select">Sort results</label>
      <select class="select-control" id="sort-select"></select>
      <button class="command-button" type="button" data-action="density"><span class="button-label">Density: </span><span id="density-label">Compact</span></button>
      <button class="command-button" type="button" data-action="copy-view">Copy view</button>
      <button class="command-button" type="button" data-action="export">Export CSV</button>
      <button class="command-button active" type="button" data-action="inspector" aria-pressed="true" aria-expanded="true" aria-controls="inspector">Inspector</button>
    </div>

    <div class="active-filters empty" id="active-filters" aria-label="Active filters"></div>

    <section class="context-bar" aria-label="Visible universe summary">
      <div class="context-metrics">
        <span class="context-metric"><b id="visible-primary">0</b><span id="visible-primary-label">ideas</span></span>
        <span class="context-metric"><b id="visible-articles">0</b><span id="visible-secondary-label">notes</span></span>
        <span class="context-metric"><b id="visible-managers">0</b><span>managers</span></span>
      </div>
      <div class="direction-mix" id="direction-mix" aria-label="Visible direction distribution"></div>
      <span class="mix-legend" id="mix-legend"></span>
    </section>

    <section class="table-shell" id="table-shell" aria-label="Research results">
      <div class="data-table" id="data-table" role="table" aria-rowcount="0">
        <div class="table-head idea-grid" id="table-head" role="row" aria-rowindex="1"></div>
        <div id="table-body" role="rowgroup"></div>
      </div>
      <div class="empty-state" id="empty-state">
        <h2 id="empty-title">No matching records</h2>
        <p id="empty-copy">Adjust the search or clear one of the active filters.</p>
      </div>
      <div class="load-more-wrap" id="load-more-wrap">
        <button class="load-more" id="load-more" type="button"></button>
      </div>
    </section>
  </main>

  <aside class="inspector" id="inspector" aria-label="Evidence inspector">
    <div class="inspector-header">
      <span class="inspector-label">Evidence inspector</span>
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

<div class="toast" id="toast" role="status" aria-live="polite"></div>
<div class="sr-only" id="announcer" aria-live="polite" aria-atomic="true"></div>

<dialog id="shortcut-dialog" aria-labelledby="shortcut-title">
  <div class="dialog-header">
    <h2 id="shortcut-title">Keyboard workflow</h2>
    <button class="inspector-close" type="button" data-close-dialog aria-label="Close keyboard shortcuts">×</button>
  </div>
  <div class="shortcut-grid">
    <div class="shortcut-item"><span>Focus global search</span><kbd>/</kbd></div>
    <div class="shortcut-item"><span>Move through rows</span><span><kbd>J</kbd> <kbd>K</kbd></span></div>
    <div class="shortcut-item"><span>Open evidence inspector</span><kbd>Enter</kbd></div>
    <div class="shortcut-item"><span>Open original research</span><kbd>O</kbd></div>
    <div class="shortcut-item"><span>Save selected idea</span><kbd>S</kbd></div>
    <div class="shortcut-item"><span>Copy selected citation</span><kbd>C</kbd></div>
    <div class="shortcut-item"><span>Toggle filters</span><kbd>F</kbd></div>
    <div class="shortcut-item"><span>Ideas / Research / Saved</span><span><kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd></span></div>
    <div class="shortcut-item"><span>Close panel</span><kbd>Esc</kbd></div>
    <div class="shortcut-item"><span>Show this reference</span><kbd>?</kbd></div>
  </div>
  <p class="dialog-foot">Shortcuts are disabled while typing in a form control.</p>
</dialog>

<noscript><div>This research terminal requires JavaScript to filter and inspect the embedded dataset.</div></noscript>

<script>
const ARTICLES = __ARTICLES_JSON__;
const IDEAS = __IDEAS_JSON__;

const ARTICLE_BY_ID = new Map(ARTICLES.map(function (article) { return [article.id, article]; }));
const IDEA_BY_ID = new Map(IDEAS.map(function (idea) { return [idea.id, idea]; }));
const MANAGER_LABELS = new Map(IDEAS.filter(function (idea) { return idea.manager_key; }).map(function (idea) { return [idea.manager_key,idea.manager]; }));
const MANAGERS = Array.from(MANAGER_LABELS.keys()).sort(function (a, b) { return MANAGER_LABELS.get(a).localeCompare(MANAGER_LABELS.get(b)); });
const MAX_DATE = ARTICLES.reduce(function (latest, article) { return article.date > latest ? article.date : latest; }, '1970-01-01');
const VALID_SOURCES = new Set(['substack','medium']);
const VALID_DIRECTIONS = new Set(['long','short','arbitrage/relative value','long/short','unspecified']);
const VALID_INSTRUMENTS = new Set(['equity','option','volatility','bond','futures','commodity','FX','swap','CDS','repo','prediction_market','weather_derivative','unspecified']);
const VALID_QUALITY = new Set(['quant','thesis','outcome']);
const VALID_CONTENT = new Set(['full','excerpt']);
const PAGE_SIZE = {ideas:100,research:80,saved:100};

function normalize(value) {
  return String(value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
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
    'long/short':'Long / short', unspecified:'Not stated'
  };
  return labels[value] || 'Not stated';
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
function setFromParam(params, key, validValues) {
  const raw = params.get(key);
  if (!raw) return new Set();
  return new Set(raw.split('|').map(function (value) { return value.trim(); }).filter(function (value) {
    return value && (!validValues || validValues.has(value));
  }));
}

ARTICLES.forEach(function (article) {
  article._ideas = article.idea_ids.map(function (id) { return IDEA_BY_ID.get(id); }).filter(Boolean);
  article._search = normalize([
    article.title, article.subtitle, article.source, article.instruments.join(' '),
    article.directions.join(' '), article.managers.join(' '),
    article._ideas.map(function (idea) {
      return [idea.description,idea.underlying,idea.thesis,idea.quant,idea.outcome,idea.manager].join(' ');
    }).join(' ')
  ].join(' '));
});
IDEAS.forEach(function (idea) {
  idea._article = ARTICLE_BY_ID.get(idea.article_id);
  idea._search = normalize([
    idea._article.title, idea._article.subtitle, idea._article.source,
    idea.description, idea.direction, idea.instruments.join(' '), idea.underlying,
    idea.thesis, idea.quant, idea.outcome, idea.manager
  ].join(' '));
});

let savedIdeas = new Set();
try {
  const stored = JSON.parse(localStorage.getItem('nrt-saved-ideas') || '[]');
  if (Array.isArray(stored)) savedIdeas = new Set(stored.filter(function (id) { return IDEA_BY_ID.has(id); }));
} catch (_error) {}

let storedDensity = 'compact';
let storedInspector = true;
try {
  storedDensity = localStorage.getItem('nrt-density') === 'comfortable' ? 'comfortable' : 'compact';
  storedInspector = localStorage.getItem('nrt-inspector') !== 'hidden';
} catch (_error) {}

const state = {
  view:'ideas',
  query:'',
  sources:new Set(),
  directions:new Set(),
  instruments:new Set(),
  managers:new Set(),
  quality:new Set(),
  content:new Set(),
  range:'all',
  coverage:'all',
  sort:'newest',
  density:storedDensity,
  selected:'',
  limit:PAGE_SIZE.ideas,
  inspector:storedInspector
};

function hydrateFromHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  if (['ideas','research','saved'].includes(params.get('view'))) state.view = params.get('view');
  state.query = params.get('q') || '';
  state.sources = setFromParam(params,'src',VALID_SOURCES);
  state.directions = setFromParam(params,'dir',VALID_DIRECTIONS);
  state.instruments = setFromParam(params,'inst',VALID_INSTRUMENTS);
  state.managers = setFromParam(params,'mgr',new Set(MANAGERS));
  state.quality = setFromParam(params,'evidence',VALID_QUALITY);
  state.content = setFromParam(params,'content',VALID_CONTENT);
  if (['30d','90d','1y','all'].includes(params.get('range'))) state.range = params.get('range');
  if (['all','ideas','research'].includes(params.get('coverage'))) state.coverage = params.get('coverage');
  if (params.get('sort')) state.sort = params.get('sort');
  if (['compact','comfortable'].includes(params.get('density'))) state.density = params.get('density');
  state.selected = params.get('selected') || '';
  state.limit = PAGE_SIZE[state.view];
}

function updateHash() {
  const params = new URLSearchParams();
  if (state.view !== 'ideas') params.set('view',state.view);
  if (state.query) params.set('q',state.query);
  if (state.sources.size) params.set('src',Array.from(state.sources).join('|'));
  if (state.directions.size) params.set('dir',Array.from(state.directions).join('|'));
  if (state.instruments.size) params.set('inst',Array.from(state.instruments).join('|'));
  if (state.managers.size) params.set('mgr',Array.from(state.managers).join('|'));
  if (state.quality.size) params.set('evidence',Array.from(state.quality).join('|'));
  if (state.content.size) params.set('content',Array.from(state.content).join('|'));
  if (state.range !== 'all') params.set('range',state.range);
  if (state.coverage !== 'all' && state.view === 'research') params.set('coverage',state.coverage);
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
    quant:record.has_quant,thesis:record.has_thesis,outcome:record.has_outcome
  } : {
    quant:hasValue(record.quant),thesis:hasValue(record.thesis),outcome:hasValue(record.outcome)
  };
  return Array.from(state.quality).every(function (key) { return values[key]; });
}
function ideaMatches(idea, skip) {
  const article = idea._article;
  if (state.view === 'saved' && !savedIdeas.has(idea.id)) return false;
  if (skip !== 'source' && state.sources.size && !state.sources.has(article.source)) return false;
  if (!inDateRange(article.date)) return false;
  if (skip !== 'direction' && state.directions.size && !state.directions.has(idea.direction)) return false;
  if (skip !== 'instrument' && !setMatches(state.instruments,idea.instruments)) return false;
  if (skip !== 'manager' && state.managers.size && !state.managers.has(idea.manager_key)) return false;
  if (skip !== 'quality' && !qualityMatches(idea,false)) return false;
  if (skip !== 'content' && state.content.size && !state.content.has(article.content_status)) return false;
  return matchesSearch(idea._search);
}
function ideaMatchesResearchFacets(idea,skip) {
  if (skip !== 'direction' && state.directions.size && !state.directions.has(idea.direction)) return false;
  if (skip !== 'instrument' && !setMatches(state.instruments,idea.instruments)) return false;
  if (skip !== 'manager' && state.managers.size && !state.managers.has(idea.manager_key)) return false;
  if (skip !== 'quality' && !qualityMatches(idea,false)) return false;
  return matchesSearch(idea._search);
}
function articleMatches(article, skip) {
  if (skip !== 'source' && state.sources.size && !state.sources.has(article.source)) return false;
  if (!inDateRange(article.date)) return false;
  const hasTradeFilters =
    (skip !== 'direction' && state.directions.size) ||
    (skip !== 'instrument' && state.instruments.size) ||
    (skip !== 'manager' && state.managers.size) ||
    (skip !== 'quality' && state.quality.size);
  if (hasTradeFilters && !article._ideas.some(function (idea) { return ideaMatchesResearchFacets(idea,skip); })) return false;
  if (skip !== 'content' && state.content.size && !state.content.has(article.content_status)) return false;
  if (state.coverage === 'ideas' && article.trade_count === 0) return false;
  if (state.coverage === 'research' && article.trade_count !== 0) return false;
  return matchesSearch(article._search);
}
function relevanceScore(record) {
  if (!state.query) return 0;
  queryTokens();
  if (relevanceScoreCache.has(record)) return relevanceScoreCache.get(record);
  const tokens = queryTokens();
  let score;
  if (state.view === 'research') {
    const title = normalize(record.title);
    const managers = normalize(record.managers.join(' '));
    score = tokens.reduce(function (value, token) {
      return value + (title.includes(token) ? 8 : 0) + (managers.includes(token) ? 5 : 0);
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
    const leftArticle = state.view === 'research' ? left : left._article;
    const rightArticle = state.view === 'research' ? right : right._article;
    if (state.sort === 'oldest') return leftArticle.date.localeCompare(rightArticle.date);
    if (state.sort === 'manager') return String(left.manager || '').localeCompare(String(right.manager || '')) || rightArticle.date.localeCompare(leftArticle.date);
    if (state.sort === 'market') return String((left.instruments || [])[0] || '').localeCompare(String((right.instruments || [])[0] || '')) || rightArticle.date.localeCompare(leftArticle.date);
    if (state.sort === 'direction') return String(left.direction || '').localeCompare(String(right.direction || '')) || rightArticle.date.localeCompare(leftArticle.date);
    if (state.sort === 'article') return leftArticle.title.localeCompare(rightArticle.title);
    if (state.sort === 'most-ideas') return right.trade_count - left.trade_count || right.date.localeCompare(left.date);
    if (state.sort === 'read-time') return right.read_minutes - left.read_minutes || right.date.localeCompare(left.date);
    if (state.sort === 'title') return left.title.localeCompare(right.title);
    if (state.sort === 'relevance') return relevanceScore(right) - relevanceScore(left) || rightArticle.date.localeCompare(leftArticle.date);
    return rightArticle.date.localeCompare(leftArticle.date);
  });
}
function filteredRecords(skip) {
  const records = state.view === 'research'
    ? ARTICLES.filter(function (article) { return articleMatches(article,skip); })
    : IDEAS.filter(function (idea) { return ideaMatches(idea,skip); });
  return sortedRecords(records);
}

function setSortOptions() {
  const select = document.getElementById('sort-select');
  const research = state.view === 'research';
  const options = research ? [
    ['newest','Newest first'],['oldest','Oldest first'],['most-ideas','Most ideas'],
    ['read-time','Longest read'],['title','Title A–Z']
  ] : [
    ['newest','Newest first'],['oldest','Oldest first'],['manager','Manager A–Z'],
    ['market','Market A–Z'],['direction','Direction A–Z'],['article','Article A–Z'],
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
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('most-ideas') + '"><button class="head-sort" type="button" data-sort="most-ideas">Ideas</button></div>' +
      '<div class="head-cell" role="columnheader">Coverage</div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('read-time') + '"><button class="head-sort" type="button" data-sort="read-time">Read</button></div>' +
      '<div class="head-cell" role="columnheader"><span class="sr-only">Open</span></div>';
  } else {
    head.className = 'table-head idea-grid';
    head.innerHTML =
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('newest') + '"><button class="head-sort" type="button" data-sort="newest">Date</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('direction') + '"><button class="head-sort" type="button" data-sort="direction">Bias</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('market') + '"><button class="head-sort" type="button" data-sort="market">Market</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('manager') + '"><button class="head-sort" type="button" data-sort="manager">Manager / firm</button></div>' +
      '<div class="head-cell" role="columnheader" aria-sort="' + ariaSort('article') + '"><button class="head-sort" type="button" data-sort="article">Idea / structure</button></div>' +
      '<div class="head-cell" role="columnheader">Evidence</div>' +
      '<div class="head-cell" role="columnheader">Source</div>' +
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
  const label = 'Quantitative detail ' + (quant ? 'available' : 'unavailable') + '; thesis ' +
    (thesis ? 'available' : 'unavailable') + '; reported outcome ' + (outcome ? 'available' : 'unavailable');
  return '<span class="evidence-set" role="img" aria-label="' + label + '">' +
    '<span class="evidence-flag ' + (quant ? 'on' : '') + '" aria-hidden="true" title="Quantitative detail">Q' + (quant ? '+' : '−') + '</span>' +
    '<span class="evidence-flag ' + (thesis ? 'on' : '') + '" aria-hidden="true" title="Edge or thesis">T' + (thesis ? '+' : '−') + '</span>' +
    '<span class="evidence-flag ' + (outcome ? 'on' : '') + '" aria-hidden="true" title="Reported outcome">O' + (outcome ? '+' : '−') + '</span>' +
    '</span>';
}
function ideaRow(idea) {
  const article = idea._article;
  const primaryInstrument = idea.instruments[0] || 'unspecified';
  const otherInstruments = idea.instruments.slice(1).map(instrumentLabel).join(', ');
  const selected = state.selected === idea.id;
  const rowLabel = formatDate(article.date) + ', ' + directionLabel(idea.direction) + ', ' +
    idea.instruments.map(instrumentLabel).join(', ') + ', ' + (idea.manager || 'manager not stated') + ', ' +
    (idea.description || 'no description extracted') + ', ' + sourceLabel(article.source);
  return '<div class="data-row idea-grid" role="row" data-record-id="' + idea.id + '" tabindex="' + (selected ? '0' : '-1') + '" aria-selected="' + selected + '" aria-label="' + escapeHtml(rowLabel) + '">' +
    '<div class="data-cell cell-date" role="cell"><time datetime="' + article.date + '">' + shortDate(article.date) + '</time></div>' +
    '<div class="data-cell cell-bias" role="cell"><span class="direction-badge ' + directionClass(idea.direction) + '">' + directionLabel(idea.direction) + '</span></div>' +
    '<div class="data-cell cell-market" role="cell"><div class="instrument-primary">' + escapeHtml(instrumentLabel(primaryInstrument)) + '</div>' +
      (otherInstruments ? '<div class="instrument-secondary">+' + escapeHtml(otherInstruments) + '</div>' : '') + '</div>' +
    '<div class="data-cell cell-manager" role="cell"><div class="manager-name ' + (idea.manager ? '' : 'missing') + '">' + escapeHtml(idea.manager || '—') + '</div></div>' +
    '<div class="data-cell cell-idea" role="cell"><div class="idea-title">' + escapeHtml(idea.description || 'No description extracted') + '</div>' +
      '<div class="idea-context">' + escapeHtml(idea.underlying || article.title) + '</div></div>' +
    '<div class="data-cell cell-evidence" role="cell">' + evidenceMarkup(idea) + '</div>' +
    '<div class="data-cell cell-source" role="cell"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span></div>' +
    '<div class="data-cell cell-open" role="cell"><a class="row-open" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener" aria-label="Open ' + escapeHtml(article.title) + ' in a new tab">↗</a></div>' +
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
    number(article.trade_count) + ' extracted ideas, ' + (article.content_status === 'full' ? 'full text indexed' : 'excerpt indexed');
  return '<div class="data-row research-grid" role="row" data-record-id="' + article.id + '" tabindex="' + (selected ? '0' : '-1') + '" aria-selected="' + selected + '" aria-label="' + escapeHtml(rowLabel) + '">' +
    '<div class="data-cell cell-date" role="cell"><time datetime="' + article.date + '">' + shortDate(article.date) + '</time></div>' +
    '<div class="data-cell cell-source" role="cell"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span></div>' +
    '<div class="data-cell cell-article" role="cell"><div class="article-title">' + escapeHtml(article.title) + '</div><div class="article-subtitle">' + escapeHtml(article.subtitle || 'No abstract available') + '</div></div>' +
    '<div class="data-cell cell-count number-cell" role="cell">' + number(article.trade_count) + '</div>' +
    '<div class="data-cell cell-coverage" role="cell">' + coverage + '</div>' +
    '<div class="data-cell cell-read number-cell" role="cell">' + read + '</div>' +
    '<div class="data-cell cell-open" role="cell"><a class="row-open" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener" aria-label="Open ' + escapeHtml(article.title) + ' in a new tab">↗</a></div>' +
    '</div>';
}
function renderRows(records) {
  const body = document.getElementById('table-body');
  const fragment = document.createDocumentFragment();
  const visible = records.slice(0,state.limit);
  visible.forEach(function (record) {
    const template = document.createElement('template');
    template.innerHTML = state.view === 'research' ? researchRow(record) : ideaRow(record);
    fragment.appendChild(template.content.firstElementChild);
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
  const savedEmpty = state.view === 'saved' && savedIdeas.size === 0;
  document.getElementById('empty-title').textContent = savedEmpty ? 'No saved ideas on this device' : 'No matching records';
  document.getElementById('empty-copy').textContent = savedEmpty
    ? 'Select an idea, open the inspector, and choose Save idea.'
    : 'Adjust the search or clear one of the active filters.';

  const more = document.getElementById('load-more-wrap');
  more.classList.toggle('visible',records.length > visible.length);
  document.getElementById('load-more').textContent = 'Show next ' + Math.min(PAGE_SIZE[state.view],records.length - visible.length) + ' of ' + number(records.length - visible.length) + ' remaining';
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
  document.getElementById('visible-primary-label').textContent = state.view === 'research' ? 'notes' : 'ideas';
  document.getElementById('visible-articles').textContent = number(visibleArticles.length);
  document.getElementById('visible-secondary-label').textContent = state.view === 'research' ? 'ideas' : 'notes';
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
    ' · Not stated ' + number(counts.unspecified);
}

function contextualRecords(skip) {
  if (state.view === 'research') return ARTICLES.filter(function (article) { return articleMatches(article,skip); });
  return IDEAS.filter(function (idea) { return ideaMatches(idea,skip); });
}
function recordArticle(record) {
  return state.view === 'research' ? record : record._article;
}
function recordValues(record, facet) {
  if (state.view === 'research') {
    if (facet === 'source') return [record.source];
    if (facet === 'content') return [record.content_status];
    const matchingIdeas = record._ideas.filter(function (idea) { return ideaMatchesResearchFacets(idea,facet); });
    if (facet === 'direction') return matchingIdeas.map(function (idea) { return idea.direction; });
    if (facet === 'instrument') return matchingIdeas.flatMap(function (idea) { return idea.instruments; });
    if (facet === 'manager') return matchingIdeas.map(function (idea) { return idea.manager_key; }).filter(Boolean);
    if (facet === 'quality') return matchingIdeas.flatMap(function (idea) {
      return [
        hasValue(idea.quant) ? 'quant' : '', hasValue(idea.thesis) ? 'thesis' : '', hasValue(idea.outcome) ? 'outcome' : ''
      ].filter(Boolean);
    });
  } else {
    if (facet === 'source') return [record._article.source];
    if (facet === 'direction') return [record.direction];
    if (facet === 'instrument') return record.instruments;
    if (facet === 'manager') return record.manager_key ? [record.manager_key] : [];
    if (facet === 'content') return [record._article.content_status];
    if (facet === 'quality') return [
      hasValue(record.quant) ? 'quant' : '', hasValue(record.thesis) ? 'thesis' : '', hasValue(record.outcome) ? 'outcome' : ''
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
  if (facet === 'quality') return {quant:'Quantified',thesis:'Has thesis',outcome:'Reported outcome'}[value];
  if (facet === 'content') return value === 'full' ? 'Full text' : 'Excerpt';
  if (facet === 'manager') return MANAGER_LABELS.get(value) || value;
  if (facet === 'range') return value.toUpperCase();
  if (facet === 'coverage') return value === 'ideas' ? 'With ideas' : value === 'research' ? 'Research-only' : 'All research';
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
  container.classList.toggle('empty',chips.length === 0);
  container.innerHTML = chips.length ? '<span class="active-label">Active</span>' + chips.join('') : '';
}
function setPressedStates() {
  document.body.dataset.view = state.view;
  document.body.classList.toggle('density-compact',state.density === 'compact');
  document.body.classList.toggle('density-comfortable',state.density === 'comfortable');
  document.body.classList.toggle('inspector-hidden',!state.inspector);
  document.querySelectorAll('[data-view]').forEach(function (button) {
    const active = button.dataset.view === state.view;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  const facetSets = {
    source:state.sources,direction:state.directions,instrument:state.instruments,
    manager:state.managers,quality:state.quality,content:state.content
  };
  document.querySelectorAll('[data-filter]').forEach(function (button) {
    const facet = button.dataset.filter;
    let active = false;
    if (facetSets[facet]) active = facetSets[facet].has(button.dataset.value);
    if (facet === 'range') active = state.range === button.dataset.value;
    if (facet === 'coverage') active = state.coverage === button.dataset.value;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  document.querySelectorAll('[data-clear-facet]').forEach(function (button) {
    const facet = button.dataset.clearFacet;
    const active = facetSets[facet] ? facetSets[facet].size === 0 : false;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
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
  const saved = savedIdeas.has(idea.id);
  const badges = [inspectorBadge(idea.direction)];
  idea.instruments.forEach(function (instrument) {
    badges.push('<span class="coverage-badge">' + escapeHtml(instrumentLabel(instrument)) + '</span>');
  });
  if (idea.manager) badges.push('<span class="coverage-badge">' + escapeHtml(idea.manager) + '</span>');
  return '<div class="inspector-content">' +
    '<div class="record-eyebrow"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><time datetime="' + article.date + '">' + formatDate(article.date) + '</time><span class="record-id">' + idea.id.toUpperCase() + '</span></div>' +
    '<h2 class="record-title">' + escapeHtml(article.title) + '</h2>' +
    (article.subtitle ? '<p class="record-subtitle">' + escapeHtml(article.subtitle) + '</p>' : '') +
    '<div class="record-actions">' +
      '<a class="primary-action" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener">Open original ↗</a>' +
      (alternate ? '<a class="secondary-action" href="' + escapeHtml(safeUrl(alternate)) + '" target="_blank" rel="noopener">Medium copy ↗</a>' : '') +
      '<button class="secondary-action ' + (saved ? 'saved' : '') + '" type="button" data-save-idea="' + idea.id + '">' + (saved ? '★ Saved' : '☆ Save idea') + '</button>' +
      '<button class="secondary-action" type="button" data-copy-citation="' + idea.id + '">Copy citation</button>' +
    '</div>' +
    '<div class="record-facts">' + badges.join('') + '</div>' +
    detailSection('Extracted idea / structure',idea.description,'primary-text') +
    detailSection('Underlying',idea.underlying) +
    detailSection('Edge / thesis',idea.thesis) +
    '<section class="inspector-section"><h3>Quantitative evidence</h3>' +
      (idea.quant ? '<div class="quant-block">' + escapeHtml(idea.quant) + '</div>' : '<p class="missing">—</p>') +
    '</section>' +
    '<section class="inspector-section"><h3>Reported outcome</h3>' +
      (idea.outcome ? '<p class="reported-outcome">' + escapeHtml(idea.outcome) + '</p>' : '<p class="missing">—</p>') +
    '</section>' +
    '<div class="provenance">This record was extracted from published research. “Not stated” means the source did not express a reliable directional position. Reported outcomes are reproduced neutrally and are not independently verified.</div>' +
    '</div>';
}
function renderArticleInspector(article) {
  const alternate = article.alternate_urls && article.alternate_urls.medium;
  const coverageText = article.trade_count === 0
    ? 'Research-only — no explicit signal extracted.'
    : article.trade_count + ' extracted idea' + (article.trade_count === 1 ? '' : 's') + '.';
  const related = article._ideas.slice(0,8).map(function (idea) {
    return '<button class="related-idea" type="button" data-related-idea="' + idea.id + '">' +
      '<span class="direction-badge ' + directionClass(idea.direction) + '">' + directionLabel(idea.direction) + '</span> ' +
      escapeHtml(idea.description) + '</button>';
  }).join('');
  return '<div class="inspector-content">' +
    '<div class="record-eyebrow"><span class="source-badge source-' + article.source + '">' + sourceLabel(article.source) + '</span><time datetime="' + article.date + '">' + formatDate(article.date) + '</time><span class="record-id">' + article.id.toUpperCase() + '</span></div>' +
    '<h2 class="record-title">' + escapeHtml(article.title) + '</h2>' +
    (article.subtitle ? '<p class="record-subtitle">' + escapeHtml(article.subtitle) + '</p>' : '') +
    '<div class="record-actions">' +
      '<a class="primary-action" href="' + escapeHtml(safeUrl(article.url)) + '" target="_blank" rel="noopener">Open original ↗</a>' +
      (alternate ? '<a class="secondary-action" href="' + escapeHtml(safeUrl(alternate)) + '" target="_blank" rel="noopener">Medium copy ↗</a>' : '') +
      '<button class="secondary-action" type="button" data-copy-article="' + article.id + '">Copy citation</button>' +
    '</div>' +
    '<div class="article-stats">' +
      '<div class="article-stat"><b>' + number(article.trade_count) + '</b><span>Ideas</span></div>' +
      '<div class="article-stat"><b>' + (article.content_status === 'excerpt' ? 'Preview' : article.read_minutes ? article.read_minutes + 'm' : '—') + '</b><span>' + (article.content_status === 'excerpt' ? 'Access' : 'Est. read') + '</span></div>' +
      '<div class="article-stat"><b>' + (article.content_status === 'full' ? 'Full' : 'Excerpt') + '</b><span>Indexed</span></div>' +
    '</div>' +
    '<section class="inspector-section"><h3>Coverage status</h3><p class="primary-text">' + escapeHtml(coverageText) + '</p></section>' +
    (related ? '<section class="inspector-section"><h3>Extracted ideas</h3><div class="related-ideas">' + related + '</div></section>' : '') +
    '<div class="provenance">Research metadata and extracted structures are provided for discovery. Review the original publication before making an investment or execution decision.</div>' +
    '</div>';
}
function renderInspector() {
  const container = document.getElementById('inspector-content');
  if (!state.selected) {
    container.innerHTML = '<div class="inspector-empty"><div class="inspector-empty-mark">N/R</div><h2>Select a record</h2><p>Inspect the complete idea, evidence, provenance, and source without losing your position in the monitor.</p></div>';
    return;
  }
  if (state.view === 'research') {
    const article = ARTICLE_BY_ID.get(state.selected);
    container.innerHTML = article ? renderArticleInspector(article) : '';
  } else {
    const idea = IDEA_BY_ID.get(state.selected);
    container.innerHTML = idea ? renderIdeaInspector(idea) : '';
  }
}

function render() {
  setSortOptions();
  const records = filteredRecords();
  const ids = new Set(records.map(function (record) { return record.id; }));
  if (!ids.has(state.selected)) state.selected = records.length ? records[0].id : '';
  setPressedStates();
  renderTableHead();
  renderRows(records);
  renderContext(records);
  renderActiveFilters();
  updateFacetCounts();
  renderInspector();
  document.getElementById('result-summary').textContent =
    number(records.length) + ' ' + (state.view === 'research' ? 'research notes' : 'extracted ideas');
  updateHash();
  document.getElementById('announcer').textContent =
    number(records.length) + ' results in ' + (state.view === 'research' ? 'Research Library' : state.view === 'saved' ? 'Saved ideas' : 'Idea Monitor');
}

function resetFilters() {
  state.query = '';
  state.sources.clear();
  state.directions.clear();
  state.instruments.clear();
  state.managers.clear();
  state.quality.clear();
  state.content.clear();
  state.range = 'all';
  state.coverage = 'all';
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
  if (state.view === 'research') return ARTICLE_BY_ID.get(state.selected);
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
  try {
    await navigator.clipboard.writeText(value);
  } catch (_error) {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    textarea.remove();
  }
  showToast(message || 'Copied');
}
function ideaCitation(idea) {
  const article = idea._article;
  return article.title + ' — ' + directionLabel(idea.direction) + ' — ' +
    (idea.manager || 'Manager not stated') + ' — ' + article.url;
}
function articleCitation(article) {
  return article.title + ' (' + formatDate(article.date) + ') — ' + article.url;
}
function toggleSaved(id) {
  if (!IDEA_BY_ID.has(id)) return;
  const active = document.activeElement;
  const restoreSave = active && active.closest && active.closest('[data-save-idea="' + CSS.escape(id) + '"]');
  const restoreRow = active && active.closest && active.closest('[data-record-id]');
  if (savedIdeas.has(id)) savedIdeas.delete(id); else savedIdeas.add(id);
  try { localStorage.setItem('nrt-saved-ideas',JSON.stringify(Array.from(savedIdeas))); } catch (_error) {}
  showToast(savedIdeas.has(id) ? 'Idea saved on this device' : 'Idea removed from saved');
  render();
  if (restoreSave) {
    const replacement = document.querySelector('[data-save-idea="' + CSS.escape(id) + '"]');
    if (replacement) replacement.focus();
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
    rows = [['Date','Source','Article','Subtitle','Extracted ideas','Content access','URL']].concat(records.map(function (article) {
      return [article.date,sourceLabel(article.source),article.title,article.subtitle,article.trade_count,article.content_status,article.url];
    }));
  } else {
    rows = [['Date','Direction','Instruments','Underlying','Manager / firm','Extracted idea','Edge / thesis','Quant detail','Reported outcome','Article','Source','URL']].concat(records.map(function (idea) {
      const article = idea._article;
      return [article.date,directionLabel(idea.direction),idea.instruments.map(instrumentLabel).join('; '),idea.underlying,idea.manager,idea.description,idea.thesis,idea.quant,idea.outcome,article.title,sourceLabel(article.source),article.url];
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

document.getElementById('table-body').addEventListener('click',function (event) {
  if (event.target.closest('a')) return;
  const row = event.target.closest('[data-record-id]');
  if (row) selectRecord(row.dataset.recordId,false,true);
});
document.getElementById('table-body').addEventListener('dblclick',function (event) {
  const row = event.target.closest('[data-record-id]');
  if (!row || event.target.closest('a')) return;
  const article = selectedArticle();
  if (article) window.open(safeUrl(article.url),'_blank','noopener');
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
  const view = event.target.closest('[data-view]');
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
    else {
      const map = {
        source:state.sources,direction:state.directions,instrument:state.instruments,
        manager:state.managers,quality:state.quality,content:state.content
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
  const remove = event.target.closest('[data-remove-filter]');
  if (remove) {
    const chipIndex = Array.from(document.querySelectorAll('[data-remove-filter]')).indexOf(remove);
    const name = remove.dataset.removeFilter;
    const map = {
      source:state.sources,direction:state.directions,instrument:state.instruments,
      manager:state.managers,quality:state.quality,content:state.content
    };
    if (map[name]) map[name].delete(remove.dataset.value);
    if (name === 'range') state.range = 'all';
    if (name === 'coverage') state.coverage = 'all';
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
  const save = event.target.closest('[data-save-idea]');
  if (save) {
    toggleSaved(save.dataset.saveIdea);
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

let searchTimer;
document.getElementById('search').addEventListener('input',function (event) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(function () {
    state.query = event.target.value;
    state.limit = PAGE_SIZE[state.view];
    if (state.query && state.sort === 'newest') state.sort = 'relevance';
    if (!state.query && state.sort === 'relevance') state.sort = 'newest';
    render();
  },120);
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
  } else if (event.key === 'j' || (event.key === 'ArrowDown' && target.closest('[data-record-id]'))) {
    event.preventDefault();
    moveSelection(1);
  } else if (event.key === 'k' || (event.key === 'ArrowUp' && target.closest('[data-record-id]'))) {
    event.preventDefault();
    moveSelection(-1);
  } else if (event.key === 'Enter' && state.selected) {
    openInspector(true);
  } else if (event.key.toLowerCase() === 'o') {
    const article = selectedArticle();
    if (article) window.open(safeUrl(article.url),'_blank','noopener');
  } else if (event.key.toLowerCase() === 's' && state.view !== 'research' && state.selected) {
    toggleSaved(state.selected);
  } else if (event.key.toLowerCase() === 'c' && state.selected) {
    if (state.view === 'research') {
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
  } else if (event.key === '1' || event.key === '2' || event.key === '3') {
    state.view = event.key === '1' ? 'ideas' : event.key === '2' ? 'research' : 'saved';
    state.sort = 'newest';
    state.selected = '';
    state.limit = PAGE_SIZE[state.view];
    render();
    focusSelectedRow();
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

function renderStaticStats() {
  const quantified = IDEAS.filter(function (idea) { return hasValue(idea.quant); }).length;
  const sourceCounts = ARTICLES.reduce(function (counts,article) {
    counts[article.source] = (counts[article.source] || 0) + 1;
    return counts;
  },{});
  document.getElementById('kpi-ideas').textContent = number(IDEAS.length);
  document.getElementById('kpi-research').textContent = number(ARTICLES.length);
  document.getElementById('kpi-managers').textContent = number(MANAGERS.length);
  document.getElementById('kpi-quantified').textContent = number(quantified);
  document.getElementById('kpi-sources').textContent =
    number(sourceCounts.substack || 0) + ' Substack / ' + number(sourceCounts.medium || 0) + ' Medium';
  document.getElementById('data-through').textContent = formatDate(MAX_DATE);
  const theme = document.documentElement.dataset.theme || 'dark';
  document.getElementById('theme-button').textContent = theme === 'light' ? 'Dark' : 'Light';
}

hydrateFromHash();
document.getElementById('search').value = state.query;
state.inspector = storedInspector;
renderStaticStats();
render();
</script>
</body>
</html>
"""

HTML = (HTML_TEMPLATE
        .replace('__ARTICLES_JSON__', articles_json)
        .replace('__IDEAS_JSON__', ideas_json)
        .replace('__MANAGER_BUTTONS__', manager_html))

out = DOCS_DIR / 'index.html'
with open(out, 'w', encoding='utf-8') as handle:
    handle.write(HTML)

print(
    f'Built {out} ({len(HTML) // 1024} KB, '
    f'{len(client_articles)} research notes, {len(client_ideas)} extracted ideas)'
)
