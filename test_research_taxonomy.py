import json
import unittest
from pathlib import Path

import research_taxonomy


ROOT = Path(__file__).parent


class ResearchTaxonomyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.articles = json.loads(
            (ROOT / 'articles_index.json').read_text(encoding='utf-8')
        )
        cls.fixture = json.loads(
            (ROOT / 'family_classification_fixture.json').read_text(encoding='utf-8')
        )

    def test_committed_twenty_article_fixture_has_full_agreement(self):
        self.assertEqual(self.fixture['schema_version'], 1)
        examples = self.fixture['examples']
        self.assertEqual(len(examples), 20)
        by_identity = {
            (article['source'], article['slug']): article
            for article in self.articles
        }
        agreements = 0
        for example in examples:
            identity = (example['source'], example['slug'])
            self.assertIn(identity, by_identity)
            actual = research_taxonomy.classify_family(by_identity[identity])
            agreements += actual == example['family']
        self.assertGreaterEqual(agreements / len(examples), 0.85)
        self.assertEqual(agreements, len(examples))

    def test_every_current_article_gets_exactly_one_allowed_family(self):
        classified = research_taxonomy.add_families(self.articles)
        self.assertEqual(len(classified), len(self.articles))
        for original, article in zip(self.articles, classified):
            self.assertIsNot(original, article)
            self.assertEqual(
                article['family'], research_taxonomy.classify_family(original),
            )
            self.assertIn(article['family'], research_taxonomy.ALLOWED_FAMILIES)

        families = research_taxonomy.build_families_index(classified)
        self.assertEqual(tuple(families), research_taxonomy.ALLOWED_FAMILIES)
        slugs = [slug for family in families.values() for slug in family]
        self.assertEqual(len(slugs), len(self.articles))
        self.assertEqual(set(slugs), {article['slug'] for article in self.articles})

    def test_classifier_uses_safe_other_abstention(self):
        article = {
            'title': 'A General Introduction to Portfolio Mathematics',
            'subtitle': 'Definitions and worked examples.',
            'brief': {'lead': None, 'sections': [], 'fallback_evidence': None},
        }
        self.assertEqual(research_taxonomy.classify_family(article), 'other')

    def test_duplicate_family_index_slugs_fail_closed(self):
        rows = [
            {'slug': 'same', 'family': 'other'},
            {'slug': 'same', 'family': 'other'},
        ]
        with self.assertRaisesRegex(ValueError, 'missing or duplicate slug'):
            research_taxonomy.build_families_index(rows)


if __name__ == '__main__':
    unittest.main()
