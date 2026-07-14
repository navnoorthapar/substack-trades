import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).parent


class InstitutionalTerminalBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._site_temp = tempfile.TemporaryDirectory(prefix='nrt-site-test-')
        cls.site_dir = Path(cls._site_temp.name)
        environment = os.environ.copy()
        environment['SITE_OUTPUT_DIR'] = str(cls.site_dir)
        subprocess.run(
            [sys.executable, str(ROOT / 'build_site.py')],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        cls.html = (cls.site_dir / 'index.html').read_text(encoding='utf-8')
        cls.source_articles = json.loads((ROOT / 'articles_index.json').read_text(encoding='utf-8'))
        cls.source_ideas = json.loads((ROOT / 'trades_extracted.json').read_text(encoding='utf-8'))
        article_match = re.search(r'const ARTICLES = (.*?);\n', cls.html)
        idea_match = re.search(r'const IDEAS = (.*?);\n', cls.html)
        if not article_match or not idea_match:
            raise AssertionError('generated client payload is missing')
        cls.articles = json.loads(article_match.group(1))
        cls.ideas = json.loads(idea_match.group(1))

    @classmethod
    def tearDownClass(cls):
        cls._site_temp.cleanup()

    def test_complete_multi_source_dataset_is_embedded_once(self):
        self.assertEqual(len(self.articles), len(self.source_articles))
        self.assertEqual(len(self.ideas), len(self.source_ideas))
        self.assertEqual(
            Counter(article['source'] for article in self.articles),
            Counter(article['source'] for article in self.source_articles),
        )
        self.assertEqual(sum(article['trade_count'] for article in self.articles), len(self.source_ideas))

        article_ids = {article['id'] for article in self.articles}
        idea_ids = {idea['id'] for idea in self.ideas}
        self.assertEqual(len(article_ids), len(self.articles))
        self.assertEqual(len(idea_ids), len(self.ideas))
        self.assertTrue(all(idea['article_id'] in article_ids for idea in self.ideas))
        self.assertTrue(all('article_url' not in idea for idea in self.ideas))

    def test_institutional_views_and_workflows_are_present(self):
        for text in (
            'Idea Monitor',
            'Research Library',
            'Evidence inspector',
            'Export CSV',
            'Copy view',
            'Reported outcome',
            'Research-only',
        ):
            self.assertIn(text, self.html)
        expected_manager_keys = {
            ' '.join((idea.get('fund_name_if_mentioned') or '').split()).casefold()
            for idea in self.source_ideas
            if (idea.get('fund_name_if_mentioned') or '').strip()
        }
        self.assertEqual(self.html.count('data-filter="manager"'), len(expected_manager_keys))
        self.assertEqual(
            {idea['manager_key'] for idea in self.ideas if idea['manager_key']},
            expected_manager_keys,
        )
        self.assertIn('slice(0,state.limit)', self.html)

    def test_saved_idea_identity_does_not_depend_on_extraction_order(self):
        builder = (ROOT / 'build_site.py').read_text(encoding='utf-8')
        identity_block = re.search(
            r"idea_id\s*=\s*stable_id\((.*?)\n\s*\)",
            builder,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(identity_block)
        self.assertIn('normalize_identity_text(description)', identity_block.group(1))
        self.assertNotIn('index', identity_block.group(1))
        self.assertIn("len(idea_ids) != len(set(idea_ids))", builder)

    def test_accessibility_structure_and_focus_behavior(self):
        self.assertIn('class="skip-link"', self.html)
        self.assertIn('<h1 class="sr-only">', self.html)
        self.assertIn('role="table"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('prefers-reduced-motion', self.html)
        self.assertNotIn('autofocus', self.html)

    def test_outcomes_are_not_assigned_a_success_state(self):
        self.assertNotIn('trade-outcome-loss', self.html)
        self.assertNotIn('trade-outcome"', self.html)
        self.assertIn('class="reported-outcome"', self.html)

    def test_offscreen_drawers_are_removed_from_the_accessibility_tree(self):
        self.assertIn('function syncOverlayAccessibility()', self.html)
        self.assertRegex(
            self.html,
            r'(?:\.inert\s*=|toggleAttribute\(\s*[\'\"]inert[\'\"])',
        )
        self.assertRegex(
            self.html,
            r'(?:setAttribute|toggleAttribute)\(\s*[\'\"]aria-hidden[\'\"]',
        )
        self.assertGreaterEqual(
            len(re.findall(r'\bsyncOverlayAccessibility\s*\(', self.html)),
            3,
            'drawer accessibility state should be synchronized at definition, interaction, and initialization/resize',
        )
        for panel_id in ('filter-rail', 'inspector'):
            self.assertRegex(self.html, rf'id="{panel_id}"[^>]*aria-label=')

    def test_csv_export_neutralizes_spreadsheet_formulas(self):
        match = re.search(
            r'function csvCell\(value\)\s*\{(?P<body>.*?)\n\}',
            self.html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, 'CSV escaping helper is missing')
        body = match.group('body')
        guard = re.search(r'/\^(?P<guard>[^/]+)/', body)
        self.assertIsNotNone(guard, 'CSV helper must guard formula-leading characters')
        guarded_classes = [value.replace('\\', '') for value in re.findall(r'\[([^\]]+)\]', guard.group('guard'))]
        self.assertTrue(
            any(all(character in value for character in '=+-@') for value in guarded_classes),
            'CSV formula guard must include =, +, -, and @',
        )
        self.assertRegex(
            body,
            r'''(?:text|value)\s*=\s*["']'["']\s*\+''',
            'formula-like cells should be prefixed with an apostrophe before CSV quoting',
        )

    def test_incremental_rendering_stays_bounded(self):
        self.assertRegex(
            self.html,
            r'\brecords\.slice\(\s*0\s*,\s*state\.limit\s*\)',
        )
        self.assertNotRegex(
            self.html,
            r'(?:selectedIndex.{0,320}state\.limit|state\.limit.{0,320}selectedIndex)',
            'a deep selection must not expand the render limit to the full result set',
        )
        self.assertNotRegex(self.html, r'state\.limit\s*=\s*Math\.ceil\s*\(')

    def test_direction_mix_legend_names_all_supported_states(self):
        legend_match = re.search(
            r'document\.getElementById\([\'\"]mix-legend[\'\"]\)\.textContent\s*=(.*?);',
            self.html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(legend_match, 'direction mix legend is missing')
        legend = legend_match.group(1)
        self.assertIn('L/S', legend)
        self.assertIn('Not stated', legend)
        self.assertRegex(legend, r"counts\[['\"]long/short['\"]\]")
        self.assertRegex(legend, r'counts\.unspecified')

    def test_institutional_palette_is_neutral_and_readable_in_both_themes(self):
        root_match = re.search(r':root\s*\{(?P<body>.*?)\}\s*html\[data-theme="light"\]', self.html, re.DOTALL)
        light_match = re.search(r'html\[data-theme="light"\]\s*\{(?P<body>.*?)\}', self.html, re.DOTALL)
        self.assertIsNotNone(root_match)
        self.assertIsNotNone(light_match)

        def tokens(block):
            return dict(re.findall(r'--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})', block))

        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(left, right):
            first, second = luminance(left), luminance(right)
            return (max(first, second) + 0.05) / (min(first, second) + 0.05)

        dark = tokens(root_match.group('body'))
        light = tokens(light_match.group('body'))
        for palette in (dark, light):
            for foreground in ('text', 'text-secondary', 'text-muted', 'accent', 'positive', 'negative', 'relative', 'long-short', 'quant'):
                self.assertGreaterEqual(contrast(palette[foreground], palette['surface-1']), 4.5, foreground)
            self.assertGreaterEqual(contrast(palette['on-accent'], palette['accent-strong']), 4.5)
            self.assertGreaterEqual(contrast(palette['control-line'], palette['surface-1']), 3.0)
            for semantic in ('positive', 'negative', 'relative', 'long-short', 'quant'):
                self.assertGreaterEqual(contrast(palette[semantic], palette[f'{semantic}-soft']), 4.5, f'{semantic} badge')

        for surface in ('bg', 'surface-1', 'surface-2', 'surface-3'):
            channels = [int(dark[surface][index:index + 2], 16) for index in (1, 3, 5)]
            self.assertLessEqual(max(channels) - min(channels), 20, f'{surface} should remain neutral graphite')
        self.assertGreaterEqual(contrast(light['text-muted'], light['selected']), 4.5)
        self.assertIn('background:var(--accent-strong);color:var(--on-accent)', self.html)
        self.assertIn('#search:focus{border-color:var(--control-line)', self.html)
        self.assertNotEqual(dark['quant'], dark['relative'])

    def test_semantic_colors_are_scoped_to_information_states(self):
        self.assertRegex(self.html, r'\.status-dot\{[^}]*background:var\(--positive\)')
        self.assertRegex(self.html, r'\.evidence-flag\.on\{[^}]*color:var\(--quant\)')
        self.assertRegex(self.html, r'\.source-badge\{[^}]*color:var\(--text-secondary\)')
        self.assertIn('.source-substack::before{background:var(--source-substack)}', self.html)
        self.assertIn('.source-medium::before{background:var(--source-medium)}', self.html)
        for class_name, token in (
            ('dir-long', 'positive'),
            ('dir-short', 'negative'),
            ('dir-arb', 'relative'),
            ('dir-ls', 'long-short'),
        ):
            self.assertRegex(self.html, rf'\.{class_name}\{{[^}}]*color:var\(--{token}\)')

    def test_mobile_filter_drawer_has_a_wired_close_control(self):
        close_buttons = re.findall(r'<button\b[^>]*\bid="filter-close"[^>]*>', self.html)
        self.assertEqual(len(close_buttons), 1)
        self.assertIn('aria-label=', close_buttons[0])
        self.assertRegex(
            self.html,
            r'''document\.getElementById\(["']filter-close["']\)\.addEventListener\(["']click["']''',
        )

    def test_date_header_toggles_sort_order_and_reports_aria_sort(self):
        self.assertGreaterEqual(self.html.count('data-sort="newest"'), 2)
        self.assertIn("ariaSort('newest')", self.html)

        handler_start = self.html.find("document.getElementById('table-head').addEventListener")
        self.assertGreaterEqual(handler_start, 0, 'sortable table header handler is missing')
        handler_end = self.html.find('\n});', handler_start)
        self.assertGreater(handler_end, handler_start)
        handler = self.html[handler_start:handler_end]
        branch_toggle = (
            re.search(
                r'''state\.sort\s*===\s*["']newest["'].*state\.sort\s*=\s*["']oldest["']''',
                handler,
                flags=re.DOTALL,
            )
            and re.search(
                r'''state\.sort\s*===\s*["']oldest["'].*state\.sort\s*=\s*["']newest["']''',
                handler,
                flags=re.DOTALL,
            )
        )
        ternary_toggle = re.search(
            r'''state\.sort\s*===\s*["']newest["']\s*\?\s*["']oldest["']\s*:\s*["']newest["']''',
            handler,
        )
        self.assertRegex(handler, r'''button\.dataset\.sort\s*===\s*["']newest["']''')
        self.assertTrue(branch_toggle or ternary_toggle, 'date header must toggle newest and oldest')

        aria_start = self.html.find('function ariaSort(key)')
        aria_end = self.html.find('function renderTableHead()', aria_start)
        self.assertGreaterEqual(aria_start, 0, 'ariaSort helper is missing')
        self.assertGreater(aria_end, aria_start)
        aria_helper = self.html[aria_start:aria_end]
        self.assertRegex(
            aria_helper,
            r'''key\s*===\s*["']newest["']\s*&&\s*state\.sort\s*===\s*["']oldest["'].*return\s*["']ascending["']''',
        )


if __name__ == '__main__':
    unittest.main()
