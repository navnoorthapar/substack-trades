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

Validated model: qwen2.5:14b (local, free). On a 150-trade audit of the hard
residual cases it resolved ~11% at ~88% precision with ZERO direction
inversions — at/above the regex's precision bar. The weaker qwen2.5-coder:7b was
rejected (it confidently inverted some long/short calls). Precision comes from
three guards in the prompt + code below: an explicit "describe-context ->
abstain" list, a "closing a position is not a short" rule, and a HIGH-confidence
-only gate.

Still gated behind DIRECTION_LLM_ENABLE so it never runs unintentionally;
refresh.sh sets it. Requires the Ollama daemon up and the model pulled:
    ollama serve                 # daemon (usually already running as a service)
    ollama pull qwen2.5:14b

Tuning via env:
    DIRECTION_LLM_ENABLE  must be 1/true/yes to run at all (refresh.sh sets it)
    DIRECTION_LLM_MODEL   ollama model id   (default qwen2.5:14b)
    OLLAMA_URL            base url          (default http://localhost:11434)
    DIRECTION_LLM_LIMIT   cap trades per run (for testing; default = all)
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from extract_trades import has_negated_trade_signal, is_reference_only_block

ROOT   = Path(__file__).parent
TRADES = Path(os.environ.get('TRADES_PATH', ROOT / 'trades_extracted.json'))
CACHE  = Path(os.environ.get('DIRECTION_CACHE_PATH', ROOT / '.direction_cache.json'))

MODEL      = os.environ.get('DIRECTION_LLM_MODEL', 'qwen2.5:14b')
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
LIMIT      = int(os.environ.get('DIRECTION_LLM_LIMIT', '0') or 0)

VALID = ('long', 'short', 'long/short', 'arbitrage/relative value', 'unspecified')

SYSTEM = """You label the DIRECTION of a single hedge-fund trade described in a short text, for a trade-intelligence dashboard. Return exactly one label:

- "long" — the fund is net long / bullish the underlying (buys, owns, accumulates a stake, long calls, long the asset/vol).
- "short" — net short / bearish (shorts, sells short, buys protection or puts, bets against, underweight, short vol/VIX, writes/sells calls).
- "long/short" — an explicit two-legged position: pairs trade, calendar/curve spread, "long X and short Y", or market-neutral with named legs.
- "arbitrage/relative value" — convergence / basis / merger / dispersion / relative-value trades where the position IS the spread rather than a single direction.
- "unspecified" — the text does NOT clearly state a directional position for THIS fund. Includes market-making / liquidity provision, pure performance or PnL commentary, methodology/footnote notes, background narrative, or anything ambiguous.

Always return "unspecified" for text that describes CONTEXT rather than a specific position the fund took, including:
- portfolio-structure descriptions ("holds 30-40 uncorrelated positions across asset classes"),
- citations, URLs, glossary or methodology text,
- macro commentary, outlook or warnings with no stated position ("2026 will be a dangerous year"),
- option-pricing / hedging-cost mechanics, and dealer or market-maker hedging flow,
- general descriptions of hedge funds' role or economics in a market mechanism (securities lending / earning the spread on borrowed shares, the variance-risk-premium-as-insurance analogy, "as directional traders they read balance sheets") rather than a specific named position.

Rules:
- PRECISION OVER RECALL. Only assign a direction when the text explicitly states a position the fund actually took. When unsure, return "unspecified". Do not guess from vibes, tone, or mechanism.
- CLOSING a position is NOT a short. Exiting, selling, trimming, reducing, or removing an existing long (e.g. "removed its entire X position", "-100%", "sold its stake", "cut exposure", "exited") is "unspecified" unless a NEW short position is explicitly opened.
- Use "high" confidence ONLY when the position is explicitly stated; otherwise "medium" or "low".
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


def _eligible_for_direction_resolution(trade):
    """Never let cached/model output reverse an explicit denial or citation."""
    description = str(trade.get('trade_description') or '')
    return not (
        has_negated_trade_signal(description)
        or is_reference_only_block(description)
    )


def _load_cache():
    if CACHE.exists():
        try:
            with open(CACHE, encoding='utf-8') as handle:
                return json.load(handle)
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
    """Classify one trade. Returns one of VALID, or None on a *persistent backend
    outage* (Ollama down/restarting) so the caller can leave it UNCACHED and retry
    on a later run — an outage must never be frozen into the cache as a real
    abstention."""
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
        "format": SCHEMA,                              # Ollama structured output
        "options": {"temperature": 0, "num_ctx": 2048},  # deterministic + small KV cache
    }
    body = json.dumps(payload).encode('utf-8')
    last_err = None
    for attempt in range(4):  # ride out a daemon crash+restart (RAM pressure on 14B)
        try:
            req = urllib.request.Request(
                f'{OLLAMA_URL}/api/chat', data=body,
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read())
            content = (resp.get('message') or {}).get('content', '')
            data = json.loads(content)
            # Precision-first: accept a direction only at HIGH confidence. medium/low
            # is where the over-labeling lived in testing, so it abstains there.
            if data.get('confidence') != 'high':
                return 'unspecified'
            direction = data.get('direction', 'unspecified')
            return direction if direction in VALID else 'unspecified'
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e            # backend outage — wait for restart, then retry
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            # malformed model output etc. — a real (cacheable) abstention, not an outage
            print(f'    classify parse error ({type(e).__name__}): {e}')
            return 'unspecified'
    print(f'    backend unavailable after retries: {last_err}')
    return None


def run():
    # Gated so it never runs unintentionally (e.g. a stray manual invocation, or
    # before a model is pulled). refresh.sh sets DIRECTION_LLM_ENABLE=1 for the
    # automated pipeline. The default model (qwen2.5:14b) was validated to ~88%
    # precision with zero inversions; do not point this at a weaker model.
    if os.environ.get('DIRECTION_LLM_ENABLE', '').lower() not in ('1', 'true', 'yes'):
        print('Direction LLM resolver disabled (set DIRECTION_LLM_ENABLE=1 to enable). '
              'Keeping regex-only directions.')
        return

    with open(TRADES, encoding='utf-8') as handle:
        trades = json.load(handle)
    unspecified = [
        t for t in trades
        if (t.get('direction') or 'unspecified') == 'unspecified'
    ]
    targets = [t for t in unspecified if _eligible_for_direction_resolution(t)]
    blocked = len(unspecified) - len(targets)
    if blocked:
        print(
            f'{blocked} explicitly negated/reference-only trade(s) kept unspecified; '
            'cached/model direction overrides are disabled for them.'
        )
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
                if label is None:
                    continue  # backend outage: leave uncached so a re-run retries it
                cache[k] = label
                if label != 'unspecified':
                    t['direction'] = label
                changed = True
                if i % 25 == 0:
                    print(f'  {i}/{len(todo)} done')
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
