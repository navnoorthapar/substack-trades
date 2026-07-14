import gzip
import hashlib
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
        environment['SITE_REVISION'] = 'test-revision'
        subprocess.run(
            [sys.executable, str(ROOT / 'build_site.py')],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        cls.html_path = cls.site_dir / 'index.html'
        cls.html_bytes = cls.html_path.read_bytes()
        cls.html = cls.html_bytes.decode('utf-8')
        article_payload = json.loads((ROOT / 'articles_index.json').read_text(encoding='utf-8'))
        cls.source_articles = (
            article_payload.get('articles', [])
            if isinstance(article_payload, dict)
            else article_payload
        )
        cls.source_ideas = json.loads((ROOT / 'trades_extracted.json').read_text(encoding='utf-8'))
        article_match = re.search(r'const ARTICLES = (.*?);\n', cls.html)
        idea_match = re.search(r'const IDEAS = (.*?);\n', cls.html)
        snapshot_match = re.search(r'const SNAPSHOT = (.*?);\n', cls.html)
        if not article_match or not idea_match or not snapshot_match:
            raise AssertionError('generated client payload is missing')
        cls.articles = json.loads(article_match.group(1))
        cls.ideas = json.loads(idea_match.group(1))
        cls.snapshot = json.loads(snapshot_match.group(1))

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

    def test_documentation_coverage_matches_the_five_source_fields_exactly(self):
        field_names = {'market', 'stance', 'underlying', 'thesis', 'numeric'}
        actual_distribution = Counter()
        actual_field_counts = Counter()
        for idea in self.ideas:
            fields = idea['documentation_fields']
            self.assertEqual(set(fields), field_names)
            self.assertTrue(all(type(value) is bool for value in fields.values()))
            self.assertEqual(idea['documentation_score'], sum(fields.values()))
            self.assertTrue(0 <= idea['documentation_score'] <= 5)
            self.assertTrue(
                all(type(idea[name]) is bool for name in (
                    'reference_line', 'negation_risk', 'description_truncated'
                ))
            )
            actual_distribution[idea['documentation_score']] += 1
            actual_field_counts.update(name for name, present in fields.items() if present)

        expected_distribution = Counter()
        expected_field_counts = Counter()
        for source in self.source_ideas:
            instruments = source.get('instruments') or ['unspecified']
            fields = {
                'market': any(value and value != 'unspecified' for value in instruments),
                'stance': bool(source.get('direction') and source['direction'] != 'unspecified'),
                'underlying': bool(str(source.get('underlying') or '').strip()),
                'thesis': bool(str(source.get('edge_or_thesis') or '').strip()),
                'numeric': bool(str(source.get('any_quant_detail') or '').strip()),
            }
            expected_distribution[sum(fields.values())] += 1
            expected_field_counts.update(name for name, present in fields.items() if present)

        self.assertEqual(actual_distribution, expected_distribution)
        self.assertEqual(actual_field_counts, expected_field_counts)

    def test_manager_aliases_are_canonical_but_raw_mentions_are_preserved(self):
        source_mentions = Counter(
            ' '.join(str(idea.get('fund_name_if_mentioned') or '').split())
            for idea in self.source_ideas
        )
        embedded_mentions = Counter(idea['manager_raw'] for idea in self.ideas)
        self.assertEqual(embedded_mentions, source_mentions)

        canonical_keys = {idea['manager_key'] for idea in self.ideas if idea['manager_key']}
        raw_keys = {mention.casefold() for mention in source_mentions if mention}
        self.assertLess(len(canonical_keys), len(raw_keys), 'known aliases should be consolidated')
        self.assertEqual(self.html.count('data-filter="manager"'), len(canonical_keys))

        for idea in self.ideas:
            if idea['manager']:
                self.assertEqual(idea['manager_key'], ' '.join(idea['manager'].split()).casefold())
            else:
                self.assertFalse(idea['manager_key'])

        expected_aliases = {
            'citadel': 'Citadel / Ken Griffin',
            'griffin / citadel': 'Citadel / Ken Griffin',
            'bridgewater': 'Bridgewater / Ray Dalio',
            'dalio / bridgewater': 'Bridgewater / Ray Dalio',
            'ackman': 'Pershing Square / Bill Ackman',
            'duquesne': 'Duquesne / Stanley Druckenmiller',
        }
        observed = {}
        for idea in self.ideas:
            key = idea['manager_raw'].casefold()
            if key in expected_aliases:
                observed.setdefault(key, set()).add(idea['manager'])
        for raw, canonical in expected_aliases.items():
            if raw in raw_keys:
                self.assertEqual(observed.get(raw), {canonical})
        self.assertIn('Original entity mention', self.html)

    def test_research_brief_is_default_and_states_the_decision_boundary(self):
        self.assertRegex(self.html, r'<body[^>]*data-view="briefing"')
        self.assertRegex(self.html, r"const state\s*=\s*\{\s*view:['\"]briefing['\"]")
        self.assertIn('function renderBriefing(records)', self.html)
        for text in (
            'Owner research brief',
            'Recent high-context passages',
            'Documentation coverage',
            'Publication health',
            'One author, two publication channels',
            'not independent corroborating sources',
            'not verified positions',
            'not confidence or quality',
            'Live price and valuation',
            'liquidity and capacity',
            'portfolio fit',
        ):
            self.assertIn(text, self.html)

        briefing_start = self.html.index('function renderBriefing(records)')
        briefing_end = self.html.index('\nfunction contextualRecords', briefing_start)
        briefing = self.html[briefing_start:briefing_end]
        self.assertIn('idea.documentation_score >= 4', briefing)
        self.assertIn('!reviewFlagged(idea)', briefing)
        self.assertIn("idea._article.content_status === 'full'", briefing)
        self.assertIn('data-brief-record', briefing)

    def test_decision_queue_is_schema_bounded_local_and_portable(self):
        for text in (
            "const WORKFLOW_KEY = 'nrt-decision-queue-v1'",
            "new Set(['review','monitor','archived'])",
            "localStorage.getItem('nrt-saved-ideas')",
            'data-workflow-status',
            'data-workflow-tags',
            'data-workflow-note',
            'function backupQueue()',
            'function restoreQueueFile(file)',
            'data_checksum:String(SNAPSHOT.data_checksum',
            'Queue backup could not be validated',
            'Queue could not be saved in this browser',
            'Copy failed—select and copy manually',
            'Stored only in this browser unless backed up',
            'Do not enter confidential',
        ):
            self.assertIn(text, self.html)
        self.assertRegex(self.html, r'id="queue-restore-input"[^>]*accept="application/json,\.json"')
        self.assertRegex(self.html, r'payload\.schema_version\s*!==\s*1')
        self.assertRegex(self.html, r'payload\.items\.slice\(\s*0\s*,\s*2000\s*\)')
        self.assertRegex(self.html, r'note:String\([^)]*\)\.slice\(0,4000\)')
        self.assertRegex(self.html, r'tags:String\([^)]*\)\.slice\(0,500\)')

    def test_institutional_views_and_workflows_are_present(self):
        for text in (
            'Research Brief',
            'Observation Monitor',
            'Research Library',
            'Decision Queue',
            'Research evidence',
            'Export CSV',
            'Copy view',
            'Source passage',
            'Parsed stance',
            'Mentioned entity',
            'Numeric context',
            'Reported outcome',
        ):
            self.assertIn(text, self.html)
        expected_manager_keys = {
            idea['manager_key'] for idea in self.ideas if idea['manager_key']
        }
        self.assertEqual(self.html.count('data-filter="manager"'), len(expected_manager_keys))
        self.assertRegex(self.html, r'records\.slice\(\s*0\s*,\s*state\.limit\s*\)')

    def test_saved_idea_identity_does_not_depend_on_extraction_order(self):
        builder = (ROOT / 'build_site.py').read_text(encoding='utf-8')
        identity_block = re.search(
            r"idea_id\s*=\s*stable_id\((.*?)\n\s*\)",
            builder,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(identity_block)
        self.assertIn('normalize_identity_text(identity_description)', identity_block.group(1))
        self.assertNotIn('index', identity_block.group(1))
        self.assertRegex(
            builder,
            r"description\.endswith\(['\"]…['\"]\)",
            'truncation punctuation must not change durable queue identity',
        )
        self.assertIn("len(idea_ids) != len(set(idea_ids))", builder)

    def test_accessibility_structure_and_focus_behavior(self):
        self.assertIn('class="skip-link"', self.html)
        self.assertIn('<h1 class="sr-only">', self.html)
        self.assertIn('role="grid"', self.html)
        self.assertIn('aria-multiselectable="false"', self.html)
        self.assertIn('role="gridcell"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('prefers-reduced-motion', self.html)
        self.assertNotIn('autofocus', self.html)

    def test_grid_links_search_filters_and_drawers_have_complete_keyboard_semantics(self):
        self.assertGreaterEqual(self.html.count('role="row" data-record-id='), 2)
        self.assertGreaterEqual(self.html.count('aria-keyshortcuts="Enter Space ArrowUp ArrowDown Home End'), 2)
        row_links = re.findall(r'<a\b[^>]*class="row-open"[^>]*>', self.html)
        self.assertGreaterEqual(len(row_links), 2)
        for link in row_links:
            self.assertIn('tabindex="-1"', link)
            self.assertIn('target="_blank"', link)
            self.assertIn('rel="noopener noreferrer"', link)

        search_start = self.html.find("document.getElementById('search').addEventListener('keydown'")
        search_end = self.html.find("document.getElementById('manager-search')", search_start)
        self.assertGreaterEqual(search_start, 0, 'search Enter handler is missing')
        self.assertGreater(search_end, search_start)
        search_handler = self.html[search_start:search_end]
        self.assertRegex(search_handler, r"event\.key\s*!==\s*['\"]Enter['\"]")
        self.assertIn('render();', search_handler)
        self.assertIn('focusSelectedRow();', search_handler)

        self.assertIn("button.setAttribute('aria-label','Remove filter: '", self.html)
        self.assertIn("mark.setAttribute('aria-hidden','true')", self.html)
        self.assertRegex(self.html, r'data-empty-action="clear"')
        self.assertRegex(self.html, r'data-empty-action="browse"')
        self.assertRegex(self.html, r"setAttribute\(['\"]role['\"],['\"]dialog['\"]\)")
        self.assertRegex(self.html, r"setAttribute\(['\"]aria-modal['\"],['\"]true['\"]\)")

        mobile_start = self.html.index('@media(max-width:760px){')
        mobile_end = self.html.index('@media(max-width:430px){', mobile_start)
        mobile = self.html[mobile_start:mobile_end]
        self.assertRegex(mobile, r'#search\{[^}]*font-size:16px')
        for selector in (
            r'#search',
            r'\.utility-button',
            r'\.facet-option,\.facet-clear,\.date-option,\.manager-search,\.preset-button',
            r'\.view-tab',
            r'\.select-control,\.command-button',
            r'\.filter-chip,\.primary-action,\.secondary-action,\.inspector-close,\.load-more',
        ):
            match = re.search(selector + r'\{[^}]*(?:min-)?height:(\d+)px', mobile)
            self.assertIsNotNone(match, f'mobile target size missing for {selector}')
            self.assertGreaterEqual(int(match.group(1)), 44, f'mobile target too small for {selector}')

    def test_literal_dom_id_references_resolve_and_ids_are_unique(self):
        ids = re.findall(r'\bid=["\']([^"\']+)["\']', self.html)
        self.assertEqual(len(ids), len(set(ids)), 'generated HTML contains duplicate IDs')
        references = set(re.findall(
            r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", self.html
        ))
        missing = references.difference(ids)
        self.assertFalse(missing, f'JavaScript references missing literal IDs: {sorted(missing)}')

    def test_snapshot_manifest_security_policy_and_freshness_are_embedded(self):
        manifest = json.loads((ROOT / 'snapshot_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(self.snapshot, manifest)
        self.assertEqual(self.snapshot['article_count'], len(self.articles))
        self.assertEqual(self.snapshot['observation_count'], len(self.ideas))
        self.assertEqual(set(self.snapshot['sources']), {'substack', 'medium'})

        checksum = hashlib.sha256()
        checksum.update((ROOT / 'articles_index.json').read_bytes())
        checksum.update(b'\0')
        checksum.update((ROOT / 'trades_extracted.json').read_bytes())
        self.assertEqual(self.snapshot['data_checksum'], checksum.hexdigest())

        def meta_content(name):
            match = re.search(
                rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]*)">', self.html
            )
            self.assertIsNotNone(match, f'{name} metadata is missing')
            return match.group(1)

        self.assertEqual(meta_content('nrt-revision'), 'test-revision')
        self.assertEqual(meta_content('nrt-article-count'), str(len(self.articles)))
        self.assertEqual(meta_content('nrt-observation-count'), str(len(self.ideas)))
        self.assertEqual(meta_content('nrt-data-checksum'), self.snapshot['data_checksum'])
        self.assertIn('<meta name="referrer" content="no-referrer">', self.html)
        csp_match = re.search(
            r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', self.html
        )
        self.assertIsNotNone(csp_match)
        csp = csp_match.group(1)
        for directive in (
            "default-src 'none'", "connect-src 'none'", "object-src 'none'",
            "base-uri 'none'", "form-action 'none'", "frame-src 'none'",
        ):
            self.assertIn(directive, csp)

        freshness_start = self.html.index('function renderStaticStats()')
        freshness = self.html[freshness_start:]
        self.assertIn('SNAPSHOT.checked_at', freshness)
        self.assertIn('SNAPSHOT.latest_publication', freshness)
        self.assertIn('SNAPSHOT.sources', freshness)
        self.assertRegex(freshness, r"source\.status\s*===\s*['\"]ok['\"]")
        for freshness_class in ('fresh', 'degraded', 'stale'):
            self.assertIn(freshness_class, freshness)
        self.assertIn('9 AM, 1 PM, and 10 PM Asia/Kolkata', self.html)

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
        render_start = self.html.index('function renderRows(records)')
        render_end = self.html.index('\nfunction renderContext(records)', render_start)
        render_rows = self.html[render_start:render_end]
        self.assertRegex(render_rows, r'\brecords\.slice\(\s*0\s*,\s*state\.limit\s*\)')
        self.assertRegex(
            render_rows,
            r'visible\s*=\s*\[selectedRecord\]\.concat\('
            r'visible\.slice\(\s*0\s*,\s*Math\.max\(\s*0\s*,\s*state\.limit\s*-\s*1\s*\)\s*\)\s*\)',
            'a deep selection should replace one visible row, not expand the page',
        )
        self.assertIn("row.classList.add('pinned-selection')", render_rows)
        self.assertNotRegex(
            render_rows,
            r'(?:selectedIndex.{0,320}state\.limit|state\.limit.{0,320}selectedIndex)',
            'a deep selection must not expand the render limit to the full result set',
        )
        self.assertNotRegex(self.html, r'state\.limit\s*=\s*Math\.ceil\s*\(')

    def test_static_artifact_stays_inside_the_institutional_performance_budget(self):
        self.assertLess(
            len(self.html_bytes),
            1_800_000,
            'single-file artifact exceeded the reviewed 1.8 MB transfer budget',
        )
        self.assertLess(
            len(gzip.compress(self.html_bytes, compresslevel=9)),
            450_000,
            'compressed first load exceeded the reviewed 450 KB budget',
        )

    def test_direction_mix_legend_names_all_supported_states(self):
        legend_match = re.search(
            r'document\.getElementById\([\'\"]mix-legend[\'\"]\)\.textContent\s*=(.*?);',
            self.html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(legend_match, 'direction mix legend is missing')
        legend = legend_match.group(1)
        self.assertIn('L/S', legend)
        self.assertIn('No reliable stance', legend)
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
        self.assertRegex(self.html, r'\.status-dot\{[^}]*background:var\(--text-muted\)')
        self.assertRegex(self.html, r'\.status-dot\.fresh\{[^}]*background:var\(--positive\)')
        self.assertRegex(self.html, r'\.status-dot\.degraded\{[^}]*background:var\(--relative\)')
        self.assertRegex(self.html, r'\.status-dot\.stale\{[^}]*background:var\(--negative\)')
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
