#!/usr/bin/env python3
"""
Quality filter and deduplication pass for extracted trades.
Removes false positives, meta-content, and paywall notices.
"""
import json
import re

from pathlib import Path
ROOT = Path(__file__).parent
INPUT_PATH  = ROOT / 'trades_extracted.json'
OUTPUT_PATH = ROOT / 'trades_extracted.json'

# Patterns indicating the "trade" is actually meta-content, not a real trade
META_PATTERNS = [
    r'I\'ve put together',
    r'full video breakdown',
    r'Patreon member',
    r'exclusive(?:ly)? for',
    r'subscribe',
    r'covering every fund',
    r'this analysis deconstructs',
    r'the following (?:analysis|breakdown|article)',
    r'published exclusively',
    r'breaking down (?:this|the) article',
    r'^This (?:article|piece|post|analysis)',
    r'upcoming (?:article|piece|post)',
    r'next (?:article|section)',
    r'in this (?:article|piece|post)',
    r'^In this (?:article|piece|post)',
    r'drawing from court documents',
    r'verified sources',
    r'full forensic breakdown',
    r'is published exclusively',
    r'part \d+ of \d+',
    r'complete (?:breakdown|analysis|guide)',
    r'below is (?:a|the)',
    r'this post covers',
    r'at the end of this',
    r'for paid subscribers',
    r'upgrade to read',
    r'paywall',
    r'subscribe to continue',
    r'become a member',
    # References / citations paragraphs — not trade descriptions
    r'^Primary sources?:',
    r'^(?:All )?(?:sources?|references?|citations?|data):',
    r'https?://\S+.*https?://\S+.*https?://\S+',  # paragraph is mostly URLs
    r'(?:Bloomberg|Reuters|FT|WSJ|CNBC|SEC|CFTC) —.*(?:Bloomberg|Reuters|FT|WSJ|CNBC|SEC|CFTC) —',
    # News-source attribution lines — headline not a trade description
    r'^(?:Bloomberg|Reuters|FT|WSJ|CNBC|Hedgeweek|BNN|BIS|Financial Times|CityAM|Barron\'s|MarketWatch)\s*[—–-]\s',
    # Section header bullets inside longer breakdowns
    r'^(?:→|►|•|·)\s*(?:Instrument|Strategy|Trade Setup|Trade Anatomy|Definition|Execution|Note|Hedge|Step)\s*[—–:\s]',
    # "Trade 3 —", "Trade Anatomy —" style headers
    r'^Trade (?:\d+|Anatomy|Setup|Structure|Profile|Case)\s*[—–-]',
    # "The six strategies above document..." meta-commentary sentences
    r'^(?:The )?(?:six|five|four|three|two|seven|eight|nine|ten|eleven|twelve) (?:strategies|trades|methods|approaches|techniques|ways)\b',
    # Educational examples clearly flagged as such
    r'^Example:\s',
    r'^(?:This|The) (?:following|above|below) (?:example|illustration|diagram|table|chart)',
    # News headline with trailing source attribution (not a trade description)
    r'[—–-]\s*(?:Bloomberg|Reuters|FT|WSJ|CNBC|Hedgeweek|BNN Bloomberg|BIS|Financial Times|MarketWatch|CityAM)\s*$',
]

# Patterns that indicate a REAL trade (need at least one)
REAL_TRADE_INDICATORS = [
    r'\b(bought|purchased|shorted|sold|entered|established|built|initiated|opened|acquired)\b',
    r'\b(long position|short position|net long|net short|bet (?:on|against))\b',
    r'\b(CDS on|put on|call on|options on|futures on|swap on)\b',
    r'\b(arbitrage[d]?|hedge[d]?|traded|positioned)\b',
    r'\b(profit(?:ed)? of|return(?:ed)? of|gain(?:ed)?|made \$|lost \$)\b',
    r'\b(?:fund|manager|investor|trader|desk)\s+(?:bought|sold|shorted|went|built|entered)\b',
    r'\b(?:Soros|Ackman|Paulson|Druckenmiller|Buffett|Amaranth|LTCM|Andurand|BlueCrest|Bridgewater|Citadel|Millennium|Point72|Renaissance|D\.E\. Shaw|Elliott|Tiger|Odey|Rokos)\b',
]

# Patterns that indicate meaningful quant details
HAS_MEANINGFUL_QUANT = [
    r'\$[\d,]+(?:\.\d+)?\s*(?:billion|million|B|M)\b',
    r'[\d.]+%',
    r'[\d,]+\s*(?:basis points?|bps)',
    r'at [\d,.$]+\s*(?:per share|per barrel|per unit)?',
    r'strike\s+(?:of|at)?\s*[\d,.$]+',
    r'spread\s+(?:of|at)?\s*[\d,]+',
    r'notional\s+(?:of\s+)?[\d,$]+',
]

def is_false_positive(trade):
    """Return True if this trade is likely a false positive."""
    desc = trade.get('trade_description', '')

    # Check meta patterns
    for pattern in META_PATTERNS:
        if re.search(pattern, desc, re.IGNORECASE):
            return True

    # Must have a real trade indicator
    has_real_indicator = any(re.search(p, desc, re.IGNORECASE) for p in REAL_TRADE_INDICATORS)
    if not has_real_indicator:
        # Check if it has a meaningful quant and instrument with direction
        has_quant = any(re.search(p, desc, re.IGNORECASE) for p in HAS_MEANINGFUL_QUANT)
        has_direction = trade.get('direction') not in (None, 'unspecified')
        has_instrument = bool(trade.get('instruments') and trade['instruments'] != [] and
                              trade['instruments'] != ['unspecified'])
        if not (has_quant and has_direction and has_instrument):
            return True

    # Description too short to be meaningful
    if len(desc) < 80:
        return True

    return False

def merge_duplicates(trades):
    """Remove near-duplicate trades from the same article."""
    seen = {}
    unique = []
    for trade in trades:
        key = (trade.get('article_url', ''), trade.get('trade_description', '')[:150])
        if key not in seen:
            seen[key] = True
            unique.append(trade)
    return unique

def clean_underlying(underlying):
    """Clean up underlying field."""
    if not underlying:
        return None
    # Remove fragments that are clearly not underlyings
    bad_patterns = [
        r'small fraction of the fund',
        r'billion dollar capital',
        r'hedge fund',
        r'covering every',
        r'crisis separated',
        r'global hedge fund',
        r'return for one',
    ]
    for p in bad_patterns:
        if re.search(p, underlying, re.IGNORECASE):
            underlying = re.sub(p + r'[^;]*;?\s*', '', underlying, flags=re.IGNORECASE).strip('; ')
    return underlying.strip('; ') if underlying.strip('; ') else None

def main():
    print(f"Loading trades from {INPUT_PATH}...")
    with open(INPUT_PATH, 'r', encoding='utf-8') as f:
        trades = json.load(f)

    print(f"Original trade count: {len(trades)}")
    total_articles_processed = len({t.get('article_url') for t in trades if t.get('article_url')})

    # Step 1: Remove false positives
    filtered = [t for t in trades if not is_false_positive(t)]
    print(f"After false-positive filter: {len(filtered)}")

    # Step 2: Clean underlying fields
    for t in filtered:
        t['underlying'] = clean_underlying(t.get('underlying'))

    # Step 3: Remove duplicates
    unique = merge_duplicates(filtered)
    print(f"After deduplication: {len(unique)}")

    # Step 4: Sort by date descending
    def get_date(t):
        return t.get('article_date', '1970-01-01') or '1970-01-01'
    unique.sort(key=get_date, reverse=True)

    # Save
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)

    print(f"\nFinal trade count saved to {OUTPUT_PATH}: {len(unique)}")

    # ─── Summary ──────────────────────────────────────────────────────────────
    articles = {}
    instrument_counts = {}
    direction_counts = {}

    for t in unique:
        url = t.get('article_url', '')
        articles[url] = t.get('article_title', url)

        for instr in t.get('instruments', []):
            if instr and instr != 'unspecified':
                instrument_counts[instr] = instrument_counts.get(instr, 0) + 1

        d = t.get('direction') or 'unspecified'
        direction_counts[d] = direction_counts.get(d, 0) + 1

    print(f'\n{"="*60}')
    print('FINAL SUMMARY')
    print(f'{"="*60}')
    print(f'Total articles processed: {total_articles_processed}')
    print(f'Total trades found:       {len(unique)}')
    print(f'Articles with trades:     {len(articles)}')
    print()
    print('Breakdown by instrument type:')
    for instr, cnt in sorted(instrument_counts.items(), key=lambda x: -x[1]):
        print(f'  {instr:25s}: {cnt}')
    print()
    print('Breakdown by direction:')
    for d, cnt in sorted(direction_counts.items(), key=lambda x: -x[1]):
        print(f'  {d:25s}: {cnt}')

    return unique

if __name__ == '__main__':
    main()
