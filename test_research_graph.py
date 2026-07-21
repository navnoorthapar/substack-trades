import json
import math
import re
import unittest
from pathlib import Path

import research_graph
from research_graph import build_related_graph, build_search_index


ROOT = Path(__file__).parent
NORMALIZED_TERM_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


def fixture_article(position, title, subtitle=''):
    return {
        'source': 'substack',
        'slug': f'fixture-{position}',
        'title': title,
        'subtitle': subtitle,
        'post_date': f'2026-01-{position + 1:02d}T00:00:00Z',
        'url': f'https://example.test/p/fixture-{position}',
        'brief': {
            'lead': {'text': title},
            'sections': [],
            'fallback_evidence': None,
            'checkpoints': [],
        },
    }


class ResearchGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.articles = json.loads(
            (ROOT / 'articles_index.json').read_text(encoding='utf-8')
        )
        cls.search_index = build_search_index(cls.articles)
        cls.related = build_related_graph(cls.articles, cls.search_index)
        cls.article_position = {
            f'{article["source"]}:{article["slug"]}': position
            for position, article in enumerate(cls.articles)
        }

    def test_normalization_handles_possessives_punctuation_greek_and_accents(self):
        cases = {
            'Hull-White’s': 'hull-white',
            'D.E. Shaw': 'd-e-shaw',
            'Γ Scalping': 'gamma-scalping',
            'S&P 500': 's-and-p-500',
            'Société Générale': 'societe-generale',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(research_graph._normalize_term(value), expected)

    def test_seed_aliases_and_precision_gated_capitalized_phrase_extraction(self):
        articles = [
            fixture_article(
                0,
                "Hull-White’s Γ Model Meets D.E. Shaw and the S&P 500",
                'GitHub Repository: https://example.test/private-noise',
            ),
            fixture_article(1, 'Acme Capital: Inventory Control Under Stress'),
            fixture_article(2, 'Crude & Capital: An Editorial Construction'),
        ]
        search_index = build_search_index(articles)
        first_entities = search_index['articles'][0]['entities']
        self.assertIn('hull-white', first_entities)
        self.assertIn('gamma', first_entities)
        self.assertIn('d-e-shaw', first_entities)
        self.assertIn('spx', first_entities)
        self.assertIn('acme-capital', search_index['articles'][1]['entities'])
        all_terms = set(search_index['entities'])
        self.assertFalse(any('github' in term for term in all_terms))
        self.assertNotIn('crude-and-capital', all_terms)

    def test_search_index_is_compact_deterministic_and_reverse_consistent(self):
        self.assertEqual(set(self.search_index), {'entities', 'articles'})
        rows = self.search_index['articles']
        inverted = self.search_index['entities']
        self.assertEqual(len(rows), len(self.articles))
        self.assertEqual(list(inverted), sorted(inverted))

        for position, (source, row) in enumerate(zip(self.articles, rows)):
            self.assertEqual(
                set(row),
                {'slug', 'source', 'title', 'post_date', 'url', 'entities'},
            )
            for field in ('slug', 'source', 'title', 'post_date', 'url'):
                self.assertEqual(row[field], source[field])
            self.assertEqual(row['entities'], sorted(set(row['entities'])))
            for entity in row['entities']:
                self.assertRegex(entity, NORMALIZED_TERM_RE)
                self.assertIn(position, inverted[entity])

        for entity, positions in inverted.items():
            self.assertRegex(entity, NORMALIZED_TERM_RE)
            self.assertEqual(positions, sorted(set(positions)))
            self.assertTrue(positions)
            expected = [
                position for position, row in enumerate(rows)
                if entity in row['entities']
            ]
            self.assertEqual(positions, expected)

        compact = json.dumps(
            self.search_index, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8')
        self.assertLess(len(compact), 500_000)
        self.assertEqual(build_search_index(self.articles), self.search_index)

    def test_ten_real_entity_lookups_resolve_plausible_articles(self):
        checks = {
            'citadel': (
                34,
                'de-shaw-citadel-and-renaissance-run',
            ),
            'jane-street': (
                12,
                'how-hrt-and-jane-street-made-225b',
            ),
            'millennium': (
                20,
                'how-citadel-millennium-and-aqr-apply-fractional-kelly-and-why-half-kelly-is-5-too-large-below-9f555ed24981',
            ),
            'optiver': (
                2,
                'optiver-wrote-the-neutral-fix-for',
            ),
            'gamma': (
                10,
                'gamma-scalping-and-the-volatility-risk-premium-the-formula-behind-jane-streets-4-3b-india-trade-1c04cfbeaaf1',
            ),
            'gold': (
                6,
                'gold-65-silver-144-in-2025-how-bridgewater',
            ),
            'black-scholes': (
                20,
                'black-scholes-delta-is-wrong-hull',
            ),
            'statistical-arbitrage': (
                16,
                'de-shaw-citadel-and-renaissance-run',
            ),
            'heston': (
                7,
                'how-quant-funds-made-6-annually-buying-rough-vol-the-heston-fourier-edge-422b76e195db',
            ),
            'vix': (
                11,
                'joint-spxvix-calibration-follow-up',
            ),
        }
        rows = self.search_index['articles']
        for term, (minimum_count, expected_slug) in checks.items():
            with self.subTest(term=term):
                positions = self.search_index['entities'].get(term)
                self.assertIsNotNone(positions)
                self.assertGreaterEqual(len(positions), minimum_count)
                slugs = {rows[position]['slug'] for position in positions}
                self.assertIn(expected_slug, slugs)

    def test_related_graph_covers_every_article_with_truthful_reasons(self):
        expected_keys = [
            f'{article["source"]}:{article["slug"]}'
            for article in self.articles
        ]
        self.assertEqual(list(self.related), expected_keys)

        search_rows = self.search_index['articles']
        entity_sets = [set(row['entities']) for row in search_rows]
        feature_sets = []
        for article in self.articles:
            features = set(research_graph._feature_counter(article['title']))
            features.update(research_graph._feature_counter(
                research_graph._clean_subtitle(article)
            ))
            features.update(research_graph._feature_counter(
                ' '.join(research_graph._brief_text_parts(article))
            ))
            feature_sets.append(features)

        for first_index, key in enumerate(expected_keys):
            related_rows = self.related[key]
            self.assertEqual(len(related_rows), 5)
            related_keys = [
                f'{row["source"]}:{row["slug"]}' for row in related_rows
            ]
            self.assertEqual(len(related_keys), len(set(related_keys)))
            self.assertNotIn(key, related_keys)
            ordering = [(-row['score'], related_key)
                        for row, related_key in zip(related_rows, related_keys)]
            self.assertEqual(ordering, sorted(ordering))

            for row, related_key in zip(related_rows, related_keys):
                self.assertEqual(
                    set(row),
                    {'slug', 'source', 'title', 'url', 'score', 'why'},
                )
                second_index = self.article_position[related_key]
                source = self.articles[second_index]
                for field in ('slug', 'source', 'title', 'url'):
                    self.assertEqual(row[field], source[field])
                self.assertIs(type(row['score']), float)
                self.assertTrue(math.isfinite(row['score']))
                self.assertGreater(row['score'], 0.0)
                self.assertLessEqual(row['score'], 1.0)
                self.assertEqual(row['score'], round(row['score'], 6))
                self.assertIsInstance(row['why'], list)
                self.assertGreaterEqual(len(row['why']), 1)
                self.assertLessEqual(len(row['why']), 3)
                self.assertEqual(len(row['why']), len(set(row['why'])))
                for reason in row['why']:
                    self.assertRegex(
                        reason,
                        r'^shared: [a-z0-9]+(?:-[a-z0-9]+)*$',
                    )
                    term = reason.removeprefix('shared: ')
                    shared_entity = (
                        term in entity_sets[first_index]
                        and term in entity_sets[second_index]
                    )
                    shared_feature = (
                        term in feature_sets[first_index]
                        and term in feature_sets[second_index]
                    )
                    self.assertTrue(
                        shared_entity or shared_feature,
                        f'{key} -> {related_key} invented reason {reason}',
                    )
                    self.assertFalse(term.startswith(('family-', 'source-')))

    def test_five_grounded_articles_have_sensible_related_membership(self):
        checks = {
            'substack:optiver-wrote-the-neutral-fix-for': {
                'optivers-35b-market-making-engine',
                'prediction-market-arbitrage-how-quants',
            },
            'medium:gamma-scalping-and-the-volatility-risk-premium-the-formula-behind-jane-streets-4-3b-india-trade-1c04cfbeaaf1': {
                'gamma-scalping-and-the-volatility-risk-premium-citadels-alpha-engine-the-gamestop-squeeze-and-1d5360e67675',
                'volatility-trading-strategies-used-by-capstone-citadel-variance-swaps-dispersion-trading-and-21931589df8b',
                'delta-hedging-how-the-volatility-risk-premium-powers-institutional-finance-and-why-the-same-mec-f7216e40c3fe',
            },
            'substack:gold-65-silver-144-in-2025-how-bridgewater': {
                'deconstructing-headlands-1b-algorithmic-trading-strategy-temporal-arbitrage-in-precious-metals-ff1109506e0b',
                'how-deutsche-banks-precious-metals-desk-generated-systematic-alpha-through-market-manipulation-a7f0bc6b3edc',
                'eight-banks-paid-13b-for-silver-manipulation',
            },
            'substack:hull-whites-mean-reversion-problem': {
                'black-scholes-delta-is-wrong-hull',
                'de-shaw-citadel-and-renaissance-run',
                'statistical-arbitrage-the-quant-strategy',
            },
            'substack:de-shaw-citadel-and-renaissance-run': {
                'statistical-arbitrage-the-quant-strategy',
                'renaissance-technologies-the-100',
                'de-shaws-1998-crisis-how-a-372-million',
            },
        }
        for key, expected_slugs in checks.items():
            with self.subTest(article=key):
                actual = {row['slug'] for row in self.related[key]}
                self.assertGreaterEqual(len(actual & expected_slugs), 2)

    def test_related_graph_is_deterministic_and_rejects_tiny_snapshots(self):
        self.assertEqual(
            build_related_graph(self.articles, self.search_index),
            self.related,
        )
        tiny = [fixture_article(index, f'Unique Topic {index}') for index in range(5)]
        with self.assertRaisesRegex(ValueError, 'at least 6 articles'):
            build_related_graph(tiny, build_search_index(tiny))


if __name__ == '__main__':
    unittest.main()
