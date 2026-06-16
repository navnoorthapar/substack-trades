#!/usr/bin/env python3
"""Hybrid direction resolver — the LLM half of a regex+LLM hybrid extractor.

Web research (and our own corpus audit) is clear: rule-based regex is brittle on
narrative financial prose and plateaus on nuanced fields like trade direction.
The production-grade pattern is a HYBRID — let the (cheap, deterministic) regex
classify the confident majority, and call an LLM ONLY on the low-confidence
remainder, with the LLM instructed to abstain ("unspecified") when unsure. That
abstention is what keeps precision high instead of trading false-positives for
coverage.

This script is that LLM pass. It reads trades_extracted.json, finds the trades
the regex left as 'unspecified', and asks Claude to classify each — keeping the
regex result for everything else.

Design goals (so it is safe to drop into the automated pipeline):
  * Opt-in / fail-safe. No `anthropic` SDK or no ANTHROPIC_API_KEY  ->  clean
    no-op; the regex-only output is left untouched and the pipeline continues.
  * Cached. Classifications are cached by description hash in
    .direction_cache.json, so each run only calls the API for newly-seen
    'unspecified' trades. This keeps cost ~$0 after the first backfill AND makes
    the output deterministic (so refresh.sh's "commit only if changed" gate does
    not churn on LLM nondeterminism).
  * Atomic writes; never raises into the pipeline.

Enable on the Mac that runs the pipeline:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...        # add to the LaunchAgent env too

Tuning via env:
    DIRECTION_LLM_MODEL   model id (default claude-opus-4-8; claude-haiku-4-5 is
                          the cheap option for this simple classification)
"""
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

ROOT   = Path(__file__).parent
TRADES = ROOT / 'trades_extracted.json'
CACHE  = ROOT / '.direction_cache.json'

MODEL = os.environ.get('DIRECTION_LLM_MODEL', 'claude-opus-4-8')
VALID = ('long', 'short', 'long/short', 'arbitrage/relative value', 'unspecified')

SYSTEM = """You label the DIRECTION of a single hedge-fund trade described in a short text, for a trade-intelligence dashboard. Return exactly one label:

- "long" — the fund is net long / bullish the underlying (buys, owns, accumulates a stake, long calls, long the asset/vol).
- "short" — net short / bearish (shorts, sells short, buys protection or puts, bets against, underweight, short vol/VIX, writes/sells calls).
- "long/short" — an explicit two-legged position: pairs trade, calendar/curve spread, "long X and short Y", or market-neutral with named legs.
- "arbitrage/relative value" — convergence / basis / merger / dispersion / relative-value trades where the position IS the spread rather than a single direction.
- "unspecified" — the text does NOT clearly state a directional position for THIS fund. Includes market-making / liquidity provision, pure performance or PnL commentary, methodology/footnote notes, background narrative, or anything ambiguous.

Rules:
- PRECISION OVER RECALL. If you are not confident the text states a clear position for this fund, return "unspecified" with low confidence. Do not guess.
- Classify the position of the fund the paragraph is ABOUT — ignore incidental mentions of other market participants (e.g. "market makers were short gamma" is not the fund's trade).
- Owning or buying puts is bearish -> "short". Selling or writing calls is bearish -> "short". Selling puts is bullish.
- A long straddle/strangle (long both a call and a put) is a volatility position, not directional -> "unspecified" unless a clear net direction is stated."""

SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": list(VALID)},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["direction", "confidence"],
    "additionalProperties": False,
}


def _key(desc):
    return hashlib.sha256(desc.encode('utf-8')).hexdigest()


def _load_cache():
    if CACHE.exists():
        try:
            return json.load(open(CACHE, encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _atomic_write(path, obj):
    tmp = path.parent / (path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _classify(client, trade):
    """Return one of VALID. Any failure degrades to 'unspecified' (never raises)."""
    desc = (trade.get('trade_description') or '')[:1500]
    instruments = ', '.join(trade.get('instruments') or [])
    underlying = trade.get('underlying') or ''
    user = f"Instruments: {instruments}\nUnderlying: {underlying}\n\nTrade text:\n{desc}"
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == 'text'), '')
        data = json.loads(text)
        if data.get('confidence') == 'low':
            return 'unspecified'
        direction = data.get('direction', 'unspecified')
        return direction if direction in VALID else 'unspecified'
    except Exception as e:  # API error, parse error, network — all fail safe
        print(f'    classify error ({type(e).__name__}): {e}')
        return 'unspecified'


def run():
    trades = json.load(open(TRADES, encoding='utf-8'))
    targets = [t for t in trades if (t.get('direction') or 'unspecified') == 'unspecified']
    if not targets:
        print('No unspecified directions to resolve.')
        return

    cache = _load_cache()
    changed = False

    # 1) Apply any cached classifications first — free, no API call needed.
    todo = []
    for t in targets:
        k = _key(t.get('trade_description') or '')
        cached = cache.get(k)
        if cached in VALID:
            if cached != 'unspecified':
                t['direction'] = cached
                changed = True
        else:
            todo.append((k, t))

    # 2) Resolve the rest via the LLM — only if it's actually available.
    if todo:
        sdk_ok = True
        try:
            import anthropic  # noqa: F401
        except ImportError:
            sdk_ok = False
        key_ok = bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'))

        if not (sdk_ok and key_ok):
            why = 'anthropic SDK not installed' if not sdk_ok else 'ANTHROPIC_API_KEY not set'
            print(f'{len(todo)} unspecified trade(s) left for the LLM, but {why} — '
                  f'skipping LLM pass (regex-only output kept). '
                  f'`pip install anthropic` + set ANTHROPIC_API_KEY to enable.')
        else:
            import anthropic
            client = anthropic.Anthropic()
            print(f'Resolving {len(todo)} regex-unspecified trades via {MODEL}...')
            for i, (k, t) in enumerate(todo, 1):
                label = _classify(client, t)
                cache[k] = label
                if label != 'unspecified':
                    t['direction'] = label
                changed = True
                if i % 25 == 0:
                    print(f'  {i}/{len(todo)} resolved')
                    _atomic_write(CACHE, cache)  # checkpoint progress

    if changed:
        _atomic_write(TRADES, trades)
        _atomic_write(CACHE, cache)
        dist = Counter((t.get('direction') or 'unspecified') for t in trades)
        print('Direction distribution after LLM pass:',
              {k: dist.get(k, 0) for k in VALID})


def main():
    # The pipeline must never break because of this optional step.
    try:
        run()
    except Exception as e:
        print(f'llm_direction: non-fatal error, continuing with regex output: '
              f'{type(e).__name__}: {e}')


if __name__ == '__main__':
    main()
