#!/usr/bin/env python3
"""Tests for deterministic, source-grounded research threads."""
import copy
import json
import unittest
from pathlib import Path

from research_graph import build_search_index
from research_threads import (
    build_thread_index,
    entity_kind,
    entity_label,
    validate_thread_index,
)


ROOT = Path(__file__).parent


def article(
        slug, title, published_at, lead, source='substack',
        content_status='full'):
    return {
        'source': source,
        'source_id': slug,
        'slug': slug,
        'title': title,
        'subtitle': '',
        'post_date': published_at,
        'url': f'https://example.test/{slug}',
        'audience': 'everyone',
        'wordcount': 100,
        'content_status': content_status,
        'family': 'other',
        'brief': {
            'schema_version': 1,
            'body_sha256': '0' * 64,
            'lead': {
                'text': lead,
                'start': 0,
                'end': len(lead),
                'sha256': '1' * 64,
                'truncated': False,
            },
            'sections': [],
            'fallback_evidence': None,
            'checkpoints': [],
        },
    }


def client_row(value):
    return {
        'id': f"a_{value['slug']}",
        'title': value['title'],
        'subtitle': value['subtitle'],
        'url': value['url'],
        'published_at': value['post_date'],
        'brief': value['brief'],
        'publication_precision': (
            'day' if len(value['post_date']) == 10 else 'instant'
        ),
    }


class ResearchThreadTests(unittest.TestCase):
    def setUp(self):
        self.articles = [
            article(
                'first',
                'Optiver and VIX market making',
                '2026-01-02T08:00:00Z',
                'Optiver discussed VIX liquidity within market structure.',
            ),
            article(
                'second',
                'How Optiver approaches venue design',
                '2026-01-02T12:00:00Z',
                'Optiver proposed changes to market structure.',
            ),
            article(
                'third',
                'VIX dispersion after a volatility shock',
                '2026-02-03T09:00:00Z',
                'VIX dispersion changed after the volatility shock.',
            ),
            article(
                'fourth',
                'VIX and options capacity',
                '2026-03-04T09:00:00Z',
                'VIX options capacity remained the central question.',
            ),
            article(
                'unrelated',
                'Copper inventory mechanics',
                '2026-03-05T09:00:00Z',
                'Copper inventories changed across exchanges.',
            ),
        ]
        self.clients = [client_row(value) for value in self.articles]
        self.search = build_search_index(self.articles)
        self.index = build_thread_index(self.clients, self.search)

    def test_repeated_topics_are_chronological_and_exact(self):
        self.assertIn('optiver', self.index['topics'])
        self.assertIn('vix', self.index['topics'])
        self.assertEqual(
            self.index['topics']['optiver']['article_ids'],
            ['a_first', 'a_second'],
        )
        self.assertEqual(
            self.index['topics']['vix']['article_ids'],
            ['a_first', 'a_third', 'a_fourth'],
        )
        self.assertEqual(
            self.index['topics']['market-structure']['match_codes'],
            ['o', 'o'],
        )
        self.assertNotIn('copper', self.index['topics'])
        self.assertNotIn('a_unrelated', self.index['defaults'])

    def test_title_match_then_specificity_selects_default_topic(self):
        self.assertEqual(self.index['defaults']['a_first'], 'optiver')

    def test_full_timestamp_orders_same_day_publications(self):
        ids = self.index['topics']['optiver']['article_ids']
        published = {
            value['id']: value['published_at'] for value in self.clients
        }
        self.assertEqual(ids, ['a_first', 'a_second'])
        self.assertEqual(
            [published[article_id] for article_id in ids],
            ['2026-01-02T08:00:00Z', '2026-01-02T12:00:00Z'],
        )

    def test_output_is_deterministic(self):
        repeated = build_thread_index(self.clients, self.search)
        self.assertEqual(self.index, repeated)

    def test_validation_rejects_an_ungrounded_membership(self):
        corrupted = copy.deepcopy(self.index)
        corrupted['defaults']['a_first'] = 'copper'
        with self.assertRaisesRegex(ValueError, 'invalid default topic'):
            validate_thread_index(corrupted, self.clients, self.search)

    def test_validation_rejects_membership_not_owned_by_search_index(self):
        corrupted = copy.deepcopy(self.index)
        corrupted_clients = copy.deepcopy(self.clients)
        unrelated = next(
            value for value in corrupted_clients if value['id'] == 'a_unrelated'
        )
        unrelated['title'] = 'VIX copper inventory mechanics'
        corrupted['topics']['vix']['article_ids'].append('a_unrelated')
        corrupted['topics']['vix']['match_codes'].append('t')
        corrupted['topics']['vix']['article_count'] += 1
        corrupted['defaults']['a_unrelated'] = 'vix'
        corrupted['article_count'] += 1
        with self.assertRaisesRegex(ValueError, 'not owned by the search index'):
            validate_thread_index(
                corrupted, corrupted_clients, self.search,
            )

    def test_validation_rejects_inconsistent_display_metadata(self):
        corrupted = copy.deepcopy(self.index)
        corrupted['topics']['vix']['label'] = 'Volatility certainty'
        with self.assertRaisesRegex(ValueError, 'label is inconsistent'):
            validate_thread_index(corrupted, self.clients, self.search)

        corrupted = copy.deepcopy(self.index)
        corrupted['topics']['vix']['kind'] = 'recommendation'
        with self.assertRaisesRegex(ValueError, 'kind is inconsistent'):
            validate_thread_index(corrupted, self.clients, self.search)

    def test_missing_search_owner_and_naive_timestamp_fail_closed(self):
        missing = copy.deepcopy(self.clients)
        missing[0]['url'] = 'https://example.test/not-indexed'
        with self.assertRaisesRegex(ValueError, 'absent from search index'):
            build_thread_index(missing, self.search)

        naive = copy.deepcopy(self.clients)
        naive[0]['published_at'] = '2026-01-02T08:00:00'
        with self.assertRaisesRegex(ValueError, 'no timezone'):
            build_thread_index(naive, self.search)

        imprecise = copy.deepcopy(self.clients)
        imprecise[0]['publication_precision'] = 'day'
        with self.assertRaisesRegex(ValueError, 'precision is inconsistent'):
            build_thread_index(imprecise, self.search)

    def test_labels_and_kinds_are_deliberately_broad(self):
        self.assertEqual(entity_label('spx'), 'S&P 500')
        self.assertEqual(entity_label('d-e-shaw'), 'D. E. Shaw')
        self.assertEqual(entity_label('market-making'), 'Market Making')
        self.assertEqual(entity_kind('vix'), 'market / instrument')
        self.assertEqual(entity_kind('black-scholes'), 'model / mechanism')
        self.assertEqual(entity_kind('optiver'), 'organization / institution')

    def test_real_body_backed_archive_has_useful_compact_coverage(self):
        payload = json.loads(
            (ROOT / 'articles_index.json').read_text(encoding='utf-8'),
        )
        source = payload.get('articles', payload) if isinstance(payload, dict) else payload
        body_articles = [
            value for value in source
            if value.get('content_status') != 'registry'
        ]
        search = build_search_index(source)
        clients = [
            {
                'id': f"{value['source']}:{value['slug']}",
                'title': value['title'],
                'subtitle': value.get('subtitle') or '',
                'url': value['url'],
                'published_at': value['post_date'],
                'publication_precision': (
                    'day' if len(value['post_date']) == 10 else 'instant'
                ),
                'brief': value.get('brief'),
            }
            for value in body_articles
        ]
        index = build_thread_index(clients, search)
        compact = json.dumps(
            index, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8')
        self.assertGreaterEqual(index['article_count'], 250)
        self.assertGreaterEqual(index['topic_count'], 50)
        self.assertLess(len(compact), 150_000)


if __name__ == '__main__':
    unittest.main()
