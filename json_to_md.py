#!/usr/bin/env python3
"""Convert trades_extracted.json to a well-formatted Markdown file."""
import json
import os
from collections import defaultdict

INPUT  = '/Users/navnoorbawa/Downloads/substack trades/trades_extracted.json'
OUTPUT = '/Users/navnoorbawa/Downloads/substack trades/trades_extracted.md'
CORPUS = '/Users/navnoorbawa/Downloads/substack trades/all_posts.json'


def slugify(text):
    """Heading anchor for the *full* heading text (must include the '{i}. ' prefix)."""
    s = ''.join(c if c.isalnum() or c == ' ' else '' for c in str(text).lower())
    s = s.strip().replace(' ', '-')
    return '-'.join(p for p in s.split('-') if p)


def cell(value):
    """Make a value safe inside a single-row Markdown table cell."""
    s = str(value)
    s = s.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    return s.replace('|', '\\|')


with open(INPUT, 'r', encoding='utf-8') as f:
    trades = json.load(f)

# Total posts in the corpus the trades were extracted from (derived, not hardcoded).
total_posts = None
if os.path.exists(CORPUS):
    with open(CORPUS, 'r', encoding='utf-8') as f:
        corpus = json.load(f)
    if isinstance(corpus, list):
        total_posts = len(corpus)

# Group trades by article
by_article = defaultdict(list)
for t in trades:
    by_article[t['article_url']].append(t)

# Sort articles by date descending
articles_sorted = sorted(
    by_article.items(),
    key=lambda x: x[1][0].get('article_date', '') or '',
    reverse=True
)

lines = []

# ── Header ────────────────────────────────────────────────────────────────────
lines.append('# Substack Trades — navnoorbawa.substack.com')
lines.append('')
if total_posts is not None:
    lines.append(f'**Total posts in corpus:** {total_posts}  ')
lines.append(f'**Articles with trades:** {len(articles_sorted)}  ')
lines.append(f'**Total trade records:** {len(trades)}')
lines.append('')
lines.append('---')
lines.append('')

# ── Table of Contents ─────────────────────────────────────────────────────────
lines.append('## Table of Contents')
lines.append('')
for i, (url, article_trades) in enumerate(articles_sorted, 1):
    title = article_trades[0].get('article_title') or url
    date  = (article_trades[0].get('article_date') or '')[:10]
    # Anchor must match the rendered heading, which carries the "{i}. " prefix.
    anchor = slugify(f'{i}. {title}')
    lines.append(f'{i}. [{title[:80]}](#{anchor}) — {date} ({len(article_trades)} trades)')
lines.append('')
lines.append('---')
lines.append('')

# ── Per-article sections ───────────────────────────────────────────────────────
for i, (url, article_trades) in enumerate(articles_sorted, 1):
    first = article_trades[0]
    title = first.get('article_title') or url
    date  = (first.get('article_date') or '')[:10]

    lines.append(f'## {i}. {title}')
    lines.append('')
    lines.append(f'**URL:** {url}  ')
    lines.append(f'**Date:** {date}  ')
    lines.append(f'**Trades found:** {len(article_trades)}')
    lines.append('')

    for j, t in enumerate(article_trades, 1):
        lines.append(f'### Trade {j}')
        lines.append('')

        desc = t.get('trade_description', '')
        if desc:
            lines.append(f'**Description:**  ')
            lines.append(desc)
            lines.append('')

        instruments = t.get('instruments') or []
        direction   = t.get('direction') or '—'
        underlying  = t.get('underlying') or '—'
        thesis      = t.get('edge_or_thesis') or '—'
        quant       = t.get('any_quant_detail') or '—'
        outcome     = t.get('outcome_if_mentioned') or '—'

        lines.append('| Field | Value |')
        lines.append('|---|---|')
        lines.append(f'| **Instruments** | {cell(", ".join(instruments)) if instruments else "—"} |')
        lines.append(f'| **Direction** | {cell(direction)} |')
        lines.append(f'| **Underlying** | {cell(underlying)} |')
        lines.append(f'| **Edge / Thesis** | {cell(thesis)} |')
        lines.append(f'| **Quant Detail** | {cell(quant)} |')
        lines.append(f'| **Outcome / PnL** | {cell(outcome)} |')
        lines.append('')

    lines.append('---')
    lines.append('')

# ── Footer summary ─────────────────────────────────────────────────────────────
lines.append('## Summary Statistics')
lines.append('')

instrument_counts = defaultdict(int)
direction_counts  = defaultdict(int)
for t in trades:
    for instr in (t.get('instruments') or []):
        if instr and instr != 'unspecified':
            instrument_counts[instr] += 1
    d = t.get('direction') or 'unspecified'
    direction_counts[d] += 1

lines.append('### By Instrument Type')
lines.append('')
lines.append('| Instrument | Count |')
lines.append('|---|---|')
for instr, cnt in sorted(instrument_counts.items(), key=lambda x: -x[1]):
    lines.append(f'| {instr} | {cnt} |')
lines.append('')

lines.append('### By Direction')
lines.append('')
lines.append('| Direction | Count |')
lines.append('|---|---|')
for d, cnt in sorted(direction_counts.items(), key=lambda x: -x[1]):
    lines.append(f'| {d} | {cnt} |')
lines.append('')

content = '\n'.join(lines)
with open(OUTPUT, 'w', encoding='utf-8') as f:
    f.write(content)

size_kb = os.path.getsize(OUTPUT) / 1024
print(f'Written {len(trades)} trades across {len(articles_sorted)} articles.')
print(f'Output: {OUTPUT}  ({size_kb:.0f} KB)')
