#!/usr/bin/env python3
"""Build docs/index.html — hedge fund trade intelligence dashboard."""
import json
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone

ROOT     = Path(__file__).parent
DOCS_DIR = ROOT / 'docs'
DOCS_DIR.mkdir(exist_ok=True)

with open(ROOT / 'trades_extracted.json') as f:
    trades = json.load(f)

BUILT_AT = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

# Group trades by article URL — no dependency on all_posts.json
trades_by_url = defaultdict(list)
for t in trades:
    trades_by_url[t['article_url']].append(t)

articles = []
for url, article_trades in trades_by_url.items():
    first = article_trades[0]
    articles.append({
        'title':       first.get('article_title', url),
        'date':        (first.get('article_date') or '1970-01-01')[:10],
        'url':         url,
        'trade_count': len(article_trades),
        'trades':      article_trades,
    })

articles.sort(key=lambda x: x['date'], reverse=True)

data_json = json.dumps(articles, ensure_ascii=False)

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trade Intelligence — navnoorbawa</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#000;
  --surface:#0d0d0d;
  --card:#111;
  --border:#1c1c1c;
  --border2:#252525;
  --text:#e2e2e2;
  --muted:#666;
  --dim:#444;
  --accent:#22c55e;
  --long:#22c55e;
  --short:#ef4444;
  --arb:#60a5fa;
  --ls:#a78bfa;
  --header-bg:rgba(0,0,0,0.92);
  --font-mono:'SF Mono','Fira Code','Cascadia Code',monospace;
  --font-sans:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;
}}
html.light{{
  --bg:#f8f8f8;
  --surface:#fff;
  --card:#fff;
  --border:#e4e4e4;
  --border2:#d0d0d0;
  --text:#111;
  --muted:#888;
  --dim:#bbb;
  --accent:#16a34a;
  --long:#16a34a;
  --short:#dc2626;
  --arb:#2563eb;
  --ls:#7c3aed;
  --header-bg:rgba(248,248,248,0.95);
}}
html{{background:var(--bg);color:var(--text);font-family:var(--font-sans);font-size:14px;line-height:1.5;transition:background .2s,color .2s}}
body{{min-height:100vh;display:flex;flex-direction:column}}

/* ── TOP BAR ── */
header{{
  position:sticky;top:0;z-index:100;
  background:var(--header-bg);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
  padding:0 24px;
  display:flex;align-items:center;gap:20px;height:56px;
}}
.brand{{font-size:11px;font-family:var(--font-mono);letter-spacing:.14em;color:var(--accent);text-transform:uppercase;white-space:nowrap}}
.stats-bar{{display:flex;gap:20px;font-family:var(--font-mono);font-size:11px;color:var(--muted)}}
.stats-bar span b{{color:var(--text);font-weight:600}}
.spacer{{flex:1}}
#search{{
  width:280px;height:34px;
  background:var(--surface);border:1px solid var(--border2);
  border-radius:6px;color:var(--text);font-size:13px;padding:0 12px;
  outline:none;font-family:var(--font-sans);
}}
#search:focus{{border-color:var(--accent);box-shadow:0 0 0 2px rgba(34,197,94,.15)}}
#search::placeholder{{color:var(--dim)}}

/* ── LAYOUT ── */
.layout{{display:flex;flex:1;}}
aside{{
  width:220px;flex-shrink:0;
  padding:20px 16px;
  border-right:1px solid var(--border);
  position:sticky;top:56px;height:calc(100vh - 56px);overflow-y:auto;
}}
main{{flex:1;padding:20px 24px;max-width:960px;}}

/* ── SIDEBAR FILTERS ── */
.filter-group{{margin-bottom:24px}}
.filter-label{{font-size:10px;font-family:var(--font-mono);letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:10px;display:block}}
.filter-btn{{
  display:flex;align-items:center;gap:8px;width:100%;
  background:none;border:none;color:var(--muted);cursor:pointer;
  padding:5px 8px;border-radius:5px;font-size:12px;text-align:left;
  font-family:var(--font-sans);transition:background .15s,color .15s;
}}
.filter-btn:hover{{background:var(--surface);color:var(--text)}}
.filter-btn.active{{background:var(--surface);color:var(--accent)}}
.filter-btn .count{{margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)}}
.filter-btn.active .count{{color:var(--accent)}}
.dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
.dot-equity{{background:#60a5fa}}
.dot-volatility{{background:#f59e0b}}
.dot-option{{background:#a78bfa}}
.dot-bond{{background:#34d399}}
.dot-futures{{background:#fb923c}}
.dot-commodity{{background:#fbbf24}}
.dot-FX{{background:#38bdf8}}
.dot-repo{{background:#94a3b8}}
.dot-swap{{background:#c084fc}}
.dot-CDS{{background:#f87171}}
.dot-prediction_market{{background:#4ade80}}
.dot-weather_derivative{{background:#7dd3fc}}
.dot-all{{background:var(--accent)}}

/* ── ARTICLE CARDS ── */
.result-count{{font-size:12px;color:var(--muted);margin-bottom:16px;font-family:var(--font-mono)}}
.article-card{{
  background:var(--card);border:1px solid var(--border);
  border-radius:8px;margin-bottom:12px;overflow:hidden;
  transition:border-color .2s;
}}
.article-card:hover{{border-color:var(--border2)}}
.article-header{{
  padding:14px 16px;cursor:pointer;
  display:flex;align-items:flex-start;gap:12px;
}}
.article-header:hover .article-title{{color:var(--accent)}}
.article-meta{{
  font-family:var(--font-mono);font-size:10px;color:var(--muted);
  white-space:nowrap;padding-top:2px;
}}
.article-date{{display:block}}
.article-body{{flex:1;min-width:0}}
.article-title{{
  font-size:13.5px;font-weight:500;color:var(--text);
  line-height:1.4;margin-bottom:6px;transition:color .15s;
}}
.tag-row{{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}}
.tag{{
  font-size:10px;font-family:var(--font-mono);
  padding:2px 7px;border-radius:3px;
  border:1px solid;
}}
/* dark tag colours */
.tag-equity{{color:#60a5fa;border-color:#1e3a5f;background:#0a1929}}
.tag-volatility{{color:#f59e0b;border-color:#44300a;background:#1a1000}}
.tag-option{{color:#a78bfa;border-color:#3b2a6e;background:#150f29}}
.tag-bond{{color:#34d399;border-color:#0a3626;background:#061610}}
.tag-futures{{color:#fb923c;border-color:#4a2010;background:#1a0c05}}
.tag-commodity{{color:#fbbf24;border-color:#443509;background:#1a1200}}
.tag-FX{{color:#38bdf8;border-color:#0a2e44;background:#041018}}
.tag-repo{{color:#94a3b8;border-color:#2a3040;background:#0d1018}}
.tag-swap{{color:#c084fc;border-color:#3a1f60;background:#120a24}}
.tag-CDS{{color:#f87171;border-color:#4a1515;background:#1a0606}}
.tag-prediction_market{{color:#4ade80;border-color:#163c22;background:#071610}}
.tag-weather_derivative{{color:#7dd3fc;border-color:#0a2a3e;background:#040f18}}
.dir-tag{{font-size:10px;font-family:var(--font-mono);padding:2px 7px;border-radius:3px;border:1px solid}}
.dir-long{{color:var(--long);border-color:#14532d;background:#052012}}
.dir-short{{color:var(--short);border-color:#4a1515;background:#1a0606}}
.dir-arb{{color:var(--arb);border-color:#1e3a5f;background:#0a1929}}
.dir-ls{{color:var(--ls);border-color:#3b2a6e;background:#150f29}}
/* light mode tag overrides */
html.light .tag-equity{{background:#eff6ff;border-color:#bfdbfe}}
html.light .tag-volatility{{background:#fffbeb;border-color:#fde68a}}
html.light .tag-option{{background:#f5f3ff;border-color:#ddd6fe}}
html.light .tag-bond{{background:#f0fdf4;border-color:#bbf7d0}}
html.light .tag-futures{{background:#fff7ed;border-color:#fed7aa}}
html.light .tag-commodity{{background:#fefce8;border-color:#fef08a}}
html.light .tag-FX{{background:#f0f9ff;border-color:#bae6fd}}
html.light .tag-repo{{background:#f8fafc;border-color:#cbd5e1}}
html.light .tag-swap{{background:#faf5ff;border-color:#e9d5ff}}
html.light .tag-CDS{{background:#fef2f2;border-color:#fecaca}}
html.light .tag-prediction_market{{background:#f0fdf4;border-color:#bbf7d0}}
html.light .tag-weather_derivative{{background:#f0f9ff;border-color:#bae6fd}}
html.light .dir-long{{background:#f0fdf4;border-color:#bbf7d0}}
html.light .dir-short{{background:#fef2f2;border-color:#fecaca}}
html.light .dir-arb{{background:#eff6ff;border-color:#bfdbfe}}
html.light .dir-ls{{background:#f5f3ff;border-color:#ddd6fe}}
.trade-badge{{
  margin-left:auto;flex-shrink:0;
  font-family:var(--font-mono);font-size:10px;color:var(--muted);
  padding:3px 8px;border:1px solid var(--border);border-radius:4px;
  align-self:flex-start;white-space:nowrap;
}}
.expand-icon{{
  flex-shrink:0;width:18px;height:18px;color:var(--dim);
  display:flex;align-items:center;justify-content:center;
  transition:transform .2s;font-size:10px;
}}
.article-card.open .expand-icon{{transform:rotate(90deg)}}

/* ── TRADE LIST ── */
.trades-panel{{display:none;border-top:1px solid var(--border);}}
.article-card.open .trades-panel{{display:block}}
.article-link{{
  display:block;padding:8px 16px;font-size:11px;color:var(--accent);
  text-decoration:none;border-bottom:1px solid var(--border);
  font-family:var(--font-mono);letter-spacing:.03em;
}}
.article-link:hover{{background:var(--surface)}}
.trade-item{{
  padding:12px 16px;border-bottom:1px solid var(--border);
}}
.trade-item:last-child{{border-bottom:none}}
.trade-row1{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
.trade-desc{{font-size:12.5px;color:var(--text);line-height:1.55;margin-bottom:6px}}
.trade-field{{font-size:11px;color:var(--muted);margin-top:4px}}
.trade-field b{{color:var(--text);font-weight:500;opacity:.7}}
.trade-quant{{
  font-family:var(--font-mono);font-size:11px;
  color:#d97706;margin-top:4px;
}}
html.light .trade-quant{{color:#b45309}}
.trade-outcome{{font-size:11px;color:var(--accent);margin-top:4px;font-style:italic}}
.trade-outcome-loss{{font-size:11px;color:var(--short);margin-top:4px;font-style:italic}}

/* ── EMPTY STATE ── */
.empty{{text-align:center;padding:80px 20px;color:var(--muted)}}
.empty h2{{font-size:16px;margin-bottom:8px;color:var(--dim)}}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{{width:5px;height:5px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:var(--border2);border-radius:3px}}
::-webkit-scrollbar-thumb:hover{{background:var(--dim)}}

/* ── MOBILE ── */
#filter-toggle{{display:none;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--border2);color:var(--muted);cursor:pointer;border-radius:6px;padding:0 10px;height:34px;font-size:13px;font-family:var(--font-sans);white-space:nowrap;transition:background .15s,color .15s;}}
@media(max-width:768px){{
  header{{flex-wrap:wrap;height:auto;padding:10px 16px;gap:8px;}}
  .brand{{flex:1 1 auto}}
  .stats-bar,.spacer{{display:none}}
  #search{{order:10;width:100%;}}
  #filter-toggle{{display:flex}}
  .layout{{flex-direction:column}}
  aside{{width:100%;height:auto;position:static;border-right:none;border-bottom:1px solid var(--border);padding:12px 16px;display:none;}}
  aside.open{{display:block}}
  .filter-group{{margin-bottom:16px}}
  main{{padding:12px 16px;max-width:100%;}}
  .result-count{{margin-bottom:10px;}}
  .article-header{{padding:12px 14px;gap:8px;flex-wrap:wrap;}}
  .article-meta{{display:flex;flex-direction:row;gap:8px;white-space:nowrap;font-size:9px;flex:0 0 100%;order:3;padding-top:0;}}
  .article-date{{display:inline;margin:0;}}
  .article-body{{flex:1 1 calc(100% - 90px);}}
  .article-title{{font-size:13px;}}
  .trade-badge{{font-size:9px;padding:2px 6px;}}
  .trade-item{{padding:10px 14px;}}
  .trade-desc{{font-size:12px;}}
}}
</style>
</head>
<body>

<header>
  <span class="brand">&#9670; Trade Intelligence</span>
  <div class="stats-bar">
    <span><b id="stat-articles">0</b> articles</span>
    <span><b id="stat-trades">0</b> trades</span>
    <span><b id="stat-range">—</b></span>
    <span style="margin-left:8px;color:var(--dim)">updated {BUILT_AT}</span>
  </div>
  <div class="spacer"></div>
  <input id="search" type="text" placeholder="Search articles, funds, instruments…" autocomplete="off" spellcheck="false">
  <button id="theme-toggle" onclick="toggleTheme()" title="Toggle light / dark" style="
    background:var(--surface);border:1px solid var(--border2);color:var(--muted);
    cursor:pointer;border-radius:6px;padding:0 10px;height:34px;font-size:13px;
    font-family:var(--font-sans);white-space:nowrap;transition:background .15s,color .15s;
  ">&#9788; Light</button>
  <button id="filter-toggle" onclick="toggleFilters()">&#9776; Filters</button>
</header>

<div class="layout">
<aside>
  <div class="filter-group">
    <span class="filter-label">Direction</span>
    <button class="filter-btn active" data-dir="all" onclick="setDir(this)">
      <span class="dot dot-all"></span> All <span class="count" id="cnt-dir-all"></span>
    </button>
    <button class="filter-btn" data-dir="long" onclick="setDir(this)">
      <span style="width:7px;height:7px;border-radius:50%;background:var(--long);flex-shrink:0"></span> Long <span class="count" id="cnt-dir-long"></span>
    </button>
    <button class="filter-btn" data-dir="short" onclick="setDir(this)">
      <span style="width:7px;height:7px;border-radius:50%;background:var(--short);flex-shrink:0"></span> Short <span class="count" id="cnt-dir-short"></span>
    </button>
    <button class="filter-btn" data-dir="arbitrage/relative value" onclick="setDir(this)">
      <span style="width:7px;height:7px;border-radius:50%;background:var(--arb);flex-shrink:0"></span> Arb / RV <span class="count" id="cnt-dir-arb"></span>
    </button>
    <button class="filter-btn" data-dir="long/short" onclick="setDir(this)">
      <span style="width:7px;height:7px;border-radius:50%;background:var(--ls);flex-shrink:0"></span> L/S <span class="count" id="cnt-dir-ls"></span>
    </button>
  </div>

  <div class="filter-group">
    <span class="filter-label">Instrument</span>
    <button class="filter-btn active" data-inst="all" onclick="setInst(this)">
      <span class="dot dot-all"></span> All <span class="count" id="cnt-inst-all"></span>
    </button>
    <button class="filter-btn" data-inst="equity" onclick="setInst(this)"><span class="dot dot-equity"></span> Equity <span class="count" id="cnt-inst-equity"></span></button>
    <button class="filter-btn" data-inst="volatility" onclick="setInst(this)"><span class="dot dot-volatility"></span> Volatility <span class="count" id="cnt-inst-volatility"></span></button>
    <button class="filter-btn" data-inst="option" onclick="setInst(this)"><span class="dot dot-option"></span> Options <span class="count" id="cnt-inst-option"></span></button>
    <button class="filter-btn" data-inst="bond" onclick="setInst(this)"><span class="dot dot-bond"></span> Bonds <span class="count" id="cnt-inst-bond"></span></button>
    <button class="filter-btn" data-inst="futures" onclick="setInst(this)"><span class="dot dot-futures"></span> Futures <span class="count" id="cnt-inst-futures"></span></button>
    <button class="filter-btn" data-inst="commodity" onclick="setInst(this)"><span class="dot dot-commodity"></span> Commodity <span class="count" id="cnt-inst-commodity"></span></button>
    <button class="filter-btn" data-inst="FX" onclick="setInst(this)"><span class="dot dot-FX"></span> FX <span class="count" id="cnt-inst-FX"></span></button>
    <button class="filter-btn" data-inst="repo" onclick="setInst(this)"><span class="dot dot-repo"></span> Repo <span class="count" id="cnt-inst-repo"></span></button>
    <button class="filter-btn" data-inst="swap" onclick="setInst(this)"><span class="dot dot-swap"></span> Swaps <span class="count" id="cnt-inst-swap"></span></button>
    <button class="filter-btn" data-inst="CDS" onclick="setInst(this)"><span class="dot dot-CDS"></span> CDS <span class="count" id="cnt-inst-CDS"></span></button>
    <button class="filter-btn" data-inst="prediction_market" onclick="setInst(this)"><span class="dot dot-prediction_market"></span> Pred. Mkt <span class="count" id="cnt-inst-prediction_market"></span></button>
    <button class="filter-btn" data-inst="weather_derivative" onclick="setInst(this)"><span class="dot dot-weather_derivative"></span> Weather <span class="count" id="cnt-inst-weather_derivative"></span></button>
  </div>
</aside>

<main>
  <div class="result-count" id="result-count"></div>
  <div id="feed"></div>
</main>
</div>

<script>
const DATA = {data_json};

// ── State ──
let activeDir  = 'all';
let activeInst = 'all';
let query      = '';

// ── Filter & Render ──
function tradeMatchesDir(t, dir) {{
  if (dir === 'all') return true;
  return t.direction === dir;
}}
function tradeMatchesInst(t, inst) {{
  if (inst === 'all') return true;
  return (t.instruments || []).includes(inst);
}}
function matchesQuery(art, q, trades) {{
  if (!q) return true;
  const src = trades || art.trades;
  const haystack = [
    art.title,
    ...src.map(t => [t.trade_description, t.underlying, t.edge_or_thesis, t.outcome_if_mentioned].join(' '))
  ].join(' ').toLowerCase();
  return q.toLowerCase().split(' ').filter(Boolean).every(w => haystack.includes(w));
}}

function filteredTrades(art) {{
  return art.trades.filter(t => tradeMatchesDir(t, activeDir) && tradeMatchesInst(t, activeInst));
}}

function render() {{
  const feed = document.getElementById('feed');
  const q = query.trim();
  const filtersActive = activeDir !== 'all' || activeInst !== 'all';

  const tradeCache = new Map();
  for (const art of DATA) {{
    tradeCache.set(art, filtersActive ? filteredTrades(art) : art.trades);
  }}

  const visible = DATA.filter(art => {{
    const trades = tradeCache.get(art);
    if (filtersActive && trades.length === 0) return false;
    return matchesQuery(art, q, trades);
  }});

  document.getElementById('result-count').textContent =
    `${{visible.length}} article${{visible.length !== 1 ? 's' : ''}} · ${{visible.reduce((s, a) => s + tradeCache.get(a).length, 0)}} trades`;

  feed.innerHTML = '';
  if (visible.length === 0) {{
    feed.innerHTML = '<div class="empty"><h2>No results</h2><p>Try a different search or filter.</p></div>';
    return;
  }}

  for (const art of visible) {{
    const card = buildCard(art, tradeCache.get(art));
    feed.appendChild(card);
  }}
}}

function dirClass(dir) {{
  if (dir === 'long') return 'dir-long';
  if (dir === 'short') return 'dir-short';
  if (dir === 'arbitrage/relative value') return 'dir-arb';
  if (dir === 'long/short') return 'dir-ls';
  return '';
}}
function dirLabel(dir) {{
  if (dir === 'arbitrage/relative value') return 'Arb/RV';
  if (dir === 'long/short') return 'L/S';
  return dir.charAt(0).toUpperCase() + dir.slice(1);
}}

function esc(s) {{
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function buildCard(art, trades) {{
  const div = document.createElement('div');
  div.className = 'article-card';

  // Instruments from filtered trades
  const insts = [...new Set(trades.flatMap(t => t.instruments || []))].sort();
  const dirs  = [...new Set(trades.map(t => t.direction).filter(d => d && d !== 'unspecified'))].sort();

  const instTags = insts.map(i => `<span class="tag tag-${{i}}">${{i}}</span>`).join('');
  const dirTags  = dirs.map(d => {{
    const dc = dirClass(d);
    return dc ? `<span class="dir-tag ${{dc}}">${{dirLabel(d)}}</span>` : '';
  }}).join('');

  div.innerHTML = `
    <div class="article-header" onclick="toggleCard(this.parentElement)">
      <div class="article-meta">
        <span class="article-date">${{art.date}}</span>
      </div>
      <div class="article-body">
        <div class="article-title">${{esc(art.title)}}</div>
        <div class="tag-row">${{instTags}}${{dirTags}}</div>
      </div>
      <div class="trade-badge">${{trades.length}} trade${{trades.length !== 1 ? 's' : ''}}</div>
      <div class="expand-icon">&#9658;</div>
    </div>
    <div class="trades-panel">
      <a class="article-link" href="${{esc(art.url)}}" target="_blank" rel="noopener">&#8599; Open on Substack</a>
      ${{trades.map(t => buildTrade(t)).join('')}}
    </div>`;

  return div;
}}

function isLossOutcome(s) {{
  return /\b(lost|loss(?:es)?|losing|declined?|fell|fall|blew.?up|wiped|bankrupt|collapse[d]?|down \$|negative return|drawdown)\b/i.test(s);
}}

function buildTrade(t) {{
  const dc = dirClass(t.direction);
  const dirBadge = dc ? `<span class="dir-tag ${{dc}}">${{dirLabel(t.direction)}}</span>` : '';
  const instTags = (t.instruments || []).map(i => `<span class="tag tag-${{i}}">${{i}}</span>`).join('');

  const desc    = esc(t.trade_description || '');
  const underlying = t.underlying ? `<div class="trade-field"><b>Underlying:</b> ${{esc(t.underlying)}}</div>` : '';
  const thesis  = t.edge_or_thesis ? `<div class="trade-field"><b>Edge / Thesis:</b> ${{esc(t.edge_or_thesis)}}</div>` : '';
  const quant   = t.any_quant_detail ? `<div class="trade-quant">&#9670; ${{esc(t.any_quant_detail)}}</div>` : '';
  const outLoss = t.outcome_if_mentioned && isLossOutcome(t.outcome_if_mentioned);
  const outcome = t.outcome_if_mentioned ? `<div class="${{outLoss ? 'trade-outcome-loss' : 'trade-outcome'}}">${{outLoss ? '&#10007;' : '&#10003;'}} ${{esc(t.outcome_if_mentioned)}}</div>` : '';

  return `<div class="trade-item">
    <div class="trade-row1">${{dirBadge}}${{instTags}}</div>
    <div class="trade-desc">${{desc}}</div>
    ${{underlying}}${{thesis}}${{quant}}${{outcome}}
  </div>`;
}}

function toggleCard(card) {{
  card.classList.toggle('open');
}}

// ── Sidebar counts ──
function updateCounts() {{
  const dirs  = ['all','long','short','arbitrage/relative value','long/short'];
  const insts = ['all','equity','volatility','option','bond','futures','commodity','FX','repo','swap','CDS','prediction_market','weather_derivative'];

  for (const d of dirs) {{
    const id = d === 'all' ? 'cnt-dir-all' : d === 'arbitrage/relative value' ? 'cnt-dir-arb' : d === 'long/short' ? 'cnt-dir-ls' : `cnt-dir-${{d}}`;
    const el = document.getElementById(id);
    if (el) el.textContent = DATA.reduce((s, a) => s + a.trades.filter(t => tradeMatchesDir(t, d)).length, 0);
  }}
  for (const i of insts) {{
    const el = document.getElementById(`cnt-inst-${{i}}`);
    if (el) el.textContent = i === 'all'
      ? DATA.reduce((s, a) => s + a.trade_count, 0)
      : DATA.reduce((s, a) => s + a.trades.filter(t => tradeMatchesInst(t, i)).length, 0);
  }}
}}

function setDir(btn) {{
  document.querySelectorAll('[data-dir]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeDir = btn.dataset.dir;
  render();
}}
function setInst(btn) {{
  document.querySelectorAll('[data-inst]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeInst = btn.dataset.inst;
  render();
}}

// ── Search ──
let searchTimer;
document.getElementById('search').addEventListener('input', e => {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {{ query = e.target.value; render(); }}, 180);
}});

// ── Stats ──
function renderStats() {{
  const dates = DATA.map(a => a.date).sort();
  document.getElementById('stat-articles').textContent = DATA.length;
  document.getElementById('stat-trades').textContent = DATA.reduce((s, a) => s + a.trade_count, 0).toLocaleString();
  document.getElementById('stat-range').textContent = dates[0] + ' → ' + dates[dates.length - 1];
}}

// ── Filter panel (mobile) ──
function toggleFilters() {{
  const aside = document.querySelector('aside');
  aside.classList.toggle('open');
  const open = aside.classList.contains('open');
  document.getElementById('filter-toggle').innerHTML = open ? '&#10005; Close' : '&#9776; Filters';
}}

// ── Theme toggle ──
function toggleTheme() {{
  const isLight = document.documentElement.classList.toggle('light');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-toggle').innerHTML = isLight ? '&#9790; Dark' : '&#9788; Light';
}}
(function () {{
  if (localStorage.getItem('theme') === 'light') {{
    document.documentElement.classList.add('light');
    document.getElementById('theme-toggle').innerHTML = '&#9790; Dark';
  }}
}})();

// ── Init ──
renderStats();
updateCounts();
render();
if (window.innerWidth > 768) document.getElementById('search').focus();
</script>
</body>
</html>"""

out = DOCS_DIR / 'index.html'
with open(out, 'w', encoding='utf-8') as f:
    f.write(HTML)

print(f"Built {out} ({len(HTML)//1024} KB)")
