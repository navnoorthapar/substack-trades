import base64
import binascii
import hashlib
import json
import re
import struct
import tempfile
import unittest
import zlib
from html.parser import HTMLParser
from pathlib import Path

import share_cards


class _StubMarkup(HTMLParser):
    """Collect the element nodes and inline handlers a rendered stub produces."""

    def __init__(self):
        super().__init__()
        self.tags = []
        self.handlers = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.handlers.extend(
            (tag, name) for name, _value in attrs if name.startswith('on')
        )


def parse_png(payload):
    if not payload.startswith(b'\x89PNG\r\n\x1a\n'):
        raise AssertionError('missing PNG signature')
    position = 8
    chunks = []
    while position < len(payload):
        length = struct.unpack('>I', payload[position:position + 4])[0]
        kind = payload[position + 4:position + 8]
        data = payload[position + 8:position + 8 + length]
        crc = struct.unpack(
            '>I', payload[position + 8 + length:position + 12 + length],
        )[0]
        if crc != binascii.crc32(kind + data) & 0xffffffff:
            raise AssertionError('invalid PNG chunk CRC')
        chunks.append((kind, data))
        position += 12 + length
    return chunks


class ShareCardTests(unittest.TestCase):
    def test_indexed_png_is_valid_deterministic_and_bounded(self):
        args = (
            'Black–Scholes Delta Is Wrong: Hull–White’s Fix Beats It',
            'substack',
            '2026-06-30T12:00:00Z',
        )
        first = share_cards.render_share_card(*args)
        second = share_cards.render_share_card(*args)
        self.assertEqual(first, second)
        self.assertLess(len(first), share_cards.MAX_CARD_BYTES)

        chunks = parse_png(first)
        header = next(data for kind, data in chunks if kind == b'IHDR')
        self.assertEqual(
            struct.unpack('>IIBBBBB', header),
            (1200, 630, 8, 3, 0, 0, 0),
        )
        compressed = b''.join(data for kind, data in chunks if kind == b'IDAT')
        scanlines = zlib.decompress(compressed)
        self.assertEqual(len(scanlines), (share_cards.WIDTH + 1) * share_cards.HEIGHT)
        self.assertTrue(all(
            scanlines[row * (share_cards.WIDTH + 1)] == 0
            for row in range(share_cards.HEIGHT)
        ))

    def test_unicode_normalization_and_long_title_layout(self):
        normalized = share_cards.normalize_card_text(
            'Color γ, ₹, “quoted” — and naïve café',
        )
        self.assertEqual(
            normalized, 'COLOR GAMMA , INR , "QUOTED" - AND NAIVE CAFE',
        )
        lines = share_cards.layout_title('word ' * 100)
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[-1].endswith('...'))
        self.assertTrue(all(len(line) <= 35 for line in lines))

    def test_stub_has_exact_escaped_open_graph_contract(self):
        article = {
            'slug': 'color-γ-t',
            'title': 'Rates & Risk: <A Test>',
            'subtitle': 'Evidence "without" invented positions.',
        }
        stub = share_cards.render_article_stub(
            article, 'a_52e608d0ef5392',
            'https://navnoorthapar.github.io/substack-trades',
        )
        self.assertIn(
            'property="og:title" content="Rates &amp; Risk: &lt;A Test&gt;"', stub,
        )
        self.assertIn('/cards/color-%CE%B3-t.png', stub)
        self.assertIn('/a/color-%CE%B3-t.html', stub)
        self.assertIn('#selected=a_52e608d0ef5392', stub)
        self.assertIn('og:image:width" content="1200', stub)
        self.assertIn("script-src 'sha256-", stub)

    def test_emit_creates_one_card_and_stub_per_unique_slug(self):
        articles = [
            {
                'id': 'a_one', 'slug': 'one', 'title': 'First article',
                'source': 'substack', 'post_date': '2026-07-20',
            },
            {
                'id': 'a_gamma', 'slug': 'color-γ-t', 'title': 'Gamma article',
                'source': 'medium', 'post_date': '2026-07-19',
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            stats = share_cards.emit_share_assets(
                articles, output, 'https://example.test/research',
            )
            self.assertEqual(stats['count'], 2)
            self.assertLessEqual(stats['max_png_bytes'], share_cards.MAX_CARD_BYTES)
            self.assertEqual(len(list((output / 'cards').glob('*.png'))), 2)
            self.assertEqual(len(list((output / 'a').glob('*.html'))), 2)
            self.assertEqual(
                (output / 'cards' / 'one.png').read_bytes(),
                share_cards.render_share_card(
                    'First article', 'substack', '2026-07-20',
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'duplicate article slug'):
                share_cards.emit_share_assets(
                    [articles[0], articles[0]], Path(directory),
                    'https://example.test/research',
                )

    def test_registry_stub_redirects_to_the_public_source_not_a_missing_dossier(self):
        article = {
            'slug': 'registry-123',
            'title': 'Metadata-only registry article',
            'content_status': 'registry',
            'url': 'https://www.example.test/publication/registry-123',
        }
        stub = share_cards.render_article_stub(
            article, 'a_registry', 'https://example.test/research',
        )
        self.assertIn(
            'location.replace("https://www.example.test/publication/registry-123")',
            stub,
        )
        self.assertIn('Open the original publication', stub)
        self.assertNotIn('#selected=', stub)

    def test_registry_source_url_cannot_terminate_the_stub_script(self):
        """A registry URL is data, never markup that closes the script element."""
        payload = 'https://www.example.test/p/x</script><img src=x onerror=alert(1)>'
        article = {
            'slug': 'registry-escape',
            'title': 'Registry article with a hostile source URL',
            'content_status': 'registry',
            'url': payload,
        }
        stub = share_cards.render_article_stub(
            article, 'a_registry', 'https://example.test/research',
        )
        script = re.search(r'<script>(.*?)</script>', stub, re.S)
        self.assertIsNotNone(script)
        body = script.group(1)
        # The element must close exactly once, at the generated boundary.
        self.assertEqual(stub.count('</script>'), 1)
        self.assertNotIn('<img', stub)
        self.assertNotIn('</script>', body)
        # The escaped literal must still redirect to the exact original URL.
        literal = re.search(r'location\.replace\((.*)\);', body, re.S).group(1)
        self.assertEqual(json.loads(literal), payload)
        # The declared CSP hash must cover the exact emitted script text.
        digest = base64.b64encode(
            hashlib.sha256(body.encode('utf-8')).digest()
        ).decode('ascii')
        self.assertIn(f"script-src 'sha256-{digest}'", stub)

    def test_no_article_string_field_can_inject_markup_into_a_stub(self):
        """Sweep hostile values through every stub-reaching string field.

        A field either fails closed during validation or renders as inert
        text. It may never become an element node, an inline handler, or an
        early close of the single generated script element.
        """
        payloads = (
            '</script><img src=x onerror=alert(1)>',
            '"><script>alert(1)</script>',
            "'; alert(1); //",
            '</title><svg onload=alert(1)>',
            '&<>"\'`',
            '</SCRIPT ><b>',
            '\\"; alert(1); //',
            '<!--<script>',
            ']]><script>alert(1)</script>',
            'javascript:alert(1)',
        )
        base = {
            'slug': 'ok-slug', 'title': 'Title', 'subtitle': 'Subtitle',
            'content_status': 'registry', 'url': 'https://example.test/p/1',
            'post_date': '2026-08-03',
        }
        # Every element the stub template itself is allowed to emit.
        permitted = {
            'html', 'head', 'meta', 'title', 'link', 'script', 'body', 'p', 'a',
        }
        rendered = 0
        for field in ('title', 'subtitle', 'url', 'slug', 'post_date',
                      'content_status'):
            for payload in payloads:
                article = dict(base)
                article[field] = (
                    f'https://example.test/p/{payload}' if field == 'url'
                    else payload
                )
                with self.subTest(field=field, payload=payload):
                    try:
                        stub = share_cards.render_article_stub(
                            article, 'a_0123456789abcd', 'https://host.test/r',
                        )
                    except ValueError:
                        continue  # fail-closed is a correct outcome
                    rendered += 1
                    markup = _StubMarkup()
                    markup.feed(stub)
                    self.assertEqual(
                        [tag for tag in markup.tags if tag not in permitted], [],
                    )
                    self.assertEqual(markup.handlers, [])
                    self.assertEqual(stub.count('<script>'), 1)
                    self.assertEqual(stub.count('</script>'), 1)
                    body = re.search(r'<script>(.*?)</script>', stub, re.S).group(1)
                    digest = base64.b64encode(
                        hashlib.sha256(body.encode('utf-8')).digest()
                    ).decode('ascii')
                    self.assertIn(f"script-src 'sha256-{digest}'", stub)
        # Guard the guard: the sweep must actually exercise the renderer.
        self.assertGreater(rendered, 40)


if __name__ == '__main__':
    unittest.main()
