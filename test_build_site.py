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

from article_briefs import is_boilerplate_text


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
        cls.brief_path = cls.site_dir / 'article_briefs.json'
        cls.brief_bytes = cls.brief_path.read_bytes()
        cls.brief_archive = json.loads(cls.brief_bytes.decode('utf-8'))
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

    def test_embedded_passages_and_explicit_truncation_flags_match_the_source(self):
        """The UI must never silently alter a bounded evidence passage."""
        source_by_identity = {
            (
                str(source.get('article_url') or ''),
                str(source.get('trade_description') or '').strip(),
            ): source
            for source in self.source_ideas
        }
        self.assertEqual(
            len(source_by_identity),
            len(self.source_ideas),
            'source URL plus exact passage should identify every observation',
        )
        article_by_id = {article['id']: article for article in self.articles}
        for idea in self.ideas:
            article = article_by_id[idea['article_id']]
            identity = (article['url'], idea['description'])
            self.assertIn(identity, source_by_identity)
            source = source_by_identity[identity]
            self.assertEqual(idea['description'], str(source['trade_description']).strip())
            self.assertIs(type(idea['description_truncated']), bool)
            self.assertEqual(
                idea['description_truncated'],
                bool(source.get('description_truncated', False)),
                'explicit source truncation metadata must survive the build unchanged',
            )
            if idea['description_truncated']:
                self.assertLessEqual(len(idea['description']), 800)

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

    def test_article_intelligence_brief_is_default_and_source_led(self):
        self.assertRegex(self.html, r'<body[^>]*data-view="briefing"')
        self.assertRegex(self.html, r"const state\s*=\s*\{\s*view:['\"]briefing['\"]")
        self.assertIn('function renderIntelligenceBrief(records)', self.html)
        for text in (
            'Intelligence Brief',
            'Research intelligence brief · source-backed',
            'Start with the article. Test it against its evidence.',
            'Authored framing, contextual evidence, mechanism, limitations, falsifiers, and public checkpoints',
            'shown as exact source passages, never converted into a synthetic recommendation',
            "Author\\'s framing",
            'Why this article is surfaced',
            'Upcoming checkpoints cited',
            'Continue reading',
            'Recent article dossiers',
            'Open full dossier',
            'Published research, not a live market as-of or a portfolio recommendation.',
            'Evidence boundaries',
            'Raw extracted observations',
            'built from exact authored sections, not observation count',
            'Extracted passages describe mixed structures; no single article-level stance is assigned.',
            'does not infer current holdings, conviction, expected return, portfolio fit, or a live market view',
        ):
            self.assertIn(text, self.html)

        briefing_start = self.html.index('function renderIntelligenceBrief(records)')
        briefing_end = self.html.index('\nfunction contextualRecords', briefing_start)
        briefing = self.html[briefing_start:briefing_end]
        self.assertIn('ARTICLE_BY_ID.get(state.selected)', briefing)
        self.assertIn('articleClaim(selected)', briefing)
        self.assertIn("briefSection(selected,'evidence')", briefing)
        self.assertIn("section.kind === 'implementation'", briefing)
        self.assertIn('data-brief-article', briefing)
        self.assertNotIn('documentation_score', briefing)

        article_view_start = self.html.index('function isArticleView()')
        article_view_end = self.html.index('\nfunction briefSection', article_view_start)
        self.assertIn("state.view === 'briefing'", self.html[article_view_start:article_view_end])
        contextual_start = self.html.index('function contextualRecords(skip)')
        contextual_end = self.html.index('\nfunction recordArticle', contextual_start)
        self.assertIn('return ARTICLES.filter', self.html[contextual_start:contextual_end])

    def test_displayed_article_framing_rejects_boilerplate(self):
        contaminated = [
            (article['id'], article['subtitle'])
            for article in self.articles
            if article.get('subtitle') and is_boilerplate_text(article['subtitle'])
        ]
        self.assertEqual(contaminated, [])
        claim_start = self.html.index('function articleClaim(article)')
        claim_end = self.html.index('\nfunction articleEvidence', claim_start)
        claim_function = self.html[claim_start:claim_end]
        self.assertLess(
            claim_function.index('(lead && lead.text)'),
            claim_function.index('(article && article.subtitle)'),
        )

    def test_hash_hydrated_article_search_loads_the_complete_dossier_archive(self):
        startup = self.html[self.html.index('hydrateFromHash();'):]
        self.assertIn(
            'if (state.query && isArticleView()) renderArticleAwareSearch(false);',
            startup,
        )
        search_start = self.html.index('function renderArticleAwareSearch(focusResult)')
        search_end = self.html.index("document.getElementById('search').addEventListener('input'", search_start)
        search = self.html[search_start:search_end]
        self.assertIn('loadBriefArchive().then(finish)', search)
        self.assertIn('briefArchiveReady', search)

    def test_deferred_article_dossiers_are_complete_release_bound_and_lossless(self):
        self.assertTrue(self.brief_path.is_file())
        self.assertEqual(self.brief_archive['schema_version'], 1)
        self.assertEqual(
            self.brief_archive['data_checksum'],
            self.snapshot['data_checksum'],
        )
        deferred = self.brief_archive['briefs']
        self.assertIsInstance(deferred, dict)

        inline_ids = {article['id'] for article in self.articles if article['brief'] is not None}
        deferred_ids = set(deferred)
        all_ids = {article['id'] for article in self.articles}
        self.assertTrue(inline_ids)
        self.assertTrue(deferred_ids)
        self.assertFalse(inline_ids.intersection(deferred_ids))
        self.assertEqual(inline_ids.union(deferred_ids), all_ids)

        def compact_span(value):
            if not isinstance(value, dict) or not value.get('text'):
                return None
            return {
                'text': value['text'],
                'truncated': bool(value.get('truncated')),
            }

        def compact_brief(value):
            value = value if isinstance(value, dict) else {}
            return {
                'lead': compact_span(value.get('lead')),
                'sections': [
                    {
                        'kind': section.get('kind') or '',
                        'heading': section.get('heading') or '',
                        'text': section['text'],
                        'truncated': bool(section.get('truncated')),
                        'source_order': int(section.get('source_order') or 0),
                    }
                    for section in value.get('sections') or []
                    if isinstance(section, dict) and section.get('text')
                ],
                'fallback_evidence': compact_span(value.get('fallback_evidence')),
                'checkpoints': [
                    {
                        'date': checkpoint.get('date') or '',
                        'date_label': checkpoint.get('date_label') or '',
                        'text': checkpoint['text'],
                        'context_kind': checkpoint.get('context_kind') or '',
                    }
                    for checkpoint in value.get('checkpoints') or []
                    if isinstance(checkpoint, dict) and checkpoint.get('text')
                ],
            }

        source_by_url = {article['url'].rstrip('/'): article for article in self.source_articles}
        for article in self.articles:
            source = source_by_url[article['url'].rstrip('/')]
            expected = compact_brief(source.get('brief'))
            actual = article['brief'] if article['brief'] is not None else deferred[article['id']]
            self.assertEqual(actual, expected)

        for source in self.source_articles:
            brief = source.get('brief')
            self.assertIsInstance(brief, dict)
            self.assertEqual(brief.get('schema_version'), 1)
            self.assertRegex(str(brief.get('body_sha256') or ''), r'^[0-9a-f]{64}$')
            spans = [brief.get('lead'), brief.get('fallback_evidence')]
            spans.extend(brief.get('sections') or [])
            spans.extend(brief.get('checkpoints') or [])
            for span in (value for value in spans if value is not None):
                self.assertEqual(span['end'] - span['start'], len(span['text']))
                self.assertEqual(
                    span['sha256'],
                    hashlib.sha256(span['text'].encode('utf-8')).hexdigest(),
                )

        for text in (
            "'article_briefs.json?v='",
            "cache:'no-cache'",
            'response.ok',
            'payload.schema_version !== 1',
            'payload.data_checksum !== SNAPSHOT.data_checksum',
            "article.brief = payload.briefs[id]",
            'refreshArticleSearch(article)',
            'Loading the exact article dossier',
            'Checking the deferred dossier against this release.',
        ):
            self.assertIn(text, self.html)

    def test_new_since_review_requires_an_explicit_acknowledgement(self):
        initialization_start = self.html.index("let lastSeenPublication = ''")
        acknowledgement_start = self.html.index('function markReviewedThroughLatest()', initialization_start)
        initialization = self.html[initialization_start:acknowledgement_start]
        self.assertIn('localStorage.getItem(LAST_SEEN_KEY)', initialization)
        self.assertNotIn(
            'localStorage.setItem(LAST_SEEN_KEY',
            initialization,
            'loading or rendering the terminal must not silently acknowledge new research',
        )

        acknowledgement_end = self.html.index('\nfunction backupQueue()', acknowledgement_start)
        acknowledgement = self.html[acknowledgement_start:acknowledgement_end]
        self.assertIn('localStorage.setItem(LAST_SEEN_KEY,MAX_DATE)', acknowledgement)
        self.assertIn('NEW_SINCE_DATE = MAX_DATE', acknowledgement)
        self.assertIn("action.dataset.action === 'mark-reviewed'", self.html)
        self.assertIn('markReviewedThroughLatest();', self.html)

    def test_inspector_resets_only_when_the_selected_context_changes(self):
        start = self.html.index("let renderedInspectorKey = ''")
        end = self.html.index('\nfunction render()', start)
        inspector = self.html[start:end]
        self.assertIn("const inspectorKey = state.view + ':' + state.selected", inspector)
        self.assertIn('inspectorKey !== renderedInspectorKey', inspector)
        self.assertIn("document.getElementById('inspector').scrollTop = 0", inspector)
        self.assertIn('renderedInspectorKey = inspectorKey', inspector)

    def test_decision_queue_v2_is_structured_bounded_local_and_portable(self):
        for text in (
            "const WORKFLOW_KEY = 'nrt-decision-queue-v2'",
            "const LEGACY_WORKFLOW_KEY = 'nrt-decision-queue-v1'",
            "new Set(['review','diligence','monitor','archived'])",
            "new Set(['low','normal','high'])",
            "new Set(['unrated','low','medium','high'])",
            'const MAX_QUEUE_ITEMS = 250',
            "localStorage.getItem('nrt-saved-ideas')",
            'Human-entered IC decision packet',
            'data-workflow-select="status"',
            'data-workflow-select="priority"',
            'data-workflow-select="confidence"',
            'data-workflow-field="owner"',
            'data-workflow-field="review_date"',
            'data-workflow-field="next_action"',
            'data-workflow-field="thesis"',
            'data-workflow-field="contrary"',
            'data-workflow-field="catalyst"',
            'data-workflow-field="horizon"',
            'data-workflow-field="payoff"',
            'data-workflow-field="risk"',
            'data-workflow-field="implementation"',
            'data-workflow-field="portfolio"',
            'data-workflow-field="tags"',
            'data-workflow-field="note"',
            'function backupQueue()',
            'function restoreQueueFile(file)',
            'data_checksum:String(SNAPSHOT.data_checksum',
            'source_snapshot:sourceSnapshotForIdea(id)',
            'Retained source snapshots',
            'Passage snapshot unavailable',
            'new Map(workflowItems)',
            'packets merged',
            'backup source snapshot differs',
            'Queue backup could not be validated',
            'Queue could not be saved in this browser',
            'Copy failed—select and copy manually',
            'Copy decision packet',
            'Archive packet',
            'Return to review',
            'Stored only in this browser unless backed up',
            'Not an enterprise audit record',
            'Do not enter confidential',
        ):
            self.assertIn(text, self.html)
        self.assertRegex(self.html, r'id="queue-restore-input"[^>]*accept="application/json,\.json"')
        self.assertRegex(self.html, r'schema_version\s*:\s*2')
        self.assertRegex(self.html, r'!\[1,2\]\.includes\(payload\.schema_version\)')
        self.assertRegex(self.html, r'payload\.items\.slice\(\s*0\s*,\s*MAX_QUEUE_ITEMS\s*\)')
        self.assertRegex(self.html, r'item\[field\]\s*=\s*String\(value\[field\]\s*\|\|\s*[\'\"]{2}\)\.slice\(0,WORKFLOW_TEXT_LIMITS\[field\]\)')
        self.assertIn('note:4000', self.html)
        self.assertIn('tags:500', self.html)

        for gate_key, label in (
            ('source', 'Original publication reviewed'),
            ('independent', 'Independent evidence obtained'),
            ('market', 'Live market price and valuation checked'),
            ('liquidity', 'Liquidity, capacity, borrow and funding checked'),
            ('portfolio', 'Portfolio exposure, correlation and stress checked'),
            ('compliance', 'Legal and compliance constraints checked'),
        ):
            self.assertIn("['" + gate_key + "','" + label + "']", self.html)
        self.assertIn("const PACKET_CASE_FIELDS = ['thesis','contrary','catalyst','horizon','payoff','risk','implementation','portfolio']", self.html)
        self.assertIn('total:18', self.html)
        self.assertIn('completed === 18', self.html)
        self.assertIn('not approval', self.html)

        toggle_start = self.html.index('function toggleSaved(id)')
        toggle_end = self.html.index('\nfunction csvCell(value)', toggle_start)
        toggle = self.html[toggle_start:toggle_end]
        self.assertIn("previous.status === 'archived' ? 'review' : 'archived'", toggle)
        self.assertIn('Decision packet archived', toggle)
        self.assertIn('Decision packet returned to review', toggle)

        self.assertIn("document.addEventListener('focusout'", self.html)
        self.assertIn("window.addEventListener('pagehide'", self.html)

    def test_institutional_methodology_links_and_operating_boundary_are_explicit(self):
        for text in (
            'Records are research observations—not verified trades, current holdings, or recommendations.',
            'does not contain live prices, positions, P&amp;L, sizing, execution, portfolio risk, liquidity, financing, counterparties, investor records, or compliance approvals.',
            'https://www.sec.gov/newsroom/press-releases/2024-17',
            'https://www.aima.org/article/presenting-the-2025-edition.html',
            'https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a',
            'These references guide questions; they do not certify a packet.',
            'Packet coverage counts populated analyst fields and self-attested control gates.',
            'It is not a confidence score, approval, recommendation, or evidence that a control was performed.',
        ):
            self.assertIn(text, self.html)

    def test_institutional_views_and_workflows_are_present(self):
        for text in (
            'Intelligence Brief',
            'Evidence Explorer',
            'Article Library',
            'Review Queue',
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
        self.assertIn('renderArticleAwareSearch(true);', search_handler)
        article_search_start = self.html.index('function renderArticleAwareSearch(focusResult)')
        article_search_end = self.html.index("document.getElementById('search').addEventListener('input'", article_search_start)
        article_search = self.html[article_search_start:article_search_end]
        self.assertIn('render();', article_search)
        self.assertIn('focusSelectedRow();', article_search)
        self.assertIn('loadBriefArchive().then(finish)', article_search)

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
        self.assertRegex(
            mobile,
            r'\.view-tabs\{[^}]*max-width:100%[^}]*overflow-x:auto',
            'all terminal views must remain horizontally reachable on narrow screens',
        )
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
            "default-src 'none'", "connect-src 'self'", "object-src 'none'",
            "base-uri 'none'", "form-action 'none'", "frame-src 'none'",
        ):
            self.assertIn(directive, csp)
        self.assertNotIn("connect-src 'none'", csp)

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
            2_000_000,
            'single-file artifact exceeded the reviewed 2.0 MB transfer budget',
        )
        self.assertLess(
            len(gzip.compress(self.html_bytes, compresslevel=9)),
            500_000,
            'compressed first load exceeded the reviewed 500 KB budget',
        )
        self.assertGreaterEqual(
            len(self.brief_bytes),
            100_000,
            'deferred dossier payload is unexpectedly empty or incomplete',
        )
        self.assertLessEqual(
            len(self.brief_bytes),
            800_000,
            'deferred dossier payload exceeded its reviewed 800 KB budget',
        )
        artifact_files = [path for path in self.site_dir.rglob('*') if path.is_file()]
        self.assertEqual(
            {path.relative_to(self.site_dir).as_posix() for path in artifact_files},
            {'index.html', 'article_briefs.json'},
        )
        self.assertTrue(all(not path.is_symlink() for path in artifact_files))
        self.assertLess(
            sum(path.stat().st_size for path in artifact_files),
            3_000_000,
            'complete static artifact exceeded the reviewed 3.0 MB policy',
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
