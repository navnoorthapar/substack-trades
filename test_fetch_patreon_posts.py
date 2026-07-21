import json
import tempfile
import unittest
from pathlib import Path

import fetch_patreon_posts


def resource(source_id, viewable=False):
    return {
        'type': 'post',
        'id': str(source_id),
        'attributes': {
            'title': f'Public catalogue title {source_id}',
            'published_at': '2026-07-20T10:00:00+00:00',
            'url': (
                'https://www.patreon.com/NavnoorBawa/posts/'
                f'public-catalogue-title-{source_id}'
            ),
            'current_user_can_view': viewable,
            'was_posted_by_campaign_owner': True,
        },
    }


class SparsePages:
    def __init__(self):
        self.urls = []
        self.next_url = (
            'https://www.patreon.com/api/posts?'
            'filter%5Bcampaign_id%5D=14781816&page%5Bcursor%5D=second'
        )

    def __call__(self, url):
        self.urls.append(url)
        if len(self.urls) == 1:
            return {
                'data': [resource(index) for index in range(1000, 1100)],
                'links': {'next': self.next_url},
                'meta': {'pagination': {'total': 120}},
            }
        if url != self.next_url:
            raise AssertionError('unexpected cursor URL')
        return {
            'data': [resource(index, viewable=index == 1102)
                     for index in range(1100, 1103)],
            'links': {'next': None},
            'meta': {'pagination': {'total': 103}},
        }


class PatreonFetcherTests(unittest.TestCase):
    def test_sparse_json_api_pages_produce_only_registry_contract_fields(self):
        pages = SparsePages()
        rows = fetch_patreon_posts.fetch_registry(pages)
        self.assertEqual(len(rows), 103)
        self.assertEqual(len(pages.urls), 2)
        self.assertEqual(
            set(rows[0]),
            {'source_id', 'title', 'url', 'post_date', 'access'},
        )
        self.assertNotIn('payment_amount', rows[0])
        self.assertEqual(
            next(row for row in rows if row['source_id'] == '1102')['access'],
            'public',
        )

    def test_off_origin_pagination_and_malformed_access_fail_closed(self):
        def off_origin(_url):
            return {'data': [resource(1001)],
                    'links': {'next': 'https://example.com/api/posts'}}

        with self.assertRaisesRegex(ValueError, 'approved API origin'):
            fetch_patreon_posts.fetch_registry(off_origin)

        item = resource(1001)
        item['attributes']['current_user_can_view'] = 'false'
        with self.assertRaisesRegex(ValueError, 'must be boolean'):
            fetch_patreon_posts.fetch_registry(
                lambda _url: {'data': [item], 'links': {'next': None}},
            )

    def test_valid_cached_snapshot_is_retained_on_failure(self):
        cached = [{
            'source_id': '1001',
            'title': 'Public catalogue title 1001',
            'url': (
                'https://www.patreon.com/NavnoorBawa/posts/'
                'public-catalogue-title-1001'
            ),
            'post_date': '2026-07-20',
            'access': 'paid',
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'patreon.json'
            path.write_text(json.dumps(cached), encoding='utf-8')

            def fail(_url):
                raise OSError('temporary network failure')

            rows, status = fetch_patreon_posts.refresh_registry(path, fail)
            self.assertEqual(rows, cached)
            self.assertEqual(status['status'], 'cached-fallback')

    def test_without_a_valid_cache_fetch_failure_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing.json'

            def fail(_url):
                raise OSError('temporary network failure')

            with self.assertRaises(OSError):
                fetch_patreon_posts.refresh_registry(missing, fail)

    def test_invalid_cache_does_not_block_a_fresh_complete_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.json'
            path.write_text('{not json', encoding='utf-8')
            rows, status = fetch_patreon_posts.refresh_registry(
                path,
                lambda _url: {
                    'data': [resource(1001)],
                    'links': {'next': None},
                },
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(status['status'], 'fresh')


if __name__ == '__main__':
    unittest.main()
