#!/usr/bin/env python3
"""Hybrid direction resolver — the LLM half of a regex+LLM hybrid extractor.

Web research on structured extraction from financial prose converges on a
hybrid: rule-based regex handles the confident majority (cheap, deterministic),
and an LLM resolves only the low-confidence remainder, instructed to abstain
when unsure — which is what keeps precision high instead of trading
false-positives for coverage.

This script is that LLM pass, and it runs **100% free and local** against an
Ollama model on this machine (no API key, no per-token cost, no quota). It calls
Ollama's HTTP endpoint with nothing but the Python standard library, so the
pipeline keeps its "stdlib only, no pip install" property.

It reads trades_extracted.json, finds the trades the regex left as
'unspecified', and asks the local model to classify each — keeping the regex
result for everything else.

Safe to drop into the automated pipeline:
  * Opt-in / fail-safe. If Ollama isn't running (or the model isn't pulled) it is
    a clean no-op — regex-only output is left untouched and the pipeline
    continues.
  * Cached by description hash in .direction_cache.json, so each run only calls
    the model for newly-seen 'unspecified' trades. Keeps repeat cost ~free and
    output deterministic (so refresh.sh's "commit only if changed" gate doesn't
    churn on model nondeterminism).
  * Atomic writes; never raises into the pipeline.

DISABLED BY DEFAULT. Tested locally, qwen2.5-coder:7b scored *below* the tuned
regex on precision for the hard residual cases (it confidently inverted some
long/short calls), so it is not allowed to run automatically and degrade the
data. The hybrid stays wired and ready: point DIRECTION_LLM_MODEL at a model you
have validated (a larger local instruct model, etc.) and set DIRECTION_LLM_ENABLE=1.

Enable (one time, free, local):
    ollama serve                      # daemon (usually already running)
    ollama pull <a-model-you-trust>   # e.g. a larger instruct model
    export DIRECTION_LLM_ENABLE=1
    export DIRECTION_LLM_MODEL=<that-model>

Tuning via env:
    DIRECTION_LLM_ENABLE  must be 1/true/yes to run at all (default: off)
    DIRECTION_LLM_MODEL   ollama model id   (default qwen2.5-coder:7b)
    OLLAMA_URL            base url          (default http://localhost:11434)
    DIRECTION_LLM_LIMIT   cap trades per run (for testing; default = all)
"""
import hashlib
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT   = Path(__file__).parent
TRADES = ROOT / 'trades_extracted.json'
CACHE  = ROOT / '.direction_cache.json'

MODEL      = os.environ.get('DIRECTION_LLM_MODEL', 'qwen2.5-coder:7b')
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
LIMIT      = int(os.environ.get('DIRECTION_LLM_LIMIT', '0') or 0)

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
- A long straddle/strangle (long both a call and a put) is a volatility position, not directional -> "unspecified" unless a clear net direction is stated.

Answer with JSON only: {"direction": <label>, "confidence": "high"|"medium"|"low"}."""

SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": list(VALID)},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["direction", "confidence"],
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


def _ollama_models():
    """Return the set of installed model names, or None if the server is down."""
    try:
        req = urllib.request.Request(f'{OLLAMA_URL}/api/tags')
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return {m.get('name', '') for m in data.get('models', [])}
    except Exception:
        return None


def _classify(trade):
    """Return one of VALID. Any failure degrades to 'unspecified' (never raises)."""
    desc = (trade.get('trade_description') or '')[:1500]
    instruments = ', '.join(trade.get('instruments') or [])
    underlying = trade.get('underlying') or ''
    user = f"Instruments: {instruments}\nUnderlying: {underlying}\n\nTrade text:\n{desc}"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": SCHEMA,              # Ollama structured output
        "options": {"temperature": 0},  # deterministic -> stable cache
    }
    try:
        req = urllib.request.Request(
            f'{OLLAMA_URL}/api/chat',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
        content = (resp.get('message') or {}).get('content', '')
        data = json.loads(content)
        if data.get('confidence') == 'low':
            return 'unspecified'
        direction = data.get('direction', 'unspecified')
        return direction if direction in VALID else 'unspecified'
    except Exception as e:
        print(f'    classify error ({type(e).__name__}): {e}')
        return 'unspecified'


def run():
    # Default OFF. The local 7B model tested below the regex's precision bar on the
    # hard residual cases (it confidently inverted some directions), so it must not
    # silently run in the automated pipeline and degrade the data. Opt in only once
    # you've pointed DIRECTION_LLM_MODEL at a model you've validated.
    if os.environ.get('DIRECTION_LLM_ENABLE', '').lower() not in ('1', 'true', 'yes'):
        print('Direction LLM resolver disabled (set DIRECTION_LLM_ENABLE=1 to enable). '
              'Keeping regex-only directions.')
        return

    trades = json.load(open(TRADES, encoding='utf-8'))
    targets = [t for t in trades if (t.get('direction') or 'unspecified') == 'unspecified']
    if not targets:
        print('No unspecified directions to resolve.')
        return

    cache = _load_cache()
    changed = False

    # 1) Apply cached classifications first — free, no model call needed.
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

    # 2) Resolve the rest via the local model — only if Ollama is actually up.
    if todo:
        if LIMIT:
            todo = todo[:LIMIT]
        installed = _ollama_models()
        if installed is None:
            print(f'{len(todo)} unspecified trade(s) left for the LLM, but Ollama is not '
                  f'reachable at {OLLAMA_URL} — skipping LLM pass (regex-only output kept). '
                  f'Start it with `ollama serve`.')
        elif MODEL not in installed and not any(m.split(':')[0] == MODEL.split(':')[0] for m in installed):
            print(f'{len(todo)} unspecified trade(s) left for the LLM, but model "{MODEL}" '
                  f'is not installed — skipping. Run `ollama pull {MODEL}`. '
                  f'Installed: {sorted(installed) or "none"}.')
        else:
            print(f'Resolving {len(todo)} regex-unspecified trades via local {MODEL}...')
            for i, (k, t) in enumerate(todo, 1):
                label = _classify(t)
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
