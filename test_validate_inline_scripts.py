import tempfile
import unittest
from pathlib import Path

from validate_inline_scripts import extract_inline_scripts, validate_inline_scripts


class InlineScriptValidationTests(unittest.TestCase):
    def validate(self, html):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.html'
            path.write_text(html, encoding='utf-8')
            return validate_inline_scripts(path)

    def test_valid_classic_and_module_scripts_pass(self):
        self.assertEqual(
            self.validate(
                '<script>const value = 1;</script>'
                '<script type="module">export const result = 2;</script>'
            ),
            2,
        )

    def test_one_character_syntax_failure_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'failed JavaScript syntax validation'):
            self.validate('<script>const broken = ;</script>')

    def test_unterminated_later_script_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unterminated inline script'):
            self.validate(
                '<script>const ok = 1;</script><script>const broken = ;'
            )

    def test_external_or_missing_scripts_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'external scripts'):
            self.validate('<script src="https://example.test/app.js"></script>')
        with self.assertRaisesRegex(ValueError, 'external scripts'):
            self.validate('<script src=""></script>')
        with self.assertRaisesRegex(ValueError, 'no executable inline scripts'):
            self.validate('<main>No script</main>')

    def test_structural_parser_handles_whitespace_in_closing_tag(self):
        scripts = extract_inline_scripts(
            '<script type="application/ld+json">{"safe":true}</script >'
            '<script>const exact = "</not-a-script>";</script >'
        )
        self.assertEqual(scripts[0], ('application/ld+json', '{"safe":true}'))
        self.assertEqual(scripts[1], ('', 'const exact = "</not-a-script>";'))


if __name__ == '__main__':
    unittest.main()
