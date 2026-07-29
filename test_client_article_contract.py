import unittest
from typing import Any, Tuple

from client_article_contract import (
    ARTICLE_WIRE_SCHEMA_VERSION,
    compact_client_article,
    hydrate_client_article,
)

CURRENT_BODY_PROVENANCE = {
    'body_revision_status': 'current',
    'source_updated_at': '2026-01-02T08:00:00Z',
    'observed_source_updated_at': '2026-01-02T08:00:00Z',
}


def full_article(**updates):
    article = {
        'id': 'a_0123456789abcd',
        'title': 'Example',
        'subtitle': '',
        'date': '2026-01-02',
        'published_at': '2026-01-02T08:00:00Z',
        'publication_precision': 'instant',
        'url': 'https://example.test/article',
        'source': 'substack',
        'alternate_urls': {},
        'wordcount': 550,
        'read_minutes': 2,
        'content_status': 'full',
        **CURRENT_BODY_PROVENANCE,
        'brief': None,
        'brief_features': {
            'lead': True,
            'evidence': True,
            'countercase': True,
            'falsifier': True,
            'implementation': True,
            'mechanism': True,
            'checkpoint_count': 3,
        },
        'idea_ids': ['i_0123456789abcd', 'i_fedcba98765432'],
        'trade_count': 2,
        'directions': ['long'],
        'instruments': ['options'],
        'underlyings': ['VIX'],
        'managers': [],
        'manager_keys': [],
        'has_quant': True,
        'has_thesis': False,
        'has_outcome': True,
    }
    article.update(updates)
    return article


class ClientArticleContractTests(unittest.TestCase):
    def test_schema_version_and_full_round_trip(self):
        self.assertEqual(ARTICLE_WIRE_SCHEMA_VERSION, 2)
        original = full_article()
        compact = compact_client_article(original)

        for key in (
            'date', 'publication_precision', 'read_minutes', 'trade_count',
            'brief_features', 'has_quant', 'has_thesis', 'has_outcome',
            'alternate_urls', 'brief', 'managers', 'manager_keys',
        ):
            self.assertNotIn(key, compact)
        self.assertEqual(compact['_b'], [63, 3])
        self.assertEqual(compact['_q'], 5)
        self.assertEqual(
            compact['idea_ids'],
            ['i_0123456789abcd', 'i_fedcba98765432'],
        )
        self.assertEqual(hydrate_client_article(compact), original)
        self.assertNotIn('date', compact)
        self.assertNotIn('_b', original)

    def test_absent_defaults_restore_fresh_runtime_containers(self):
        original = full_article(
            published_at='2026-01-03',
            date='2026-01-03',
            publication_precision='day',
            wordcount=0,
            read_minutes=0,
            brief_features={
                'lead': False,
                'evidence': False,
                'countercase': False,
                'falsifier': False,
                'implementation': False,
                'mechanism': False,
                'checkpoint_count': 0,
            },
            idea_ids=[],
            trade_count=0,
            directions=[],
            instruments=[],
            underlyings=[],
            has_quant=False,
            has_thesis=False,
            has_outcome=False,
        )
        compact = compact_client_article(original)
        self.assertNotIn('_b', compact)
        self.assertNotIn('_q', compact)
        self.assertNotIn('idea_ids', compact)

        first = hydrate_client_article(compact)
        second = hydrate_client_article(compact)
        self.assertEqual(first, original)
        self.assertEqual(first['idea_ids'], [])
        self.assertIsNot(first['idea_ids'], second['idea_ids'])
        self.assertIsNot(first['alternate_urls'], second['alternate_urls'])

    def test_present_idea_ids_must_be_a_real_list(self):
        invalid_values: Tuple[Any, ...] = (
            None, 'i_1', ('i_1',), {}, False, 0,
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, 'idea_ids must be a list'):
                    hydrate_client_article({
                        'id': 'a_0123456789abcd',
                        'title': 'Example',
                        'subtitle': '',
                        'published_at': '2026-01-03',
                        'url': 'https://example.test/article',
                        'source': 'substack',
                        'wordcount': 0,
                        'content_status': 'full',
                        **CURRENT_BODY_PROVENANCE,
                        'idea_ids': invalid,
                    })

    def test_all_present_default_containers_are_type_checked(self):
        cases = {
            'alternate_urls': [],
            'brief': [],
            'directions': {},
            'instruments': 'options',
            'underlyings': None,
            'managers': (),
            'manager_keys': False,
        }
        for key, invalid in cases.items():
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    hydrate_client_article({
                        'id': 'a_0123456789abcd',
                        'title': 'Example',
                        'subtitle': '',
                        'published_at': '2026-01-03',
                        'url': 'https://example.test/article',
                        'source': 'substack',
                        'wordcount': 0,
                        'content_status': 'full',
                        **CURRENT_BODY_PROVENANCE,
                        key: invalid,
                    })

    def test_compact_masks_require_exact_types_and_ranges(self):
        invalid_brief_codes = (
            None,
            (1, 0),
            [1],
            [1, 0, 0],
            [True, 0],
            [-1, 0],
            [64, 0],
            [1, False],
            [1, -1],
            [1, 4],
        )
        for value in invalid_brief_codes:
            with self.subTest(brief=value):
                with self.assertRaises(ValueError):
                    hydrate_client_article({
                        'id': 'a_0123456789abcd',
                        'title': 'Example',
                        'subtitle': '',
                        'published_at': '2026-01-03',
                        'url': 'https://example.test/article',
                        'source': 'substack',
                        'wordcount': 0,
                        'content_status': 'full',
                        **CURRENT_BODY_PROVENANCE,
                        '_b': value,
                    })

        invalid_coverage_masks: Tuple[Any, ...] = (None, False, 1.0, -1, 8)
        for coverage_value in invalid_coverage_masks:
            with self.subTest(coverage=coverage_value):
                with self.assertRaises(ValueError):
                    hydrate_client_article({
                        'id': 'a_0123456789abcd',
                        'title': 'Example',
                        'subtitle': '',
                        'published_at': '2026-01-03',
                        'url': 'https://example.test/article',
                        'source': 'substack',
                        'wordcount': 0,
                        'content_status': 'full',
                        **CURRENT_BODY_PROVENANCE,
                        '_q': coverage_value,
                    })

    def test_wire_rejects_runtime_fields_and_invalid_base_values(self):
        for key, value in (
            ('date', '2026-01-03'),
            ('publication_precision', 'day'),
            ('read_minutes', 1),
            ('trade_count', 0),
            ('brief_features', {}),
            ('has_quant', False),
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'runtime fields'):
                    hydrate_client_article({
                        'id': 'a_0123456789abcd',
                        'title': 'Example',
                        'subtitle': '',
                        'published_at': '2026-01-03',
                        'url': 'https://example.test/article',
                        'source': 'substack',
                        'wordcount': 0,
                        'content_status': 'full',
                        **CURRENT_BODY_PROVENANCE,
                        key: value,
                    })

        invalid_rows = (
            {'wordcount': 0},
            {'published_at': '2026-02-30', 'wordcount': 0},
            {'published_at': '2026-01-03T12:00:00', 'wordcount': 0},
            {'published_at': '2026-01-03', 'wordcount': False},
            {'published_at': '2026-01-03', 'wordcount': -1},
        )
        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    hydrate_client_article(row)

    def test_read_time_matches_javascript_bankers_rounding(self):
        cases = {
            0: 0,
            1: 1,
            110: 1,
            111: 1,
            330: 2,
            550: 2,
            770: 4,
        }
        for wordcount, expected in cases.items():
            with self.subTest(wordcount=wordcount):
                hydrated = hydrate_client_article({
                    'id': 'a_0123456789abcd',
                    'title': 'Example',
                    'subtitle': '',
                    'published_at': '2026-01-03',
                    'url': 'https://example.test/article',
                    'source': 'substack',
                    'wordcount': wordcount,
                    'content_status': 'full',
                    **CURRENT_BODY_PROVENANCE,
                })
                self.assertEqual(hydrated['read_minutes'], expected)

    def test_compactor_rejects_inconsistent_or_malformed_full_rows(self):
        mutations = (
            {'date': '2026-01-01'},
            {'publication_precision': 'day'},
            {'read_minutes': 3},
            {'trade_count': 1},
            {'has_quant': 1},
            {'brief_features': {
                'lead': True,
                'evidence': True,
                'countercase': True,
                'falsifier': True,
                'implementation': True,
                'mechanism': True,
                'checkpoint_count': 4,
            }},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    compact_client_article(full_article(**mutation))

        with self.assertRaisesRegex(ValueError, 'reserved compact wire keys'):
            compact_client_article(full_article(_q=1))

    def test_body_revision_provenance_is_required_and_fail_closed(self):
        current = compact_client_article(full_article())
        self.assertEqual(
            hydrate_client_article(current)['body_revision_status'],
            'current',
        )

        for field in CURRENT_BODY_PROVENANCE:
            with self.subTest(missing=field):
                invalid = dict(current)
                invalid.pop(field)
                with self.assertRaisesRegex(ValueError, 'missing fields'):
                    hydrate_client_article(invalid)

        valid_prior = full_article(
            content_status='excerpt',
            body_revision_status='prior',
            source_updated_at='2026-01-01T08:00:00Z',
            observed_source_updated_at='2026-01-02T08:00:00Z',
        )
        self.assertEqual(
            hydrate_client_article(
                compact_client_article(valid_prior)
            )['body_revision_status'],
            'prior',
        )
        valid_unverified = full_article(
            content_status='excerpt',
            body_revision_status='unverified',
            source_updated_at='',
            observed_source_updated_at='2026-01-02T08:00:00Z',
        )
        self.assertEqual(
            hydrate_client_article(
                compact_client_article(valid_unverified)
            )['body_revision_status'],
            'unverified',
        )

        invalid_rows = (
            full_article(
                source_updated_at='',
                observed_source_updated_at='',
            ),
            full_article(
                source_updated_at='2026-01-01T08:00:00Z',
                observed_source_updated_at='2026-01-02T08:00:00Z',
            ),
            full_article(
                body_revision_status='prior',
                source_updated_at='2026-01-01T08:00:00Z',
                observed_source_updated_at='2026-01-02T08:00:00Z',
            ),
            full_article(body_revision_status='unverified'),
        )
        for row in invalid_rows:
            with self.subTest(status=row['body_revision_status']):
                with self.assertRaises(ValueError):
                    compact_client_article(row)


if __name__ == '__main__':
    unittest.main()
