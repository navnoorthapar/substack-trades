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
        cls.observation_path = cls.site_dir / 'observations.json'
        cls.observation_bytes = cls.observation_path.read_bytes()
        cls.observation_archive = json.loads(cls.observation_bytes.decode('utf-8'))
        article_payload = json.loads((ROOT / 'articles_index.json').read_text(encoding='utf-8'))
        cls.source_articles = (
            article_payload.get('articles', [])
            if isinstance(article_payload, dict)
            else article_payload
        )
        cls.source_ideas = json.loads((ROOT / 'trades_extracted.json').read_text(encoding='utf-8'))
        article_match = re.search(r'const ARTICLES = (.*?);\n', cls.html)
        snapshot_match = re.search(r'const SNAPSHOT = (.*?);\n', cls.html)
        if not article_match or not snapshot_match:
            raise AssertionError('generated client payload is missing')
        cls.articles = json.loads(article_match.group(1))
        cls.ideas = cls.observation_archive['observations']
        cls.snapshot = json.loads(snapshot_match.group(1))

    @classmethod
    def tearDownClass(cls):
        cls._site_temp.cleanup()

    def test_complete_multi_source_dataset_is_deferred_once(self):
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
            'payload.schema_version !== 1',
            'payload.data_checksum !== SNAPSHOT.data_checksum',
            'rows.length !== Number(SNAPSHOT.observation_count || 0)',
            'const expectedArticleById = new Map()',
            'nextMap.has(idea.id)',
            'expectedArticleById.get(idea.id) !== idea.article_id',
            'nextMap.size !== expectedArticleById.size',
            'relevanceScoreCache = new WeakMap()',
            'Observation archive does not match this release',
        ):
            self.assertIn(required, self.html)

    def test_observation_deep_links_wait_for_verified_release_asset(self):
        gate_start = self.html.index('function currentStateNeedsObservations()')
        render_start = self.html.index('function render() {', gate_start)
        render_end = self.html.index('\nfunction resetFilters', render_start)
        gate = self.html[gate_start:render_end]
        for text in (
            'if (!isArticleView()) return true',
            'state.query || state.directions.size || state.instruments.size',
            'function renderObservationGate()',
            'release-bound observation asset',
            'data-retry-observations',
            'no evidence-absence conclusion has been drawn',
            'if (!observationsReady && currentStateNeedsObservations())',
        ):
            self.assertIn(text, gate)
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
            'Latest Brief',
            'Investment committee brief · published information',
            'Author’s opening thesis',
            'Exact authored passage',
            'How the argument works',
            'No analyst conclusion, score, or portfolio recommendation is inferred.',
            'Source dossier and decision boundaries',
            'Institutional diligence map',
            'Evidence ledger',
            'Detected numbers with their authored context',
            'Dossier coverage in this lens',
            'Related archive context',
            'Recent article dossiers',
            'What changes our mind',
            'Author’s countercase',
            'What would change the view',
            'Device-local IC overlay',
            'Local · this device',
            'Open source dossier',
            'Copy IC brief',
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
        self.assertIn("analysisPanelMarkup(mechanismRow,'Mechanism'", briefing)
        self.assertIn("decisionSheetSectionMarkup(countercaseRow,'Author’s countercase')", briefing)
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
        spotlight_end = self.html.index('\nfunction analysisPanelMarkup', spotlight_start)
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
        self.assertIn("const openingLabel = leadRow ? 'Author’s opening thesis' : 'Published article framing'", briefing)
        self.assertIn('Packets attach to individual observations', briefing)
        self.assertIn('never silently assigns an article-level recommendation', briefing)
        self.assertNotIn('Analyst synthesis', briefing)
        self.assertNotIn('Evidence quality', briefing)

    def test_editorial_visual_system_is_light_first_and_responsive(self):
        for text in (
            '--serif:"Iowan Old Style"',
            '--bg:#e8e9e5',
            '--surface-1:#faf9f5',
            '--text:#172027',
            '--accent:#173f5d',
            '.intel-title{',
            'var(--serif)',
            '.ic-rail{',
            '.intel-side.ic-sheet{',
            '@media(max-width:1439px)',
            '@media(max-width:1023px)',
            '@media(max-width:759px)',
        ):
            self.assertIn(text, self.html)
        self.assertIn("var theme = stored || 'light'", self.html)
        self.assertIn("var themeRevision = 'editorial-brief-2026-07'", self.html)
        self.assertRegex(
            self.html,
            r'body\[data-view="briefing"\] \.kpi-strip,\s*body\[data-view="briefing"\] \.command-bar',
        )
        self.assertRegex(self.html, r'\.intel-wrap\{[^}]*grid-template-columns:220px minmax\(620px,1fr\) 360px')
        self.assertNotIn('min-width:1180px', self.html)

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
            'payload.schema_version !== 1',
            'payload.data_checksum !== SNAPSHOT.data_checksum',
            "article.brief = payload.briefs[id]",
            'refreshArticleSearch(article)',
            'Loading the exact article dossier',
            'Checking the deferred dossier against this release.',
        ):
            self.assertIn(text, self.html)

    def test_client_article_briefs_retain_exact_source_span_provenance(self):
        """Every workbench passage must retain its validated source identity."""
        deferred = self.brief_archive['briefs']
        span_count = 0
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
        self.assertGreater(span_count, len(self.articles))

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
            'Dossier coverage in this lens',
            'High-precision section presence only; not research quality, confidence, or a recommendation score.',
        ):
            self.assertIn(text, coverage)
        self.assertNotIn('documentation_score', coverage)

    def test_related_archive_context_explains_only_exact_metadata_overlap(self):
        start = self.html.index('function relatedArticleRows(selected)')
        end = self.html.index('\nfunction articleReasons', start)
        related = self.html[start:end]
        for text in (
            'selected.manager_keys',
            'selected.underlyings',
            'selected.instruments',
            'Same mentioned entity:',
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
        self.assertIn('Copy IC brief', self.html)
        self.assertIn('Print / PDF', self.html)
        self.assertRegex(
            self.html,
            re.compile(
                r"ARTICLE_BY_ID\.get\(copyBrief\.dataset\.copyBrief\).*?"
                r"copyText\(articleBriefText\(article\),'Institutional brief copied with source provenance'\)",
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
            'Latest Brief',
            'Evidence Monitor',
            'Research Library',
            'Decision Queue',
            'Research evidence',
            'Export CSV',
            'Copy view',
            'Source passage',
            'Parsed directional language',
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
        self.assertIn('<p class="sr-only">Navnoor Research Terminal</p>', self.html)
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
            '.app-header', '.kpi-strip', '.filter-rail', '.command-bar',
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
        self.assertIn('target.focus()', popstate)
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
        self.assertRegex(freshness, r'ageHours\s*>\s*16')
        self.assertNotRegex(freshness, r'ageHours\s*>\s*36')
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
        self.assertGreaterEqual(
            len(self.observation_bytes),
            500_000,
            'deferred observation payload is unexpectedly empty or incomplete',
        )
        self.assertLessEqual(
            len(self.observation_bytes),
            1_500_000,
            'deferred observation payload exceeded its reviewed 1.5 MB budget',
        )
        artifact_files = [path for path in self.site_dir.rglob('*') if path.is_file()]
        self.assertEqual(
            {path.relative_to(self.site_dir).as_posix() for path in artifact_files},
            {'index.html', 'article_briefs.json', 'observations.json'},
        )
        self.assertTrue(all(not path.is_symlink() for path in artifact_files))
        self.assertLess(
            sum(path.stat().st_size for path in artifact_files),
            3_000_000,
            'complete static artifact exceeded the reviewed 3.0 MB policy',
        )

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

        for surface in ('bg', 'surface-1', 'surface-2', 'surface-3', 'surface-raised'):
            channels = [int(dark[surface][index:index + 2], 16) for index in (1, 3, 5)]
            self.assertLessEqual(max(channels) - min(channels), 24, f'{surface} should remain cool graphite')
            self.assertLessEqual(channels[0], channels[1], f'{surface} should not carry a warm cast')
            self.assertLessEqual(channels[1], channels[2], f'{surface} should not carry a warm cast')
        self.assertGreaterEqual(contrast(light['text-muted'], light['selected']), 4.5)
        self.assertIn('background:var(--accent-strong);color:var(--on-accent)', self.html)
        self.assertIn('.primary-action:hover{background:var(--accent-hover);border-color:var(--accent-hover)}', self.html)
        self.assertIn('.primary-action:active{background:var(--accent-active);border-color:var(--accent-active)}', self.html)
        self.assertIn('#search:focus{border-color:var(--control-line)', self.html)
        self.assertIn('::selection{background:var(--selection-bg);color:var(--selection-text)}', self.html)
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
        self.assertIn("var theme = stored || 'light'", self.html)
        self.assertIn("localStorage.setItem('nrt-theme-revision',themeRevision)", self.html)
        self.assertGreaterEqual(self.html.count("getElementById('theme-color').content"), 3)
        self.assertIn("this.setAttribute('aria-label','Switch to '", self.html)
        self.assertIn('id="freshness-dot" aria-hidden="true"', self.html)
        self.assertIn('id="freshness-state">Unknown</span>', self.html)
        self.assertIn("const freshnessStatus = freshnessClass === 'stale' ? 'Stale'", self.html)
        self.assertIn("freshnessClass === 'fresh' ? 'Current'", self.html)
        self.assertIn("freshnessClass === 'degraded' ? 'Degraded'", self.html)
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
        self.assertRegex(self.html, r'\.documentation-badge\.complete\{[^}]*color:var\(--positive\)')
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
