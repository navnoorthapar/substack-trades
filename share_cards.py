#!/usr/bin/env python3
"""Render deterministic 1200x630 indexed-PNG article share cards.

The renderer is intentionally standard-library only so the scheduled macOS
Python 3.9 job and GitHub Actions produce byte-identical assets without Pillow,
fonts, a browser, or operating-system rendering differences.
"""

import base64
import binascii
import hashlib
import html
import json
import re
import struct
import unicodedata
import zlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Sequence
from urllib.parse import quote, urlsplit


WIDTH = 1200
HEIGHT = 630
MAX_CARD_BYTES = 100_000
PALETTE = (
    (7, 15, 23),       # terminal black
    (243, 246, 248),   # primary text
    (242, 169, 59),    # institutional amber
    (118, 139, 157),   # secondary text
    (19, 34, 47),      # panel
    (61, 123, 184),    # restrained blue
)
SOURCE_LABELS = {
    'substack': 'SUBSTACK',
    'medium': 'MEDIUM',
    'patreon': 'PATREON',
    'fxempire': 'FX EMPIRE',
}


_PATTERNS = {
    'A': '01110/10001/10001/11111/10001/10001/10001',
    'B': '11110/10001/10001/11110/10001/10001/11110',
    'C': '01111/10000/10000/10000/10000/10000/01111',
    'D': '11110/10001/10001/10001/10001/10001/11110',
    'E': '11111/10000/10000/11110/10000/10000/11111',
    'F': '11111/10000/10000/11110/10000/10000/10000',
    'G': '01111/10000/10000/10111/10001/10001/01111',
    'H': '10001/10001/10001/11111/10001/10001/10001',
    'I': '11111/00100/00100/00100/00100/00100/11111',
    'J': '00111/00010/00010/00010/10010/10010/01100',
    'K': '10001/10010/10100/11000/10100/10010/10001',
    'L': '10000/10000/10000/10000/10000/10000/11111',
    'M': '10001/11011/10101/10101/10001/10001/10001',
    'N': '10001/11001/10101/10011/10001/10001/10001',
    'O': '01110/10001/10001/10001/10001/10001/01110',
    'P': '11110/10001/10001/11110/10000/10000/10000',
    'Q': '01110/10001/10001/10001/10101/10010/01101',
    'R': '11110/10001/10001/11110/10100/10010/10001',
    'S': '01111/10000/10000/01110/00001/00001/11110',
    'T': '11111/00100/00100/00100/00100/00100/00100',
    'U': '10001/10001/10001/10001/10001/10001/01110',
    'V': '10001/10001/10001/10001/10001/01010/00100',
    'W': '10001/10001/10001/10101/10101/11011/10001',
    'X': '10001/10001/01010/00100/01010/10001/10001',
    'Y': '10001/10001/01010/00100/00100/00100/00100',
    'Z': '11111/00001/00010/00100/01000/10000/11111',
    '0': '01110/10001/10011/10101/11001/10001/01110',
    '1': '00100/01100/00100/00100/00100/00100/01110',
    '2': '01110/10001/00001/00010/00100/01000/11111',
    '3': '11110/00001/00001/01110/00001/00001/11110',
    '4': '00010/00110/01010/10010/11111/00010/00010',
    '5': '11111/10000/10000/11110/00001/00001/11110',
    '6': '01110/10000/10000/11110/10001/10001/01110',
    '7': '11111/00001/00010/00100/01000/01000/01000',
    '8': '01110/10001/10001/01110/10001/10001/01110',
    '9': '01110/10001/10001/01111/00001/00001/01110',
    '.': '00000/00000/00000/00000/00000/00110/00110',
    ',': '00000/00000/00000/00000/00110/00110/00100',
    ':': '00000/00110/00110/00000/00110/00110/00000',
    ';': '00000/00110/00110/00000/00110/00110/00100',
    '-': '00000/00000/00000/11111/00000/00000/00000',
    '+': '00000/00100/00100/11111/00100/00100/00000',
    '/': '00001/00010/00010/00100/01000/01000/10000',
    '&': '01100/10010/10100/01000/10101/10010/01101',
    '$': '00100/01111/10100/01110/00101/11110/00100',
    '%': '11001/11010/00100/01000/10110/00110/00000',
    '(': '00010/00100/01000/01000/01000/00100/00010',
    ')': '01000/00100/00010/00010/00010/00100/01000',
    '!': '00100/00100/00100/00100/00100/00000/00100',
    '?': '01110/10001/00001/00010/00100/00000/00100',
    "'": '00100/00100/00000/00000/00000/00000/00000',
    '"': '01010/01010/00000/00000/00000/00000/00000',
    '#': '01010/11111/01010/01010/11111/01010/00000',
}
FONT = {
    character: tuple(int(row, 2) for row in pattern.split('/'))
    for character, pattern in _PATTERNS.items()
}
FONT[' '] = (0, 0, 0, 0, 0, 0, 0)

UNICODE_REPLACEMENTS = {
    '€': ' EUR ', '£': ' GBP ', '¥': ' JPY ', '₹': ' INR ',
    'α': ' ALPHA ', 'β': ' BETA ', 'γ': ' GAMMA ', 'δ': ' DELTA ',
    'θ': ' THETA ', 'λ': ' LAMBDA ', 'σ': ' SIGMA ',
    '×': ' X ', '→': ' TO ', '↔': ' VS ', '–': '-', '—': '-', '−': '-',
    '“': '"', '”': '"', '‘': "'", '’': "'", '…': '...',
}


def normalize_card_text(value: object) -> str:
    """Return deterministic uppercase text supported by the embedded font."""
    text = str(value or '')
    for original, replacement in UNICODE_REPLACEMENTS.items():
        text = text.replace(original, replacement)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(
        character for character in text
        if not unicodedata.combining(character)
    ).upper()
    text = ''.join(
        character if character in FONT else '?'
        for character in text
    )
    return ' '.join(text.split())


def layout_title(
        value: object, max_lines: int = 4, max_columns: int = 35,
) -> List[str]:
    """Greedily wrap title text and visibly ellipsize overflow."""
    if max_lines < 1 or max_columns < 4:
        raise ValueError('title layout bounds are too small')
    words = normalize_card_text(value).split()
    lines: List[str] = []
    while words and len(lines) < max_lines:
        word = words.pop(0)
        if len(word) > max_columns:
            words.insert(0, word[max_columns:])
            word = word[:max_columns]
        if not lines or len(lines[-1]) + 1 + len(word) > max_columns:
            lines.append(word)
        else:
            lines[-1] += ' ' + word
    if words:
        line = lines[-1]
        lines[-1] = line[:max_columns - 3].rstrip() + '...'
    return lines or ['UNTITLED RESEARCH']


def _rectangle(
        pixels: bytearray, left: int, top: int, right: int, bottom: int,
        color: int,
) -> None:
    left = max(0, left)
    top = max(0, top)
    right = min(WIDTH, right)
    bottom = min(HEIGHT, bottom)
    row = bytes([color]) * max(0, right - left)
    for y in range(top, bottom):
        start = y * WIDTH + left
        pixels[start:start + len(row)] = row


def _draw_text(
        pixels: bytearray, text: object, x: int, y: int, scale: int,
        color: int,
) -> None:
    cursor = x
    for character in normalize_card_text(text):
        glyph = FONT.get(character, FONT['?'])
        for row_index, bits in enumerate(glyph):
            for column in range(5):
                if bits & (1 << (4 - column)):
                    _rectangle(
                        pixels,
                        cursor + column * scale,
                        y + row_index * scale,
                        cursor + (column + 1) * scale,
                        y + (row_index + 1) * scale,
                        color,
                    )
        cursor += 6 * scale


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return (
        struct.pack('>I', len(data)) + body
        + struct.pack('>I', binascii.crc32(body) & 0xffffffff)
    )


def _encode_indexed_png(pixels: bytes) -> bytes:
    scanlines = b''.join(
        b'\x00' + pixels[row * WIDTH:(row + 1) * WIDTH]
        for row in range(HEIGHT)
    )
    header = struct.pack('>IIBBBBB', WIDTH, HEIGHT, 8, 3, 0, 0, 0)
    palette = b''.join(bytes(color) for color in PALETTE)
    return (
        b'\x89PNG\r\n\x1a\n'
        + _png_chunk(b'IHDR', header)
        + _png_chunk(b'PLTE', palette)
        + _png_chunk(b'IDAT', zlib.compress(scanlines, 9))
        + _png_chunk(b'IEND', b'')
    )


def _display_date(value: object) -> str:
    try:
        parsed = datetime.strptime(str(value or '')[:10], '%Y-%m-%d')
    except ValueError:
        return 'DATE UNAVAILABLE'
    return parsed.strftime('%d %b %Y').upper()


def render_share_card(title: object, source: object, post_date: object) -> bytes:
    """Render one Bloomberg-terminal-inspired, byte-stable indexed PNG."""
    pixels = bytearray([0]) * (WIDTH * HEIGHT)
    _rectangle(pixels, 48, 42, 1152, 588, 4)
    _rectangle(pixels, 48, 42, 58, 588, 2)
    _rectangle(pixels, 80, 139, 1120, 141, 5)
    _rectangle(pixels, 80, 516, 1120, 518, 5)

    _rectangle(pixels, 80, 67, 128, 115, 5)
    _draw_text(pixels, 'N/R', 86, 80, 3, 1)
    _draw_text(pixels, 'NAVNOOR RESEARCH', 152, 73, 4, 1)
    _draw_text(pixels, 'PUBLISHED RESEARCH INTELLIGENCE', 153, 108, 2, 3)

    source_key = str(source or '').strip().casefold()
    source_label = SOURCE_LABELS.get(source_key, normalize_card_text(source))
    source_label = source_label or 'RESEARCH'
    _rectangle(pixels, 80, 166, 98, 190, 2)
    _draw_text(pixels, source_label, 112, 166, 3, 2)
    _draw_text(pixels, _display_date(post_date), 880, 166, 3, 3)

    for index, line in enumerate(layout_title(title)):
        _draw_text(pixels, line, 80, 226 + index * 57, 5, 1)

    _draw_text(pixels, 'ARTICLE DOSSIER', 80, 548, 3, 3)
    _draw_text(pixels, 'NAVNOORTHAPAR.GITHUB.IO', 740, 548, 3, 2)
    png = _encode_indexed_png(bytes(pixels))
    if len(png) > MAX_CARD_BYTES:
        raise ValueError(f'share card exceeded {MAX_CARD_BYTES} bytes')
    return png


def _safe_slug(value: object) -> str:
    slug = unicodedata.normalize('NFC', str(value or ''))
    if (
            not slug or len(slug) > 180 or slug in ('.', '..')
            or '/' in slug or '\\' in slug or '\x00' in slug
            or not all(character == '-' or character.isalnum() for character in slug)
    ):
        raise ValueError(f'unsafe article slug: {slug!r}')
    return slug


def _site_root(value: object) -> str:
    site_url = str(value or '').strip().rstrip('/')
    parsed = urlsplit(site_url)
    if (
            parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
    ):
        raise ValueError('site_url must be an absolute HTTP(S) root')
    return site_url


def _description(article: Mapping[str, object]) -> str:
    value = str(article.get('subtitle') or '').strip()
    if not value:
        value = 'Published research by Navnoor Research.'
    value = ' '.join(value.split())
    if len(value) > 200:
        value = value[:197].rstrip() + '...'
    return value


def render_article_stub(
        article: Mapping[str, object], article_id: str, site_url: str,
) -> str:
    """Return an OG page linking humans to its dossier or registry source."""
    slug = _safe_slug(article.get('slug'))
    if not re.fullmatch(r'[A-Za-z0-9_-]+', article_id):
        raise ValueError('article_id must be a safe hash-route identifier')
    root = _site_root(site_url)
    encoded_slug = quote(slug, safe='-')
    canonical = f'{root}/a/{encoded_slug}.html'
    image_url = f'{root}/cards/{encoded_slug}.png'
    registry_only = article.get('content_status') == 'registry'
    if registry_only:
        route = str(article.get('url') or '')
        parsed_route = urlsplit(route)
        if parsed_route.scheme != 'https' or not parsed_route.hostname:
            raise ValueError('registry stub requires a canonical HTTPS source URL')
        action_label = 'Open the original publication'
    else:
        route = f'../#selected={quote(article_id, safe="_- ").replace(" ", "%20")}'
        action_label = 'Open this research dossier'
    script = f'location.replace({json.dumps(route, ensure_ascii=True)});'
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    def escape(value: object) -> str:
        return html.escape(str(value), quote=True)
    title = str(article.get('title') or 'Untitled research')
    description = _description(article)
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
  <meta http-equiv="refresh" content="0;url={escape(route)}">
  <title>{escape(title)} — Navnoor Research</title>
  <meta name="description" content="{escape(description)}">
  <meta name="robots" content="index,follow">
  <link rel="canonical" href="{escape(canonical)}">
  <meta property="og:type" content="article">
  <meta property="og:site_name" content="Navnoor Research">
  <meta property="og:title" content="{escape(title)}">
  <meta property="og:description" content="{escape(description)}">
  <meta property="og:url" content="{escape(canonical)}">
  <meta property="og:image" content="{escape(image_url)}">
  <meta property="og:image:alt" content="Share card for {escape(title)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="article:published_time" content="{escape(article.get('post_date') or '')}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{escape(title)}">
  <meta name="twitter:description" content="{escape(description)}">
  <meta name="twitter:image" content="{escape(image_url)}">
  <script>{script}</script>
</head>
<body><p><a href="{escape(route)}">{action_label}</a></p></body>
</html>
'''


def emit_share_assets(
        articles: Sequence[Mapping[str, object]], output_dir: Path,
        site_url: str,
) -> Dict[str, int]:
    """Atomically emit one card and one OG stub for every unique article."""
    cards_dir = output_dir / 'cards'
    stubs_dir = output_dir / 'a'
    cards_dir.mkdir(parents=True, exist_ok=True)
    stubs_dir.mkdir(parents=True, exist_ok=True)
    seen = set()
    png_bytes = 0
    stub_bytes = 0
    max_png_bytes = 0

    for index, article in enumerate(articles):
        slug = _safe_slug(article.get('slug'))
        if slug in seen:
            raise ValueError(f'duplicate article slug: {slug!r}')
        seen.add(slug)
        article_id = str(article.get('id') or f'a_{index}')
        png = render_share_card(
            article.get('title'), article.get('source'), article.get('post_date'),
        )
        stub = render_article_stub(article, article_id, site_url).encode('utf-8')
        for path, content in (
                (cards_dir / f'{slug}.png', png),
                (stubs_dir / f'{slug}.html', stub),
        ):
            temporary = path.with_name(path.name + '.tmp')
            temporary.write_bytes(content)
            temporary.replace(path)
        png_bytes += len(png)
        stub_bytes += len(stub)
        max_png_bytes = max(max_png_bytes, len(png))
    return {
        'count': len(articles),
        'png_bytes': png_bytes,
        'stub_bytes': stub_bytes,
        'max_png_bytes': max_png_bytes,
    }
