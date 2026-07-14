#!/usr/bin/env python3
"""Build compact, source-verifiable intelligence briefs from article bodies.

The brief layer deliberately does not summarize, rank, or recommend.  It keeps
the author's own lead and selected authored sections, together with exact body
offsets and hashes, so the deployed interface can surface useful research
without manufacturing an investment conclusion.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date


SCHEMA_VERSION = 1
VALID_SECTION_KINDS = {
    'evidence', 'mechanism', 'countercase', 'falsifier', 'implementation',
}
MAX_LEAD_CHARS = 620
MAX_SECTION_CHARS = 760
MAX_EVIDENCE_CHARS = 620
MAX_SECTIONS = 5
MAX_CHECKPOINTS = 3

PROMOTIONAL_RE = re.compile(
    r'(?:\bpatreon\b|\byoutube\b|\blinkedin\b|\bnotebooklm\b|'
    r'\bsubscribe\b|\bsign[ -]?up\b|\bnewsletter\b|\bdiscord\b|'
    r'\bunlock(?:ed)?\b|\bsupport (?:this|the) work\b|'
    r'\bjoin (?:the |our )?(?:community|membership)\b|'
    r'\bprefer (?:to )?(?:watch|listen)|\bwatch (?:the )?(?:full )?video\b|'
    r'\bvideo overview\b|\bcompanion (?:document|note|piece|report)\b|'
    r'\bread the full (?:trade|mechanism|research|analysis)\b|'
    r'\bexclusive (?:trade|research|analysis|content)\b|'
    r'\bfollow (?:me|the research|for more)\b|\babout the author\b|'
    r'\bdisclaimer\b|\b(?:purely|strictly) educational\b|'
    r'\bfor (?:educational|informational) purposes?\b|'
    r'\bnot (?:financial|investment|trading) advice\b|'
    r'\bpart of my .{0,60}research series\b|'
    r'\bdedicated to\b|\bin (?:loving )?memory of\b|'
    r'\bpublic research journal\b|'
    r'\bconnect with\b|github\.com|streamlit\.app)',
    re.IGNORECASE,
)
BYLINE_RE = re.compile(
    r'^(?:written\s+)?by\s*:?\s*navnoor\s+bawa(?:\s*[·|,—–-].*)?$',
    re.IGNORECASE,
)
UPDATE_NOTICE_RE = re.compile(
    r'\b(?:article|post|page|research|last)\s+(?:was\s+)?updated\b',
    re.IGNORECASE,
)
URL_OR_CITATION_RE = re.compile(
    r'(?:https?://|www\.|\bdoi\s*:|\bssrn\b|\barxiv\b)',
    re.IGNORECASE,
)
REFERENCE_HEADING_RE = re.compile(
    r'^(?:(?:primary|verified|complete|major|data)\s+)?'
    r'(?:sources?|references?)(?:\s*(?:&|and)\s*'
    r'(?:references?|methodology|further reading))?'
    r'(?:\s*[·:—–-]\s*[^.!?]{1,100})?$',
    re.IGNORECASE,
)

# Order matters.  Patterns are deliberately anchored: a prose sentence that
# happens to contain "results", "capacity", or "mechanism" is not a heading.
HEADING_PATTERNS = (
    ('falsifier', re.compile(
        r'^(?:what (?:would|could) (?:change|prove|confirm|invalidate|kill|falsif)\b.*|'
        r'what changes? (?:the|this) (?:view|case)\b.*|'
        r'.*\b(?:would|could) (?:substantially )?(?:weaken|invalidate|falsify|change) '
        r'(?:the|this) (?:thesis|view|case)\b.*|'
        r'(?:\w+\s+){0,5}falsif(?:y|ies|iable|ication)\b.*)$',
        re.IGNORECASE,
    )),
    ('countercase', re.compile(
        r'^(?:(?:the|an?|our|strongest|obvious|main|key|one|two|three|four|five)\s+)*'
        r'(?:objections?|counter(?:case|argument|point)s?|caveats?|limitations?|'
        r'risk factors|risk case|case against)\b.*|'
        r'^(?:what can go wrong|why this (?:may|might) fail|'
        r'what (?:this|the data) (?:does not|doesn.t) (?:show|prove))\b.*',
        re.IGNORECASE,
    )),
    ('implementation', re.compile(
        r'^(?:the\s+)?(?:what to (?:watch|check|do)\b.*|actionable\b.*|'
        r'implementation\b.*|(?:operational\s+)?playbook\b.*|position siz\w*\b.*|'
        r'capacity(?: analysis| bound| ceiling| discipline)?\b.*|'
        r'decay and capacity\b.*|practical implications?\b.*|next steps?\b.*|'
        r'trade construction\b.*|execution blueprints?\b.*|the trade)$|'
        r'^(?:tier|phase|part)\s+\w+\s*[—–:\-]\s+.*(?:implementation|execution)\b.*',
        re.IGNORECASE,
    )),
    ('evidence', re.compile(
        r'^(?:(?:part|act|phase)\s+\w+\s*[—–:\-]\s+)?(?:the\s+)?'
        r'(?:numbers?\b.*|data (?:points?|ledger|table)\b.*|'
        r'what the (?:data|numbers?)\b.*|'
        r'evidence\b.*|empirical\b.*|results?\b.*|computed\b.*|'
        r'founding paper\b.*|financials\b.*|'
        r'financial (?:results?|statements?|record|data|performance)\b.*|'
        r'the gap\b.*|record\b.*|'
        r'study window\b.*|sample size\b.*|calculation error\b.*|'
        r'verification table\b.*|key empirical findings?\b.*)|'
        r'^.{1,90}\b(?:primary|on-record|empirical) evidence$',
        re.IGNORECASE,
    )),
    ('mechanism', re.compile(
        r'^(?:(?:part|phase)\s+\w+\s*[—–:\-]\s+)?(?:the\s+)?'
        r'(?:mechanisms?\b.*|mechanics\b.*|how it works\b.*|'
        r'where .{1,80} (?:bites|comes from)\b.*|'
        r'why .{0,60}\b(?:edge|spread|discount|premium|signal|effect|anomaly|'
        r'strategy|trade|model)\b.{0,40}\b(?:works|matters|exists)\b.*|'
        r'convexity\b.*|'
        r'setup\b.*|core insight\b.*|missing term\b.*|'
        r'p&l mechanics\b.*|execution mechanics\b.*|'
        r'mechanics of\b.*|non-linear convexity\b.*)$',
        re.IGNORECASE,
    )),
)

CHECKPOINT_HEADING_RE = re.compile(
    r'^(?:what to (?:watch|check)\b.*|(?:public |key |upcoming )?'
    r'(?:checkpoints?|dates?|calendar)\b.*|catalyst watch\b.*|event calendar\b.*)$',
    re.IGNORECASE,
)

NUMBER_RE = re.compile(
    r'(?:[$€£¥]\s?\d|\d(?:[\d,.]*\d)?\s?(?:%|bp\b|bps\b|basis points?\b|'
    r'[x×]\b|million\b|billion\b|trillion\b))',
    re.IGNORECASE,
)
EVIDENCE_CONTEXT_RE = re.compile(
    r'(?:return|profit|loss|spread|sharpe|volatil|variance|drawdown|revenue|'
    r'assets?|notional|premium|cost|price|market share|sample|observations?|'
    r'r\s*[²2]|rmse|t-stat|beta|liquidity|carry|capacity|exposure|value|'
    r'outperform|underperform)',
    re.IGNORECASE,
)

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}
DATE_RE = re.compile(
    r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,\s*|\s+)(20\d{2})\b',
    re.IGNORECASE,
)
EVENT_RE = re.compile(
    r'(?:deadline|vote|consultation|decision|expir|matur|closing|hearing|'
    r'amendment|feedback|release|report|meeting|review|earnings|tender|'
    r'rebalance|implementation|effective)',
    re.IGNORECASE,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _normalized_text(text: str) -> str:
    return ' '.join(str(text or '').split()).casefold()


def is_boilerplate_text(text: str) -> bool:
    """Return True for promotion, bylines, update notices, and link records.

    The dossier is intentionally allowed to omit material.  A false negative
    here can promote a call-to-action into research; a false positive merely
    leaves the reader with the source title and original-article link.
    """
    clean = ' '.join(str(text or '').split()).strip()
    if not clean:
        return False
    if PROMOTIONAL_RE.search(clean) or BYLINE_RE.fullmatch(clean):
        return True
    if UPDATE_NOTICE_RE.search(clean):
        return True
    if URL_OR_CITATION_RE.search(clean):
        return True
    return False


def _blocks(body: str) -> list[dict]:
    """Return non-empty body lines with exact, whitespace-trimmed offsets."""
    blocks = []
    for match in re.finditer(r'[^\n]+', body):
        raw = match.group(0)
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right <= left:
            continue
        start = match.start() + left
        end = match.start() + right
        blocks.append({'text': body[start:end], 'start': start, 'end': end})
    return blocks


def classify_heading(text: str) -> str | None:
    """Classify only high-signal authored headings; abstain aggressively."""
    clean = ' '.join(str(text or '').split()).strip()
    if not clean or len(clean) > 140 or len(clean.split()) > 22:
        return None
    if clean.endswith(('.', ';')) or re.search(r'[.!?][\"\'’”)]*\s+\S', clean):
        return None
    if is_boilerplate_text(clean) or REFERENCE_HEADING_RE.fullmatch(clean):
        return None
    for kind, pattern in HEADING_PATTERNS:
        if pattern.fullmatch(clean):
            return kind
    return None


def _is_checkpoint_heading(text: str) -> bool:
    clean = ' '.join(str(text or '').split()).strip()
    return bool(
        clean and len(clean) <= 140 and len(clean.split()) <= 22
        and not is_boilerplate_text(clean)
        and not REFERENCE_HEADING_RE.fullmatch(clean)
        and not re.search(r'[.!?][\"\'’”)]*\s+\S', clean)
        and CHECKPOINT_HEADING_RE.fullmatch(clean)
    )


def _looks_like_prose(text: str) -> bool:
    clean = text.strip()
    if len(clean) < 80 or is_boilerplate_text(clean):
        return False
    if REFERENCE_HEADING_RE.fullmatch(clean):
        return False
    # Reject code/table fragments while retaining financial prose with symbols.
    alpha = sum(character.isalpha() for character in clean)
    spaces = clean.count(' ')
    return alpha >= 45 and spaces >= 10


def _bounded_span(body: str, start: int, end: int, limit: int) -> dict:
    """Return an exact bounded source span, preferring a complete sentence."""
    original_end = end
    if end - start > limit:
        candidate = body[start:start + limit]
        boundaries = [match.end() for match in re.finditer(
            r'[.!?](?:[\"\'’”)]*)\s+', candidate
        ) if match.end() >= min(240, limit // 2)]
        if boundaries:
            end = start + boundaries[-1]
            while end > start and body[end - 1].isspace():
                end -= 1
        else:
            cut = candidate.rfind(' ')
            end = start + (cut if cut >= min(240, limit // 2) else limit)
    text = body[start:end]
    return {
        'text': text,
        'start': start,
        'end': end,
        'sha256': _sha256(text),
        'truncated': end < original_end,
    }


def _heading_record(block: dict) -> dict:
    return {
        'heading': block['text'],
        'heading_start': block['start'],
        'heading_end': block['end'],
        'heading_sha256': _sha256(block['text']),
    }


def _first_prose_after(blocks: list[dict], index: int) -> dict | None:
    """Return only the immediately attached paragraph; never cross a boundary."""
    if index + 1 >= len(blocks):
        return None
    candidate = blocks[index + 1]
    if (classify_heading(candidate['text'])
            or REFERENCE_HEADING_RE.fullmatch(candidate['text'].strip())
            or is_boilerplate_text(candidate['text'])
            or not _looks_like_prose(candidate['text'])):
        return None
    return candidate


def _first_lead(body: str, blocks: list[dict], title: str = '') -> dict | None:
    title_key = _normalized_text(title)
    for block in blocks[:12]:
        if title_key and _normalized_text(block['text']) == title_key:
            continue
        if classify_heading(block['text']) or not _looks_like_prose(block['text']):
            continue
        return _bounded_span(body, block['start'], block['end'], MAX_LEAD_CHARS)
    return None


def _fallback_evidence(body: str, blocks: list[dict], occupied: list[tuple[int, int]]):
    for block in blocks:
        text = block['text']
        if REFERENCE_HEADING_RE.fullmatch(text.strip()):
            break
        if not _looks_like_prose(text):
            continue
        if not NUMBER_RE.search(text) or not EVIDENCE_CONTEXT_RE.search(text):
            continue
        if any(block['start'] < end and block['end'] > start for start, end in occupied):
            continue
        return _bounded_span(body, block['start'], block['end'], MAX_EVIDENCE_CHARS)
    return None


def _sentence_span(body: str, block: dict, match_start: int) -> tuple[int, int]:
    """Bound a checkpoint to its containing exact sentence/paragraph."""
    local = match_start - block['start']
    text = block['text']
    before = max(text.rfind('. ', 0, local), text.rfind('! ', 0, local),
                 text.rfind('? ', 0, local))
    start = block['start'] + (before + 2 if before >= 0 else 0)
    endings = [position for position in (
        text.find('. ', local), text.find('! ', local), text.find('? ', local)
    ) if position >= 0]
    end = block['start'] + (min(endings) + 1 if endings else len(text))
    return start, end


def _checkpoints(body: str, blocks: list[dict], article_date: str) -> list[dict]:
    try:
        published = date.fromisoformat(str(article_date or '')[:10])
    except ValueError:
        published = date.min
    candidates = []
    seen = set()
    for index, block in enumerate(blocks):
        if REFERENCE_HEADING_RE.fullmatch(block['text'].strip()):
            break
        if not _is_checkpoint_heading(block['text']):
            continue
        prose = _first_prose_after(blocks, index)
        if prose is None:
            continue
        for match in DATE_RE.finditer(prose['text']):
            month, day, year = match.groups()
            try:
                normalized = date(int(year), MONTHS[month.casefold()], int(day))
            except ValueError:
                continue
            if normalized <= published:
                continue
            start, end = _sentence_span(body, prose, prose['start'] + match.start())
            sentence = body[start:end]
            if (not EVENT_RE.search(sentence) or UPDATE_NOTICE_RE.search(sentence)
                    or is_boilerplate_text(sentence)):
                continue
            key = (normalized.isoformat(), sentence)
            if key in seen:
                continue
            seen.add(key)
            span = _bounded_span(body, start, end, 520)
            span.update({
                'date': normalized.isoformat(),
                'date_label': match.group(0),
                'source_order': index,
                'context_kind': 'implementation',
            })
            candidates.append(span)

    # One contextual sentence per date.  Prefer an explicit watch/action
    # section over a falsifier section, then present dates chronologically.
    candidates.sort(key=lambda item: (
        item['date'], 0 if item['context_kind'] == 'implementation' else 1,
        item['source_order'],
    ))
    results = []
    used_dates = set()
    for item in candidates:
        if item['date'] in used_dates:
            continue
        used_dates.add(item['date'])
        results.append(item)
        if len(results) >= MAX_CHECKPOINTS:
            break
    return results


def build_article_brief(post: dict) -> dict:
    """Return a compact evidence dossier whose text is fully source traceable."""
    body = str(post.get('body_text') or '')
    blocks = _blocks(body)
    title = str(post.get('title') or post.get('post_title') or '')
    title_key = _normalized_text(title)
    lead = _first_lead(body, blocks, title)
    sections = []
    seen_kinds = set()

    for index, block in enumerate(blocks):
        if REFERENCE_HEADING_RE.fullmatch(block['text'].strip()):
            break
        if title_key and _normalized_text(block['text']) == title_key:
            continue
        kind = classify_heading(block['text'])
        if not kind or kind in seen_kinds:
            continue
        prose = _first_prose_after(blocks, index)
        if prose is None:
            continue
        section = {'kind': kind, 'source_order': index, **_heading_record(block)}
        section.update(_bounded_span(
            body, prose['start'], prose['end'], MAX_SECTION_CHARS
        ))
        sections.append(section)
        seen_kinds.add(kind)
        if len(sections) >= MAX_SECTIONS:
            break

    # Preserve author order in the dossier.  The UI can group by kind without
    # pretending that one passage is "stronger" than another.
    sections.sort(key=lambda item: item['source_order'])
    occupied = [(section['start'], section['end']) for section in sections]
    if lead:
        occupied.append((lead['start'], lead['end']))
    fallback_evidence = None
    if not any(section['kind'] == 'evidence' for section in sections):
        fallback_evidence = _fallback_evidence(body, blocks, occupied)

    return {
        'schema_version': SCHEMA_VERSION,
        'body_sha256': _sha256(body),
        'lead': lead,
        'sections': sections,
        'fallback_evidence': fallback_evidence,
        'checkpoints': _checkpoints(body, blocks, post.get('post_date', '')),
    }


def validate_brief_against_body(brief: dict, body: str) -> None:
    """Raise ValueError unless every stored brief fragment is an exact span."""
    validate_brief_structure(brief)
    if not isinstance(brief, dict) or brief.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('article brief has an unsupported schema version')
    if brief.get('body_sha256') != _sha256(body):
        raise ValueError('article brief body hash does not match its source')

    spans = []
    if brief.get('lead') is not None:
        spans.append(('lead', brief['lead']))
    if brief.get('fallback_evidence') is not None:
        spans.append(('fallback evidence', brief['fallback_evidence']))
    for index, section in enumerate(brief.get('sections') or []):
        spans.append((f'section {index}', section))
        heading_start = section.get('heading_start')
        heading_end = section.get('heading_end')
        heading = section.get('heading')
        if (not isinstance(heading_start, int) or not isinstance(heading_end, int)
                or body[heading_start:heading_end] != heading
                or section.get('heading_sha256') != _sha256(str(heading or ''))):
            raise ValueError(f'article brief section {index} heading is not an exact span')
    for index, checkpoint in enumerate(brief.get('checkpoints') or []):
        spans.append((f'checkpoint {index}', checkpoint))

    for label, span in spans:
        if not isinstance(span, dict):
            raise ValueError(f'article brief {label} is not an object')
        start, end, text = span.get('start'), span.get('end'), span.get('text')
        if (not isinstance(start, int) or not isinstance(end, int)
                or start < 0 or end <= start or body[start:end] != text):
            raise ValueError(f'article brief {label} is not an exact source span')
        if span.get('sha256') != _sha256(text):
            raise ValueError(f'article brief {label} hash is invalid')


def validate_brief_structure(brief: dict) -> None:
    """Validate the deployable brief schema without requiring the body cache."""
    if not isinstance(brief, dict) or brief.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('article brief has an unsupported schema version')
    body_hash = brief.get('body_sha256')
    if not isinstance(body_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', body_hash):
        raise ValueError('article brief has an invalid body hash')

    def validate_span(label: str, span: dict) -> None:
        if not isinstance(span, dict):
            raise ValueError(f'article brief {label} is not an object')
        start, end, text = span.get('start'), span.get('end'), span.get('text')
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or start < 0 or end <= start or not isinstance(text, str)
                or not text or end - start != len(text)):
            raise ValueError(f'article brief {label} has invalid span metadata')
        if span.get('sha256') != _sha256(text):
            raise ValueError(f'article brief {label} hash is invalid')
        if type(span.get('truncated')) is not bool:
            raise ValueError(f'article brief {label} has no truncation state')
        if is_boilerplate_text(text):
            raise ValueError(f'article brief {label} contains boilerplate')

    for field in ('lead', 'fallback_evidence'):
        value = brief.get(field)
        if value is not None:
            validate_span(field.replace('_', ' '), value)

    sections = brief.get('sections')
    if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
        raise ValueError('article brief has an invalid section list')
    kinds = []
    orders = []
    for index, section in enumerate(sections):
        validate_span(f'section {index}', section)
        kind = section.get('kind')
        if kind not in VALID_SECTION_KINDS:
            raise ValueError(f'article brief section {index} has an invalid kind')
        kinds.append(kind)
        source_order = section.get('source_order')
        if not isinstance(source_order, int) or isinstance(source_order, bool):
            raise ValueError(f'article brief section {index} has no source order')
        orders.append(source_order)
        heading = section.get('heading')
        heading_start = section.get('heading_start')
        heading_end = section.get('heading_end')
        if (not isinstance(heading, str) or not heading
                or not isinstance(heading_start, int) or isinstance(heading_start, bool)
                or not isinstance(heading_end, int) or isinstance(heading_end, bool)
                or heading_end - heading_start != len(heading)
                or section.get('heading_sha256') != _sha256(heading)):
            raise ValueError(f'article brief section {index} heading metadata is invalid')
    if len(kinds) != len(set(kinds)) or orders != sorted(orders):
        raise ValueError('article brief sections are duplicated or out of source order')
    for index, section in enumerate(sections):
        if classify_heading(section['heading']) != section['kind']:
            raise ValueError(f'article brief section {index} heading is not eligible')

    checkpoints = brief.get('checkpoints')
    if not isinstance(checkpoints, list) or len(checkpoints) > MAX_CHECKPOINTS:
        raise ValueError('article brief has an invalid checkpoint list')
    dates = []
    for index, checkpoint in enumerate(checkpoints):
        validate_span(f'checkpoint {index}', checkpoint)
        checkpoint_date = checkpoint.get('date')
        try:
            date.fromisoformat(checkpoint_date)
        except (TypeError, ValueError):
            raise ValueError(f'article brief checkpoint {index} has an invalid date') from None
        if not isinstance(checkpoint.get('date_label'), str):
            raise ValueError(f'article brief checkpoint {index} has no date label')
        if checkpoint.get('context_kind') not in {'implementation', 'falsifier'}:
            raise ValueError(f'article brief checkpoint {index} has invalid context')
        dates.append(checkpoint_date)
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError('article brief checkpoints are duplicated or out of date order')
