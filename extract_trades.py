#!/usr/bin/env python3
"""
Extract trades from all Substack posts saved in all_posts.json.
Uses pattern matching + contextual extraction to identify investment positions.
"""
import json
import os
import re
import sys
from datetime import datetime

from pathlib import Path
ROOT = Path(__file__).parent
INPUT_PATH = Path(os.environ.get('POSTS_INPUT', ROOT / 'all_posts.json')).expanduser()
OUTPUT_PATH = Path(os.environ.get('TRADES_OUTPUT', ROOT / 'trades_extracted.json')).expanduser()


def atomic_write_json(path, value):
    """Write JSON beside its destination, then atomically replace it."""
    path = Path(path)
    tmp_path = path.parent / (path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        # A serialization or replace failure must not leave a stale temp file.
        if tmp_path.exists():
            tmp_path.unlink()

# ─── Pattern libraries ────────────────────────────────────────────────────────

# LONG signals. NB: bare "buy/buying" is deliberately excluded — it matches order
# flow ("1,619 buy orders"), dealer hedging ("buy to rebalance"), and option-leg
# mechanics ("buy ATM puts"). "bought"/"purchased" are kept (explicit actions).
DIRECTION_LONG = (
    r'\b(went long|goes long|going long|enter(?:ed|s|ing)? (?:a |an )?long'
    r'|(?:took|established|initiated|opened|put on|building|built|added) (?:a |an )?long'
    r'|long position|net long|bullish|upside bet|leveraged long|bought|purchase[d]?|acqui(?:red|res|ring)'
    r'|long exposure|long (?:the|bias)'
    # mirror of the SHORT asset list so "long oil" / "long equities" classify symmetrically
    r'|long (?:the )?(?:bond[s]?|gilt[s]?|treasur\w+|equit\w+|stock[s]?|share[s]?|oil|crude|gold|silver|copper|natural gas|gas|duration|credit|the dollar|sterling|yen|euro|pound)'
    r'|long (?:vol(?:atility)?|gamma|vega)|long call[s]?|bought call[s]?|call spread|overweight'
    # "accumulated" alone also describes losses/negative gamma. Require a
    # plausibly owned asset or an explicit long/bullish position after it.
    r'|accumulated (?:a |an |the )?(?:(?:long|bullish) (?:positions?|exposure)|(?:(?!(?:short|bearish)\b)[\w.$%\-]+\s+){0,3}(?:positions?|shares?|stock|equit(?:y|ies)|stake|holdings?|bonds?|calls?|call options?))'
    r'|(?:added|increased) (?:to )?(?:the )?(?:long|position|stake|holdings?|exposure)'
    # activist / disclosed stakes — a clean long signal ("rebuilt a $2B stake in")
    r'|(?:built|rebuilt|raised|amassed|disclosed|acquired|took|owns?|holds?|established) (?:a |an )?(?:[\$\d.,]+\+?\s*(?:billion|million|bn|mn)?\s*)?(?:minority |majority |new |large |sizable |controlling |[\d.]+%\s+)?(?:stake|equity stake|long position) in'
    r'|[\d.]+%\s+stake|stake in'
    r'|deployed (?:capital )?(?:into|to|in)|allocated (?:capital )?to)\b'
)
DIRECTION_SHORT = (
    r'\b(shorted|shorting|went short|goes short|going short|enter(?:ed|s|ing)? (?:a |an )?short'
    r'|(?:established|initiated|opened|put on|took|built|building|added) (?:a |an )?short'
    r'|sold short|short position|net short|short seller[s]?|bet(?:ting)? against|CDS buy|bearish'
    r'|short exposure|leveraged short|short bias|underweight'
    r'|short(?:ed)? (?:the )?(?:bond[s]?|gilt[s]?|treasur\w+|equit\w+|stock[s]?|share[s]?|currenc\w+|dollar|sterling|yen|euro|pound|oil|crude|gold|silver|copper|gas|credit|duration|index)'
    r'|short (?:vol(?:atility)?|gamma|vega|VIX)|selling (?:vol(?:atility)?|index vol)|sold vol(?:atility)?'
    r'|(?:sold|wrote|writing|selling) call[s]?|bought put[s]?|put option[s]?|protective put|bought protection|put buyer[s]?'
    r'|fad(?:e|ed|ing) the)\b'
)
DIRECTION_ARB = (
    r'\b(arbitrage[d]?|arb(?:ed)?|relative value|pairs trade|basis trade|convergence trade|spread trade'
    r'|market neutral|merger arb|risk arb|event[\s\-]driven'
    r'|steepener|flattener|curve (?:steepener|flattener|trade)'
    r'|dispersion (?:trade|strateg\w+|book|play)|calendar spread|time spread)\b'
)

INSTRUMENTS = {
    # "note/notes" removed — matches "researchers' note", footnotes, URLs.
    # Replaced with specific bond-note forms that only appear in financial context.
    'bond': r'\b(bond[s]?|gilt[s]?|treasury(?!\s+(?:stock|shares?|department|secretary))|treasuries|t\.bond[s]?|t\.note[s]?|treasury note[s]?|senior note[s]?|convertible note[s]?|subordinated note[s]?|floating rate note[s]?|promissory note[s]?|unsecured note[s]?|secured note[s]?|corporate bond[s]?|sovereign bond[s]?|IG bond[s]?|HY bond[s]?|high.yield bond[s]?|investment.grade bond[s]?|junk bond[s]?|government bond[s]?)\b',
    'CDS': r'\b(CDS|credit default swap[s]?|credit protection|CDX|iTraxx|LCDS)\b',
    # Removed: standalone put/call/spread/cap/floor (too broad — "credit spread", "market cap", "cash flow")
    'option': r'\b(option[s]?|put option[s]?|call option[s]?|straddle[s]?|strangle[s]?|collar[s]?|butterfly spread|condor spread|warrant[s]?|swaption[s]?|put spread[s]?|call spread[s]?|protective put|covered call|exotic option)\b',
    'equity': r'\b(stock[s]?|share[s]?|equity|equities|common stock|preferred stock|ADR[s]?|GDR[s]?|ETF[s]?|index fund[s]?)\b',
    'futures': r'\b(futures|future contracts?|commodity futures?|financial futures?)\b',
    # Removed standalone "swap" — too generic. Require context word or specific form.
    'swap': r'\b(interest rate swap[s]?|IRS swap|TRS|total return swap[s]?|variance swap[s]?|volatility swap[s]?|cross.currency swap[s]?|credit swap[s]?|swap (?:position|trade|desk|book)|swapped (?:into|out|the))\b',
    # Removed: dollar/USD/EUR/GBP/JPY/CNY/CHF/yen/euro/pound — these are price denominations not FX trades
    'FX': r'\b(forex|foreign exchange|FX (?:trade|position|hedge|carry|alpha|swap|forward|option)|exchange rate|currency (?:trade|position|hedge|swap|pair|crisis|peg|attack|devaluation|appreciation|depreciation|carry)|NDF|carry trade|currency future[s]?|dollar index|DXY|EM currenc|emerging market currenc|spot rate|FX book)\b',
    'commodity': r'\b(oil|crude|WTI|Brent|natural gas|gold|silver|copper|wheat|corn|commodity|commodities|EUA|carbon credit[s]?)\b',
    # Removed "financing" (too generic)
    'repo': r'\b(repo|repurchase agreement|reverse repo|haircut|repo market|repo rate)\b',
    'weather_derivative': r'\b(weather derivative[s]?|temperature future[s]?|cat bond[s]?|catastrophe bond[s]?)\b',
    'prediction_market': r'\b(prediction market[s]?|Polymarket|Kalshi|event contract[s]?)\b',
    # "volatility" alone is too broad — every finance article mentions it. Require a trading context.
    'volatility': r'\b(VIX|implied vol(?:atility)?|realized vol(?:atility)?|variance swap|dispersion trade|volatility swap|vol surface|vol regime|vol arb(?:itrage)?|long vol|short vol|selling vol|buying vol|vol trade|volatility trade|volatility (?:position|strategy|fund|hedge|bet|exposure|premium|product|index)|variance risk premium|vol premium|volatility risk premium)\b',
}

QUANT_PATTERNS = [
    r'\$[\d,]+(?:\.\d+)?\s*(?:billion|million|trillion|bn|mn|trn|B|M|T)\b',
    r'[₹£€¥]\s*[\d,]+(?:\.\d+)?\s*(?:billion|million|trillion|crore|lakh|bn|mn|B|M)\b',
    r'[\d,]+(?:\.\d+)?\s*(?:crore|lakh)\b',
    r'[\d,]+(?:\.\d+)?\s*(?:billion|million|trillion|bn|mn|trn)\s+(?:dollars?|euros?|pounds?|yen)',
    r'[\d.]+%',
    r'[\d,]+\s*basis points?',
    r'[\d,]+\s*bps?',
    r'\d+x\s+(?:leverage|return|multiple)',
    r'[\d.]+\s*(?:cents?|pence)\s+on\s+the\s+dollar',
    r'strike(?:\s+price)?\s+(?:of\s+)?[\d,.$]+',
    r'at\s+[\d,.$]+(?:\s+(?:per share|per barrel|per ton|per ounce))?',
    r'yield\s+of\s+[\d.]+%',
    r'spread\s+of\s+[\d,]+\s*(?:bps?|basis points?)?',
    r'[\d,]+\s*contracts?',
    r'[\d,]+\s*shares?',
    r'notional\s+(?:of\s+)?[\d,$]+',
    r'P[&/]?L\s+of\s+[\$\d,.]+'
]

# ─── Hedge fund / manager name extraction ──────────────────────────────────────
_FUND_NAMES = [
    # Iconic managers (last name)
    'Soros', 'Ackman', 'Paulson', 'Druckenmiller', 'Buffett', 'Einhorn',
    'Tepper', 'Griffin', 'Cohen', 'Dalio', 'Simons', 'Loeb', 'Bacon',
    'Andurand', 'Odey', 'Rokos', 'Klarman', 'Howard',
    # Full-name variants
    'Stanley Druckenmiller', 'Bill Ackman', 'George Soros', 'John Paulson',
    'David Einhorn', 'David Tepper', 'Ken Griffin', 'Steve Cohen',
    'Ray Dalio', 'Jim Simons', 'Seth Klarman', 'Paul Tudor Jones',
    'Julian Robertson', 'Lee Ainslie', 'Louis Bacon', 'Alan Howard',
    'Dan Loeb', 'Howard Marks',
    # Fund / firm names
    'Bridgewater', 'Citadel', 'Millennium', 'Point72', 'Renaissance Technologies',
    'BlueCrest', 'Elliott Management', 'Elliott Associates',
    'Tiger Global', 'Tiger Management', 'LTCM', 'Long-Term Capital Management',
    'Amaranth', 'Two Sigma', 'D.E. Shaw', 'Viking Global', 'Lone Pine',
    'Third Point', 'Balyasny', 'AQR', 'Winton', 'Aspect Capital',
    'Brevan Howard', 'Man Group', 'Tudor Investment', 'Glenview', 'Coatue',
    'Maverick Capital', 'Pershing Square', 'Greenlight Capital',
    'Appaloosa Management', 'Baupost Group', 'Moore Capital', 'Highbridge',
    'Farallon', 'Canyon Capital', 'Caxton', 'King Street', 'Centerbridge',
    'Oaktree Capital', 'Duquesne',
]
# Longest-first so multi-word names match before single-word prefixes
FUND_NAMES_RE = re.compile(
    r'\b(' + '|'.join(re.escape(n) for n in sorted(_FUND_NAMES, key=len, reverse=True)) + r')\b',
    re.IGNORECASE
)

# Normalize long-form / firm names to canonical short names
_FUND_CANONICAL = {
    'bill ackman': 'Ackman',
    'george soros': 'Soros',
    'john paulson': 'Paulson',
    'stanley druckenmiller': 'Druckenmiller',
    'david einhorn': 'Einhorn / Greenlight',
    'david tepper': 'Tepper / Appaloosa',
    'ken griffin': 'Griffin / Citadel',
    'steve cohen': 'Cohen / Point72',
    'ray dalio': 'Dalio / Bridgewater',
    'jim simons': 'Simons / Renaissance',
    'seth klarman': 'Klarman / Baupost',
    'paul tudor jones': 'Tudor Jones',
    'julian robertson': 'Robertson / Tiger',
    'lee ainslie': 'Ainslie / Maverick',
    'louis bacon': 'Bacon / Moore',
    'alan howard': 'Howard / Brevan',
    'dan loeb': 'Loeb / Third Point',
    'howard marks': 'Marks / Oaktree',
    'elliott management': 'Elliott',
    'elliott associates': 'Elliott',
    'renaissance technologies': 'Renaissance',
    'long-term capital management': 'LTCM',
    'greenlight capital': 'Einhorn / Greenlight',
    'appaloosa management': 'Tepper / Appaloosa',
    'baupost group': 'Klarman / Baupost',
    'oaktree capital': 'Marks / Oaktree',
    'tiger global': 'Tiger',
    'tiger management': 'Tiger',
    'maverick capital': 'Ainslie / Maverick',
    'pershing square': 'Ackman / Pershing',
}

def extract_fund_name(text):
    """Return first hedge fund or manager name found in text, canonicalized."""
    m = FUND_NAMES_RE.search(text)
    if not m:
        return None
    name = m.group(1)
    return _FUND_CANONICAL.get(name.lower(), name)

# Sentence-level trade trigger keywords
TRADE_TRIGGERS = [
    r'\b(?:bought|purchased|acquired|established|built|entered|initiated|opened)\b',
    r'\b(?:sold|shorted|established a short|went short on|bet against)\b',
    r'\b(?:long position|short position|net long|net short)\b',
    r'\b(?:trade[d]?|position[d]?|invest(?:ed|s|ing))\b',
    r'\b(?:CDS on|put on|call on|options on)\b',
    r'\b(?:arbitrage(?:d|s)?|arb(?:ed|s)?)\b',
    r'\b(?:hedge[d]?|hedging)\b',
    r'\b(?:profit(?:ed|s)?|loss|made \$|lost \$|gain(?:ed|s)?|return(?:ed|s)?)\b',
    r'\b(?:spread trade|basis trade|relative value|pairs trade)\b',
    r'\b(?:leveraged|levered|unlevered)\b',
]

# Negated trade language must not be promoted into a directional observation.
# Keep this deliberately local: a negator governs at most the nearby clause and
# stops at an adversative conjunction.  The explicit ``not only`` exception is
# important because that construction normally introduces (rather than denies)
# a real position, e.g. "not only long oil but also short airlines".
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|neither|nor|cannot|can['’]t|"
    r"didn['’]t|doesn['’]t|isn['’]t|wasn['’]t|weren['’]t|"
    r"hasn['’]t|haven['’]t|hadn['’]t|won['’]t|wouldn['’]t|"
    r"couldn['’]t|shouldn['’]t)\b",
    re.IGNORECASE,
)
_NEGATION_EXCEPTION_RE = re.compile(r'^\s*(?:only|just|merely)\b', re.IGNORECASE)
_NEGATION_STOP_RE = re.compile(
    r'\b(?:but|however|yet|although|though|rather|instead)\b',
    re.IGNORECASE,
)
_OWNS_PUTS_RE = re.compile(
    r'\b(?:long|bought|purchased|buying|own(?:s|ed)?|hold(?:s|ing)?)\s+put[s]?\b',
    re.IGNORECASE,
)
_AFFIRMATIVE_AFTER_CONTRAST_RE = re.compile(
    r'\b(?:went|goes|going)\s+(?:long|short)\b|'
    r'\b(?:established|initiated|opened|put on|took|built)\s+(?:a\s+|an\s+)?(?:long|short)\b|'
    r'\b(?:shorted|sold short|bought puts?|bought protection)\b|'
    r'\b(?:long|short)\s+(?:the\s+)?(?:bond|gilt|treasur|equit|stock|share|oil|crude|gold|silver|copper|gas|credit|duration|dollar|sterling|yen|euro|pound|volatil|gamma|vega|VIX)',
    re.IGNORECASE,
)

_URL_RE = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)
_REFERENCE_HEADING_RE = re.compile(
    r'^(?:references?|bibliography|sources?|footnotes?|further reading)\s*[:&-]*\s*$',
    re.IGNORECASE,
)
_REFERENCE_MARKER_RE = re.compile(
    r'^\s*(?:\[(?:\d+|[ivxlcdm]+|[\u00b9\u00b2\u00b3\u2070-\u2079]+)\]|\d{1,3}[.)])\s*',
    re.IGNORECASE,
)
_CITATION_METADATA_RE = re.compile(
    r'\b(?:doi|arxiv|available at|retrieved|accessed|working paper|quarterly review|'
    r'journal of|proceedings|volume\s+\d+|vol\.\s*\d+|pp?\.\s*\d+)\b',
    re.IGNORECASE,
)
_CITATION_LINE_RE = re.compile(
    r'(?:\b(?:source|article|white paper|tutorial)\b\.?\s*$|'
    r'\([A-Z][a-z]{2,8}\.?\s+(?:\d{1,2},?\s+)?(?:19|20)\d{2}\)\s*$|'
    r'\b[A-Z][A-Za-z-]+,\s*(?:[A-Z]\.(?:\s*[A-Z]\.)?|[A-Z][a-z]+)'
    r'.{0,120}\((?:19|20)\d{2}\))',
    re.IGNORECASE,
)


def _is_negated_signal(text, start):
    """Return whether a nearby explicit negator governs a signal at ``start``."""
    # A sentence/clause boundary ends negation scope.  Commas intentionally do
    # not: "no verified, current arbitrage" is a common construction.
    prefix_start = max(0, start - 180)
    prefix = text[prefix_start:start]
    boundary = max(prefix.rfind(mark) for mark in ('.', '!', '?', ';', ':', '\n', ')', '—', '–'))
    clause = prefix[boundary + 1:]
    negations = list(_NEGATION_RE.finditer(clause))
    if not negations:
        return False

    negation = negations[-1]
    between = clause[negation.end():]
    # YES/NO is an instrument side in prediction markets, not grammatical
    # negation.  Only exempt an uppercase NO when nearby syntax makes that role
    # explicit; sentence-initial "No arbitrage" remains negated.
    if negation.group(0) == 'NO':
        nearby_start = max(0, prefix_start + boundary + 1 + negation.start() - 80)
        nearby_end = min(len(text), start + 80)
        nearby = text[nearby_start:nearby_end]
        if (re.search(r'\bYES\s*/\s*NO\b', nearby) or
                re.search(r'\b(?:buy|sell|bought|sold)\b.{0,60}\bNO\b', nearby, re.IGNORECASE) or
                re.search(r'\bNO\s+(?:shares?|contracts?|tokens?|position)\b', nearby)):
            return False
    if _NEGATION_EXCEPTION_RE.match(between):
        return False
    if _NEGATION_STOP_RE.search(between):
        return False

    # Bound the scope so an unrelated negation early in a long clause cannot
    # suppress a later, affirmative position.
    return len(re.findall(r"\b[\w'-]+\b", between)) <= 14


def _negated_trade_signal_spans(text):
    """Return spans of trade signals governed by explicit local negation."""
    spans = []
    patterns = (DIRECTION_LONG, DIRECTION_SHORT, DIRECTION_ARB, *TRADE_TRIGGERS)
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if _is_negated_signal(text, match.start()):
                spans.append((match.start(), match.end()))
    for match in _OWNS_PUTS_RE.finditer(text):
        if _is_negated_signal(text, match.start()):
            spans.append((match.start(), match.end()))
    return spans


def has_negated_trade_signal(text):
    """Return True when text explicitly denies a nearby trade signal."""
    return bool(_negated_trade_signal_spans(text))


def _mask_negated_trade_signals(text):
    """Blank explicitly negated trade signals while preserving text offsets."""
    spans = _negated_trade_signal_spans(text)

    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        chars[start:end] = ' ' * (end - start)
    return ''.join(chars)


def _is_reference_line(line):
    """Identify a single bibliography/source-list line conservatively."""
    line = line.strip()
    if not line or _REFERENCE_HEADING_RE.fullmatch(line):
        return True
    has_reference_marker = bool(_REFERENCE_MARKER_RE.match(line))

    urls = list(_URL_RE.finditer(line))
    if not urls:
        return has_reference_marker and bool(
            _CITATION_METADATA_RE.search(line) or _CITATION_LINE_RE.search(line)
        )

    if has_reference_marker:
        return True

    without_urls = _URL_RE.sub(' ', line)
    if len(re.findall(r'[A-Za-z]{2,}', without_urls)) <= 3:
        return True
    if line.lower().startswith(('http://', 'https://', 'www.')):
        return True
    if _CITATION_METADATA_RE.search(line):
        return True

    # Source-list entries usually consist of one citation clause followed by a
    # terminal URL.  Do not apply this to prose with multiple sentences or to a
    # URL embedded mid-paragraph; those can contain substantive analysis.
    trailing = line[urls[-1].end():].strip(' \t\r\n.,;:|()[]')
    before_url = line[:urls[-1].start()].rstrip()
    sentence_breaks = re.search(r'[.!?]\s+[A-Z]', before_url)
    source_separator = re.search(r'(?:\s[|—–-]\s|:\s*)$', before_url)
    if not trailing and not sentence_breaks and source_separator:
        return True
    return False


def is_reference_only_block(text):
    """Return True when every non-empty line is bibliography/URL metadata."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_is_reference_line(line) for line in lines)

DIRECTION_MAP = {
    'long': ['long', 'bought', 'buy', 'purchase', 'acquired', 'bullish', 'call option', 'call spread', 'upside', 'entered long'],
    'short': ['short', 'sold short', 'put option', 'bearish', 'bet against', 'cds buy', 'credit protection'],
    'arbitrage': ['arbitrage', 'arb', 'relative value', 'pairs trade', 'basis trade', 'convergence', 'merger arb', 'risk arb'],
    'relative value': ['spread trade', 'relative value', 'pairs trade', 'long short', 'market neutral'],
    'long/short': ['long', 'short'],
}

def _classify_direction_without_negation(text):
    # Strip common false-positive phrases before matching so a bare "long"/"short"
    # left behind is much more likely to be an actual position than prose.
    # "long/short" / "long-short" as a compound is a fund-type adjective, not a trade
    # ("a long/short equity fund"); drop it so it can't trip the long or short signals.
    cleaned = re.sub(r'\blong[/\-]short\b', ' ', text, flags=re.IGNORECASE)
    cleaned = re.sub(
        r'\b(?:traditional\s+)?arbitrage[- ]based pricing\b|\barbitrage pricing\b',
        ' ',
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r'\bshort[\s\-](?:term|dated|run|fall|sighted|hand|list|cut|age|change|selling|squeeze)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\blong[\s\-](?:term|run|dated|standing|time|only|awaited|haul|lasting|suffering|list|way|history|period|stretch|line|shot|shadow)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = _mask_negated_trade_signals(cleaned)
    # Owning puts (long/bought/purchased puts) is a bearish position — neutralize it so
    # the generic "bought"/"long" signal doesn't misfire it as long; count it as short.
    owns_puts = bool(_OWNS_PUTS_RE.search(cleaned))
    if owns_puts:
        cleaned = _OWNS_PUTS_RE.sub('OWNED_PUT', cleaned)
    if re.search(DIRECTION_ARB, cleaned, re.IGNORECASE):
        return 'arbitrage/relative value'
    long_match  = re.search(DIRECTION_LONG,  cleaned, re.IGNORECASE)
    short_match = re.search(DIRECTION_SHORT, cleaned, re.IGNORECASE) or owns_puts
    # Two-legged structure: "long <asset> … short <asset>" within one sentence (pairs /
    # calendar / multi-leg). Term-words were stripped above, so a residual long+short
    # pairing in a trade paragraph is almost always a genuine long/short book — even when
    # a leg's asset isn't enumerated (e.g. "long oil and short airlines"). Requiring a
    # word after each side excludes the fund-type adjective "long/short" / "long-short".
    structural_ls = bool(re.search(r'\blong\s+\w[^.]{1,80}?\bshort\s+\w', cleaned, re.IGNORECASE) or
                         re.search(r'\bshort\s+\w[^.]{1,80}?\blong\s+\w', cleaned, re.IGNORECASE))
    if (long_match and short_match) or structural_ls:
        return 'long/short'
    if long_match:
        return 'long'
    if short_match:
        return 'short'
    return 'unspecified'


def classify_direction(text):
    # Precision is more important than recall in an institutional research
    # index.  A bounded passage containing an explicitly negated trade signal
    # abstains unless an adversative clause ("but", "however", etc.) then states
    # a separate affirmative position.  The observation remains searchable and
    # its source text remains visible even when its stance is unspecified.
    # ``has_negated_trade_signal`` already exempts affirmative constructions
    # such as "not only long ..." and YES/NO prediction-market contracts.
    if has_negated_trade_signal(text):
        for stop in _NEGATION_STOP_RE.finditer(text):
            prefix = text[:stop.start()]
            tail = text[stop.end():]
            if (has_negated_trade_signal(prefix)
                    and not has_negated_trade_signal(tail)
                    and _AFFIRMATIVE_AFTER_CONTRAST_RE.search(tail)):
                affirmative = _classify_direction_without_negation(tail)
                if affirmative != 'unspecified':
                    return affirmative
        return 'unspecified'
    return _classify_direction_without_negation(text)

def find_instruments(text):
    found = []
    for instrument, pattern in INSTRUMENTS.items():
        if re.search(pattern, text, re.IGNORECASE):
            found.append(instrument)
    return found if found else ['unspecified']

def extract_quant_details(text):
    details = []
    for pattern in QUANT_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        details.extend(matches[:5])  # Limit to 5 matches per pattern
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for d in details:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return ', '.join(unique[:20]) if unique else None

def split_into_sentences(text):
    # Simple sentence splitter
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    return sentences

def find_paragraph_blocks(text, window=5):
    """Return blocks of consecutive sentences containing trade triggers."""
    sentences = split_into_sentences(text)
    blocks = []
    i = 0
    while i < len(sentences):
        sent = sentences[i]
        signal_text = _mask_negated_trade_signals(sent)
        has_trigger = any(re.search(p, signal_text, re.IGNORECASE) for p in TRADE_TRIGGERS)
        has_instrument = any(re.search(p, sent, re.IGNORECASE) for p in INSTRUMENTS.values())

        if not is_reference_only_block(sent) and has_trigger and has_instrument:
            # Build context window
            start = max(0, i - 1)
            end = min(len(sentences), i + window)
            block = ' '.join(sentences[start:end])
            blocks.append(block.strip())
            i = end
        else:
            i += 1
    return blocks

def extract_underlying(text):
    """Try to extract the main underlying asset/issuer/index."""
    # Look for company names, indices, currencies. Company capitalization is
    # meaningful: applying IGNORECASE to its legal-suffix pattern turned phrases
    # such as "against Vegas on its co-terminal" into bogus company names.
    patterns = [
        (r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}(?:\s+(?:Inc|Corp|Ltd|plc|Group|Holdings|Capital|Management|Fund|AG|SA|SE|NV|Co))\.?)\b', 0),
        (r'\b(S&P\s*500|Nasdaq|FTSE|DAX|Nikkei|Hang Seng|Russell\s*\d+|MSCI|CDX|iTraxx|VIX)\b', re.IGNORECASE),
        (r'\b(Bank Nifty|Nifty\s*50|BSE Sensex|Hang Seng|Nikkei 225|ASX 200|CAC 40|Euro Stoxx|STOXX 50|STOXX 600)\b', re.IGNORECASE),
        (r'\b(TLT|SPY|QQQ|IWM|HYG|LQD|GLD|SLV|USO|XLE|XLF|XLK|EEM|GDX|VXX|UVXY)\b', re.IGNORECASE),
        (r'\b(TTF|EUA|CCA|JGB|OAT|BTP)\b', re.IGNORECASE),
        (r'\b(US\s*(?:Treasury|10-year|2-year|30-year)|UK\s*Gilt|German\s*Bund|JGB|Italian\s*BTP|Greek\s*bond)\b', re.IGNORECASE),
        (r'\b(WTI|Brent|crude oil|natural gas|gold|silver|copper|wheat|corn|soybeans)\b', re.IGNORECASE),
        (r'\b(USD|EUR|GBP|JPY|CHF|AUD|CAD|CNY|EM currencies?)\b', re.IGNORECASE),
        (r'\b(Bitcoin|Ethereum|crypto)\b', re.IGNORECASE),
    ]
    found = []
    for pattern, flags in patterns:
        matches = re.findall(pattern, text, flags)
        found.extend(m if isinstance(m, str) else m[0] for m in matches[:3])
    if found:
        return '; '.join(list(dict.fromkeys(found))[:5])
    return None

def extract_outcome(text):
    """Extract mentions of trade outcome/PnL."""
    patterns = [
        r'(?:profit(?:ed)?|made|earned|gained?|returned?|generated)\s+(?:approximately\s+)?\$[\d,]+(?:\.\d+)?\s*(?:billion|million|B|M)\b[^.]*\.',
        r'(?:lost?|lost|loss)\s+(?:approximately\s+)?\$[\d,]+(?:\.\d+)?\s*(?:billion|million|B|M)\b[^.]*\.',
        r'return(?:ed)?\s+[\d.]+%[^.]*\.',
        r'P[&/]?L\s+of\s+[\$\d,.]+[^.]*\.',
        r'(?:up|down)\s+[\d.]+%\s+(?:on|from|in)[^.]*\.',
        r'(?:gain|loss)\s+of\s+[\$\d,.]+[^.]*\.',
    ]
    outcomes = []
    for p in patterns:
        matches = re.findall(p, text, re.IGNORECASE)
        outcomes.extend(matches[:2])
    if outcomes:
        return ' | '.join(list(dict.fromkeys(outcomes))[:3])
    return None

def extract_thesis(text):
    """Extract the edge/thesis."""
    patterns = [
        r'(?:\b(?:because|since|as|given that|due to|on the thesis that)\b|\bthesis\b\s*[:—]?)\s+([^.!?]{20,150}[.!?])',
        r'(?:the trade was based on|the edge was|the rationale was|bet(?:ting)? (?:on|that)|believed that)\s+([^.!?]{20,150}[.!?])',
        r'(?:mispricing|arbitrage opportunity|relative value|divergence|dislocation)[^.!?]{0,100}[.!?]',
        r'(?:expected|anticipated|predicted|forecasted)\s+([^.!?]{20,150}[.!?])',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            thesis = m.group(1) if m.lastindex else m.group(0)
            thesis = thesis.strip()
            if len(thesis) > 20:
                return thesis[:200]
    return None


def excerpt(text, limit=800):
    """Return an at-most-limit excerpt without cutting the final word."""
    text = text.strip()
    if len(text) <= limit:
        return text

    head = text[:limit]
    # If the limit lands inside a word, discard that partial word. Normal prose
    # always has an earlier whitespace boundary; the fallback keeps a useful
    # bounded excerpt even for a pathological single-token input.
    if not text[limit].isspace() and not head[-1].isspace():
        boundary = max(head.rfind(' '), head.rfind('\n'), head.rfind('\t'))
        if boundary > 0:
            head = head[:boundary]
    return head.rstrip()

def is_trade_block(block):
    """Check if a text block describes a specific trade (not just background info)."""
    if is_reference_only_block(block):
        return False

    signal_text = _mask_negated_trade_signals(block)
    has_trigger = any(re.search(p, signal_text, re.IGNORECASE) for p in TRADE_TRIGGERS)
    has_instrument = any(re.search(p, block, re.IGNORECASE) for p in INSTRUMENTS.values())
    has_quant = any(re.search(p, block, re.IGNORECASE) for p in QUANT_PATTERNS[:8])
    has_direction = (re.search(DIRECTION_LONG, signal_text, re.IGNORECASE) or
                     re.search(DIRECTION_SHORT, signal_text, re.IGNORECASE) or
                     re.search(DIRECTION_ARB, signal_text, re.IGNORECASE))

    # Require at least: (trigger OR direction) AND instrument
    return (has_trigger or has_direction) and has_instrument

def process_article(post):
    """Extract trades from a single article."""
    text = post.get('body_text', '')
    title = post.get('title', '')
    url = post.get('url', '')
    date = post.get('post_date', '')[:10]

    if not text or len(text) < 200:
        return []

    # Split text into paragraphs
    paragraphs = [
        p.strip()
        for p in text.split('\n\n')
        if p.strip() and len(p.strip()) > 50 and not is_reference_only_block(p)
    ]

    trades = []

    # Strategy 1: Find explicit trade blocks in paragraphs
    for i, para in enumerate(paragraphs):
        description = excerpt(para)
        # Every structured label must be supportable by the passage we publish.
        # A directional word beyond the 800-character evidence boundary cannot
        # turn an otherwise non-trade excerpt into a directional observation.
        if is_trade_block(description):
            trade = {
                'article_title': title,
                'article_url': url,
                'article_date': date,
                'trade_description': description,
                # Every field shown or scored in the terminal is derived from
                # this exact published passage.  Adjacent paragraphs are not
                # allowed to leak evidence into the visible record.
                'instruments': find_instruments(description),
                'direction': classify_direction(description),
                'underlying': extract_underlying(description),
                'edge_or_thesis': extract_thesis(description),
                'any_quant_detail': extract_quant_details(description),
                'outcome_if_mentioned': extract_outcome(description),
                'fund_name_if_mentioned': extract_fund_name(description) or extract_fund_name(title),
                'description_truncated': len(description) < len(para.strip()),
            }
            trades.append(trade)

    # If no trades found through paragraph analysis, do full-text scan
    if not trades:
        # Scan only the same non-reference material considered above.  This
        # prevents a references section from being resurrected by the fallback.
        blocks = find_paragraph_blocks('\n\n'.join(paragraphs))
        for block in blocks[:5]:  # Limit to 5 blocks
            description = excerpt(block)
            if len(description) > 50 and is_trade_block(description):
                trade = {
                    'article_title': title,
                    'article_url': url,
                    'article_date': date,
                    'trade_description': description,
                    'instruments': find_instruments(description),
                    'direction': classify_direction(description),
                    'underlying': extract_underlying(description),
                    'edge_or_thesis': extract_thesis(description),
                    'any_quant_detail': extract_quant_details(description),
                    'outcome_if_mentioned': extract_outcome(description),
                    'fund_name_if_mentioned': extract_fund_name(description) or extract_fund_name(title),
                    'description_truncated': len(description) < len(block.strip()),
                }
                trades.append(trade)

    if not trades:
        return []

    # Deduplicate by trade_description
    seen_descs = set()
    unique_trades = []
    for t in trades:
        key = t['trade_description'][:100]
        if key not in seen_descs:
            seen_descs.add(key)
            unique_trades.append(t)

    return unique_trades


def main():
    print(f"Loading posts from {INPUT_PATH}...")
    with open(INPUT_PATH, 'r', encoding='utf-8') as f:
        all_posts = json.load(f)

    print(f"Processing {len(all_posts)} articles...")

    all_trades = []
    articles_with_trades = 0
    articles_no_trades = 0
    instrument_counts = {}

    for i, post in enumerate(all_posts, 1):
        sys.stdout.write(f'\r  Article {i}/{len(all_posts)}: {post["title"][:50]:<50}')
        sys.stdout.flush()

        trades = process_article(post)

        non_placeholder = [t for t in trades if 'No specific trades' not in t.get('trade_description', '')]
        if non_placeholder:
            articles_with_trades += 1
        else:
            articles_no_trades += 1

        for trade in trades:
            for instr in trade.get('instruments', []):
                instrument_counts[instr] = instrument_counts.get(instr, 0) + 1

        all_trades.extend(trades)

    print(f'\n\nTotal trades extracted: {len(all_trades)}')
    print(f'Articles with trades: {articles_with_trades}')
    print(f'Articles with no trades identified: {articles_no_trades}')

    # Save output atomically so interruption cannot truncate the tracked dataset.
    atomic_write_json(OUTPUT_PATH, all_trades)

    print(f'\nSaved {len(all_trades)} trade records to {OUTPUT_PATH}')

    print('\n=== SUMMARY ===')
    print(f'Total articles processed: {len(all_posts)}')
    print(f'Total trades found: {len(all_trades)}')
    print('\nBreakdown by instrument type:')
    for instrument, count in sorted(instrument_counts.items(), key=lambda x: -x[1]):
        if instrument != 'unspecified':
            print(f'  {instrument:20s}: {count}')
    print(f'  {"unspecified":20s}: {instrument_counts.get("unspecified", 0)}')

    return all_trades


if __name__ == '__main__':
    main()
