"""Regression tests for nullable Substack list-response bodies."""

import json
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import fetch_all_posts


class JsonResponse:
    """Minimal urlopen response for deterministic detail-fetch tests."""

    def __init__(self, payload, url):
        self._body = json.dumps(payload).encode('utf-8')
        self._url = url
        self.status = 200
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status


class SubstackNullableBodyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.posts_path = self.root / 'all_posts.candidate.json'
        self.articles_path = self.root / 'articles.candidate.json'
        self.previous_path = self.root / 'all_posts.previous.json'
        self.status_path = self.root / 'substack-status.json'

    def list_post(
            self, slug='alpha-research',
            updated_at='2026-07-27T12:00:00.000Z',
            body_html=None, truncated_body_text=''):
        return {
            'id': 101,
            'slug': slug,
            'title': 'Alpha research',
            'subtitle': 'A source-grounded test article',
            'post_date': '2026-07-27T11:00:00.000Z',
            'updated_at': updated_at,
            'canonical_url': (
                'https://navnoorbawa.substack.com/p/' + slug
            ),
            'audience': 'everyone',
            'meter_type': 'default',
            'type': 'newsletter',
            'is_published': True,
            'wordcount': 42,
            'body_html': body_html,
            'truncated_body_text': truncated_body_text,
        }

    def cached_record(
            self, slug='alpha-research',
            source_updated_at='2026-07-27T12:00:00.000Z'):
        body_text = ' '.join(
            f'previously-captured-{index}' for index in range(50)
        )
        return {
            'source': 'substack',
            'source_id': slug,
            'slug': slug,
            'title': 'Alpha research',
            'subtitle': 'A source-grounded test article',
            'post_date': '2026-07-27T11:00:00.000Z',
            'source_updated_at': source_updated_at,
            'url': 'https://navnoorbawa.substack.com/p/' + slug,
            'audience': 'everyone',
            'meter_type': 'default',
            'type': 'newsletter',
            'is_published': True,
            'wordcount': 42,
            'body_text': body_text,
            'body_html_length': 137,
            'content_status': 'full',
        }

    def write_previous(self, records):
        self.previous_path.write_text(
            json.dumps(records),
            encoding='utf-8',
        )

    def read_json(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    def run_fetch(self, posts, detail_effect):
        def detail_urlopen(request, *args, **kwargs):
            url = request.full_url
            if isinstance(detail_effect, BaseException):
                raise detail_effect
            payload = (
                detail_effect(request)
                if callable(detail_effect)
                else detail_effect
            )
            return JsonResponse(payload, url)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'POSTS_PATH', self.posts_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'ARTICLE_INDEX_PATH', self.articles_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'PREVIOUS_POSTS_PATH', self.previous_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'FETCH_STATUS_PATH', self.status_path,
            ))
            list_fetch = stack.enter_context(mock.patch.object(
                fetch_all_posts, 'fetch_posts', return_value=posts,
            ))
            detail_fetch = stack.enter_context(mock.patch.object(
                fetch_all_posts.urllib.request,
                'urlopen',
                side_effect=detail_urlopen,
            ))
            stack.enter_context(mock.patch.object(fetch_all_posts.time, 'sleep'))
            fetch_all_posts.main()
        self.assertEqual(
            list_fetch.call_args_list,
            [
                mock.call(limit=50, offset=0),
                mock.call(limit=50, offset=0),
            ],
        )
        return detail_fetch

    def assert_exact_detail_urls(self, detail_fetch, slug):
        self.assertGreater(detail_fetch.call_count, 0)
        expected_suffix = '/api/v1/posts/' + slug
        for call in detail_fetch.call_args_list:
            request = call.args[0]
            self.assertTrue(
                request.full_url.endswith(expected_suffix),
                request.full_url,
            )

    def test_null_list_body_is_hydrated_from_exact_slug_detail(self):
        listed = self.list_post()
        detail_html = '<p>' + ' '.join(
            f'authored{index}' for index in range(50)
        ) + '</p>'
        detail = dict(listed)
        detail['body_html'] = detail_html

        detail_fetch = self.run_fetch([listed], detail)

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        self.assertEqual(detail_fetch.call_count, 1)
        stored = self.read_json(self.posts_path)
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            stored[0]['body_text'],
            fetch_all_posts.strip_html(detail_html),
        )
        self.assertEqual(stored[0]['body_html_length'], len(detail_html))
        self.assertEqual(stored[0]['content_status'], 'full')
        self.assertEqual(
            stored[0]['source_updated_at'],
            listed['updated_at'],
        )
        self.assertEqual(
            self.read_json(self.articles_path)[0]['content_status'],
            'full',
        )
        self.assertEqual(self.read_json(self.status_path)['status'], 'ok')

    def test_detail_cannot_lower_list_wordcount_to_spoof_full_coverage(self):
        listed = self.list_post(slug='wordcount-spoof')
        listed['wordcount'] = 4_000
        detail = dict(listed)
        detail['wordcount'] = 10
        detail['body_html'] = '<p>' + ' '.join(
            f'preview{index}' for index in range(50)
        ) + '</p>'

        detail_fetch = self.run_fetch([listed], detail)

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(
            stored['body_text'],
            fetch_all_posts.strip_html(detail['body_html']),
        )
        status = self.read_json(self.status_path)
        self.assertEqual(status['status'], 'degraded')
        self.assertEqual(
            status['mode'],
            'complete_api_degraded_body_provenance',
        )

    def test_body_below_ninety_seven_percent_is_not_labeled_full(self):
        listed = self.list_post(slug='near-complete-preview')
        detail = dict(listed)
        detail['body_html'] = '<p>' + ' '.join(
            f'preview{index}' for index in range(40)
        ) + '</p>'

        detail_fetch = self.run_fetch([listed], detail)

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

    def test_detail_revision_mismatch_is_never_labeled_full(self):
        listed = self.list_post(slug='revision-race')
        detail = dict(listed)
        detail['updated_at'] = '2026-07-27T12:00:01.000Z'
        detail['body_html'] = '<p>' + ' '.join(
            f'changed{index}' for index in range(50)
        ) + '</p>'

        detail_fetch = self.run_fetch([listed], detail)

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(stored['body_text'], '')
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

    def test_paid_list_excerpt_never_requests_or_claims_full_content(self):
        listed = self.list_post(
            slug='paid-teaser',
            truncated_body_text='Short exact list excerpt.',
        )
        listed['audience'] = 'only_paid'
        listed['wordcount'] = 4_111

        detail_fetch = self.run_fetch(
            [listed],
            AssertionError('paid list excerpt requested the detail endpoint'),
        )

        detail_fetch.assert_not_called()
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], listed['truncated_body_text'])
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(stored['body_html_length'], 0)
        self.assertEqual(
            self.read_json(self.articles_path)[0]['content_status'],
            'excerpt',
        )
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

    def test_paid_row_without_list_excerpt_never_requests_detail(self):
        listed = self.list_post(
            slug='paid-detail-teaser',
            truncated_body_text='',
        )
        listed['audience'] = 'only_paid'

        detail_fetch = self.run_fetch(
            [listed],
            AssertionError('access-limited row requested detail HTML'),
        )

        detail_fetch.assert_not_called()
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], '')
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(stored['body_html_length'], 0)
        self.assertEqual(stored['body_source'], 'metadata-only')
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

    def test_matching_source_update_reuses_cached_body_without_detail_fetch(self):
        previous = self.cached_record()
        self.write_previous([previous])
        listed = self.list_post(
            updated_at=previous['source_updated_at'],
            truncated_body_text='A list-response preview must not replace it.',
        )

        detail_fetch = self.run_fetch(
            [listed],
            AssertionError('unchanged cached body requested detail'),
        )

        detail_fetch.assert_not_called()
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(
            {key: stored[key] for key in previous},
            previous,
        )
        self.assertEqual(
            set(stored) - set(previous),
            {
                'body_source',
                'body_revision_status',
                'observed_source_updated_at',
            },
        )
        self.assertEqual(stored['body_source'], 'cached-unchanged')
        self.assertEqual(stored['body_revision_status'], 'current')
        self.assertEqual(
            stored['observed_source_updated_at'],
            listed['updated_at'],
        )
        self.assertEqual(self.read_json(self.status_path)['status'], 'ok')

    def test_matching_revision_cannot_hide_increased_current_wordcount(self):
        previous = self.cached_record()
        previous['wordcount'] = 50
        self.write_previous([previous])
        listed = self.list_post(
            updated_at=previous['source_updated_at'],
            truncated_body_text='A bounded exact current preview.',
        )
        listed['wordcount'] = 100

        detail_fetch = self.run_fetch(
            [listed],
            urllib.error.URLError('detail unavailable'),
        )

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], previous['body_text'])
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(stored['body_source'], 'cached-fallback')
        self.assertEqual(stored['body_revision_status'], 'current')
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

    def test_legacy_body_keeps_provenance_across_access_metadata_change(self):
        previous = self.cached_record(source_updated_at='')
        self.write_previous([previous])
        listed = self.list_post(
            updated_at='2026-07-27T12:00:00.000Z',
            truncated_body_text='A paid teaser must not replace the prior body.',
        )
        listed['audience'] = 'only_paid'

        detail_fetch = self.run_fetch(
            [listed],
            AssertionError('metadata-matched legacy cache requested detail'),
        )

        detail_fetch.assert_not_called()
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], previous['body_text'])
        self.assertEqual(stored['audience'], 'only_paid')
        self.assertEqual(stored['source_updated_at'], '')
        self.assertEqual(
            stored['observed_source_updated_at'],
            listed['updated_at'],
        )
        self.assertEqual(stored['body_source'], 'cached-legacy-unverified')
        self.assertEqual(stored['body_revision_status'], 'unverified')
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

    def test_public_excerpt_cache_retries_detail_and_upgrades_after_recovery(self):
        previous = self.cached_record()
        previous.update({
            'body_text': 'Exact paid-post teaser.',
            'body_html_length': 0,
            'body_source': 'source-excerpt',
            'wordcount': 0,
            'content_status': 'excerpt',
        })
        self.write_previous([previous])
        listed = self.list_post(
            updated_at=previous['source_updated_at'],
            truncated_body_text='A different list teaser must not replace it.',
        )

        detail_fetch = self.run_fetch(
            [listed],
            urllib.error.URLError('temporary detail outage'),
        )

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], previous['body_text'])
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(stored['body_source'], 'cached-excerpt-fallback')
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

        self.write_previous([stored])
        detail = dict(listed)
        detail['body_html'] = '<p>' + ' '.join(
            f'recovered{index}' for index in range(50)
        ) + '</p>'
        recovered_fetch = self.run_fetch([listed], detail)
        self.assert_exact_detail_urls(recovered_fetch, listed['slug'])
        recovered = self.read_json(self.posts_path)[0]
        self.assertEqual(recovered['content_status'], 'full')
        self.assertEqual(recovered['body_source'], 'detail')
        self.assertEqual(recovered['body_revision_status'], 'current')

    def test_access_limited_excerpt_cache_can_avoid_public_detail_fetch(self):
        previous = self.cached_record()
        previous.update({
            'body_text': 'Exact access-limited teaser.',
            'body_html_length': 0,
            'body_source': 'source-excerpt',
            'wordcount': 0,
            'content_status': 'excerpt',
        })
        self.write_previous([previous])
        listed = self.list_post(
            updated_at=previous['source_updated_at'],
            truncated_body_text='A different list teaser must not replace it.',
        )
        listed['audience'] = 'only_paid'

        detail_fetch = self.run_fetch(
            [listed],
            AssertionError('access-limited excerpt requested detail'),
        )
        detail_fetch.assert_not_called()
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], previous['body_text'])
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['body_source'], 'cached-excerpt')

    def test_public_legacy_cache_retries_and_upgrades_to_verified_detail(self):
        previous = self.cached_record(source_updated_at='')
        self.write_previous([previous])
        listed = self.list_post(
            updated_at='2026-07-27T12:00:00.000Z',
        )
        detail = dict(listed)
        detail['body_html'] = '<p>' + ' '.join(
            f'current{index}' for index in range(50)
        ) + '</p>'

        detail_fetch = self.run_fetch([listed], detail)
        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['content_status'], 'full')
        self.assertEqual(stored['body_source'], 'detail')
        self.assertEqual(stored['body_revision_status'], 'current')
        self.assertEqual(
            stored['source_updated_at'],
            listed['updated_at'],
        )

    def test_access_limited_matching_full_cache_is_never_labeled_full(self):
        previous = self.cached_record()
        self.write_previous([previous])
        listed = self.list_post(
            updated_at=previous['source_updated_at'],
            truncated_body_text='A current paid preview.',
        )
        listed['audience'] = 'only_paid'

        detail_fetch = self.run_fetch(
            [listed],
            AssertionError('access-limited full cache requested detail'),
        )
        detail_fetch.assert_not_called()
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], previous['body_text'])
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(stored['body_source'], 'cached-access-limited')
        self.assertEqual(stored['body_revision_status'], 'current')

    def test_detail_failure_preserves_existing_exact_record_and_degrades(self):
        previous = self.cached_record(
            source_updated_at='2026-07-27T10:00:00.000Z',
        )
        self.write_previous([previous])
        listed = self.list_post(
            updated_at='2026-07-27T12:00:00.000Z',
            truncated_body_text='A newer preview is not the prior full body.',
        )

        detail_fetch = self.run_fetch(
            [listed],
            urllib.error.URLError('detail unavailable'),
        )

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], previous['body_text'])
        self.assertEqual(stored['title'], listed['title'])
        self.assertEqual(
            stored['source_updated_at'],
            previous['source_updated_at'],
        )
        self.assertEqual(
            stored['observed_source_updated_at'],
            listed['updated_at'],
        )
        self.assertEqual(stored['body_source'], 'cached-fallback')
        self.assertEqual(stored['body_revision_status'], 'prior')
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(stored['wordcount'], 0)
        self.assertEqual(
            self.read_json(self.articles_path)[0]['content_status'],
            'excerpt',
        )
        article = self.read_json(self.articles_path)[0]
        self.assertEqual(article['body_revision_status'], 'prior')
        self.assertEqual(
            article['source_updated_at'],
            previous['source_updated_at'],
        )
        self.assertEqual(
            article['observed_source_updated_at'],
            listed['updated_at'],
        )
        self.assertEqual(
            self.read_json(self.status_path)['status'],
            'degraded',
        )

    def test_new_post_detail_failure_publishes_honest_excerpt_and_degrades(self):
        excerpt = (
            'Exact string preview supplied by the Substack list response.'
        )
        listed = self.list_post(
            slug='new-research',
            truncated_body_text=excerpt,
        )

        detail_fetch = self.run_fetch(
            [listed],
            urllib.error.URLError('detail unavailable'),
        )

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], excerpt)
        self.assertEqual(stored['body_html_length'], 0)
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(
            self.read_json(self.articles_path)[0]['content_status'],
            'excerpt',
        )
        self.assertEqual(
            self.read_json(self.status_path)['status'],
            'degraded',
        )

    def test_malformed_detail_body_degrades_to_string_excerpt(self):
        excerpt = 'The only valid captured text is this exact preview.'
        listed = self.list_post(
            slug='malformed-detail',
            truncated_body_text=excerpt,
        )
        malformed_detail = dict(listed)
        malformed_detail['body_html'] = ['not', 'HTML']

        detail_fetch = self.run_fetch([listed], malformed_detail)

        self.assert_exact_detail_urls(detail_fetch, listed['slug'])
        stored = self.read_json(self.posts_path)[0]
        self.assertEqual(stored['body_text'], excerpt)
        self.assertIsInstance(stored['body_text'], str)
        self.assertEqual(stored['body_html_length'], 0)
        self.assertEqual(stored['content_status'], 'excerpt')
        self.assertEqual(
            self.read_json(self.status_path)['status'],
            'degraded',
        )

    def test_malformed_list_and_detail_types_never_raise_type_error(self):
        listed = self.list_post(
            slug='malformed-everywhere',
            body_html={'not': 'HTML'},
            truncated_body_text=['not', 'text'],
        )
        sentinel = [{'preserve': 'candidate output on fail-closed'}]
        self.posts_path.write_text(
            json.dumps(sentinel),
            encoding='utf-8',
        )

        try:
            self.run_fetch([listed], ['not-a-detail-object'])
        except TypeError as exc:
            self.fail('malformed source values leaked TypeError: ' + str(exc))
        except SystemExit as exc:
            self.assertNotEqual(exc.code, 0)
            self.assertEqual(self.read_json(self.posts_path), sentinel)
            if self.status_path.exists():
                self.assertEqual(
                    self.read_json(self.status_path)['status'],
                    'failed',
                )
        else:
            stored = self.read_json(self.posts_path)[0]
            self.assertIsInstance(stored['body_text'], str)
            self.assertNotEqual(stored['content_status'], 'full')
            self.assertEqual(
                self.read_json(self.status_path)['status'],
                'degraded',
            )

    def test_catalogue_pagination_completes_before_body_resolution(self):
        first_page = [
            self.list_post(slug=f'page-one-{index}')
            for index in range(50)
        ]
        for index, post in enumerate(first_page):
            post['id'] = index + 1
        final_post = self.list_post(slug='page-two-final')
        final_post['id'] = 51
        pages = [
            first_page,
            [final_post],
            first_page,
            [final_post],
        ]

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'POSTS_PATH', self.posts_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'ARTICLE_INDEX_PATH', self.articles_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'PREVIOUS_POSTS_PATH', self.previous_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'FETCH_STATUS_PATH', self.status_path,
            ))
            list_fetch = stack.enter_context(mock.patch.object(
                fetch_all_posts, 'fetch_posts', side_effect=pages,
            ))

            def resolve_after_catalogue(post, previous, detail_fetcher=None):
                self.assertEqual(
                    list_fetch.call_count,
                    4,
                    'body resolution began before both catalogue passes completed',
                )
                return (
                    fetch_all_posts.post_record(
                        post,
                        'Complete source body.',
                        21,
                        'full',
                        'test',
                    ),
                    'list',
                    '',
                )

            stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'resolve_post_body',
                side_effect=resolve_after_catalogue,
            ))
            stack.enter_context(mock.patch.object(fetch_all_posts.time, 'sleep'))
            fetch_all_posts.main()

        self.assertEqual(
            list_fetch.call_args_list,
            [
                mock.call(limit=50, offset=0),
                mock.call(limit=50, offset=50),
                mock.call(limit=50, offset=0),
                mock.call(limit=50, offset=50),
            ],
        )
        stored = self.read_json(self.posts_path)
        self.assertEqual(len(stored), 51)
        self.assertEqual(stored[-1]['slug'], final_post['slug'])

    def test_mid_pagination_failure_never_resolves_or_overwrites_candidate(self):
        first_page = [
            self.list_post(slug=f'partial-page-{index}')
            for index in range(50)
        ]
        for index, post in enumerate(first_page):
            post['id'] = index + 1
        sentinel = [{'preserve': 'prior candidate bytes'}]
        self.posts_path.write_text(json.dumps(sentinel), encoding='utf-8')

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'POSTS_PATH', self.posts_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'ARTICLE_INDEX_PATH', self.articles_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'PREVIOUS_POSTS_PATH', self.previous_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'FETCH_STATUS_PATH', self.status_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'fetch_posts',
                side_effect=[
                    first_page,
                    urllib.error.URLError('page two unavailable'),
                ],
            ))
            resolver = stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'resolve_post_body',
                side_effect=AssertionError(
                    'partial catalogue reached body resolution',
                ),
            ))
            stack.enter_context(mock.patch.object(fetch_all_posts.time, 'sleep'))
            with self.assertRaises(SystemExit) as raised:
                fetch_all_posts.main()

        self.assertNotEqual(raised.exception.code, 0)
        resolver.assert_not_called()
        self.assertEqual(self.read_json(self.posts_path), sentinel)
        self.assertFalse(self.articles_path.exists())
        self.assertEqual(self.read_json(self.status_path)['status'], 'failed')

    def test_catalogue_change_between_passes_fails_closed(self):
        first = self.list_post(slug='stable-slug')
        changed = dict(first)
        changed['title'] = 'Changed during refresh'
        sentinel = [{'preserve': 'prior candidate bytes'}]
        self.posts_path.write_text(json.dumps(sentinel), encoding='utf-8')

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'POSTS_PATH', self.posts_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'ARTICLE_INDEX_PATH', self.articles_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'PREVIOUS_POSTS_PATH', self.previous_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'FETCH_STATUS_PATH', self.status_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'fetch_posts',
                side_effect=[[first], [changed]],
            ))
            resolver = stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'resolve_post_body',
                side_effect=AssertionError(
                    'unstable catalogue reached body resolution',
                ),
            ))
            with self.assertRaises(SystemExit) as raised:
                fetch_all_posts.main()

        self.assertNotEqual(raised.exception.code, 0)
        resolver.assert_not_called()
        self.assertEqual(self.read_json(self.posts_path), sentinel)
        self.assertFalse(self.articles_path.exists())
        self.assertEqual(self.read_json(self.status_path)['status'], 'failed')

    def test_cross_page_overlap_fails_closed(self):
        first_page = [
            self.list_post(slug=f'overlap-{index}')
            for index in range(50)
        ]
        for index, post in enumerate(first_page):
            post['id'] = index + 1
        duplicate = dict(first_page[-1])
        duplicate['slug'] = 'different-slug-same-id'
        sentinel = [{'preserve': 'prior candidate bytes'}]
        self.posts_path.write_text(json.dumps(sentinel), encoding='utf-8')

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'POSTS_PATH', self.posts_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'ARTICLE_INDEX_PATH', self.articles_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'PREVIOUS_POSTS_PATH', self.previous_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'FETCH_STATUS_PATH', self.status_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'fetch_posts',
                side_effect=[first_page, [duplicate]],
            ))
            resolver = stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'resolve_post_body',
            ))
            with self.assertRaises(SystemExit) as raised:
                fetch_all_posts.main()

        self.assertNotEqual(raised.exception.code, 0)
        resolver.assert_not_called()
        self.assertEqual(self.read_json(self.posts_path), sentinel)
        self.assertFalse(self.articles_path.exists())
        self.assertEqual(self.read_json(self.status_path)['status'], 'failed')

    def test_public_detail_requests_have_a_hard_per_refresh_budget(self):
        posts = [
            self.list_post(slug=f'public-detail-{index}')
            for index in range(fetch_all_posts.MAX_DETAIL_REQUESTS_PER_RUN + 1)
        ]
        for index, post in enumerate(posts):
            post['id'] = index + 1

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'POSTS_PATH', self.posts_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'ARTICLE_INDEX_PATH', self.articles_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'PREVIOUS_POSTS_PATH', self.previous_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'FETCH_STATUS_PATH', self.status_path,
            ))
            stack.enter_context(mock.patch.object(
                fetch_all_posts, 'fetch_posts', return_value=posts,
            ))
            detail_fetch = stack.enter_context(mock.patch.object(
                fetch_all_posts,
                'fetch_post_detail',
                side_effect=urllib.error.URLError('detail unavailable'),
            ))
            stack.enter_context(mock.patch.object(fetch_all_posts.time, 'sleep'))
            fetch_all_posts.main()

        self.assertEqual(
            detail_fetch.call_count,
            fetch_all_posts.MAX_DETAIL_REQUESTS_PER_RUN,
        )
        stored = self.read_json(self.posts_path)
        self.assertEqual(len(stored), len(posts))
        self.assertTrue(all(
            post['content_status'] == 'excerpt'
            and post['body_source'] == 'metadata-only'
            for post in stored
        ))
        self.assertTrue(all(
            'detail_attempted_at' in post
            for post in stored[:fetch_all_posts.MAX_DETAIL_REQUESTS_PER_RUN]
        ))
        self.assertNotIn(
            'detail_attempted_at',
            stored[fetch_all_posts.MAX_DETAIL_REQUESTS_PER_RUN],
        )
        self.assertEqual(self.read_json(self.status_path)['status'], 'degraded')

    def test_detail_budget_persists_fair_rotation_across_refreshes(self):
        posts = [
            self.list_post(slug=f'public-detail-{index:02d}')
            for index in range(fetch_all_posts.MAX_DETAIL_REQUESTS_PER_RUN + 1)
        ]
        for index, post in enumerate(posts):
            post['id'] = index + 1

        def run_once():
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    fetch_all_posts, 'POSTS_PATH', self.posts_path,
                ))
                stack.enter_context(mock.patch.object(
                    fetch_all_posts, 'ARTICLE_INDEX_PATH', self.articles_path,
                ))
                stack.enter_context(mock.patch.object(
                    fetch_all_posts, 'PREVIOUS_POSTS_PATH', self.posts_path,
                ))
                stack.enter_context(mock.patch.object(
                    fetch_all_posts, 'FETCH_STATUS_PATH', self.status_path,
                ))
                stack.enter_context(mock.patch.object(
                    fetch_all_posts, 'fetch_posts', return_value=posts,
                ))
                detail_fetch = stack.enter_context(mock.patch.object(
                    fetch_all_posts,
                    'fetch_post_detail',
                    side_effect=urllib.error.URLError('detail unavailable'),
                ))
                stack.enter_context(mock.patch.object(
                    fetch_all_posts.time, 'sleep',
                ))
                fetch_all_posts.main()
            return [
                call.args[0] for call in detail_fetch.call_args_list
            ]

        first_attempts = run_once()
        self.assertEqual(
            first_attempts,
            [post['slug'] for post in posts[:-1]],
        )
        second_attempts = run_once()
        self.assertEqual(second_attempts[0], posts[-1]['slug'])
        self.assertEqual(
            len(second_attempts),
            fetch_all_posts.MAX_DETAIL_REQUESTS_PER_RUN,
        )
        self.assertTrue(all(
            isinstance(post.get('detail_attempted_at'), str)
            for post in self.read_json(self.posts_path)
        ))


if __name__ == '__main__':
    unittest.main()
