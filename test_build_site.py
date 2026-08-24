import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from collections import Counter
from pathlib import Path

from article_briefs import is_boilerplate_text
from client_article_contract import (
    ARTICLE_WIRE_SCHEMA_VERSION,
    hydrate_client_article,
)
from snapshot_fixtures import materialize_source_tree
from validate_inline_scripts import extract_inline_scripts


ROOT = Path(__file__).parent
SYNTHETIC_GROWTH_BUILD_TIMEOUT_SECONDS = 240


class InstitutionalTerminalBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._site_temp = tempfile.TemporaryDirectory(prefix='nrt-site-test-')
        cls.site_dir = Path(cls._site_temp.name)
        # Build from a rebased copy of the tracked sources. Building in place
        # would make every test in this class fail once the tracked snapshot
        # passes the sixteen-hour freshness contract, which has nothing to do
        # with the build behaviour under test.
        cls._source_temp = tempfile.TemporaryDirectory(prefix='nrt-site-source-')
        cls.source_root = materialize_source_tree(ROOT, cls._source_temp.name)
        environment = os.environ.copy()
        environment['SITE_OUTPUT_DIR'] = str(cls.site_dir)
        environment['SITE_REVISION'] = 'test-revision'
        subprocess.run(
            [sys.executable, str(cls.source_root / 'build_site.py')],
            cwd=cls.source_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        cls.html_path = cls.site_dir / 'index.html'
        cls.html_bytes = cls.html_path.read_bytes()
        cls.html = cls.html_bytes.decode('utf-8')
        cls.article_catalog_path = cls.site_dir / 'article_catalog.json'
        cls.article_catalog_bytes = cls.article_catalog_path.read_bytes()
        cls.article_catalog = json.loads(
            cls.article_catalog_bytes.decode('utf-8')
        )
        cls.brief_path = cls.site_dir / 'article_briefs.json'
        cls.brief_bytes = cls.brief_path.read_bytes()
        cls.brief_archive = json.loads(cls.brief_bytes.decode('utf-8'))
        cls.observation_path = cls.site_dir / 'observations.json'
        cls.observation_bytes = cls.observation_path.read_bytes()
        cls.observation_archive = json.loads(cls.observation_bytes.decode('utf-8'))
        article_payload = json.loads((ROOT / 'articles_index.json').read_text(encoding='utf-8'))
        cls.source_articles = (
            article_payload.get('articles', [])
            if isinstance(article_payload, dict)
            else article_payload
        )
        cls.source_content_articles = [
            article for article in cls.source_articles
            if article.get('content_status') != 'registry'
        ]
        cls.source_ideas = json.loads((ROOT / 'trades_extracted.json').read_text(encoding='utf-8'))
        threads_match = re.search(r'const THREADS = (.*?);\n', cls.html)
        snapshot_match = re.search(r'const SNAPSHOT = (.*?);\n', cls.html)
        if not threads_match or not snapshot_match:
            raise AssertionError('generated client payload is missing')
        cls.article_payload = cls.article_catalog['articles']
        cls.articles = [
            hydrate_client_article(wire_article)
            for wire_article in cls.article_payload
        ]
        cls.threads = json.loads(threads_match.group(1))
        cls.ideas = cls.observation_archive['observations']
        cls.snapshot = json.loads(snapshot_match.group(1))

    @classmethod
    def tearDownClass(cls):
        cls._site_temp.cleanup()
        cls._source_temp.cleanup()

    def test_complete_multi_source_dataset_is_deferred_once(self):
        self.assertIn(
            f'const ARTICLE_WIRE_SCHEMA_VERSION = {ARTICLE_WIRE_SCHEMA_VERSION};',
            self.html,
        )
        self.assertEqual(
            set(self.article_catalog),
            {
                'schema_version',
                'article_wire_schema_version',
                'data_checksum',
                'articles',
            },
        )
        self.assertEqual(self.article_catalog['schema_version'], 1)
        self.assertEqual(
            self.article_catalog['article_wire_schema_version'],
            ARTICLE_WIRE_SCHEMA_VERSION,
        )
        self.assertEqual(
            self.article_catalog['data_checksum'],
            self.snapshot['data_checksum'],
        )
        self.assertEqual(len(self.articles), len(self.source_content_articles))
        self.assertEqual(len(self.ideas), len(self.source_ideas))
        self.assertEqual(
            Counter(article['source'] for article in self.articles),
            Counter(article['source'] for article in self.source_content_articles),
        )
        self.assertEqual(sum(article['trade_count'] for article in self.articles), len(self.source_ideas))

        article_ids = {article['id'] for article in self.articles}
        idea_ids = {idea['id'] for idea in self.ideas}
        self.assertEqual(len(article_ids), len(self.articles))
        self.assertEqual(len(idea_ids), len(self.ideas))
        self.assertTrue(all(idea['article_id'] in article_ids for idea in self.ideas))
        self.assertTrue(all('article_url' not in idea for idea in self.ideas))
        self.assertIn('let IDEAS = [];', self.html)
        self.assertNotRegex(self.html, r'const IDEAS\s*=\s*\[')

    def test_deferred_observations_are_complete_release_bound_and_lossless(self):
        self.assertTrue(self.observation_path.is_file())
        self.assertEqual(
            set(self.observation_archive),
            {'schema_version', 'data_checksum', 'observations'},
        )
        self.assertEqual(self.observation_archive['schema_version'], 1)
        self.assertEqual(
            self.observation_archive['data_checksum'],
            self.snapshot['data_checksum'],
        )
        self.assertIsInstance(self.ideas, list)
        self.assertEqual(len(self.ideas), self.snapshot['observation_count'])
        self.assertEqual(len(self.ideas), len(self.source_ideas))

        article_urls = {
            article['id']: article['url'].rstrip('/') for article in self.articles
        }
        source_by_identity = {
            (
                str(source.get('article_url') or '').rstrip('/'),
                str(source.get('trade_description') or '').strip(),
            ): source
            for source in self.source_ideas
        }
        self.assertEqual(
            len(source_by_identity),
            len(self.source_ideas),
            'source URL plus exact passage must identify every observation',
        )

        archive_ids = set()
        archive_identities = set()
        for idea in self.ideas:
            self.assertNotIn(idea['id'], archive_ids)
            archive_ids.add(idea['id'])
            self.assertIn(idea['article_id'], article_urls)
            identity = (article_urls[idea['article_id']], idea['description'])
            self.assertIn(identity, source_by_identity)
            self.assertNotIn(identity, archive_identities)
            archive_identities.add(identity)
            source = source_by_identity[identity]
            expected_instruments = [
                str(value) for value in (source.get('instruments') or ['unspecified'])
                if value
            ] or ['unspecified']
            expected_manager = ' '.join(unicodedata.normalize(
                'NFKC', str(source.get('fund_name_if_mentioned') or '')
            ).split())
            expected = {
                'description': str(source.get('trade_description') or '').strip(),
                'description_truncated': bool(source.get('description_truncated', False)),
                'direction': str(source.get('direction') or 'unspecified'),
                'instruments': expected_instruments,
                'underlying': source.get('underlying') or '',
                'thesis': source.get('edge_or_thesis') or '',
                'quant': source.get('any_quant_detail') or '',
                'outcome': source.get('outcome_if_mentioned') or '',
                'manager_raw': expected_manager,
            }
            self.assertEqual(
                {field: idea[field] for field in expected},
                expected,
                f'deferred observation altered source content for {identity[0]}',
            )

        self.assertEqual(archive_identities, set(source_by_identity))
        for required in (
            "'observations.json?v='",
            "cache:'no-cache'",
            'response.text()',
            'actualHash !== OBSERVATION_ARCHIVE_SHA256',
            'JSON.parse(archiveText)',
            'payload.schema_version !== 1',
            'payload.data_checksum !== SNAPSHOT.data_checksum',
            'rows.length !== Number(SNAPSHOT.observation_count || 0)',
            'const expectedArticleById = new Map()',
            'nextMap.has(idea.id)',
            'expectedArticleById.get(idea.id) !== idea.article_id',
            'nextMap.size !== expectedArticleById.size',
            'relevanceScoreCache = new WeakMap()',
            'Observation archive does not match this release',
            'function fetchReleaseText(url,unavailableMessage)',
            'const controller = new AbortController()',
            'controller.abort()',
            'signal:controller.signal',
            "error.name === 'AbortError'",
            'request timed out',
            'clearTimeout(timeoutId)',
            'function releaseMismatchError(message)',
            'function recoverFromStaleReleaseShell()',
            "current.searchParams.get('nrt_catalog_recovery') === '1'",
            "current.searchParams.set('nrt_release',token)",
            "current.searchParams.set('nrt_catalog_recovery','1')",
            'window.location.replace(current.href)',
            'if (error && error.releaseMismatch) recoverFromStaleReleaseShell()',
        ):
            self.assertIn(required, self.html)

    def test_observation_deep_links_wait_for_verified_release_asset(self):
        gate_start = self.html.index('function currentStateNeedsObservations()')
        render_start = self.html.index('function render() {', gate_start)
        render_end = self.html.index('\nfunction resetFilters', render_start)
        gate = self.html[gate_start:render_end]
        for text in (
            "if (state.view === 'structure') return structureSetupDefined()",
            'if (!isArticleView()) return true',
            'state.directions.size || state.instruments.size || state.managers.size',
            'function requestObservationsForCurrentState(forceRetry)',
            'if (observationsFailed && !forceRetry)',
            'const request = forceRetry ? retryObservations() : loadObservations()',
            'function renderObservationGate()',
            'release-bound observation asset',
            'data-retry-observations',
            'no evidence-absence conclusion has been drawn',
            'if (!observationsReady && currentStateNeedsObservations())',
            'requestObservationsForCurrentState(false)',
        ):
            self.assertIn(text, gate)
        self.assertNotIn("if (state.view === 'research') return true", gate)
        self.assertNotIn('state.query || state.directions.size', gate)
        self.assertIn('function syncExportAvailability()', gate)
        self.assertIn('exportButton.disabled = unavailable', gate)
        self.assertLess(
            gate.index('if (!observationsReady && currentStateNeedsObservations())'),
            gate.index('const records = filteredRecords()'),
        )
        # A deferred idea selection remains in state and in the URL until the
        # verified archive arrives; the loading gate must not clear it.
        observation_gate = gate[gate.index('function renderObservationGate()'):gate.index('function render() {')]
        self.assertNotIn("state.selected = ''", observation_gate)
        self.assertIn('function retryObservations()', self.html)
        self.assertIn("event.target.closest('[data-retry-observations]')", self.html)

    def test_observations_are_requested_lazily_without_a_late_brief_rerender(self):
        startup_start = self.html.rindex('hydrateFromHash();')
        startup_end = self.html.index('</script>', startup_start)
        startup = self.html[startup_start:startup_end]
        self.assertIn('if (state.query && isArticleView()) renderArticleAwareSearch(false);', startup)
        self.assertIn('else render();', startup)
        self.assertNotIn('loadObservations()', startup)
        self.assertNotIn('retryObservations()', startup)

        request_start = self.html.index('function requestObservationsForCurrentState(forceRetry)')
        request_end = self.html.index('\nfunction renderObservationGate()', request_start)
        request = self.html[request_start:request_end]
        self.assertIn(
            'if (observationsReady || !currentStateNeedsObservations()) return Promise.resolve(IDEAS);',
            request,
        )
        self.assertIn('if (observationGatePromise) return observationGatePromise;', request)
        self.assertIn('if (observationsFailed && !forceRetry) return Promise.resolve(null);', request)
        completion_gate = request.index('if (currentStateNeedsObservations()) {')
        self.assertLess(completion_gate, request.index('render();', completion_gate))
        self.assertIn('else {\n      pendingObservationFocus = null;', request)
        error_render = request.index('render();', request.index('}).catch(function ()'))
        self.assertLess(error_render, request.index('focusObservationGate(true);', error_render))

        retry_start = self.html.index("const retryObservationButton = event.target.closest('[data-retry-observations]');")
        retry_end = self.html.index('\n  const briefJump', retry_start)
        retry = self.html[retry_start:retry_end]
        self.assertIn('queueObservationResultFocus();', retry)
        self.assertIn('requestObservationsForCurrentState(true);', retry)
        self.assertIn('render();\n    focusObservationGate();', retry)
        self.assertNotIn('retryObservations().then', retry)

        search_start = self.html.index('function renderArticleAwareSearch(focusResult)')
        search_end = self.html.index("document.getElementById('search').addEventListener('input'", search_start)
        search = self.html[search_start:search_end]
        self.assertIn('if (!observationsReady && currentStateNeedsObservations())', search)
        self.assertIn('queueObservationResultFocus();', search)

    def test_dynamic_view_navigation_preserves_focus_through_async_loading(self):
        helper_start = self.html.index('function queueObservationResultFocus(kind)')
        helper_end = self.html.index('\nfunction requestObservationsForCurrentState', helper_start)
        helper = self.html[helper_start:helper_end]
        for text in (
            "kind:kind || 'entry'",
            'function focusViewEntry()',
            "document.getElementById('observation-gate-title')",
            'function focusObservationGate(consumePending)',
            "const retry = observationsFailed ? document.querySelector('[data-retry-observations]') : null",
            'if (consumePending) pendingObservationFocus = null',
            "if (pending.kind === 'inspector') openInspector(true)",
            'function renderObservationAwareNavigation(focusKind)',
            'const waiting = !observationsReady && currentStateNeedsObservations()',
            'if (waiting) queueObservationResultFocus(focusKind)',
            'if (waiting) focusObservationGate()',
            "else focusViewEntry()",
        ):
            self.assertIn(text, helper)

        handler_start = self.html.index("const view = event.target.closest('button[data-view]');")
        handler_end = self.html.index('\n  const kpiView', handler_start)
        handler = self.html[handler_start:handler_end]
        self.assertIn('state.view = view.dataset.view', handler)
        self.assertIn("renderObservationAwareNavigation('entry')", handler)
        self.assertNotIn('\n    render();', handler)

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
        self.assertLessEqual(
            len(canonical_keys),
            len(raw_keys),
            'canonicalization must not create additional manager identities',
        )
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

    def test_article_intelligence_brief_is_source_led(self):
        self.assertIn('function renderIntelligenceBrief(records)', self.html)
        for text in (
            'Article Record',
            'Article record · published information',
            'Opening authored passage',
            'Exact authored passage',
            'Exact authored passages, organized by research role.',
            'id="analysis-title">Evidence</h2>',
            'No analyst conclusion, score, or portfolio recommendation is inferred.',
            'Risks, countercase &amp; checkpoints',
            'Captured section map',
            'Evidence ledger',
            'Detected numbers with their authored context',
            'Captured section coverage',
            'Related archive context',
            'Recent articles',
            'Source-defined challenges',
            'Author’s countercase passage',
            'Author’s falsifier passage',
            'Tab-session local review',
            'Local · this tab',
            'Open article record',
            'Copy article record',
            'Print / PDF',
            'not independently verified, not a live market as-of, and not a portfolio recommendation',
            'Evidence boundaries',
            'Instrument extraction map',
            'Parser-derived observations',
            'built from exact authored sections, not observation count',
            'Extracted passages describe mixed structures; no single article-level stance is assigned.',
            'does not infer holdings, conviction, expected return, portfolio fit, or a live market view',
        ):
            self.assertIn(text, self.html)

        briefing_start = self.html.index('function renderIntelligenceBrief(records)')
        briefing_end = self.html.index('\nfunction contextualRecords', briefing_start)
        briefing = self.html[briefing_start:briefing_end]
        self.assertIn('ARTICLE_BY_ID.get(state.selected)', briefing)
        self.assertIn('articleClaim(selected)', briefing)
        self.assertIn('articleBriefSpans(selected)', briefing)
        self.assertIn('articleEvidenceLedger(selected)', briefing)
        self.assertIn('researchMapMarkup(selected)', briefing)
        self.assertIn('evidenceLedgerMarkup(selected)', briefing)
        self.assertIn('evidenceSpotlightMarkup(selected)', briefing)
        self.assertIn('researchThreadMarkup(selected)', briefing)
        self.assertIn("analysisPanelMarkup(mechanismRow,'Mechanism'", briefing)
        self.assertIn("decisionSheetSectionMarkup(countercaseRow,'Author’s countercase passage')", briefing)
        self.assertIn('briefRailMarkup(lenses)', briefing)
        self.assertIn('archiveCoverageMarkup(records)', briefing)
        self.assertIn('relatedResearchMarkup(selected)', briefing)
        self.assertIn('map(intelligenceCard)', briefing)
        self.assertNotIn('documentation_score', briefing)

        article_view_start = self.html.index('function isArticleView()')
        article_view_end = self.html.index('\nfunction briefSection', article_view_start)
        self.assertIn("state.view === 'briefing'", self.html[article_view_start:article_view_end])
        contextual_start = self.html.index('function contextualRecords(skip)')
        contextual_end = self.html.index('\nfunction recordArticle', contextual_start)
        self.assertIn('return ARTICLES.filter', self.html[contextual_start:contextual_end])

    def test_editorial_brief_uses_article_evidence_without_inventing_analysis(self):
        spotlight_start = self.html.index('function evidenceSpotlightMarkup(article)')
        spotlight_end = self.html.index('\nconst THREAD_ROLE_DEFINITIONS', spotlight_start)
        spotlight = self.html[spotlight_start:spotlight_end]
        for text in (
            'articleEvidenceLedger(article)',
            'row.values.slice(0,5)',
            'row.span.text',
            'spanProvenance(row.span)',
            'Exact authored passage',
            'not a conclusion that the full article contains no quantitative evidence',
        ):
            self.assertIn(text, spotlight)
        for forbidden in ('documentation_score', 'confidence', 'portfolio relevance', 'Math.round'):
            self.assertNotIn(forbidden, spotlight)

        briefing_start = self.html.index('function renderIntelligenceBrief(records)')
        briefing_end = self.html.index('\nfunction contextualRecords', briefing_start)
        briefing = self.html[briefing_start:briefing_end]
        self.assertIn(
            "const metadataOnlyMember = selected.publication_access === 'member' && !hasIndexedMemberPreview(selected)",
            briefing,
        )
        self.assertIn(
            "const openingLabel = leadRow ? 'Opening authored passage' : metadataOnlyMember ? 'Published metadata' : 'Published article framing'",
            briefing,
        )
        self.assertIn(
            'No anonymous article-body preview was available in this release',
            briefing,
        )
        self.assertIn('Active retained source-passage item', briefing)
        self.assertIn('current observation absent', briefing)
        self.assertIn('never treats them as current recommendations', briefing)
        self.assertIn('Array.from(workflowItems.values()).filter', briefing)
        self.assertIn('item.source_snapshot.article_id === selected.id', briefing)
        self.assertNotIn('articleIdeaIds.has(item.id)', briefing)
        self.assertNotIn('const localPackets = observationsReady ?', briefing)
        self.assertNotIn('Analyst synthesis', briefing)
        self.assertNotIn('Evidence quality', briefing)

    def test_research_threads_preserve_exact_source_chronology_and_membership(self):
        source_by_url = {
            str(article['url']).rstrip('/'): article
            for article in self.source_content_articles
        }
        article_by_id = {article['id']: article for article in self.articles}
        search = json.loads(
            (self.site_dir / 'data' / 'search_index.json').read_text(
                encoding='utf-8',
            ),
        )
        entities_by_url = {
            str(row['url']).rstrip('/'): set(row['entities'])
            for row in search['articles']
        }

        for article in self.articles:
            source = source_by_url[article['url'].rstrip('/')]
            self.assertEqual(article['published_at'], source['post_date'])
            self.assertEqual(
                article['publication_precision'],
                'day' if len(source['post_date']) == 10 else 'instant',
            )

        self.assertEqual(self.threads['schema_version'], 1)
        self.assertEqual(self.threads['topic_count'], len(self.threads['topics']))
        self.assertEqual(
            self.threads['article_count'],
            len(self.threads['defaults']),
        )
        self.assertGreaterEqual(self.threads['topic_count'], 50)
        self.assertGreaterEqual(self.threads['article_count'], 250)
        self.assertLess(
            len(json.dumps(
                self.threads,
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8')),
            150_000,
        )

        for key, topic in self.threads['topics'].items():
            self.assertGreaterEqual(topic['article_count'], 2)
            self.assertEqual(topic['article_count'], len(topic['article_ids']))
            self.assertEqual(len(topic['match_codes']), len(topic['article_ids']))
            self.assertTrue(all(topic['match_codes']))
            values = [
                article_by_id[article_id]['published_at']
                for article_id in topic['article_ids']
            ]
            self.assertEqual(values, sorted(values))
            for article_id in topic['article_ids']:
                article = article_by_id[article_id]
                self.assertIn(key, entities_by_url[article['url'].rstrip('/')])
                default_topic = self.threads['defaults'][article_id]
                self.assertIn(
                    article_id,
                    self.threads['topics'][default_topic]['article_ids'],
                )

    def test_research_threads_are_bounded_accessible_and_evidence_safe(self):
        start = self.html.index('const THREAD_ROLE_DEFINITIONS')
        end = self.html.index('\nfunction analysisPanelMarkup', start)
        markup = self.html[start:end]
        for text in (
            'Research history across related publications',
            'Capture comparison with preceding indexed publication',
            'threadWindow(topic.article_ids,article.id,7)',
            'Exact prior passage is deferred',
            'Load exact passage comparison',
            'Opening-passage numeric tokens',
            'Matched in: ',
            'does not establish a changed view, contradiction, conviction, or portfolio action',
            'they do not infer the author’s current position, consistency, conviction, accuracy, performance, or portfolio suitability',
            'role="table"',
            'aria-current="true"',
            'datetime=',
        ):
            self.assertIn(text, markup)
        self.assertNotIn('ensureArticleBrief(', markup)
        self.assertNotIn('documentation_score', markup)
        self.assertNotIn('expected return', markup)
        self.assertIn('const attachedTopics = row.topics.slice();', markup)
        self.assertNotIn('row.topics.slice(0,6)', markup)

        update_hash_start = self.html.index('function updateHash(includeQuery)')
        update_hash_end = self.html.index('\nlet queryCacheKey', update_hash_start)
        update_hash = self.html[update_hash_start:update_hash_end]
        self.assertIn("threadRow.topics.includes(state.threadTopic)", update_hash)
        self.assertIn("params.set('topic',state.threadTopic)", update_hash)
        self.assertIn('const THREAD_ARTICLES = (function () {', self.html)

        click_start = self.html.index("const threadTopic = event.target.closest('[data-thread-topic]')")
        click_end = self.html.index("const briefLens = event.target.closest('[data-brief-lens]')", click_start)
        click_handlers = self.html[click_start:click_end]
        self.assertIn('row.topics.includes(threadTopic.dataset.threadTopic)', click_handlers)
        self.assertIn('topic.article_ids.includes(threadArticle.dataset.threadArticle)', click_handlers)
        self.assertIn('ensureArticleBrief(prior)', click_handlers)
        self.assertIn('threadComparisonRequest !== request', click_handlers)
        self.assertIn("state.view === 'briefing'", click_handlers)
        self.assertIn("kind:'thread-comparison'", click_handlers)

        mobile_start = self.html.rindex('@media(max-width:759px)')
        mobile_end = self.html.index('@media(max-width:520px)', mobile_start)
        mobile = self.html[mobile_start:mobile_end]
        self.assertIn('.thread-topic{min-height:44px;font-size:12px}', mobile)
        self.assertIn('.thread-load-boundary .secondary-action{width:100%;min-height:44px', mobile)
        self.assertIn(
            '.thread-role-row{grid-template-columns:minmax(105px,1fr) 60px 74px}',
            mobile,
        )
        self.assertIn('.thread-kind,.thread-topic span,.thread-facts b', mobile)
        self.assertIn(
            '.thread-node{position:relative;min-width:0;padding-left:25px}',
            self.html,
        )

    def test_unified_allocator_workspace_honors_system_theme_and_is_responsive(self):
        for text in (
            '--serif:ui-serif,"Iowan Old Style"',
            '--bg:#f4f6f8',
            '--surface-1:#ffffff',
            '--text:#142033',
            '--accent:#174ea6',
            '--bg:#090e15',
            '--surface-1:#0f1620',
            '--selected-line:#78a9ff',
            'One allocator-grade system. Theme changes color and elevation, never geometry.',
            '.desk-landing-hero{',
            '.desk-proof-strip{',
            '.desk-source-footer a{min-height:44px;display:inline-flex;align-items:center',
            '.text-button,.filter-chip,.primary-action,.secondary-action,.inspector-close,.load-more{min-height:44px}',
            '.intel-title{',
            'var(--serif)',
            '.ic-rail{',
            '.intel-side.ic-sheet{',
            '@media(max-width:1040px)',
            '@media(max-width:1020px)',
            '@media(max-width:899px)',
            '@media(max-width:759px)',
            '@media(max-width:480px)',
        ):
            self.assertIn(text, self.html)
        self.assertIn("var themeRevision = 'allocator-workspace-2026-08'", self.html)
        self.assertIn("window.matchMedia('(prefers-color-scheme: dark)').matches", self.html)
        self.assertIn(
            "var theme = stored || (systemDark ? 'dark' : 'light')",
            self.html,
        )
        self.assertIn(
            'id="theme-button" type="button" aria-label="Switch to dark theme">Dark mode</button>',
            self.html,
        )
        self.assertNotRegex(
            self.html,
            r'html\[data-theme="(?:light|dark)"\]\s+\.(?:app-header|brand-name|global-search)',
            'theme selection must not swap product geometry or typography',
        )
        self.assertLess(
            self.html.index("var themeRevision = 'allocator-workspace-2026-08'"),
            self.html.index('<style>'),
            'theme bootstrap must run before styles to prevent a wrong-theme first paint',
        )
        self.assertRegex(
            self.html,
            r'body\[data-view="briefing"\] \.kpi-strip,\s*body\[data-view="briefing"\] \.command-bar',
        )
        self.assertRegex(self.html, r'\.intel-wrap\{[^}]*grid-template-columns:220px minmax\(620px,1fr\) 360px')
        self.assertNotIn('min-width:1180px', self.html)
        for text in (
            ':root{--header-h:104px}',
            ':root{--header-h:104px;--kpi-h:42px}',
            'grid-template-rows:52px 52px',
            '.header-library,#method-button{display:none}',
            '.global-search{grid-column:1/-1;grid-row:2}',
            '.utility-button{min-height:44px}',
            '.brand-name{display:none}',
            '.freshness{width:18px;max-width:18px;gap:0;justify-content:center}',
            '#palette-button{font-size:0}',
            '#theme-button::before{content:"◐";font-size:15px;line-height:1}',
            'body[data-view="structure"] #mobile-filter-button{display:none!important}',
        ):
            self.assertIn(text, self.html)
        self.assertRegex(
            self.html,
            r'body\[data-view="structure"\] \.workspace\{\s*height:calc\(100vh - var\(--header-h\)\)',
        )

    def test_principal_landing_surfaces_exact_release_review_and_source_state(self):
        for text in (
            'Original markets research with the source trail attached.',
            'Read the latest note',
            'Get full research access',
            'Latest research',
            'What needs attention',
            'Open local reviews',
            'Mark current research reviewed',
            'Search the archive',
            'Company, market, strategy, or theme',
            'Data health &amp; coverage',
            'Coverage across Substack, Medium, Patreon, and FX Empire',
            'Open verification record',
            'Published-source research, not a recommendation or live portfolio view.',
            "const newResearchLabel = reviewBaselineExists ? 'New since review' : 'Recent · 7 days';",
            "const newFilterLabel = reviewBaselineExists ? 'New since last review' : 'Recent · 7 days';",
            "const freshness = snapshotFreshness();",
            "const sourceRollupClass = healthySources === CATALOGUE_SOURCES.length",
            'Sources healthy',
            'data-desk-article=',
            'data-owner-search-form',
            'data-owner-new-research',
            'data-action="mark-reviewed"',
            'data-action="undo-mark-reviewed"',
            '}).slice(0,4);',
        ):
            self.assertIn(text, self.html)

        match = re.search(r'const CATALOGUE_SOURCES = (.*?);\n', self.html)
        self.assertIsNotNone(match)
        catalogue_sources = json.loads(match.group(1))
        self.assertEqual(
            [row['source'] for row in catalogue_sources],
            ['substack', 'medium', 'patreon', 'fxempire'],
        )

        def has_captured_text(row):
            try:
                if int(row.get('wordcount') or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
            preview = row.get('member_preview') or {}
            try:
                if isinstance(preview, dict) and int(preview.get('character_count') or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
            brief = row.get('brief') or {}
            if not isinstance(brief, dict):
                return False
            spans = [brief.get('lead'), brief.get('fallback_evidence')]
            spans.extend(brief.get('sections') or [])
            spans.extend(brief.get('checkpoints') or [])
            return any(
                isinstance(span, dict) and bool(str(span.get('text') or '').strip())
                for span in spans
            )

        for summary in catalogue_sources:
            source_rows = [
                row for row in self.source_articles
                if row.get('source') == summary['source']
            ]
            captured = sum(has_captured_text(row) for row in source_rows)
            self.assertEqual(summary['count'], len(source_rows))
            self.assertEqual(
                summary['article_count'],
                sum(row.get('content_status') != 'registry' for row in source_rows),
            )
            self.assertEqual(summary['captured_text_count'], captured)
            self.assertEqual(summary['metadata_only_count'], len(source_rows) - captured)
        landing_start = self.html.index('function deskLandingMarkup()')
        landing_end = self.html.index('\nfunction renderStructureDesk', landing_start)
        landing = self.html[landing_start:landing_end]
        self.assertNotIn('body-backed', landing)
        self.assertEqual(landing.count('<input'), 1)
        self.assertIn('id="owner-search-input"', landing)
        self.assertIn('escapeHtml(SUBSCRIPTION_URL)', landing)
        self.assertIn(
            '<h1 id="desk-landing-title">Original markets research with the source trail attached.</h1>',
            landing,
        )
        self.assertIn('<h2>Latest notes</h2>', landing)
        self.assertIn('<h2>What needs attention</h2>', landing)
        self.assertIn('target="_blank" rel="noopener noreferrer"', landing)
        self.assertIn(
            'aria-label="Get full Navnoor Research access (opens in a new tab)"',
            landing,
        )
        self.assertNotIn('id="structure-question-input"', landing)
        self.assertNotIn('id="structure-focus-input"', landing)
        self.assertNotIn('desk-role-tags', landing)
        self.assertNotIn('desk-operating-loop', landing)
        self.assertIn('<details class="desk-source-panel">', landing)
        self.assertIn("escapeHtml(sourceRollupClass)", landing)
        self.assertIn('.desk-source-panel>summary>b.ok{color:var(--positive)}', self.html)
        self.assertIn('.desk-source-panel>summary>b.degraded{color:var(--warning)}', self.html)
        self.assertLess(
            landing.index('Original markets research with the source trail attached.'),
            landing.index('Search the archive'),
        )
        self.assertLess(
            landing.index('Search the archive'),
            landing.index('Data health &amp; coverage'),
        )

    def test_owner_search_progressively_opens_the_local_evidence_workspace(self):
        handler_start = self.html.index("document.addEventListener('submit',function (event) {")
        handler_end = self.html.index(
            "\ndocument.addEventListener('click',function (event) {",
            handler_start,
        )
        handler = self.html[handler_start:handler_end]
        for text in (
            "event.target.closest('[data-owner-search-form]')",
            'event.preventDefault()',
            ".replace(/\\s+/g,' ').trim().slice(0,120)",
            'state.structureFocus = value',
            "state.structureQuestion = ''",
            'state.structureControlsOpen = false',
            'state.structureShareable = false',
            "state.structureAnchor = ''",
            "state.structurePassage = ''",
            "renderObservationAwareNavigation('entry')",
            "document.getElementById('structure-focus-input')",
        ):
            self.assertIn(text, handler)
        for forbidden in ('fetch(', 'localStorage', 'updateHash('):
            self.assertNotIn(forbidden, handler)
        self.assertGreaterEqual(
            self.html.count(
                "document.getElementById('structure-focus-input') || "
                "document.getElementById('owner-search-input')"
            ),
            2,
        )

    def test_owner_navigation_clears_stale_workspace_and_archive_filters(self):
        clear_start = self.html.index('function clearArchiveScope()')
        clear_end = self.html.index('\nfunction resetFilters()', clear_start)
        clear_scope = self.html[clear_start:clear_end]
        for text in (
            "state.query = ''",
            'state.sources.clear()',
            'state.revisions.clear()',
            'state.publicationAccess.clear()',
            'state.newOnly = false',
            "state.briefLens = 'all'",
            "state.structureQuestion = ''",
            "state.structureFocus = ''",
            "state.structureAnchor = ''",
            "state.structurePassage = ''",
            'state.structureShareable = false',
        ):
            self.assertIn(text, clear_scope)

        latest_start = self.html.index(
            "const deskArticle = event.target.closest('[data-desk-article]')"
        )
        latest_end = self.html.index(
            "\n  const loadArticleReview = event.target.closest('[data-load-article-review]')",
            latest_start,
        )
        latest_handler = self.html[latest_start:latest_end]
        self.assertLess(
            latest_handler.index('clearArchiveScope()'),
            latest_handler.index("state.view = 'briefing'"),
        )
        self.assertLess(
            latest_handler.index('state.selected = article.id'),
            latest_handler.index("renderObservationAwareNavigation('entry')"),
        )

        view_start = self.html.index(
            "const view = event.target.closest('button[data-view]')"
        )
        view_end = self.html.index(
            "\n  const kpiView = event.target.closest('[data-kpi-view]')",
            view_start,
        )
        view_handler = self.html[view_start:view_end]
        self.assertIn(
            "view.dataset.view === 'structure' || view.hasAttribute('data-owner-research')",
            view_handler,
        )
        self.assertIn('clearArchiveScope()', view_handler)
        self.assertIn('data-owner-research', self.html)
        self.assertIn(
            "document.getElementById('structure-focus-input') || "
            "document.getElementById('owner-search-input') || "
            "document.getElementById('search')",
            self.html,
        )

        new_start = self.html.index('function openOwnerNewResearch()')
        new_end = self.html.index('\nfunction openOwnerReview()', new_start)
        new_handler = self.html[new_start:new_end]
        for text in (
            'const hasNewResearch = ARTICLES.some(isNewArticle)',
            'clearArchiveScope()',
            "state.view = 'research'",
            'state.newOnly = hasNewResearch',
            "state.sort = 'newest'",
            "renderObservationAwareNavigation('entry')",
        ):
            self.assertIn(text, new_handler)
        self.assertIn("newResearch ? 'Review' : 'Browse all'", self.html)

        review_start = self.html.index('function openOwnerReview()')
        review_end = self.html.index('\nfunction applyPreset', review_start)
        review_handler = self.html[review_start:review_end]
        for text in (
            'clearArchiveScope()',
            "state.view = 'queue'",
            "new Set(['review','diligence','monitor'])",
            "state.sort = 'newest'",
            'state.limit = PAGE_SIZE.queue',
            "renderObservationAwareNavigation('entry')",
        ):
            self.assertIn(text, review_handler)
        self.assertNotIn("'archived'", review_handler)
        self.assertIn('data-owner-review', self.html)

    def test_owner_home_hides_workstation_chrome_until_a_search_is_submitted(self):
        render_start = self.html.index('function renderStructureDesk(rows, gate)')
        render_end = self.html.index('\nfunction render()', render_start)
        render = self.html[render_start:render_end]
        home_guard = render.index('if (!gate && !defined) {')
        home_return = render.index('return;', home_guard)
        self.assertIn(
            "document.body.dataset.structureReady = 'false'",
            render[home_guard:home_return],
        )
        self.assertIn(
            "document.body.dataset.structureReady = 'true'",
            render[home_return:],
        )
        for text in (
            'body[data-view="structure"] .header-library,',
            'body[data-view="structure"] #method-button,',
            'body[data-view="structure"] #shortcut-button{display:none}',
            'body[data-view="structure"][data-structure-ready="false"] .result-summary,',
            'body[data-view="structure"][data-structure-ready="false"] .command-button[data-action="copy-view"],',
            'body[data-view="structure"][data-structure-ready="false"] .command-button[data-action="export"]{display:none}',
        ):
            self.assertIn(text, self.html)

    def test_all_source_links_use_a_strict_source_specific_allowlist(self):
        match = re.search(r'const CATALOGUE_SOURCES = (.*?);\n', self.html)
        self.assertIsNotNone(match)
        catalogue_sources = json.loads(match.group(1))
        cases = [
            [row['latest']['url'], row['source']]
            for row in catalogue_sources
            if row.get('latest')
        ]
        function_start = self.html.index('function safeUrl(value)')
        function_end = self.html.index('\nconst MONTHS', function_start)
        functions = self.html[function_start:function_end]
        script = functions + '\nconst cases = ' + json.dumps(cases) + ''';
for (const [url,source] of cases) {
  if (safeCatalogueUrl(url,source) !== url) throw new Error('rejected ' + source);
}
const bySource = new Map(cases.map(function (entry) { return [entry[1],entry[0]]; }));
const rejected = [
  [bySource.get('medium'),'substack'],
  [bySource.get('substack'),'medium'],
  [bySource.get('substack'),'unknown'],
  ['https://www.patreon.com/SomeoneElse/posts/test-1','patreon'],
  ['https://www.patreon.com/NavnoorBawa/posts/test-1?redirect=1','patreon'],
  ['https://www.fxempire.com/news/article/test-1','fxempire'],
  ['https://evil.example/forecasts/article/test-1','fxempire'],
  ['javascript:alert(1)','patreon']
];
for (const [url,source] of rejected) {
  if (safeCatalogueUrl(url,source) !== '#') throw new Error('accepted hostile URL');
}
'''
        subprocess.run(
            ['node', '-e', script],
            cwd='/tmp',
            check=True,
            capture_output=True,
            text=True,
        )
        coverage_start = self.html.index('function deskSourceCoverageMarkup(row)')
        coverage_end = self.html.index('\nfunction deskLandingMarkup()', coverage_start)
        coverage = self.html[coverage_start:coverage_end]
        self.assertNotIn('href=', coverage)
        self.assertNotIn('latest.url', coverage)
        self.assertNotIn('safeUrl(latest.url)', self.html)

    def test_article_record_shows_every_exact_passage_before_local_review_handoff(self):
        launcher_start = self.html.index('function articleReviewLauncherMarkup(article)')
        launcher_end = self.html.index('\nfunction briefRailMarkup', launcher_start)
        launcher = self.html[launcher_start:launcher_end]
        self.assertIn('const candidates = article._ideas || [];', launcher)
        self.assertIn('escapeHtml(passageText(idea))', launcher)
        self.assertNotIn('.slice(0,8)', launcher)
        self.assertNotIn('boundedPromotionText', launcher)
        per_article = Counter(idea['article_id'] for idea in self.ideas)
        self.assertGreater(max(per_article.values()), 8, 'fixture must exercise the old eight-passage cap')
        self.assertGreater(
            max(len(str(idea.get('description') or '')) for idea in self.ideas),
            190,
            'fixture must exercise the old preview-only anchor',
        )

        handoff_start = self.html.index('function startArticlePassageReview(id,articleId)')
        handoff_end = self.html.index('\nfunction clampWorkflowText', handoff_start)
        handoff = self.html[handoff_start:handoff_end]
        self.assertIn("idea.article_id !== articleId", handoff)
        self.assertIn("state.view !== 'briefing' || state.selected !== articleId", handoff)
        self.assertIn('retained.article_id !== articleId', handoff)
        self.assertIn('retained.url !== current.url || retained.passage !== current.passage', handoff)
        self.assertIn('workflowItems.size >= MAX_QUEUE_ITEMS', handoff)
        self.assertIn('if (!persistWorkflow())', handoff)
        self.assertIn('workflowItems.delete(id)', handoff)

    def test_hidden_mobile_drawers_cannot_inert_the_visible_workspace(self):
        sync_start = self.html.index('function syncOverlayAccessibility()')
        sync_end = self.html.index('\nfunction closeDrawers()', sync_start)
        sync = self.html[sync_start:sync_end]
        self.assertIn('const filtersAvailable = viewHasResearchDrawers();', sync)
        self.assertIn('const inspectorAvailable = viewHasResearchDrawers();', sync)
        self.assertIn("if (!filtersAvailable) document.body.classList.remove('filters-open')", sync)
        self.assertIn("if (!inspectorAvailable) document.body.classList.remove('inspector-open')", sync)
        self.assertIn('const inspectorOpen = inspectorAvailable && inspectorNarrow', sync)

        toggle_start = self.html.index('function toggleResearchFilters(invoker)')
        toggle_end = self.html.index("document.getElementById('filter-close')", toggle_start)
        toggle = self.html[toggle_start:toggle_end]
        self.assertIn('if (!viewHasResearchDrawers())', toggle)
        self.assertIn('Research filters are not used in this view', toggle)
        self.assertIn("document.getElementById('manager-search').focus()", toggle)
        self.assertIn('toggleResearchFilters(this);', self.html)
        self.assertIn('toggleResearchFilters(document.activeElement);', self.html)
        self.assertIn('body[data-view="structure"] #mobile-filter-button{display:none!important}', self.html)
        self.assertGreaterEqual(
            self.html.count('body[data-view="structure"]{--header-h:52px}'),
            2,
            'Structure must keep a one-row header in tablet and phone media rules',
        )

    def test_hidden_brief_rail_has_complete_compact_navigation(self):
        start = self.html.index('function briefCompactNavMarkup(lenses)')
        end = self.html.index('\nlet pendingBriefFocus', start)
        compact = self.html[start:end]
        for text in (
            'aria-label="Article record navigation"',
            'aria-label="Archive views"',
            'aria-label="Section filters"',
            "['briefing','Article Record']",
            "['ideas','Parsed Passages']",
            "['research','Article Index']",
            "['queue','Local Review']",
            'data-brief-lens=',
            'aria-pressed=',
            'aria-current="page"',
        ):
            self.assertIn(text, compact)

        briefing_start = self.html.index('function renderIntelligenceBrief(records)')
        briefing_end = self.html.index('\nfunction contextualRecords', briefing_start)
        briefing = self.html[briefing_start:briefing_end]
        self.assertGreaterEqual(briefing.count('briefCompactNavMarkup(lenses)'), 2)
        self.assertIn('const lenses = BRIEF_LENSES', briefing)
        lens_start = self.html.index('const BRIEF_LENSES = Object.freeze([')
        lens_end = self.html.index(']);', lens_start)
        lens_definition = self.html[lens_start:lens_end]
        for lens in ('all', 'checkpoint', 'evidence', 'countercase', 'falsifier', 'implementation'):
            self.assertIn("['" + lens + "',", lens_definition)

        narrow_start = self.html.index('@media(max-width:1439px)')
        narrow_end = self.html.index('@media(max-width:1023px)', narrow_start)
        self.assertIn('.ic-rail{display:none}', self.html[narrow_start:narrow_end])
        self.assertIn('.ic-compact-nav{display:grid}', self.html[narrow_start:narrow_end])
        narrow_css = self.html[narrow_start:narrow_end]
        self.assertIn('#brief-thesis,#brief-key-evidence,#brief-thread,#brief-analysis,#brief-dossier,#brief-evidence-ledger,#brief-checkpoints,#brief-archive{scroll-margin-top:calc(var(--brief-compact-nav-h) + 7px)}', narrow_css)
        self.assertIn('.intel-side.ic-sheet{', narrow_css)
        self.assertIn('top:var(--brief-compact-nav-h)', narrow_css)
        self.assertIn('height:calc(100dvh - var(--header-h) - var(--brief-compact-nav-h))', narrow_css)
        mobile_start = self.html.index('@media(max-width:759px)', narrow_end)
        mobile_end = self.html.index('@media print{', mobile_start)
        self.assertRegex(
            self.html[mobile_start:mobile_end],
            r'\.ic-compact-button\{[^}]*min-height:44px',
        )

        focus_start = self.html.index('function restorePendingBriefFocus(consumePending,preferStatusFocus)')
        focus_end = self.html.index('\nfunction renderIntelligenceBrief', focus_start)
        focus = self.html[focus_start:focus_end]
        self.assertIn('window.innerWidth < 1440', focus)
        self.assertIn("document.querySelector('.ic-compact-nav ' + lensSelector)", focus)
        self.assertIn('if (consumePending !== false) pendingBriefFocus = null', focus)
        self.assertIn("document.getElementById('brief-status-title')", focus)
        self.assertIn("document.querySelector('[data-retry-briefs]')", focus)
        briefing_status = self.html[briefing_start:briefing_end]
        self.assertIn('restorePendingBriefFocus(!preservePendingFocus,preferStatusFocus)', briefing_status)
        self.assertGreaterEqual(briefing_status.count("','',true);"), 2)
        self.assertGreaterEqual(briefing_status.count("',false,true);"), 2)
        self.assertIn('shell.dataset.statusAnnouncement = title', briefing_status)
        self.assertIn('briefStatusAnnouncement ||', self.html)
        inspector_start = self.html.index('function renderInspector()')
        inspector_end = self.html.index('\nfunction currentStateNeedsObservations', inspector_start)
        inspector = self.html[inspector_start:inspector_end]
        self.assertEqual(
            inspector.count("if (state.view !== 'briefing') restorePendingBriefFocus();"),
            2,
        )
        render_start = self.html.index('function render() {')
        render_end = self.html.index('\nfunction resetFilters', render_start)
        self.assertIn(
            "if (state.view !== 'briefing') pendingBriefFocus = null;",
            self.html[render_start:render_end],
        )
        self.assertIn('.ic-jump.unavailable{cursor:default;color:var(--text-muted)}', self.html)
        self.assertNotIn('.ic-jump.unavailable{cursor:default;color:var(--text-muted);opacity:', self.html)

        gate_start = self.html.index('function renderObservationGate()')
        gate_end = self.html.index('\nfunction render() {', gate_start)
        gate = self.html[gate_start:gate_end]
        self.assertIn('briefRailMarkup(BRIEF_LENSES)', gate)
        self.assertIn('briefCompactNavMarkup(BRIEF_LENSES)', gate)
        self.assertIn('An unavailable asset is never presented as missing evidence.', gate)

    def test_print_forces_light_ic_sheet_and_removes_only_local_overlay(self):
        print_start = self.html.index('@media print{')
        print_end = self.html.index('@media(prefers-reduced-motion', print_start)
        print_css = self.html[print_start:print_end]
        for text in (
            ':root,html[data-theme="light"],html[data-theme="dark"]',
            '--bg:#ffffff!important',
            '--surface-1:#ffffff!important',
            '--text:#142033!important',
            '.thread-topic-list,.thread-load-boundary .secondary-action',
            '.intel-side.ic-sheet{',
            'display:block!important',
            'position:static!important',
            'order:3',
            'height:auto!important',
            'overflow:visible!important',
            '.ic-sheet-checkpoint{display:block!important;break-inside:avoid',
            '.intel-lead{display:contents!important',
            '.intel-lead-inner{order:1',
            '.ic-evidence-strip{order:2}',
            '.ic-analysis{order:4}',
            '.ic-dossier{order:5}',
            '.research-thread{order:6',
            '.screen-only{display:none!important}',
            '.print-only{display:inline!important}',
        ):
            self.assertIn(text, print_css)
        hidden_rule = print_css[print_css.index('.app-header,'):print_css.index('{display:none!important}', print_css.index('.app-header,'))]
        self.assertNotIn('.intel-side,', hidden_rule)
        self.assertIn('Source-defined challenges · published source', self.html)
        self.assertIn('Independent research remains required.', self.html)

    def test_clipboard_failure_preserves_text_in_accessible_manual_fallback(self):
        self.assertIn('id="manual-copy-dialog" aria-labelledby="manual-copy-title"', self.html)
        self.assertIn('id="manual-copy-text" readonly aria-label="Text ready to copy"', self.html)
        self.assertIn("else showManualCopyDialog(value);", self.html)
        self.assertIn("textarea.value = String(value || '');", self.html)
        self.assertIn('textarea.focus();', self.html)
        self.assertIn('textarea.select();', self.html)
        self.assertNotIn('Copy failed—select and copy manually', self.html)

    def test_mobile_monitor_is_bounded_and_lighthouse_a11y_defects_are_closed(self):
        self.assertIn(
            'const PAGE_SIZE = {briefing:24,ideas:50,research:80,queue:100,structure:8};',
            self.html,
        )
        self.assertIn(
            'aria-label="Restore local review from a JSON file" tabindex="-1"',
            self.html,
        )
        self.assertIn(
            'aria-labelledby="brief-key-evidence-title"><h2 class="sr-only" '
            'id="brief-key-evidence-title">Source-linked numeric passages</h2>',
            self.html,
        )
        self.assertIn(
            '.ic-evidence-card p,.intel-article-card .intel-card-claim,'
            '.next-item .next-summary{font-size:12px}',
            self.html,
        )
        self.assertIn('.data-row,.data-row *{font-size:12px}', self.html)
        self.assertIn(
            '.filter-heading h2,.preset-button,.freshness,.primary-action,.secondary-action,',
            self.html,
        )

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
        self.assertIn('loadBriefArchive().then(function ()', search)
        self.assertIn('generation !== articleSearchGeneration', search)
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
                'start': int(value.get('start') or 0),
                'end': int(value.get('end') or 0),
                'sha256': str(value.get('sha256') or ''),
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
                        'start': int(section.get('start') or 0),
                        'end': int(section.get('end') or 0),
                        'sha256': str(section.get('sha256') or ''),
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
                        'truncated': bool(checkpoint.get('truncated')),
                        'start': int(checkpoint.get('start') or 0),
                        'end': int(checkpoint.get('end') or 0),
                        'sha256': str(checkpoint.get('sha256') or ''),
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
            'response.text()',
            'actualHash !== BRIEF_ARCHIVE_SHA256',
            'JSON.parse(archiveText)',
            'payload.schema_version !== 1',
            'payload.data_checksum !== SNAPSHOT.data_checksum',
            'validateDeferredBriefArchive(payload)',
            "article.brief = validatedBriefs[id]",
            'refreshArticleSearch(article)',
            'Loading the exact article record',
            'Checking the deferred article record against this release.',
        ):
            self.assertIn(text, self.html)

    def test_deferred_assets_are_bound_to_exact_embedded_release_hashes(self):
        catalogue_match = re.search(
            r'<meta name="nrt-article-catalog-sha256" content="([0-9a-f]{64})">',
            self.html,
        )
        brief_match = re.search(
            r'<meta name="nrt-brief-archive-sha256" content="([0-9a-f]{64})">',
            self.html,
        )
        observation_match = re.search(
            r'<meta name="nrt-observation-archive-sha256" content="([0-9a-f]{64})">',
            self.html,
        )
        self.assertIsNotNone(catalogue_match)
        self.assertIsNotNone(brief_match)
        self.assertIsNotNone(observation_match)
        self.assertEqual(
            catalogue_match.group(1),
            hashlib.sha256(self.article_catalog_bytes).hexdigest(),
        )
        self.assertEqual(brief_match.group(1), hashlib.sha256(self.brief_bytes).hexdigest())
        self.assertEqual(observation_match.group(1), hashlib.sha256(self.observation_bytes).hexdigest())
        self.assertIn(
            "const ARTICLE_CATALOG_SHA256 = document.querySelector('meta[name=\"nrt-article-catalog-sha256\"]').content",
            self.html,
        )
        self.assertIn(
            "const BRIEF_ARCHIVE_SHA256 = document.querySelector('meta[name=\"nrt-brief-archive-sha256\"]').content",
            self.html,
        )
        self.assertIn(
            "const OBSERVATION_ARCHIVE_SHA256 = document.querySelector('meta[name=\"nrt-observation-archive-sha256\"]').content",
            self.html,
        )

        corrupted_catalogue = self.article_catalog_bytes.replace(
            b'"articles":', b'"articles" :', 1,
        )
        corrupted_brief = self.brief_bytes.replace(b'"lead":', b'"lead" :', 1)
        corrupted_observations = self.observation_bytes.replace(b'"observations":', b'"observations" :', 1)
        self.assertNotEqual(
            hashlib.sha256(corrupted_catalogue).hexdigest(),
            catalogue_match.group(1),
        )
        self.assertNotEqual(hashlib.sha256(corrupted_brief).hexdigest(), brief_match.group(1))
        self.assertNotEqual(hashlib.sha256(corrupted_observations).hexdigest(), observation_match.group(1))

    def test_no_javascript_bootstrap_exposes_status_instead_of_perpetual_loading(self):
        overlay_start = self.html.index('<div class="bootstrap-status"')
        overlay_end = self.html.index('<a class="skip-link"', overlay_start)
        overlay = self.html[overlay_start:overlay_end]
        self.assertIn('<noscript><div class="bootstrap-no-js" role="status">', overlay)
        self.assertIn('The research archive cannot verify its catalogue', overlay)
        self.assertIn('href="data/latest.json">View release status</a>', overlay)
        self.assertIn('<div id="bootstrap-js" hidden>', overlay)
        self.assertLess(overlay.index('<noscript>'), overlay.index('id="bootstrap-js"'))
        self.assertIn('noscript{display:block}', self.html)
        self.assertNotIn('noscript{position:fixed', self.html)
        self.assertIn(
            "document.getElementById('bootstrap-js').hidden = false",
            self.html,
        )
        self.assertIn(
            '<div id="application-shell" inert aria-hidden="true">',
            self.html,
        )
        self.assertIn('#application-shell[inert]{display:none}', self.html)
        self.assertIn("applicationShell.removeAttribute('inert')", self.html)
        self.assertIn("applicationShell.removeAttribute('aria-hidden')", self.html)
        self.assertIn('if (retryingCatalogLoad)', self.html)
        self.assertIn('if (recoveryFocus) recoveryFocus.focus()', self.html)

    def test_generated_release_is_reproducible_across_python_hash_seeds(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            outputs = []
            for seed, directory in (('1', first), ('987654', second)):
                environment = os.environ.copy()
                environment['PYTHONHASHSEED'] = seed
                environment['SITE_OUTPUT_DIR'] = directory
                environment['SITE_REVISION'] = 'reproducible-release'
                subprocess.run(
                    [sys.executable, str(self.source_root / 'build_site.py')],
                    cwd=self.source_root,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                outputs.append({
                    path.relative_to(directory).as_posix(): path.read_bytes()
                    for path in Path(directory).rglob('*') if path.is_file()
                })
            self.assertEqual(outputs[0], outputs[1])

    def test_deferred_article_dossier_loader_fails_closed_before_install(self):
        validator_start = self.html.index("const DEFERRED_BRIEF_KEYS")
        loader_start = self.html.index("function loadBriefArchive()", validator_start)
        validator = self.html[validator_start:loader_start]
        for required in (
            "const DEFERRED_BRIEF_KEYS = ['checkpoints','fallback_evidence','lead','sections']",
            "const DEFERRED_SPAN_KEYS = ['end','sha256','start','text','truncated']",
            "const DEFERRED_SECTION_KEYS = ['end','heading','kind','sha256','source_order','start','text','truncated']",
            "const DEFERRED_CHECKPOINT_KEYS = ['context_kind','date','date_label','end','sha256','start','text','truncated']",
            "ARTICLES.filter(function (article) { return article.brief === null; })",
            "actualIds.length !== expectedIds.length",
            "!expectedIdSet.has(id)",
            "!Object.prototype.hasOwnProperty.call(payload.briefs,id)",
            "span.end - span.start !== Array.from(span.text).length",
            "!/^[0-9a-f]{64}$/.test(span.sha256)",
            "hashChecks.push({text:span.text,sha256:span.sha256,label:label})",
            "await Promise.all(hashChecks.map",
            "actualHash !== check.sha256",
            "validDeferredCheckpointDate(checkpoint && checkpoint.date)",
            "DEFERRED_SECTION_KINDS.has(section && section.kind)",
            "validateDeferredFeatureParity(ARTICLE_BY_ID.get(id),brief)",
        ):
            self.assertIn(required, validator)
        self.assertIn("window.crypto.subtle.digest('SHA-256'", self.html)

        for required in (
            "uniqueKinds.size !== kinds.length",
            "new Set(sourceOrders).size !== sourceOrders.length",
            "value <= sourceOrders[index - 1]",
            "value < checkpointDates[index - 1]",
            "uniqueKinds.has('evidence') && brief.fallback_evidence !== null",
            "captured[key] !== Boolean(features[key])",
            "brief.checkpoints.length !== Number(features.checkpoint_count || 0)",
        ):
            self.assertIn(required, validator)

        loader_end = self.html.index("\nconst ARTICLE_BY_ID", loader_start)
        loader = self.html[loader_start:loader_end]
        validation_call = loader.index('return validateDeferredBriefArchive(payload)')
        install = loader.index('article.brief = validatedBriefs[id]')
        self.assertLess(validation_call, install)
        self.assertIn("if (!Object.prototype.hasOwnProperty.call(briefs,article.id))", loader)
        ensure_start = loader.index('function ensureArticleBrief(article)')
        ensure = loader[ensure_start:]
        self.assertNotIn("{lead:null,sections:[],fallback_evidence:null,checkpoints:[]}", ensure)

    def test_client_article_briefs_retain_exact_source_span_provenance(self):
        """Every workbench passage must retain its validated source identity."""
        deferred = self.brief_archive['briefs']
        span_count = 0
        expected_span_count = 0
        for article in self.source_content_articles:
            brief = article['brief']
            expected_span_count += sum(
                value is not None
                for value in (brief.get('lead'), brief.get('fallback_evidence'))
            )
            expected_span_count += len(brief.get('sections') or [])
            expected_span_count += len(brief.get('checkpoints') or [])
        for article in self.articles:
            brief = article['brief'] if article['brief'] is not None else deferred[article['id']]
            spans = [brief.get('lead'), brief.get('fallback_evidence')]
            spans.extend(brief.get('sections') or [])
            spans.extend(brief.get('checkpoints') or [])
            for span in (value for value in spans if value is not None):
                span_count += 1
                self.assertTrue(
                    {'text', 'truncated', 'start', 'end', 'sha256'} <= set(span),
                    'a client brief span lost exact provenance fields',
                )
                self.assertIs(type(span['start']), int)
                self.assertIs(type(span['end']), int)
                self.assertGreaterEqual(span['start'], 0)
                self.assertGreater(span['end'], span['start'])
                self.assertEqual(span['end'] - span['start'], len(span['text']))
                self.assertEqual(
                    span['sha256'],
                    hashlib.sha256(span['text'].encode('utf-8')).hexdigest(),
                )
        self.assertGreater(span_count, 0)
        self.assertEqual(
            span_count,
            expected_span_count,
            'the generated release must retain every source brief span',
        )

        provenance_start = self.html.index('function spanProvenance(span)')
        provenance_end = self.html.index('\nfunction evidenceLedgerMarkup', provenance_start)
        provenance = self.html[provenance_start:provenance_end]
        for text in ('span.start', 'span.end', 'span.sha256', 'Exact source span', 'shortened for display'):
            self.assertIn(text, provenance)
        self.assertIn('spanProvenance(row.span)', self.html)
        self.assertIn('spanProvenance(checkpoint)', self.html)

    def test_evidence_ledger_keeps_reported_numbers_attached_to_authored_context(self):
        number_start = self.html.index('function numberTokenRegex()')
        number_end = self.html.index('\nfunction articleBriefSpans', number_start)
        number_logic = self.html[number_start:number_end]
        for token_family in (
            '[$€£¥]', 'basis points?', 'million', 'billion',
            'sharpe|sortino|rmse', '-\\s*to\\s*-', '[–—-]', '.slice(0,10)',
            'const seen = new Set()',
        ):
            self.assertIn(token_family, number_logic)
        self.assertIn('escapeHtml(text.slice(cursor,match.index))', number_logic)
        self.assertIn("'<mark>' + escapeHtml(match[0]) + '</mark>'", number_logic)
        self.assertNotIn("escapeHtml(value).replace", number_logic)

        ledger_start = self.html.index('function articleEvidenceLedger(article)')
        ledger_end = self.html.index('\nfunction ', ledger_start + len('function articleEvidenceLedger(article)'))
        ledger_logic = self.html[ledger_start:ledger_end]
        self.assertIn('articleBriefSpans(article)', ledger_logic)
        self.assertIn('extractNumberTokens(row.span.text)', ledger_logic)
        self.assertIn('row.values.length', ledger_logic)
        self.assertNotIn('idea.quant', ledger_logic)
        self.assertNotIn('direction', ledger_logic)

        spans_start = self.html.index('function articleBriefSpans(article)')
        spans_end = self.html.index('\nfunction articleEvidenceLedger', spans_start)
        spans = self.html[spans_start:spans_end]
        for text in ('const byIdentity = new Map()', 'span.sha256', 'row.kinds', 'row.labels'):
            self.assertIn(text, spans)

        markup_start = self.html.index('function evidenceLedgerMarkup(article)')
        markup_end = self.html.index('\nfunction researchMapMarkup', markup_start)
        markup = self.html[markup_start:markup_end]
        for text in (
            'Detected numbers with their authored context',
            'Exact number-bearing source passages',
            'Research role',
            'Detected numeric tokens · max 10',
            'Exact authored context',
            'row.span.text',
            'spanProvenance(row.span)',
            'Detection is lexical, deduplicated, and capped at ten unique tokens per passage',
            'not normalized, made comparable, or independently verified',
            'unique source span',
            'This is an extraction boundary, not a claim that the article contains no quantitative evidence.',
        ):
            self.assertIn(text, markup)

    def test_institutional_diligence_map_distinguishes_capture_from_quality(self):
        sequence_match = re.search(
            r'const BRIEF_SEQUENCE\s*=\s*\[(.*?)\];',
            self.html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(sequence_match)
        sequence = sequence_match.group(1)
        for key in ('lead', 'evidence', 'mechanism', 'countercase', 'falsifier', 'implementation'):
            self.assertIn("'" + key + "'", sequence)

        start = self.html.index('function researchMapMarkup(article)')
        end = self.html.index('\nfunction archiveCoverageMarkup', start)
        workbench_map = self.html[start:end]
        for text in (
            "BRIEF_SEQUENCE.concat([['checkpoint','Public checkpoint']])",
            'articleBriefSpans(article)',
            'Exact passage captured',
            'Not identified by rules',
            'research-map-step captured',
            'research-map-step not-captured',
            'Presence means an exact authored passage was captured',
            'not that the argument is correct, complete, investable, or independently verified',
        ):
            self.assertIn(text, workbench_map)

    def test_brief_navigation_targets_real_sections_and_shows_release_time(self):
        map_start = self.html.index('function researchMapMarkup(article)')
        map_end = self.html.index('\nfunction archiveCoverageMarkup', map_start)
        workbench_map = self.html[map_start:map_end]
        for mapping in (
            "lead:'brief-thesis'",
            "evidence:'brief-analysis'",
            "mechanism:'brief-analysis'",
            "checkpoint:'brief-checkpoints'",
        ):
            self.assertIn(mapping, workbench_map)

        briefing_start = self.html.index('function renderIntelligenceBrief(records)')
        briefing_end = self.html.index('\nfunction contextualRecords', briefing_start)
        briefing = self.html[briefing_start:briefing_end]
        for target in (
            'id="brief-thesis"',
            'id="brief-key-evidence"',
            'id="brief-analysis"',
            'id="brief-dossier"',
            'id="brief-evidence-ledger"',
            'id="brief-checkpoints"',
            'id="brief-archive"',
        ):
            self.assertIn(target, self.html)
        self.assertIn('briefRailMarkup(lenses,selected)', briefing)
        self.assertIn('Dataset assembled <time datetime=', briefing)
        self.assertIn('formatReleaseCheckedAt(SNAPSHOT.checked_at)', briefing)
        self.assertIn('sourceCollectionSummary(selected.source)', briefing)
        self.assertIn("sourceRelease.status === 'degraded' ? ' degraded'", briefing)
        self.assertIn(
            "complete_api_degraded_body_provenance:"
            "'Complete catalogue · body provenance limited'",
            self.html,
        )
        self.assertIn("cached_archive_plus_rss:'Cached archive + RSS'", self.html)
        self.assertIn(
            "validated_history_plus_current_rss:"
            "'Validated history + current RSS'",
            self.html,
        )
        self.assertIn(
            "cached_history_plus_rss_unverified_gap:"
            "'Cached history + unverified RSS gap'",
            self.html,
        )
        self.assertIn(
            "trusted_history_rss_gap_quarantined:"
            "'Trusted history · RSS gap quarantined'",
            self.html,
        )
        self.assertIn(
            "operator_reviewed_profile_bridge_plus_current_rss:"
            "'Operator-reviewed profile bridge + current RSS'",
            self.html,
        )
        self.assertIn("statusLabels = {ok:'OK',degraded:'Degraded',error:'Unavailable'}", self.html)
        self.assertIn("return iso.slice(0,10) + ' ' + iso.slice(11,16) + ' UTC';", self.html)

    def test_lens_coverage_bars_are_counts_not_quality_scores(self):
        start = self.html.index('function archiveCoverageMarkup(records)')
        end = self.html.index('\nfunction relatedArticleRows', start)
        coverage = self.html[start:end]
        for label in ('Contextual evidence', 'Mechanism', 'Countercase', 'Falsifier', 'Implementation', 'Checkpoint'):
            self.assertIn("['" + label + "'", coverage)
        for text in (
            'const denominator = records.length || 1',
            'Math.round(count / denominator * 100)',
            'Math.max(1,percent)',
            "row[0] + ': ' + count + ' of ' + records.length + ' articles'",
            'Captured section coverage',
            'High-precision section presence only; not research quality, confidence, or a recommendation score.',
        ):
            self.assertIn(text, coverage)
        self.assertNotIn('documentation_score', coverage)

    def test_related_archive_context_explains_only_exact_metadata_overlap(self):
        start = self.html.index('function relatedArticleRows(selected)')
        end = self.html.index('\nfunction relatedPremiumRows', start)
        related = self.html[start:end]
        for text in (
            'selected.manager_keys',
            'selected.underlyings',
            'selected.instruments',
            'Same organization or person:',
            'Same extracted underlying:',
            'qualified:Boolean(managers.length || underlyings.length)',
            'Exact entity or underlying overlap',
            'No direct overlap found',
        ):
            self.assertIn(text, related)
        self.assertNotIn('selected.directions', related)
        self.assertNotIn('Same parsed structure:', related)
        self.assertNotIn('Same market:', related)
        self.assertNotIn('semantic', related.casefold())
        self.assertNotIn('confidence', related.casefold())

    def test_related_subscriber_research_is_exact_context_not_personalization(self):
        start = self.html.index('function relatedPremiumRows(selected)')
        end = self.html.index('\nfunction articleReasons', start)
        related = self.html[start:end]
        for text in (
            'selected.manager_keys',
            'selected.underlyings',
            'selected.instruments',
            'THREAD_ARTICLES[selected.id]',
            'Same research topic:',
            'Same organization or person:',
            'Same extracted underlying:',
            'Same market:',
            'shared indexed field',
            'does not imply a recommendation, position, or similar conclusion',
        ):
            self.assertIn(text, related)
        self.assertIn('isPaidSubstackArticle(article)', related)
        self.assertIn('.slice(0,3)', related)
        self.assertNotIn('localStorage', related)
        self.assertNotIn('fetch(', related)
        self.assertNotIn('utm_', related.casefold())

    def test_institutional_brief_can_be_copied_and_printed_with_provenance(self):
        start = self.html.index('function articleBriefText(article)')
        end = self.html.index('\nfunction intelligenceCard', start)
        brief_text = self.html[start:end]
        for text in (
            'article.title',
            "'Source: ' + article.url",
            "'Dataset: ' + String(SNAPSHOT.data_checksum",
            'spanProvenance(row.span)',
            'spanProvenance(checkpoint)',
            'exact published-source passages; not independently verified',
        ):
            self.assertIn(text, brief_text)

        self.assertIn('data-copy-brief="', self.html)
        self.assertIn('data-print-brief', self.html)
        self.assertIn('Copy article record', self.html)
        self.assertIn('Print / PDF', self.html)
        self.assertRegex(
            self.html,
            re.compile(
                r"ARTICLE_BY_ID\.get\(copyBrief\.dataset\.copyBrief\).*?"
                r"copyText\(articleBriefText\(article\),'Article record copied with source provenance'\)",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            self.html,
            re.compile(
                r"event\.target\.closest\('\[data-print-brief\]'\).*?window\.print\(\)",
                re.DOTALL,
            ),
        )
        print_start = self.html.index('@media print{')
        print_end = self.html.index('@media(prefers-reduced-motion', print_start)
        print_css = self.html[print_start:print_end]
        self.assertIn('.intel-passage{display:block;overflow:visible;-webkit-line-clamp:unset}', print_css)
        self.assertIn('.app-header,.kpi-strip,.filter-rail,.ic-rail,.command-bar', print_css)

    def test_checkpoint_status_uses_snapshot_check_date_not_viewer_clock(self):
        start = self.html.index('function renderIntelligenceBrief(records)')
        end = self.html.index('\nfunction contextualRecords', start)
        briefing = self.html[start:end]
        self.assertIn("const checkedDate = String(SNAPSHOT.checked_at || '').slice(0,10) || MAX_DATE", briefing)
        self.assertIn("checkpoint.date < checkedDate", briefing)
        self.assertIn('Cited date passed · verification due', briefing)
        self.assertIn('Upcoming cited date', briefing)
        self.assertIn('Status is measured against the dataset check date.', briefing)
        self.assertNotIn('Date.now()', briefing)
        self.assertNotIn('new Date().toISOString()', briefing)

    def test_excerpt_boundaries_never_claim_missing_full_article_evidence(self):
        start = self.html.index('function renderArticleInspector(article)')
        end = self.html.index("\nlet renderedInspectorKey = ''", start)
        inspector = self.html[start:end]
        boundary_start = inspector.index('const gaps = [];')
        boundary_end = inspector.index("\n  if (structures.size > 1)", boundary_start)
        boundary_logic = inspector[boundary_start:boundary_end]
        excerpt_start = boundary_logic.index("if (article.content_status === 'excerpt') {")
        full_start = boundary_logic.index('} else {', excerpt_start)
        excerpt_branch = boundary_logic[excerpt_start:full_start]
        full_branch = boundary_logic[full_start:]

        self.assertIn('not assessable', excerpt_branch)
        self.assertIn('absence cannot be inferred', excerpt_branch)
        self.assertIn("!articleEvidence(article)", excerpt_branch)
        self.assertIn("!articleHasBriefKind(article,'countercase')", excerpt_branch)
        self.assertNotIn('No contextual evidence passage', excerpt_branch)
        self.assertNotIn('No explicit countercase', excerpt_branch)

        self.assertIn('No contextual evidence passage', full_branch)
        self.assertIn('No explicit countercase or falsifier section', full_branch)
        self.assertIn('not proof of absence', full_branch)

    def test_body_revision_provenance_is_visible_exported_and_review_flagged(self):
        source_by_url = {
            article['url'].rstrip('/'): article
            for article in self.source_content_articles
        }
        for article in self.articles:
            source = source_by_url[article['url'].rstrip('/')]
            for field in (
                'body_revision_status',
                'source_updated_at',
                'observed_source_updated_at',
            ):
                self.assertEqual(article[field], source[field])

        for text in (
            'function bodyRevisionLabel(article)',
            'function bodyRevisionSummary(article)',
            'function bodyRevisionWarningMarkup(article)',
            'Prior revision capture',
            'Cached excerpt retained for search',
            "idea._article.body_revision_status !== 'current'",
            "'prior-revision'",
            "'unverified-revision'",
            "'Body revision'",
            "'Body source revision'",
            "'Observed source revision'",
            "'Body revision: ' + bodyRevisionSummary(article)",
            "bodyRevisionSummary(article) + ' ' + article.url",
            'data-filter="revision" data-value="current"',
            'data-filter="revision" data-value="prior"',
            'data-filter="revision" data-value="unverified"',
            "state.revisions = setFromParam(params,'revision',VALID_BODY_REVISIONS)",
            "params.set('revision',Array.from(state.revisions).join('|'))",
            "if (facet === 'revision') return [record.body_revision_status]",
            "if (facet === 'revision') return [record._article.body_revision_status]",
            "['source','revision','access','direction','instrument','manager','quality','content']",
            "['source',state.sources],['revision',state.revisions]",
            'source:state.sources,revision:state.revisions',
            'state.revisions.clear()',
            "skip !== 'revision' && state.revisions.size",
            'id="kpi-provenance-articles"',
            'id="kpi-provenance-observations"',
            '0 current · 0 prior · 0 unverified',
            "number(provenanceArticles.current) + ' current · '",
            "number(provenanceObservations.unverified) + ' unverified observations'",
        ):
            self.assertIn(text, self.html)
        self.assertGreaterEqual(
            self.html.count('source:state.sources,revision:state.revisions'),
            4,
        )
        self.assertGreaterEqual(self.html.count('state.revisions.clear()'), 2)

        triage_start = self.html.index('function documentationMatches(idea)')
        triage_end = self.html.index('\nlet workflowStorageDirty', triage_start)
        triage = self.html[triage_start:triage_end]
        self.assertIn('!reviewFlagged(idea)', triage)
        self.assertIn("idea._article.content_status === 'full'", triage)

    def test_publication_access_and_subscriber_conversion_are_source_exact(self):
        source_by_url = {
            article['url'].rstrip('/'): article
            for article in self.source_content_articles
        }
        expected_counts = Counter()
        actual_counts = Counter()
        for article in self.articles:
            source = source_by_url[article['url'].rstrip('/')]
            audience = str(source.get('audience') or '').strip().casefold()
            if source['source'] == 'substack' and audience == 'only_paid':
                expected = 'member'
            elif source['source'] == 'substack' and audience == 'everyone':
                expected = 'public'
            elif source['source'] == 'medium' and audience == 'locked':
                expected = 'member'
            elif source['source'] == 'medium' and audience == 'public':
                expected = 'public'
            else:
                expected = 'unknown'
            self.assertEqual(article['publication_access'], expected)
            expected_preview_chars = (
                source.get('member_preview', {}).get('character_count', 0)
                if isinstance(source.get('member_preview'), dict)
                else 0
            )
            self.assertEqual(
                article['member_preview_chars'], expected_preview_chars
            )
            expected_counts[(article['source'], expected)] += 1
            actual_counts[(article['source'], article['publication_access'])] += 1
        self.assertEqual(actual_counts, expected_counts)
        self.assertGreater(expected_counts[('substack', 'member')], 0)
        self.assertGreater(expected_counts[('medium', 'member')], 0)
        self.assertGreater(expected_counts[('substack', 'public')], 0)

        for text in (
            'Publication access',
            'Indexed coverage',
            'data-filter="access" data-value="public"',
            'data-filter="access" data-value="member"',
            'data-filter="access" data-value="unknown"',
            "state.publicationAccess = setFromParam(params,'access',VALID_PUBLICATION_ACCESS)",
            "params.set('access',Array.from(state.publicationAccess).join('|'))",
            "if (facet === 'access') return [record.publication_access]",
            "if (facet === 'access') return [record._article.publication_access]",
            'Subscriber source · public preview indexed',
            'Subscriber source · metadata only · no anonymous body preview',
            'Unlock the full research',
            'Read full note on Substack ↗',
            'Get full research access ↗',
            'Already subscribed? Read the note ↗',
            'Related subscriber research',
            'Pricing and terms are shown on Substack.',
            'This archive sends no search, filter, or local-review data.',
            'https://www.navnoorbawaresearch.com/subscribe',
            'rel="noopener noreferrer"',
        ):
            self.assertIn(text, self.html)

        promotion_start = self.html.index('function premiumAccessMarkup(article,context)')
        promotion_end = self.html.index('\nfunction articleEvidence(article)', promotion_start)
        promotion = self.html[promotion_start:promotion_end]
        self.assertIn('if (!isPaidSubstackArticle(article)) return', promotion)
        self.assertIn('safeUrl(article.url)', promotion)
        self.assertIn('SUBSCRIPTION_URL', promotion)
        self.assertIn('hasIndexedMemberPreview(article)', promotion)
        self.assertIn('Published metadata', promotion)
        self.assertLess(
            promotion.index('SUBSCRIPTION_URL'),
            promotion.index('safeUrl(article.url)'),
            'the buying action should precede the returning-subscriber action',
        )
        self.assertNotIn('utm_', promotion.lower())
        self.assertNotIn('trial', promotion.lower().split('review current price', 1)[0])
        self.assertIn(
            "state.view === 'briefing' ? '' : "
            "premiumAccessMarkup(article,'article')",
            self.html,
        )
        self.assertIn(
            "state.view === 'briefing' || isPaidSubstackArticle(article)",
            self.html,
        )

    def test_new_since_review_requires_an_explicit_acknowledgement(self):
        initialization_start = self.html.index('let reviewedArticleIds = new Set()')
        storage_commit_start = self.html.index(
            'function commitReviewBaselineStorage(ids,at)', initialization_start
        )
        initialization = self.html[initialization_start:storage_commit_start]
        self.assertIn('localStorage.getItem(REVIEWED_ARTICLE_IDS_KEY)', initialization)
        self.assertIn('localStorage.getItem(LEGACY_LAST_SEEN_KEY)', initialization)
        self.assertIn('reviewedArticleIds = new Set(ARTICLES.filter', initialization)
        self.assertNotIn(
            'localStorage.setItem(REVIEWED_ARTICLE_IDS_KEY',
            initialization,
            'loading or rendering the terminal must not silently acknowledge new research',
        )

        acknowledgement_end = self.html.index('\nfunction downloadLocalFile', storage_commit_start)
        acknowledgement = self.html[storage_commit_start:acknowledgement_end]
        self.assertIn('ARTICLES.map(function (article) { return article.id; })', acknowledgement)
        self.assertIn('localStorage.setItem(REVIEWED_ARTICLE_IDS_KEY,JSON.stringify(ids))', acknowledgement)
        self.assertIn('localStorage.setItem(REVIEWED_AT_KEY,at)', acknowledgement)
        self.assertIn('if (priorCaptured)', acknowledgement)
        self.assertIn('localStorage.setItem(REVIEWED_ARTICLE_IDS_KEY,priorIds)', acknowledgement)
        storage_commit_end = acknowledgement.index('\nfunction renderCommittedReviewBaseline')
        self.assertNotIn('render()', acknowledgement[:storage_commit_end])
        self.assertIn('if (!commitReviewBaselineStorage(currentIds,nextAt))', acknowledgement)
        self.assertLess(
            acknowledgement.index('if (!commitReviewBaselineStorage(currentIds,nextAt))'),
            acknowledgement.index('reviewedArticleIds = new Set(currentIds)'),
        )
        self.assertIn('function undoReviewedThroughLatest()', acknowledgement)
        self.assertIn(
            'if (!commitReviewBaselineStorage(target.exists ? target.ids : null,target.at))',
            acknowledgement,
        )
        self.assertIn('function renderCommittedReviewBaseline(successMessage)', acknowledgement)
        self.assertIn('reviewedArticleIds = new Set(currentIds)', acknowledgement)
        new_helper_start = self.html.index('function isNewArticle(article)')
        new_helper_end = self.html.index('\nfunction reviewFlagged', new_helper_start)
        new_helper = self.html[new_helper_start:new_helper_end]
        self.assertIn('!reviewedArticleIds.has(article.id)', new_helper)
        self.assertIn('reviewBaselineExists', new_helper)
        active_filters_start = self.html.index('function renderActiveFilters()')
        active_filters_end = self.html.index('\nfunction setPressedStates()', active_filters_start)
        active_filters = self.html[active_filters_start:active_filters_end]
        self.assertIn(
            "const newFilterLabel = reviewBaselineExists ? 'New since last review' : 'Recent · 7 days';",
            active_filters,
        )
        self.assertIn("newPreset.textContent = newFilterLabel", active_filters)
        self.assertIn("escapeHtml(newFilterLabel)", active_filters)
        self.assertIn('data-preset="new">Recent · 7 days</button>', self.html)
        self.assertNotIn('article.date > NEW_SINCE_DATE', self.html)
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

    def test_research_tasks_v3_are_human_authored_tab_scoped_and_portable(self):
        for text in (
            "const WORKFLOW_KEY = 'nrt-decision-queue-session-v3'",
            "const RESTORE_ROLLBACK_KEY = 'nrt-decision-queue-restore-rollback-v1'",
            "const LEGACY_LOCAL_WORKFLOW_KEYS = ['nrt-decision-queue-v2','nrt-decision-queue-v1','nrt-saved-ideas']",
            "new Set(['review','diligence','monitor','archived'])",
            "new Set(['low','normal','high'])",
            'const MAX_QUEUE_ITEMS = 250',
            'data-workflow-select="status"',
            'data-workflow-select="priority"',
            'data-workflow-field="owner"',
            'data-workflow-field="review_date"',
            'data-workflow-field="next_action"',
            'data-workflow-field="thesis"',
            'data-workflow-field="contrary"',
            'data-workflow-field="independent_source"',
            'data-workflow-field="numeric_source"',
            'data-workflow-field="catalyst"',
            'data-workflow-field="horizon"',
            'data-workflow-field="falsifier"',
            'data-workflow-field="tags"',
            'data-workflow-field="note"',
            'function backupQueue()',
            'function restoreQueueFile(file)',
            'data_checksum:String(SNAPSHOT.data_checksum',
            'source_snapshot:sourceSnapshotForIdea(id,legacyBookmarkMigration)',
            'review_flags_verified:true',
            'Review flags unavailable at capture',
            'legacy_bookmark_migration',
            'Legacy ID-only bookmark',
            'Retained source conflicts',
            'current observation absent',
            'current comparison unavailable',
            'Retained source snapshots',
            'Passage snapshot unavailable',
            'cloneWorkflowMap(workflowItems)',
            'undoLastQueueRestore()',
            'plaintext storage scoped to this browser tab session',
            'data-action="clear-queue"',
            'data-action="backup-raw-storage"',
            'data-action="clear-unreadable-storage"',
            'workflowLoadBlocked',
            'workflowStorageDirty',
            'legacyCleanupPending',
            'lastPersistedWorkflow',
            'window.addEventListener(\'beforeunload\'',
            'Automatic clipboard access was blocked. The complete text is preserved below',
            'Copy local review',
            'Copy retained citation',
            'function retainedSourceCitation(item)',
            'function selectedOpenUrl()',
            'Archive item',
            'Return to new',
            'Stored only in this tab session unless exported',
            'Not an enterprise audit record',
            'Do not enter confidential',
        ):
            self.assertIn(text, self.html)
        self.assertRegex(self.html, r'id="queue-restore-input"[^>]*accept="application/json,\.json"')
        self.assertRegex(self.html, r'schema_version\s*:\s*3')
        self.assertRegex(self.html, r'!\[1,2,3\]\.includes\(payload\.schema_version\)')
        self.assertRegex(self.html, r'payload\.items\.slice\(\s*0\s*,\s*MAX_QUEUE_ITEMS\s*\)')
        self.assertIn("if (!/^[A-Za-z0-9_-]{1,100}$/.test(id)) return null", self.html)
        self.assertRegex(self.html, r'item\[field\]\s*=\s*String\(value\[field\]\s*\|\|\s*[\'\"]{2}\)\.slice\(0,WORKFLOW_TEXT_LIMITS\[field\]\)')
        self.assertIn('note:4000', self.html)
        self.assertIn('tags:500', self.html)
        self.assertIn("parsed.toISOString().slice(0,10) === text", self.html)
        self.assertIn('sessionStorage.setItem(WORKFLOW_KEY,serialized)', self.html)
        self.assertNotIn('localStorage.setItem(WORKFLOW_KEY,serialized)', self.html)
        persist_start = self.html.index('function persistWorkflow()')
        persist_end = self.html.index('\n\nARTICLES.forEach', persist_start)
        persist = self.html[persist_start:persist_end]
        self.assertIn('if (legacyCleanupPending)', persist)
        self.assertIn('clearLegacyLocalWorkflowKeys()', persist)
        self.assertIn('legacyCleanupPending = false', persist)

        for gate_key, label in (
            ('context_reviewed', 'Surrounding publication context reviewed'),
            ('public_source_recorded', 'Independent public source recorded'),
            ('numeric_traced', 'Key numeric phrase traced to its cited context'),
            ('contrary_recorded', 'Contrary evidence or alternative explanation recorded'),
            ('falsifier_recorded', 'Falsifier or observable checkpoint recorded'),
            ('claims_scope_reviewed', 'Local-review item checked for unsupported claims and non-confidential scope'),
        ):
            self.assertIn("['" + gate_key + "','" + label + "']", self.html)
        gates_start = self.html.index('const DILIGENCE_GATES = [')
        gates_end = self.html.index('\n];', gates_start)
        gates = self.html[gates_start:gates_end]
        for retired_gate in ("['source'", "['independent'", "['market'", "['liquidity'", "['portfolio'", "['compliance'"):
            self.assertNotIn(retired_gate, gates)
        limits_start = self.html.index('const WORKFLOW_TEXT_LIMITS = {')
        limits_end = self.html.index('\n};', limits_start)
        workflow_limits = self.html[limits_start:limits_end]
        normalization_start = self.html.index('function normalizeWorkflowItem(value)')
        normalization_end = self.html.index('\nfunction newWorkflowItem', normalization_start)
        normalization = self.html[normalization_start:normalization_end]
        for forbidden in ('confidence', 'payoff', 'implementation', 'portfolio', 'risk'):
            self.assertNotIn(forbidden, workflow_limits)
            self.assertNotIn(forbidden + ':', normalization)
        self.assertNotIn('VALID_CONFIDENCE', self.html)
        serialization_start = self.html.index('function workflowSerialization()')
        serialization_end = self.html.index('\nfunction clearLegacyLocalWorkflowKeys', serialization_start)
        self.assertIn('normalizeWorkflowItem(value)', self.html[serialization_start:serialization_end])
        backup_start = self.html.index('function backupQueue()')
        backup_end = self.html.index('\nfunction cloneWorkflowMap', backup_start)
        self.assertIn('normalizeWorkflowItem(value)', self.html[backup_start:backup_end])
        install_start = self.html.index('function installStoredWorkflow(raw)')
        install_end = self.html.index('\nfunction migrateLegacySavedIdeas', install_start)
        install = self.html[install_start:install_end]
        self.assertIn('if (serialized !== sessionRaw) sessionStorage.setItem(WORKFLOW_KEY,serialized)', install)
        self.assertIn('if (canonicalRollback !== rollbackRaw) sessionStorage.setItem(RESTORE_ROLLBACK_KEY,canonicalRollback)', install)

        export_start = self.html.index('function exportCsv()')
        queue_export_start = self.html.index("} else if (state.view === 'queue') {", export_start)
        queue_export_end = self.html.index('\n  } else {', queue_export_start)
        queue_export = self.html[queue_export_start:queue_export_end]
        for text in (
            'workflow && workflow.source_snapshot',
            'source ? source.passage',
            'source ? source.title',
            'source ? source.data_checksum',
            'retainedSourceSnapshotComparison(workflow,idea)',
            'Current-release differences',
        ):
            self.assertIn(text, queue_export)
        for mutable_current_field in ('passageText(idea)', 'idea.thesis', 'idea.quant', 'idea.outcome', 'article.title'):
            self.assertNotIn(mutable_current_field, queue_export)

        toggle_start = self.html.index('function toggleSaved(id)')
        toggle_end = self.html.index('\nfunction csvCell(value)', toggle_start)
        toggle = self.html[toggle_start:toggle_end]
        self.assertIn("previous.status === 'archived' ? 'review' : 'archived'", toggle)

        inspector_start = self.html.index('function renderIdeaInspector(idea)')
        inspector_end = self.html.index('\nfunction renderArticleInspector', inspector_start)
        inspector = self.html[inspector_start:inspector_end]
        for text in (
            'Local review item',
            'Human-authored',
            'Field presence never becomes a readiness score.',
            'retainedSourceSnapshotMarkup(workflow,idea)',
            'Human research attestations',
            'Attested ',
            'No completion or confidence score is calculated.',
            "if (state.view === 'queue' && workflow && workflow.source_snapshot)",
            'RETAINED LOCAL-REVIEW SOURCE',
            'Current-release data is not substituted into the item',
        ):
            self.assertIn(text, inspector)
        for forbidden in (
            'data-workflow-select="confidence"',
            'data-workflow-field="payoff"',
            'data-workflow-field="implementation"',
            'data-workflow-field="portfolio"',
            'Packet coverage',
            '/18',
        ):
            self.assertNotIn(forbidden, inspector)

        self.assertIn('body[data-view="queue"] .current-extraction-filter{display:none}', self.html)
        self.assertIn('function normalizeQueueFacets()', self.html)
        self.assertIn('state.managers.clear()', self.html[self.html.index('function normalizeQueueFacets()'):self.html.index('\nfunction render()', self.html.index('function normalizeQueueFacets()'))])
        self.assertIn("if (state.view === 'queue')", self.html[self.html.index('function recordValues(record, facet)'):self.html.index('\nfunction updateFacetCounts', self.html.index('function recordValues(record, facet)'))])
        observation_need_start = self.html.index('function currentStateNeedsObservations()')
        observation_need_end = self.html.index('\nfunction queueObservationResultFocus', observation_need_start)
        self.assertIn("if (state.view === 'queue') return false", self.html[observation_need_start:observation_need_end])
        self.assertIn('function requestQueueComparisonArchive()', self.html)
        self.assertIn("state.view !== 'queue' && !isArticleView() && !observationsReady", self.html)

        queue_records_start = self.html.index('function queueRecordForWorkflow(item)')
        queue_records_end = self.html.index('\nfunction reviewIsOverdue', queue_records_start)
        queue_records = self.html[queue_records_start:queue_records_end]
        self.assertIn('function queueRecords()', queue_records)
        self.assertIn('Array.from(workflowItems.values()).map(queueRecordForWorkflow)', queue_records)
        self.assertIn('_retainedOnly:true', queue_records)

        retained_start = self.html.index('function retainedSourceSnapshotComparison(item,idea)')
        retained_end = self.html.index('\nfunction blankChecks()', retained_start)
        retained = self.html[retained_start:retained_end]
        for text in (
            'captured passage',
            'dataset revision',
            'Retained local-review source snapshot',
            'The current release differs in ',
            'copied and exported items preserve it',
            'rel="noopener noreferrer"',
        ):
            self.assertIn(text, retained)

        self.assertIn("document.addEventListener('focusout'", self.html)
        self.assertIn("window.addEventListener('pagehide'", self.html)
        self.assertIn('validTimestamp(value.updated_at)', self.html)
        self.assertIn("validTimestamp(value.check_times && value.check_times[row[0]])", self.html)
        self.assertIn('item.check_times[gate] = attestedAt', self.html)
        self.assertIn('item.updated_at > existing.updated_at', self.html)
        self.assertIn('The current Local Review set will be retained as a tab-scoped rollback across reloads.', self.html)
        self.assertIn('sessionStorage.setItem(\n          RESTORE_ROLLBACK_KEY', self.html)

    def test_institutional_methodology_links_and_operating_boundary_are_explicit(self):
        for text in (
            'Records are research observations—not verified trades, current holdings, or recommendations.',
            'does not contain live prices, positions, P&amp;L, sizing, execution, portfolio risk, liquidity, financing, counterparties, investor records, or compliance approvals.',
            'https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/investment-manager-selection',
            'https://www.aima.org/article/presenting-the-2025-edition.html',
            'https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a',
            'https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-c',
            'https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-adviser-marketing',
            'These references shape research questions, evidence retention, and disclosure boundaries; they do not certify this archive or establish legal compliance.',
            'Field presence never becomes a readiness score.',
            'No completion or confidence score is calculated.',
        ):
            self.assertIn(text, self.html)

    def test_structure_desk_is_the_landing_view(self):
        """The desk is what a reader lands on, so it must be the default
        everywhere the view is decided, and it must paint before the deferred
        evidence archive verifies."""
        self.assertRegex(self.html, r'<body[^>]*data-view="structure"')
        self.assertRegex(
            self.html, r"const state\s*=\s*\{\s*view:['\"]structure['\"]")
        self.assertIn(
            "<button class=\"view-tab active\" type=\"button\" data-view=\"structure\"",
            self.html,
        )

        # The acquisition surface exposes only the three destinations an owner
        # needs. The five internal routes remain stable for old links and
        # keyboard users.
        order = re.findall(
            r'data-view="([a-z]+)" aria-keyshortcuts="Alt\+Shift\+(\d)"', self.html)
        self.assertEqual(
            order,
            [('structure', '1'), ('research', '4'), ('queue', '5')],
        )
        nav_start = self.html.index('<nav class="view-tabs"')
        nav_end = self.html.index('</nav>', nav_start)
        primary_nav = self.html[nav_start:nav_end]
        self.assertEqual(
            len(re.findall(r'<button class="view-tab(?: active)?"', primary_nav)),
            3,
        )
        for label in ('>Home</button>', '>Research</button>', '>Review <span'):
            self.assertIn(label, primary_nav)
        self.assertNotIn('data-view="briefing"', primary_nav)
        self.assertNotIn('data-view="ideas"', primary_nav)
        self.assertIn(
            "state.view = viewNumber === '1' ? 'structure'", self.html)
        for leading in (
            "['structure','Passage Search'],['briefing','Article Record']",
            "['01','structure','Passage Search'],['02','briefing','Article Record']",
        ):
            self.assertIn(leading, self.html)
        self.assertIn(
            'Search / Record / Passages / Index / Review', self.html)
        self.assertIn(
            "].includes(hashView) ? hashView : 'structure';", self.html)
        self.assertIn(
            "if (state.view !== 'structure') params.set('view',state.view);",
            self.html,
        )
        self.assertIn(
            "['briefing','ideas','research'].includes(state.view)",
            self.html,
        )

        # Controls come from build-time counts so the landing view is never an
        # empty frame while the archive is still verifying.
        facets = json.loads(
            re.search(r'const DESK_FACETS = (\{.*?\});\n', self.html, re.S).group(1)
        )
        self.assertEqual(facets['observation_count'], len(self.ideas))
        self.assertEqual(
            facets['outcome_count'],
            sum(1 for idea in self.ideas if idea['outcome']),
        )
        self.assertEqual(
            facets['source_note_count'],
            len({idea['article_id'] for idea in self.ideas}),
        )
        desk_article_ids = {idea['article_id'] for idea in self.ideas}
        self.assertEqual(
            facets['full_current_note_count'],
            sum(
                1 for article in self.articles
                if article['id'] in desk_article_ids
                and article['content_status'] == 'full'
                and article['body_revision_status'] == 'current'
            ),
        )
        self.assertTrue(facets['instruments'])
        self.assertTrue(all(len(row) == 3 for row in facets['instruments']))
        self.assertTrue(all(row[2] <= row[1] for row in facets['instruments']))

        underlying_rows = {}
        for idea in self.ideas:
            seen = set()
            for raw_part in str(idea['underlying'] or '').split(';'):
                part = raw_part.strip()
                if not part or part in {'—', '-'}:
                    continue
                key = ' '.join(
                    unicodedata.normalize('NFKC', part).split()
                ).casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                row = underlying_rows.setdefault(
                    key,
                    {'label': part, 'count': 0, 'article_ids': set()},
                )
                row['count'] += 1
                row['article_ids'].add(idea['article_id'])
        expected_underlyings = sorted(
            [
                [row['label'], row['count'], len(row['article_ids'])]
                for row in underlying_rows.values()
                if row['count'] >= 2
            ],
            key=lambda row: (-row[2], -row[1], row[0].casefold()),
        )
        self.assertEqual(
            facets['underlyings'],
            expected_underlyings,
            'build-time facets must normalize and deduplicate like runtime facets',
        )
        self.assertIn('if (!IDEAS.length) return (DESK_FACETS.instruments || [])', self.html)

    def test_structure_desk_is_wired_as_a_first_class_view(self):
        for text in (
            'Passage Search',
            '<section class="structure-shell" id="structure-shell"',
            "'briefing','ideas','research','queue','structure'",
            'structure:8',
            "state.view === 'structure'",
            'renderStructureDesk(structureMatches())',
            'id="structure-question-input"',
            'id="structure-focus-input"',
            "structureChipRow('Instrument','structure-instrument'",
            "structureChipRow('Stance','structure-direction'",
            "structureChipRow('Period','structure-period'",
            'data-structure-focus="',
            'data-structure-passage="',
            'data-structure-more="1"',
            'Original markets research with the source trail attached.',
            'id="owner-search-input"',
            'data-owner-search-form',
            'Data health &amp; coverage',
            'Retrieved authored notes',
            'Local review handoff',
            'Related mentions — excluded from the evidence set',
            'Open local review item',
            'Parser candidates',
            'Optional macro provenance',
            'not timestamp-aligned and may post-date a same-day article',
            'const RATE_CONTEXT = ',
        ):
            self.assertIn(text, self.html)
        self.assertNotIn("structureChipRow('U.S. curve band','structure-slope'", self.html)
        self.assertNotIn("structureChipRow('U.S. 10Y band','structure-level'", self.html)

        # The desk must keep the terminal's table chrome out of the way and
        # bring its own layout, exactly as the briefing view does.
        for rule in (
            r'body\[data-view="structure"\] \.structure-shell\{display:block\}',
            r'body\[data-view="structure"\] \.main-panel\{grid-column:1/-1\}',
        ):
            self.assertRegex(self.html, rule)

    def test_a_structure_desk_view_can_be_shared_and_restored(self):
        """Desk questions stay local unless Copy view explicitly shares one."""
        hash_start = self.html.index('function updateHash(includeQuery)')
        hash_end = self.html.index('\nfunction ', hash_start + 10)
        writer = self.html[hash_start:hash_end]
        for pair in (
            "params.set('squestion',state.structureQuestion.slice(0,180))",
            "params.set('focus',state.structureFocus.slice(0,120))",
            "params.set('sinst',state.structureInstrument)",
            "params.set('sdir',state.structureDirection)",
            "params.set('speriod',state.structurePeriod)",
            "params.set('smacro','1')",
            "params.set('sanchor',state.structureAnchor)",
            "params.set('spassage',state.structurePassage)",
        ):
            self.assertIn(pair, writer)
        self.assertIn(
            "state.view === 'structure' && (includeQuery || state.structureShareable)",
            writer,
        )
        self.assertIn('const returnOnly = Boolean(arguments[1]);', writer)
        self.assertIn('if (returnOnly) return new URL(target,location.href).href;', writer)
        self.assertIn("if (state.structureMacro) {", writer)
        self.assertIn("const current = location.hash || location.pathname", writer)
        self.assertNotIn("params.set('sslope'", writer)
        self.assertNotIn("params.set('slevel'", writer)
        self.assertNotIn("if (state.view === 'structure') {", writer)

        for guard in (
            "state.structureQuestion = String(params.get('squestion') || '').slice(0,180)",
            "state.structureFocus = String(params.get('focus') || '').slice(0,120)",
            "VALID_INSTRUMENTS.has(params.get('sinst'))",
            "VALID_DIRECTIONS.has(params.get('sdir'))",
            "/^[0-9]{4}$/.test(String(params.get('speriod') || ''))",
            "state.structureMacro = params.get('smacro') === '1'",
            "ARTICLE_BY_ID.has(params.get('sanchor'))",
            "/^[A-Za-z0-9_-]{1,96}$/.test(String(params.get('spassage') || ''))",
            "if (!state.structureAnchor) state.structurePassage = ''",
        ):
            self.assertIn(guard, self.html)
        self.assertIn("const VALID_RATE_BANDS = new Set(['low','mid','high']);", self.html)
        self.assertNotIn("if (state.view === 'structure') state.structureShareable = true;", writer)
        self.assertIn("state.structureShareable = false;", self.html)
        self.assertIn('maxlength="120" spellcheck="false"', self.html)
        self.assertIn('maxlength="180" spellcheck="false"', self.html)
        self.assertIn("const value = input.value.slice(0,120);", self.html)
        self.assertIn('const shareUrl = updateHash(true,true);', self.html)

    def test_structure_desk_export_matches_what_the_desk_shows(self):
        """The export preserves the source-note unit used by the desk."""
        export_start = self.html.index('function exportCsv()')
        export_end = self.html.index('\nfunction applyPreset', export_start)
        export = self.html[export_start:export_end]
        self.assertIn(
            "state.view === 'structure'\n    ? structureArticleGroups(structureMatches())",
            export,
        )
        self.assertIn("if (state.view === 'structure') {", export)
        for column in (
            "'Retrieval order (same tier then publication date)'",
            "'Retrieval tier'", "'Captured passage count'",
            "'Directly matching passage count'", "'Context-only passage count'",
            "'Detected numeric phrases'", "'Parser candidates: thesis phrases'",
            "'Detected outcome / P&L phrases'",
            "'U.S. 10Y-2Y observation by publication date'", "'Curve as of'",
            "'Related later notes via article topic (not outcome)'",
        ):
            self.assertIn(column, export)
        self.assertIn('Array.from(group.reasons).join', export)
        self.assertIn('structureGroupFacts(group)', export)
        self.assertIn('observationFollowUps(firstIdea).length', export)
        self.assertIn('state.structureMacro ? rateReading(firstIdea) : null', export)
        self.assertIn("structurePassageDirectMatch(row.idea) ? 'direct match' : 'context only'", export)
        self.assertIn("const evidenceRows = group.tier === 'subject' ? directRows : group.rows", export)

    def test_research_task_csv_excludes_scores_and_sensitive_investment_fields(self):
        export_start = self.html.index('function exportCsv()')
        export_end = self.html.index('\nfunction applyPreset', export_start)
        export = self.html[export_start:export_end]
        task_start = export.index("} else if (state.view === 'queue') {")
        task_end = export.index('\n  } else {', task_start)
        task_export = export[task_start:task_end]
        for column in (
            'Retained source passage',
            'Retained dataset checksum',
            'Current-release differences',
            'Local review status',
            'Review owner',
            'Next review',
            'Next action',
            'Research hypothesis to test',
            'Contrary evidence',
            'Public catalyst / checkpoint',
            'Horizon',
            'Falsifier / observable checkpoint',
            'Independent public source recorded',
            'Unsupported-claims / non-confidential review',
            'Item updated',
        ):
            self.assertIn(column, task_export)
        for forbidden in (
            'Analyst confidence',
            'Packet coverage',
            'Expected / actual payoff',
            'Implementation / borrow / funding',
            'Portfolio fit / sizing',
            'Readiness score',
        ):
            self.assertNotIn(forbidden, task_export)

    def test_structure_desk_shows_the_evidence_gate_in_its_own_shell(self):
        """The desk hides the table, so the shared gate would leave it blank
        while the release-bound evidence archive is still verifying."""
        gate_start = self.html.index('function renderObservationGate()')
        gate_end = self.html.index('\nfunction ', gate_start + 10)
        gate = self.html[gate_start:gate_end]
        self.assertIn("} else if (state.view === 'structure') {", gate)
        self.assertIn('data-retry-observations', gate)
        self.assertIn('renderStructureDesk([], {title:title, copy:copy, action:action})', gate)
        # The desk keeps its controls while the archive verifies, so the
        # landing view is never an empty frame.
        self.assertIn('renderStructureDesk([], {title:title', gate)
        self.assertIn('structure-none', self.html)

    def test_the_desk_starts_empty_then_leads_with_a_clustered_answer(self):
        """No setup means no synthetic class; a defined setup leads with gaps."""
        render_start = self.html.index('function renderStructureDesk(rows, gate)')
        render_end = self.html.index('\nfunction render()', render_start)
        render = self.html[render_start:render_end]

        # An undefined setup gets a concise owner landing and returns before
        # the analyst controls are built. Only a submitted search opens the
        # full evidence workspace.
        home_guard = render.index('if (!gate && !defined) {')
        home_return = render.index('return;', home_guard)
        active_workspace = render.index('const sets = gate ?', home_return)
        self.assertLess(home_guard, home_return)
        self.assertLess(home_return, active_workspace)
        self.assertIn('deskLandingMarkup()', render)
        landing_start = self.html.index('function deskLandingMarkup()')
        landing_end = self.html.index('\nfunction renderStructureDesk', landing_start)
        landing = self.html[landing_start:landing_end]
        self.assertEqual(landing.count('<input'), 1)
        self.assertIn('Original markets research with the source trail attached.', landing)
        self.assertIn('Latest notes', landing)
        self.assertIn('data-owner-search-form', landing)
        self.assertIn('<details class="desk-source-panel">', landing)
        self.assertNotIn('structure-question-input', landing)
        self.assertNotIn('desk-starts', landing)
        self.assertIn("'<div class=\"structure-workbench\">' + comparablePanel + railPanel", render)
        self.assertIn('Verbatim evidence first', render)

        # Refinements are closed until asked for, and say so to assistive tech.
        self.assertIn(
            "(state.structureControlsOpen ? '' : ' hidden')", render)
        self.assertIn(
            "'aria-expanded=\"' + (state.structureControlsOpen ? 'true' : 'false')",
            render,
        )
        self.assertIn('structureControlsOpen:false,', self.html)

        # An active refinement is visible without opening the panel, and is
        # removable from where it is shown.
        self.assertIn('desk-active-filter', render)
        self.assertIn('desk-refine-count', render)
        self.assertIn("aria-label=\"Remove ' + escapeHtml(row[2])", render)

        # A shared link that carries refinements opens them, so a reader can
        # see what produced the view they were sent.
        self.assertIn('state.structureControlsOpen = Boolean(', self.html)

        # The handoff rail reports raw source counts and explicit human gaps;
        # it never turns field presence into a score.
        self.assertIn('Local review handoff', render)
        self.assertIn('no readiness score', render)
        self.assertIn('Evidence breadth', render)
        self.assertIn('Human research case', render)
        self.assertIn('Live investment controls', render)
        self.assertNotIn('desk-anchors', render)
        self.assertNotIn('Math.round', render)

        # Corpus telemetry is engineering assurance, not decision information,
        # so the desk does not spend a reader's attention on it.
        self.assertRegex(
            self.html, r'body\[data-view="structure"\] \.kpi-strip')

        # Recurring underlyings remain available after a reader enters the
        # evidence workspace, without crowding the acquisition landing.
        self.assertIn('desk-starts', render)
        self.assertIn('class="desk-start', render)
        self.assertIn("countLabel(row.notes,'authored note')", render)
        self.assertIn("countLabel(row.count,'passage')", render)
        for rule in (
            r'\.desk-refine-toggle\[aria-expanded="true"\]',
            r'\.structure-controls\[hidden\]\{display:none\}',
        ):
            self.assertRegex(self.html, rule)

    def test_structure_desk_states_the_limits_of_the_extracted_record(self):
        """The desk must not let a reader infer outcomes the record lacks."""
        outcomes = [idea for idea in self.ideas if idea['outcome']]
        self.assertLess(
            len(outcomes), len(self.ideas),
            'this test assumes outcomes are not universally recorded',
        )
        for text in (
            'Holdings, prices, valuation, liquidity, funding, portfolio constraints, compliance, and execution are outside this static desk.',
            'Instrument fields are lexical source mentions, not validated legs.',
            'Related subsequent notes are article-topic links, not outcomes.',
            'passages contain a detected outcome / P&amp;L phrase requiring source review.',
            'No selected authored note contains a detected outcome / P&L phrase.',
            'This one-author, purposive archive is not independent corroboration or a performance base rate.',
            'Optional publication-date provenance only.',
            'fixed cut points from the complete',
            'official observation dated on or before the publication calendar date.',
            'not timestamp-aligned and may post-date a same-day article',
            'U.S. Treasury Daily Treasury Par Yield Curve Rates',
        ):
            self.assertIn(text, self.html)

        # Rate conditions must be a published reading for every observation the
        # desk ranks, and must carry the close that produced them.
        rate_context = json.loads(
            re.search(r'const RATE_CONTEXT = (\{.*?\});\n', self.html, re.S).group(1)
        )
        self.assertEqual(rate_context['schema_version'], 1)
        article_by_id = {article['id']: article for article in self.articles}
        observation_days = {
            article_by_id[idea['article_id']]['date'] for idea in self.ideas
        }
        self.assertEqual(
            sorted(set(rate_context['days']) & observation_days),
            sorted(observation_days),
            'every observation date must carry a published curve reading',
        )
        for day, row in rate_context['days'].items():
            self.assertLessEqual(row[0], day, 'a reading must not come from the future')
            self.assertIn(row[4], {'low', 'mid', 'high'})
            self.assertIn(row[5], {'low', 'mid', 'high'})

        # Follow-through must stay bounded to subjects narrow enough to mean
        # something, or the desk would assert continuity the record lacks.
        self.assertIn('const FOLLOW_UP_MAX_TOPIC_SHARE = 0.07;', self.html)
        self.assertIn(
            'if (!topic || topic.article_count > FOLLOW_UP_MAX_TOPIC_ARTICLES) return;',
            self.html,
        )
        self.assertIn(
            "return IDEAS.filter(function (idea) { return Boolean(idea.outcome); }).length;",
            self.html,
        )

    def test_institutional_views_and_workflows_are_present(self):
        for text in (
            'Article Record',
            'Parsed Passages',
            'Article Index',
            'Local Review',
            'Research evidence',
            'Export CSV',
            'Copy view',
            'Source passage',
            'Parsed directional language',
            'Organization or person',
            'Detected numeric phrase',
            'Detected outcome / P&L phrase',
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
        self.assertIn('<p class="sr-only">Navnoor Research Archive</p>', self.html)
        self.assertIn('<h1 class="intel-title" id="lead-article-title">', self.html)
        self.assertIn('role="grid"', self.html)
        self.assertIn('aria-multiselectable="false"', self.html)
        self.assertIn('role="gridcell"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('prefers-reduced-motion', self.html)
        self.assertNotIn('autofocus', self.html)

    def test_landmarks_dynamic_labels_and_desktop_inspector_focus_are_accessible(self):
        active_filters = re.search(r'<div\b[^>]*id="active-filters"[^>]*>', self.html)
        self.assertIsNotNone(active_filters)
        self.assertIn('role="region"', active_filters.group(0))
        self.assertIn('aria-label="Active filters"', active_filters.group(0))

        direction_mix = re.search(r'<div\b[^>]*id="direction-mix"[^>]*>', self.html)
        self.assertIsNotNone(direction_mix)
        self.assertIn('role="img"', direction_mix.group(0))
        self.assertIn('aria-label=', direction_mix.group(0))

        brand = re.search(r'<div\b[^>]*class="brand"[^>]*>', self.html)
        self.assertIsNotNone(brand)
        self.assertNotIn('aria-label=', brand.group(0), 'generic brand container is not a landmark')

        context_start = self.html.index('function renderContext(records)')
        context_end = self.html.index('\nconst BRIEF_KIND_LABELS', context_start)
        context = self.html[context_start:context_end]
        self.assertIn("directionMix.setAttribute('aria-label',directionSummary)", context)
        for label in ('Long ', 'Short ', 'Relative value ', 'L/S ', 'No reliable stance '):
            self.assertIn(label, context)

        inspector_start = self.html.index('function openInspector(focusInside)')
        inspector_end = self.html.index('\nfunction selectRecord', inspector_start)
        inspector = self.html[inspector_start:inspector_end]
        self.assertLess(
            inspector.index('if (window.innerWidth <= 1240)'),
            inspector.index('if (focusInside)'),
            'focus transfer must run for both desktop and narrow inspectors',
        )
        self.assertIn("document.querySelector('#inspector-content .record-title')", inspector)
        self.assertIn('heading.tabIndex = -1', inspector)
        self.assertIn('heading.focus()', inspector)
        self.assertIn("document.getElementById('inspector-close').focus()", inspector)

    def test_print_output_preserves_the_article_brief_and_removes_terminal_chrome(self):
        print_start = self.html.index('@media print{')
        print_end = self.html.index('@media(prefers-reduced-motion', print_start)
        print_css = self.html[print_start:print_end]
        for selector in (
            '.skip-link', '.app-header', '.kpi-strip', '.filter-rail', '.command-bar',
            '.active-filters', '.context-bar', '.inspector', '.drawer-backdrop',
        ):
            self.assertIn(selector, print_css)
        self.assertIn('display:none!important', print_css)
        self.assertRegex(print_css, r'html,body\{[^}]*height:auto!important[^}]*overflow:visible!important')
        self.assertRegex(print_css, r'\.workspace,\.main-panel,\.briefing-shell\{[^}]*height:auto!important[^}]*overflow:visible!important')
        self.assertRegex(print_css, r'\.intel-passage\{[^}]*display:block[^}]*overflow:visible[^}]*-webkit-line-clamp:unset')
        self.assertIn('break-inside:avoid', print_css)

    def test_meaningful_navigation_uses_browser_history_and_popstate_restores_focus(self):
        hash_start = self.html.index("let nextHistoryMode = 'replace'")
        hash_end = self.html.index('\nlet queryCacheKey', hash_start)
        hash_logic = self.html[hash_start:hash_end]
        self.assertIn("nextHistoryMode = 'push'", hash_logic)
        self.assertIn("history[nextHistoryMode === 'push' ? 'pushState' : 'replaceState']", hash_logic)
        self.assertIn('if (!restoringHistory', hash_logic)

        popstate_start = self.html.index("window.addEventListener('popstate'")
        popstate_end = self.html.index("window.addEventListener('resize'", popstate_start)
        popstate = self.html[popstate_start:popstate_end]
        self.assertIn('restoringHistory = true', popstate)
        self.assertIn('hydrateFromHash();', popstate)
        self.assertIn("document.getElementById('search').value = state.query", popstate)
        self.assertIn('render();', popstate)
        self.assertIn('restoringHistory = false', popstate)
        self.assertIn('const waiting = !observationsReady && currentStateNeedsObservations()', popstate)
        self.assertIn("queueObservationResultFocus('entry')", popstate)
        self.assertIn('if (waiting) focusObservationGate()', popstate)
        self.assertIn('else focusViewEntry()', popstate)
        self.assertGreaterEqual(
            self.html.count('markMeaningfulNavigation();'),
            4,
            'view, filter, and record changes should create navigable history entries',
        )

    def test_grid_links_search_filters_and_drawers_have_complete_keyboard_semantics(self):
        self.assertGreaterEqual(self.html.count('role="row" data-record-id='), 2)
        self.assertGreaterEqual(self.html.count('aria-keyshortcuts="Enter Space ArrowUp ArrowDown Home End'), 2)
        row_links = re.findall(r'<a\b[^>]*class="row-open"[^>]*>', self.html)
        self.assertGreaterEqual(len(row_links), 2)
        for link in row_links:
            self.assertIn('tabindex="-1"', link)
            self.assertIn('target="_blank"', link)
            self.assertIn('rel="noopener noreferrer"', link)

        self.assertIn('aria-label="Research results"', self.html)
        self.assertIn('aria-keyshortcuts="Alt+/"', self.html)
        self.assertIn('aria-keyshortcuts="Alt+Shift+?"', self.html)
        self.assertIn('Alt+Shift+O Alt+Shift+S Alt+Shift+C', self.html)
        shortcut_start = self.html.index("document.addEventListener('keydown'")
        shortcut_end = self.html.index("window.addEventListener('popstate'", shortcut_start)
        shortcuts = self.html[shortcut_start:shortcut_end]
        self.assertIn("event.altKey && !event.shiftKey && event.code === 'Slash'", shortcuts)
        self.assertIn('if (!event.altKey || !event.shiftKey) return;', shortcuts)
        for code in ('KeyG', 'KeyJ', 'KeyK', 'KeyO', 'KeyS', 'KeyC', 'KeyF'):
            self.assertIn("event.code === '" + code + "'", shortcuts)
        self.assertNotIn("event.key.toLowerCase() === 'g'", shortcuts)
        self.assertNotIn("event.key === '/'", shortcuts)

        self.assertRegex(self.html, r'id="search"[^>]*maxlength="300"')
        self.assertIn("state.query = String(params.get('q') || '').slice(0,300)", self.html)
        hash_start = self.html.index('function updateHash(includeQuery)')
        hash_end = self.html.index('\nlet queryCacheKey', hash_start)
        hash_logic = self.html[hash_start:hash_end]
        self.assertIn('if (includeQuery && state.query)', hash_logic)
        self.assertIn('updateHash(true,true);', self.html)
        self.assertIn('Shareable view copied with search phrase', self.html)

    def test_command_palette_is_reachable_labelled_and_delegates_to_real_controls(self):
        """The palette must be a real combobox that reuses existing controls."""
        self.assertIn('<dialog id="command-palette"', self.html)
        self.assertIn('aria-labelledby="command-palette-title"', self.html)
        self.assertIn('id="command-palette-input"', self.html)
        self.assertIn('role="combobox"', self.html)
        self.assertIn('aria-controls="command-palette-list"', self.html)
        self.assertIn('aria-autocomplete="list"', self.html)
        self.assertIn('<ul class="palette-list" id="command-palette-list" role="listbox"', self.html)
        # Header affordance so the shortcut is discoverable without the keyboard.
        self.assertIn('id="palette-button"', self.html)
        self.assertIn('aria-keyshortcuts="Control+K Meta+K"', self.html)
        # Ctrl/Cmd+K must not collide with the Alt+Shift desk chords.
        self.assertIn(
            "(event.metaKey || event.ctrlKey) && !event.altKey && event.code === 'KeyK'",
            self.html,
        )
        # Options are escaped and expose active-option state for screen readers.
        self.assertIn("escapeHtml(command.label)", self.html)
        self.assertIn("aria-activedescendant", self.html)
        # Palette entries delegate to the controls that already own the behaviour
        # rather than reimplementing them, so the two can never diverge.
        for selector in (
            '#theme-button', '[data-action="density"]', '[data-action="inspector"]',
            '[data-action="copy-view"]', '[data-action="export"]', '#clear-filters',
        ):
            self.assertIn("'" + selector + "'", self.html)

    def test_unmodified_desk_keys_work_without_swallowing_typed_input(self):
        """Bare j/k//? are gated behind the editable and interactive guard."""
        start = self.html.index("document.addEventListener('keydown'")
        end = self.html.index("window.addEventListener('popstate'", start)
        handler = self.html[start:end]
        guard = "if (shortcutDialog.open || manualCopyDialog.open || editable || interactive"
        self.assertIn(guard, handler)
        for bare in (
            "!event.altKey && !event.shiftKey && event.code === 'KeyJ'",
            "!event.altKey && !event.shiftKey && event.code === 'KeyK'",
            "!event.altKey && !event.shiftKey && event.code === 'Slash'",
            "!event.altKey && event.shiftKey && event.code === 'Slash'",
        ):
            self.assertIn(bare, handler)
            # Every bare binding must sit after the guard that proves focus is
            # not inside an input, otherwise it would eat typed characters.
            self.assertGreater(handler.index(bare), handler.index(guard))
        # The legacy Alt+Shift chords remain bound for existing muscle memory.
        self.assertIn('if (!event.altKey || !event.shiftKey) return;', handler)

    def test_command_bar_holds_one_row_on_the_desk_and_wraps_only_when_narrow(self):
        """Wrapping beats shrinking in flex, so the desk bar must not wrap."""
        bar = re.search(r'\.command-bar\{[^}]*\}', self.html).group(0)
        self.assertIn('flex-wrap:nowrap', bar)
        narrow = self.html.index('@media(max-width:1020px)')
        narrow_block = self.html[narrow:narrow + 400]
        self.assertIn('.command-bar{flex-wrap:wrap}', narrow_block)
        summary = re.search(r'\.result-summary\{[^}]*\}', self.html).group(0)
        self.assertIn('text-overflow:ellipsis', summary)
        self.assertIn('min-width:0', summary)

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
        self.assertIn('loadBriefArchive().then(function ()', article_search)
        self.assertIn('generation !== articleSearchGeneration', article_search)

        self.assertIn("button.setAttribute('aria-label','Remove filter: '", self.html)
        self.assertIn("mark.setAttribute('aria-hidden','true')", self.html)
        self.assertRegex(self.html, r'data-empty-action="clear"')
        self.assertRegex(self.html, r'data-empty-action="browse"')
        self.assertRegex(self.html, r"setAttribute\(['\"]role['\"],['\"]dialog['\"]\)")
        self.assertRegex(self.html, r"setAttribute\(['\"]aria-modal['\"],['\"]true['\"]\)")

        mobile_start = self.html.index('@media(max-width:759px){')
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
            r'\.text-button,\.filter-chip,\.primary-action,\.secondary-action,\.inspector-close,\.load-more',
        ):
            match = re.search(selector + r'\{[^}]*(?:min-)?height:(\d+)px', mobile)
            self.assertIsNotNone(match, f'mobile target size missing for {selector}')
            self.assertGreaterEqual(int(match.group(1)), 44, f'mobile target too small for {selector}')
        self.assertRegex(mobile, r'\.text-button,\.inspector-close\{[^}]*min-width:44px')
        self.assertRegex(self.html, r'\.inspector-close\{[^}]*min-width:24px')

    def test_literal_dom_id_references_resolve_and_ids_are_unique(self):
        ids = re.findall(r'\bid=["\']([^"\']+)["\']', self.html)
        self.assertEqual(len(ids), len(set(ids)), 'generated HTML contains duplicate IDs')
        references = set(re.findall(
            r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", self.html
        ))
        missing = references.difference(ids)
        self.assertFalse(missing, f'JavaScript references missing literal IDs: {sorted(missing)}')

    def test_snapshot_manifest_security_policy_and_freshness_are_embedded(self):
        # Compare against the manifest the site was actually built from, which
        # is the tracked one with its check times rebased onto the test clock.
        manifest = json.loads(
            (self.source_root / 'snapshot_manifest.json').read_text(encoding='utf-8')
        )
        self.assertEqual(self.snapshot, manifest)
        self.assertEqual(self.snapshot['article_count'], len(self.articles))
        self.assertEqual(self.snapshot['catalog_count'], len(self.source_articles))
        self.assertEqual(
            self.snapshot['registry_count'],
            len(self.source_articles) - len(self.source_content_articles),
        )
        self.assertEqual(self.snapshot['observation_count'], len(self.ideas))
        self.assertEqual(
            set(self.snapshot['sources']),
            {'substack', 'medium', 'patreon', 'fxempire'},
        )

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
        self.assertEqual(
            meta_content('nrt-article-catalog-sha256'),
            hashlib.sha256(self.article_catalog_bytes).hexdigest(),
        )
        self.assertEqual(
            meta_content('nrt-brief-archive-sha256'),
            hashlib.sha256(self.brief_bytes).hexdigest(),
        )
        self.assertEqual(
            meta_content('nrt-observation-archive-sha256'),
            hashlib.sha256(self.observation_bytes).hexdigest(),
        )
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
        self.assertNotIn("script-src 'unsafe-inline'", csp)
        script_bodies = [
            body for _script_type, body in extract_inline_scripts(self.html)
        ]
        expected_script_hashes = {
            base64.b64encode(hashlib.sha256(body.encode('utf-8')).digest()).decode('ascii')
            for body in script_bodies
        }
        actual_script_hashes = set(re.findall(r"'sha256-([^']+)'", csp))
        self.assertEqual(actual_script_hashes, expected_script_hashes)

        freshness_start = self.html.index('function snapshotFreshness()')
        freshness = self.html[freshness_start:]
        self.assertIn('SNAPSHOT.checked_at', freshness)
        self.assertIn('SNAPSHOT.latest_publication', freshness)
        self.assertIn('SNAPSHOT.sources', freshness)
        self.assertRegex(freshness, r"source\.status\s*===\s*['\"]ok['\"]")
        for freshness_class in ('fresh', 'degraded', 'stale'):
            self.assertIn(freshness_class, freshness)
        self.assertRegex(freshness, r'ageHours\s*>\s*16')
        self.assertNotRegex(freshness, r'ageHours\s*>\s*36')
        self.assertIn('futureToleranceMs = 10 * 60 * 1000', freshness)
        self.assertIn('manifestClockInvalid', freshness)
        self.assertIn('sourceClockInvalid', freshness)
        self.assertIn('Refresh clock invalid', freshness)
        self.assertNotIn('Math.max(0,(Date.now() - checked.getTime())', freshness)
        self.assertIn('9 AM, 1 PM, and 10 PM Asia/Kolkata', self.html)

    def test_search_social_and_discovery_metadata_are_complete_and_private(self):
        for text in (
            '<meta name="robots" content="index,follow,max-image-preview:large">',
            '<meta property="og:site_name" content="Navnoor Research Archive">',
            '<meta property="og:image" content="https://navnoorthapar.github.io/substack-trades/og.jpg">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            '<meta name="twitter:card" content="summary_large_image">',
            '<link rel="icon" type="image/svg+xml" href="favicon.svg">',
            '<link rel="manifest" href="site.webmanifest">',
            '<link rel="sitemap" type="application/xml" href="sitemap.xml">',
        ):
            self.assertIn(text, self.html)

        structured_match = re.search(
            r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
            self.html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(structured_match)
        structured = json.loads(structured_match.group(1))
        self.assertEqual(structured['@type'], 'WebApplication')
        self.assertEqual(structured['applicationCategory'], 'FinanceApplication')
        self.assertEqual(structured['url'], 'https://navnoorthapar.github.io/substack-trades/')

        robots = (self.site_dir / 'robots.txt').read_text(encoding='utf-8')
        self.assertEqual(
            robots,
            'User-agent: *\nAllow: /\nSitemap: '
            'https://navnoorthapar.github.io/substack-trades/sitemap.xml\n',
        )
        sitemap = (self.site_dir / 'sitemap.xml').read_text(encoding='utf-8')
        self.assertIn('<loc>https://navnoorthapar.github.io/substack-trades/</loc>', sitemap)
        self.assertIn(f'<lastmod>{self.snapshot["checked_at"][:10]}</lastmod>', sitemap)
        manifest = json.loads((self.site_dir / 'site.webmanifest').read_text(encoding='utf-8'))
        self.assertEqual(manifest['start_url'], './')
        self.assertEqual(manifest['scope'], './')
        self.assertEqual(manifest['icons'][0]['src'], 'favicon.svg')
        self.assertEqual(manifest['background_color'], '#f4f6f8')
        self.assertEqual(manifest['theme_color'], '#f4f6f8')
        social = (self.site_dir / 'og.jpg').read_bytes()
        self.assertTrue(social.startswith(b'\xff\xd8') and social.rstrip().endswith(b'\xff\xd9'))
        self.assertLessEqual(len(social), 500_000)
        self.assertIn('no advertising, cookies, third-party analytics, session replay', self.html)
        self.assertIn('if (window.top !== window.self)', self.html)
        self.assertIn('window.top.location.replace(window.self.location.href)', self.html)
        self.assertIn('cannot run inside a frame', self.html)

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

    def test_external_article_wire_payload_is_compact_and_losslessly_hydrated(self):
        always_derived = {
            'date',
            'publication_precision',
            'read_minutes',
            'trade_count',
            'brief_features',
            'has_quant',
            'has_thesis',
            'has_outcome',
        }
        for wire_article in self.article_payload:
            self.assertFalse(always_derived.intersection(wire_article))
        compact_bytes = json.dumps(
            self.article_payload,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
        self.assertLess(
            len(compact_bytes) / len(self.article_payload),
            1_100,
            'external article wire rows lost their compact growth headroom',
        )

        hydrate_start = self.html.index('function hydrateEmbeddedArticle(article)')
        hydrate_end = self.html.index('\nconst THREADS =', hydrate_start)
        hydrate = self.html[hydrate_start:hydrate_end]
        for text in (
            "article.date = publishedAt.slice(0,10)",
            "article.publication_precision = /^\\d{4}-\\d{2}-\\d{2}$/.test",
            'remainder === 110 && wholeMinutes % 2 === 1',
            'article.trade_count = article.idea_ids.length',
            'lead:Boolean(briefMask & 1)',
            'evidence:Boolean(briefMask & 2)',
            'has_quant = Boolean(coverageMask & 1)',
            'has_thesis = Boolean(coverageMask & 2)',
            'has_outcome = Boolean(coverageMask & 4)',
            'delete article._b',
            'delete article._q',
            'ARTICLES.forEach(hydrateEmbeddedArticle)',
        ):
            self.assertIn(text, hydrate)

        for text in (
            "'article_catalog.json?v='",
            'actualHash !== ARTICLE_CATALOG_SHA256',
            "hasExactObjectKeys(payload,['article_wire_schema_version','articles','data_checksum','schema_version'])",
            'payload.article_wire_schema_version !== ARTICLE_WIRE_SCHEMA_VERSION',
            'payload.data_checksum !== SNAPSHOT.data_checksum',
            'const ARTICLES = await loadArticleCatalog()',
            'The release could not be verified. No partial or mismatched catalogue has been displayed.',
            "status.setAttribute('role','alert')",
            "document.getElementById('bootstrap-retry').addEventListener('click',startApplication)",
        ):
            self.assertIn(text, self.html)

    def test_synthetic_catalogue_growth_cannot_consume_html_budget(self):
        growth_count = 150
        with tempfile.TemporaryDirectory(prefix='nrt-growth-') as directory:
            source_root = materialize_source_tree(
                ROOT, Path(directory) / 'source',
            )
            site_dir = Path(directory) / 'site'
            article_path = source_root / 'articles_index.json'
            raw_articles = json.loads(article_path.read_text(encoding='utf-8'))
            rows = (
                raw_articles['articles']
                if isinstance(raw_articles, dict)
                else raw_articles
            )
            base = next(
                article for article in rows
                if article.get('source') == 'substack'
                and article.get('content_status') != 'registry'
            )
            for index in range(growth_count):
                clone = json.loads(json.dumps(base))
                slug = f'catalogue-growth-fixture-{index:04d}'
                clone.update({
                    'source_id': slug,
                    'slug': slug,
                    'title': f'Catalogue growth fixture {index:04d}',
                    'url': f'https://navnoorbawa.substack.com/p/{slug}',
                })
                rows.append(clone)
            article_path.write_text(
                json.dumps(raw_articles, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
            manifest_path = source_root / 'snapshot_manifest.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['article_count'] += growth_count
            manifest['catalog_count'] += growth_count
            manifest['sources']['substack']['included_count'] += growth_count
            manifest['data_checksum'] = hashlib.sha256(
                article_path.read_bytes()
                + b'\0'
                + (source_root / 'trades_extracted.json').read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
            environment = os.environ.copy()
            environment['SITE_OUTPUT_DIR'] = str(site_dir)
            environment['SITE_REVISION'] = 'synthetic-growth'
            subprocess.run(
                [sys.executable, str(source_root / 'build_site.py')],
                cwd=source_root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=SYNTHETIC_GROWTH_BUILD_TIMEOUT_SECONDS,
            )
            grown_html = (site_dir / 'index.html').read_bytes()
            grown_catalogue = (
                site_dir / 'article_catalog.json'
            ).read_bytes()
        catalogue_growth = len(grown_catalogue) - len(self.article_catalog_bytes)
        html_growth = len(grown_html) - len(self.html_bytes)
        self.assertGreater(
            catalogue_growth,
            80_000,
            'growth fixture is too small to reproduce the former HTML failure',
        )
        self.assertLess(
            html_growth,
            catalogue_growth // 2,
            'article wire bytes leaked back into the generated HTML',
        )
        self.assertLessEqual(len(grown_html), 900_000)
        self.assertNotRegex(
            grown_html.decode('utf-8'),
            r'(?m)^\s*const\s+ARTICLES\s*=\s*\[',
        )
        builder_source = (ROOT / 'build_site.py').read_text(encoding='utf-8')
        self.assertNotIn('__ARTICLES_JSON__', builder_source)
        self.assertIn("DOCS_DIR / 'article_catalog.json'", builder_source)

    def test_static_artifact_stays_inside_the_institutional_performance_budget(self):
        self.assertLessEqual(
            len(self.html_bytes),
            900_000,
            'first-load HTML exceeded the reviewed 900 KB transfer budget',
        )
        self.assertLessEqual(
            len(gzip.compress(self.html_bytes, compresslevel=9)),
            250_000,
            'compressed first load exceeded the reviewed 250 KB budget',
        )
        self.assertLessEqual(
            len(self.article_catalog_bytes),
            4_000_000,
            'verified article catalogue exceeded its reviewed 4 MB budget',
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
        self.assertLessEqual(
            len(self.observation_bytes),
            1_500_000,
            'deferred observation payload exceeded its reviewed 1.5 MB budget',
        )
        artifact_files = [path for path in self.site_dir.rglob('*') if path.is_file()]
        slugs = {str(article['slug']) for article in self.source_articles}
        expected_files = {
            'index.html', 'article_catalog.json', 'article_briefs.json',
            'observations.json',
            'robots.txt', 'sitemap.xml', 'site.webmanifest',
            'favicon.svg', 'og.jpg',
            'data/articles_index.json', 'data/latest.json',
            'data/manifest.json', 'data/search_index.json',
            'data/related.json', 'data/families.json',
        }
        expected_files.update(f'cards/{slug}.png' for slug in slugs)
        expected_files.update(f'a/{slug}.html' for slug in slugs)
        self.assertEqual(
            {path.relative_to(self.site_dir).as_posix() for path in artifact_files},
            expected_files,
        )
        self.assertTrue(all(not path.is_symlink() for path in artifact_files))
        self.assertLess(
            sum(path.stat().st_size for path in artifact_files),
            20_000_000,
            'complete static artifact exceeded the reviewed 20 MB policy',
        )
        self.assertEqual(
            (self.site_dir / 'data' / 'articles_index.json').read_bytes(),
            (ROOT / 'articles_index.json').read_bytes(),
        )
        search_bytes = (self.site_dir / 'data' / 'search_index.json').stat().st_size
        self.assertLess(search_bytes, 500_000)
        self.assertTrue(all(
            (self.site_dir / 'cards' / f'{slug}.png').read_bytes().startswith(
                b'\x89PNG\r\n\x1a\n'
            )
            for slug in slugs
        ))

    def test_direction_mix_legend_names_all_supported_states(self):
        context_start = self.html.index('function renderContext(records)')
        context_end = self.html.index('\nconst BRIEF_KIND_LABELS', context_start)
        legend = self.html[context_start:context_end]
        self.assertIn('Parsed passage language—not exposure', legend)
        self.assertIn('L/S', legend)
        self.assertIn('No reliable stance', legend)
        self.assertRegex(legend, r"counts\[['\"]long/short['\"]\]")
        self.assertRegex(legend, r'counts\.unspecified')
        self.assertIn("directionMix.setAttribute('aria-label',directionSummary)", self.html)
        self.assertIn("directionMix.setAttribute('aria-label',directionSummary)", legend)
        self.assertIn("document.getElementById('mix-legend').textContent = directionSummary", legend)
        self.assertIn(
            'aria-label="Parsed directional language in visible passages; not portfolio exposure"',
            self.html,
        )
        self.assertIn('Coverage is not investment quality or evidence of a position.', self.html)
        self.assertNotIn('Parsed stance / structure', self.html)

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

        def color_distance(left, right):
            left_channels = [int(left[index:index + 2], 16) for index in (1, 3, 5)]
            right_channels = [int(right[index:index + 2], 16) for index in (1, 3, 5)]
            return sum(
                (left_channel - right_channel) ** 2
                for left_channel, right_channel in zip(left_channels, right_channels)
            ) ** 0.5

        dark = tokens(root_match.group('body'))
        light = tokens(light_match.group('body'))
        required_tokens = {
            'bg', 'surface-1', 'surface-2', 'surface-3', 'surface-raised',
            'selected', 'selected-line', 'selection-bg', 'selection-text',
            'text', 'text-secondary', 'text-muted',
            'accent', 'accent-strong', 'accent-hover', 'accent-active', 'accent-soft',
            'focus', 'on-accent',
            'positive', 'positive-soft', 'negative', 'negative-soft',
            'warning', 'warning-soft', 'warning-line',
            'long', 'long-soft', 'long-line', 'short', 'short-soft', 'short-line',
            'relative', 'relative-soft', 'long-short', 'long-short-soft',
            'quant', 'quant-soft', 'number', 'number-soft', 'checkpoint',
            'control-line', 'control-line-hover',
        }
        for palette in (dark, light):
            self.assertFalse(
                required_tokens - palette.keys(),
                'institutional palette is missing dedicated semantic tokens: ' +
                ', '.join(sorted(required_tokens - palette.keys())),
            )
            for foreground in ('text', 'text-secondary', 'text-muted', 'accent'):
                for surface in ('bg', 'surface-1', 'surface-2', 'surface-3', 'surface-raised', 'selected'):
                    self.assertGreaterEqual(
                        contrast(palette[foreground], palette[surface]),
                        4.5,
                        f'{foreground} on {surface}',
                    )
            for semantic in (
                'positive', 'negative', 'warning',
                'long', 'short', 'relative', 'long-short',
                'quant', 'number', 'checkpoint',
            ):
                self.assertGreaterEqual(
                    contrast(palette[semantic], palette['surface-1']),
                    4.5,
                    semantic,
                )
            for action_surface in ('accent-strong', 'accent-hover', 'accent-active'):
                self.assertGreaterEqual(contrast(palette['on-accent'], palette[action_surface]), 4.5)
            for boundary in ('control-line', 'control-line-hover', 'focus', 'selected-line'):
                for surface in ('surface-1', 'surface-2', 'surface-3', 'surface-raised'):
                    self.assertGreaterEqual(
                        contrast(palette[boundary], palette[surface]),
                        3.0,
                        f'{boundary} on {surface}',
                    )
            self.assertGreaterEqual(contrast(palette['selection-text'], palette['selection-bg']), 4.5)
            self.assertGreaterEqual(contrast(palette['selection-bg'], palette['surface-1']), 1.25)
            for semantic in (
                'positive', 'negative', 'warning',
                'long', 'short', 'relative', 'long-short',
                'quant', 'number',
            ):
                self.assertGreaterEqual(contrast(palette[semantic], palette[f'{semantic}-soft']), 4.5, f'{semantic} badge')

            for first, second in (
                ('positive', 'long'),
                ('negative', 'short'),
                ('accent', 'warning'),
                ('accent', 'checkpoint'),
                ('warning', 'checkpoint'),
            ):
                self.assertGreaterEqual(
                    color_distance(palette[first], palette[second]),
                    30,
                    f'{first} and {second} must retain distinct meanings',
                )

        def channels(color):
            return [int(color[index:index + 2], 16) for index in (1, 3, 5)]

        self.assertLessEqual(sum(channels(dark['bg'])), 60, 'dark canvas should remain near-black')
        for surface in ('bg', 'surface-1', 'surface-2', 'surface-3', 'surface-raised'):
            dark_channels = channels(dark[surface])
            self.assertLessEqual(dark_channels[0], dark_channels[1], f'{surface} should use cool slate')
            self.assertLessEqual(dark_channels[1], dark_channels[2], f'{surface} should use cool slate')

            light_channels = channels(light[surface])
            self.assertLessEqual(light_channels[0], light_channels[1], f'{surface} should use cool neutral slate')
            self.assertLessEqual(light_channels[1], light_channels[2], f'{surface} should use cool neutral slate')

        dark_accent = channels(dark['accent'])
        dark_action = channels(dark['accent-strong'])
        dark_selection = channels(dark['selected-line'])
        self.assertGreater(dark_accent[2], dark_accent[0], 'dark interactions should retain a blue information cue')
        self.assertEqual(dark_action, dark_accent)
        self.assertEqual(dark_selection, dark_accent)

        light_accent = channels(light['accent'])
        light_brick = channels(light['brick'])
        self.assertGreater(light_accent[2], light_accent[0], 'light interactions should retain a blue information cue')
        self.assertEqual(light_brick, light_accent)
        self.assertGreaterEqual(contrast(light['text-muted'], light['selected']), 4.5)
        self.assertIn('background:var(--accent-strong);color:var(--on-accent)', self.html)
        self.assertIn('.primary-action:hover{background:var(--accent-hover);border-color:var(--accent-hover)}', self.html)
        self.assertIn('.primary-action:active{background:var(--accent-active);border-color:var(--accent-active)}', self.html)
        self.assertIn('#search:focus{border-color:var(--control-line)', self.html)
        self.assertIn('::selection{background:var(--selection-bg);color:var(--selection-text)}', self.html)
        self.assertRegex(self.html, r'\.desk-start\{[^}]*border:1px solid var\(--control-line\)')
        self.assertRegex(self.html, r'\.structure-chip\{[^}]*border:1px solid var\(--control-line\)')
        self.assertIn('.utility-button{border-color:var(--control-line);background:var(--surface-1)}', self.html)
        self.assertRegex(
            self.html,
            r'\.view-tab\.active\{[^}]*border-bottom:3px solid var\(--selected-line\)',
        )
        self.assertNotEqual(dark['quant'], dark['relative'])

    def test_interactive_boundaries_patterns_and_forced_colors_are_accessible(self):
        for selector in (
            'utility-button:hover', 'date-option:hover', 'preset-button:hover',
            'command-button:hover', 'filter-chip:hover', 'row-open:hover',
            'related-idea:hover', 'workflow-gate:hover', 'select-control:hover',
            'intel-lens:hover', 'load-more:hover', 'secondary-action:hover',
        ):
            self.assertRegex(
                self.html,
                rf'\.{re.escape(selector)}\{{[^}}]*border-color:var\(--control-line-hover\)',
                selector,
            )
        for selector in ('related-idea', 'workflow-gate'):
            self.assertRegex(
                self.html,
                rf'\.{re.escape(selector)}\{{[^}}]*border:1px solid var\(--control-line\)',
                selector,
            )
        self.assertRegex(
            self.html,
            r'button\.research-map-step\{[^}]*border-color:var\(--control-line\)',
        )
        for selector in ('mix-short', 'mix-arb', 'mix-ls', 'mix-unspecified'):
            self.assertRegex(
                self.html,
                rf'\.{selector}\{{[^}}]*background-image:repeating-linear-gradient',
                selector,
            )
        self.assertIn('@media(forced-colors:active)', self.html)
        self.assertNotIn('forced-color-adjust:none', self.html)
        self.assertIn('background-image:none!important', self.html)
        self.assertIn('.mix-legend{display:inline!important;white-space:normal}', self.html)
        self.assertIn('.command-button.active,.intel-lens.active,.data-row.selected,.next-item.selected{', self.html)
        self.assertIn('@media(prefers-contrast:more)', self.html)
        self.assertIn('::-webkit-scrollbar-thumb{background:var(--control-line)', self.html)
        self.assertIn('::-webkit-scrollbar-thumb:hover{background:var(--control-line-hover)}', self.html)
        self.assertIn('*{scrollbar-color:var(--control-line) transparent}', self.html)
        self.assertIn('textarea:focus-visible,[tabindex]:focus-visible', self.html)
        self.assertRegex(
            self.html,
            r'button:disabled\{[^}]*color:var\(--text-muted\)[^}]*cursor:not-allowed',
        )
        for selector in ('brief-record:hover', 'next-item:hover', 'intel-article-card:hover', 'data-row:hover'):
            self.assertRegex(
                self.html,
                rf'\.{re.escape(selector)}\{{[^}}]*background:var\(--surface-3\)',
                selector,
            )
        self.assertRegex(
            self.html,
            r'button\.kpi-item:hover\{[^}]*background:var\(--surface-3\)'
            r'[^}]*box-shadow:inset 0 -2px var\(--selected-line\)',
        )

    def test_theme_and_freshness_status_do_not_depend_on_color(self):
        root_match = re.search(r':root\s*\{(?P<body>.*?)\}\s*html\[data-theme="light"\]', self.html, re.DOTALL)
        light_match = re.search(r'html\[data-theme="light"\]\s*\{(?P<body>.*?)\}', self.html, re.DOTALL)
        self.assertIsNotNone(root_match)
        self.assertIsNotNone(light_match)
        dark_bg = re.search(r'--bg\s*:\s*(#[0-9a-fA-F]{6})', root_match.group('body')).group(1)
        light_bg = re.search(r'--bg\s*:\s*(#[0-9a-fA-F]{6})', light_match.group('body')).group(1)
        self.assertIn(f'<meta name="theme-color" id="theme-color" content="{light_bg}">', self.html)
        self.assertIn(f"theme === 'light' ? '{light_bg}' : '{dark_bg}'", self.html)
        self.assertIn(f"next === 'light' ? '{light_bg}' : '{dark_bg}'", self.html)
        self.assertIn("candidate === 'light' || candidate === 'dark'", self.html)
        self.assertIn("var theme = stored || (systemDark ? 'dark' : 'light')", self.html)
        self.assertIn("window.matchMedia('(prefers-color-scheme: dark)')", self.html)
        self.assertIn("localStorage.setItem('nrt-theme-revision',themeRevision)", self.html)
        self.assertGreaterEqual(self.html.count("getElementById('theme-color').content"), 2)
        self.assertIn("button.setAttribute('aria-label','Switch to '", self.html)
        self.assertIn("explicit !== 'light' && explicit !== 'dark'", self.html)
        self.assertIn('id="freshness-dot" aria-hidden="true"', self.html)
        self.assertIn('id="freshness-state">Unknown</span>', self.html)
        self.assertIn('function snapshotFreshness()', self.html)
        self.assertIn("status:className === 'stale' ? 'Stale'", self.html)
        self.assertIn("className === 'fresh' ? 'Current'", self.html)
        self.assertIn("className === 'degraded' ? 'Degraded'", self.html)
        self.assertIn('const freshness = snapshotFreshness();', self.html)
        self.assertIn('const freshnessStatus = freshness.status;', self.html)
        self.assertIn("document.getElementById('freshness-state').textContent = freshnessStatus", self.html)
        self.assertIn("freshnessSummary.setAttribute('aria-label',freshnessStatus", self.html)
        mobile_start = self.html.index('@media(max-width:1020px)')
        mobile_end = self.html.index('@media(max-width:759px)', mobile_start)
        mobile_css = self.html[mobile_start:mobile_end]
        self.assertNotIn('#freshness-state', mobile_css)
        self.assertIn('.freshness-separator,.freshness>span:last-child', mobile_css)
        self.assertRegex(self.html, r'\.status-dot\.degraded\{[^}]*transform:rotate\(45deg\)')
        self.assertRegex(self.html, r'\.status-dot\.stale\{[^}]*border-radius:1px')

    def test_semantic_colors_are_scoped_to_information_states(self):
        self.assertRegex(self.html, r'\.status-dot\{[^}]*background:var\(--text-muted\)')
        self.assertRegex(self.html, r'\.status-dot\.fresh\{[^}]*background:var\(--positive\)')
        self.assertRegex(self.html, r'\.status-dot\.degraded\{[^}]*background:var\(--warning\)')
        self.assertRegex(self.html, r'\.status-dot\.stale\{[^}]*background:var\(--negative\)')
        self.assertRegex(self.html, r'\.evidence-flag\.on\{[^}]*color:var\(--quant\)')
        self.assertRegex(self.html, r'\.source-badge\{[^}]*color:var\(--text-secondary\)')
        self.assertIn('.source-substack::before{background:var(--source-substack)}', self.html)
        self.assertIn('.source-medium::before{background:var(--source-medium)}', self.html)
        for class_name, token in (
            ('dir-long', 'long'),
            ('dir-short', 'short'),
            ('dir-arb', 'relative'),
            ('dir-ls', 'long-short'),
        ):
            self.assertRegex(self.html, rf'\.{class_name}\{{[^}}]*color:var\(--{token}\)')

    def test_article_brief_uses_neutral_labels_and_dedicated_evidence_colors(self):
        for selector in ('brief-kicker', 'intel-label'):
            selector_pattern = re.escape(selector)
            self.assertRegex(
                self.html,
                rf'\.{selector_pattern}\{{[^}}]*color:var\(--text-(?:secondary|muted)\)',
            )
        self.assertRegex(
            self.html,
            r'\.article-dossier-section h3\{[^}]*color:var\(--text-(?:secondary|muted)\)',
        )
        for selector in ('intel-passage mark', 'article-dossier-section mark'):
            selector_pattern = re.escape(selector)
            self.assertRegex(
                self.html,
                rf'\.{selector_pattern}\{{[^}}]*background:var\(--number-soft\)[^}}]*color:var\(--number\)',
            )

    def test_warning_checkpoint_and_selection_colors_are_not_market_direction_colors(self):
        warning_rules = {
            'status-dot.degraded': r'background:var\(--warning\)[^}]*var\(--warning-soft\)',
            'evidence-gap': (
                r'border-color:var\(--warning-line\)[^}]*background:var\(--warning-soft\)'
                r'[^}]*color:var\(--warning\)'
            ),
            'review-flag': r'color:var\(--warning\)',
            'review-notice': (
                r'border:[^;}]*var\(--warning-line\)[^}]*background:var\(--warning-soft\)'
                r'[^}]*color:var\(--warning\)'
            ),
            'orphaned-queue h2': r'color:var\(--warning\)',
        }
        for selector, expected_rule in warning_rules.items():
            selector_pattern = re.escape(selector)
            self.assertRegex(
                self.html,
                rf'\.{selector_pattern}\{{[^}}]*{expected_rule}',
                selector,
            )
        for selector in ('checkpoint time', 'checkpoint-mini time'):
            selector_pattern = re.escape(selector)
            self.assertRegex(
                self.html,
                rf'\.{selector_pattern}\{{[^}}]*color:var\(--checkpoint\)',
                selector,
            )
        self.assertRegex(
            self.html,
            r'\.pinned-selection\{[^}]*var\(--selected-line\)',
        )
        self.assertRegex(
            self.html,
            r'\.filter-chip:hover\{[^}]*border-color:var\(--control-line-hover\)',
        )
        self.assertNotRegex(
            self.html,
            r'\.(?:intel-passage mark|article-dossier-section mark|checkpoint(?:-mini)? time|'
            r'status-dot\.degraded|evidence-gap|review-flag|review-notice|pinned-selection)'
            r'\{[^}]*(?:--relative|--positive|--negative)',
        )

    def test_market_direction_tokens_are_separate_from_operational_status_tokens(self):
        self.assertRegex(self.html, r'\.mix-long\{[^}]*background:var\(--long\)')
        self.assertRegex(self.html, r'\.mix-short\{[^}]*background:var\(--short\)')
        self.assertRegex(
            self.html,
            r'\.dir-long\{[^}]*color:var\(--long\)[^}]*border-color:var\(--long-line\)'
            r'[^}]*background:var\(--long-soft\)',
        )
        self.assertRegex(
            self.html,
            r'\.dir-short\{[^}]*color:var\(--short\)[^}]*border-color:var\(--short-line\)'
            r'[^}]*background:var\(--short-soft\)',
        )
        self.assertNotIn('documentation-badge', self.html)
        self.assertRegex(self.html, r'\.status-dot\.stale\{[^}]*background:var\(--negative\)')

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
