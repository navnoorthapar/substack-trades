import json
import unittest
import hashlib
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import fetch_medium_posts
import merge_article_sources
import validate_pipeline


ROOT = Path(__file__).parent


class MediumFetchTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload, final_url=None, headers=None):
            self.payload = payload
            self.final_url = final_url or fetch_medium_posts.RSS_URL
            self.headers = headers or {}
            self.read_size = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def geturl(self):
            return self.final_url

        def read(self, size=-1):
            self.read_size = size
            return self.payload if size < 0 else self.payload[:size]

    def test_filesystem_paths_are_fixed_and_legacy_controls_cannot_redirect(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            physical_directory = directory.resolve()
            hostile_output = directory / 'attacker-output.json'
            hostile_previous = directory / 'attacker-previous.json'
            hostile_status = directory / 'attacker-status.json'
            environment = os.environ.copy()
            environment['PYTHONPATH'] = str(ROOT) + os.pathsep + environment.get(
                'PYTHONPATH', '',
            )
            environment.update({
                'MEDIUM_OUTPUT': str(hostile_output),
                'PREVIOUS_MEDIUM': str(hostile_previous),
                'FETCH_STATUS_OUTPUT': str(hostile_status),
            })
            probe = subprocess.run(
                [
                    sys.executable,
                    '-c',
                    (
                        'import json, fetch_medium_posts as module; '
                        'print(json.dumps({'
                        '"output": str(module.OUTPUT_PATH), '
                        '"previous": str(module.PREVIOUS_PATH), '
                        '"status": str(module.FETCH_STATUS_PATH)}))'
                    ),
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            paths = json.loads(probe.stdout)
            self.assertEqual(
                paths,
                {
                    'output': str(physical_directory / 'medium.candidate.json'),
                    'previous': str(ROOT.resolve() / 'medium_posts.json'),
                    'status': str(physical_directory / 'medium-status.json'),
                },
            )

            rejected = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'fetch_medium_posts.py'),
                    '--output',
                    str(hostile_output),
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn('accepts no arguments', rejected.stderr)
            for path in (
                    hostile_output,
                    hostile_previous,
                    hostile_status,
                    directory / 'medium.candidate.json',
                    directory / 'medium-status.json'):
                self.assertFalse(path.exists())

    @staticmethod
    def _rss_payload(prefix=b''):
        return prefix + b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
<title>Bounded Medium research</title>
<link>https://medium.com/@navnoorbawa/bounded-medium-research-abcdef123456</link>
<guid>https://medium.com/@navnoorbawa/bounded-medium-research-abcdef123456</guid>
<pubDate>Fri, 17 Jul 2026 12:00:00 GMT</pubDate>
<description>Source-backed research evidence.</description>
</item></channel></rss>'''

    @staticmethod
    def _rss_record(post_id, published):
        return {
            'source': 'medium',
            'source_id': post_id,
            'medium_id': post_id,
            'post_date': published,
            'url': (
                'https://medium.com/@navnoorbawa/'
                f'research-{post_id}'
            ),
        }

    def _post(self, paragraphs):
        return {
            'id': 'abcdef123456',
            'title': 'A Long Medium Title…',
            'uniqueSlug': 'a-long-medium-title-abcdef123456',
            'mediumUrl': 'https://medium.com/@navnoorbawa/a-long-medium-title-abcdef123456',
            'canonicalUrl': '',
            'isPublished': True,
            'visibility': 'LOCKED',
            'firstPublishedAt': 1750000000000,
            'latestPublishedAt': 1750000000000,
            'pinnedByCreatorAt': 0,
            'content': {'bodyModel': {'paragraphs': paragraphs}},
        }

    @staticmethod
    def _archive_post(post_id='abcdef123456', title='Archive post'):
        slug = f'archive-post-{post_id}'
        return {
            'id': post_id,
            'title': title,
            'uniqueSlug': slug,
            'mediumUrl': f'https://medium.com/@navnoorbawa/{slug}',
            'creator': {'id': 'user-1', 'username': fetch_medium_posts.USERNAME},
            'isPublished': True,
            'inResponseToPostResult': None,
            'firstPublishedAt': 1750000000000,
            'latestPublishedAt': 1750000000000,
        }

    @staticmethod
    def _archive_page(posts, next_cursor=None):
        return {
            'data': {
                'userResult': {
                    '__typename': 'User',
                    'id': 'user-1',
                    'homepagePostsConnection': {
                        'posts': posts,
                        'pagingInfo': {
                            'next': (
                                {'from': next_cursor, 'limit': 25}
                                if next_cursor else None
                            ),
                        },
                    },
                },
            },
        }

    def test_full_heading_replaces_profile_ellipsis(self):
        converted = fetch_medium_posts.convert_post(self._post([
            {'type': 'H3', 'text': 'A Long Medium Title With Its Complete Ending', 'markups': []},
            {'type': 'P', 'text': 'The article subtitle is here.', 'markups': []},
        ]))
        self.assertEqual(converted['title'], 'A Long Medium Title With Its Complete Ending')
        self.assertEqual(converted['subtitle'], 'The article subtitle is here.')
        self.assertEqual(converted['content_status'], 'excerpt')
        self.assertEqual(converted['body_revision_status'], 'current')
        self.assertEqual(
            converted['source_updated_at'],
            converted['latest_published_at'],
        )
        self.assertEqual(
            converted['observed_source_updated_at'],
            converted['latest_published_at'],
        )

    def test_only_explicit_cross_post_notice_creates_mirror_slug(self):
        related = self._post([
            {'type': 'H3', 'text': 'Related Research', 'markups': []},
            {
                'type': 'P',
                'text': 'Earlier research covered this model.',
                'markups': [{'href': 'https://navnoorbawa.substack.com/p/other-story'}],
            },
        ])
        self.assertIsNone(fetch_medium_posts.convert_post(related)['mirror_substack_slug'])

        mirror = self._post([
            {'type': 'H3', 'text': 'The Same Story', 'markups': []},
            {
                'type': 'P',
                'text': '📖 Read this article FREE on Substack: The Same Story',
                'markups': [{
                    'href': 'https://open.substack.com/pub/navnoorbawa/p/the-same-story?utm_source=share'
                }],
            },
        ])
        self.assertEqual(
            fetch_medium_posts.convert_post(mirror)['mirror_substack_slug'],
            'the-same-story',
        )

    def test_empty_public_graphql_body_is_never_claimed_as_full_text(self):
        post = self._post([])
        post['visibility'] = 'PUBLIC'
        converted = fetch_medium_posts.convert_post(post)
        self.assertEqual(converted['body_text'], '')
        self.assertEqual(converted['content_status'], 'excerpt')
        self.assertEqual(converted['body_revision_status'], 'current')

    def test_graphql_conversion_rejects_unknown_visibility(self):
        post = self._post([])
        post['visibility'] = 'NEW_MEMBER_ENUM'
        with self.assertRaisesRegex(ValueError, 'unsupported visibility'):
            fetch_medium_posts.convert_post(post)

    def test_nonempty_public_graphql_body_is_still_only_a_proven_excerpt(self):
        post = self._post([
            {'type': 'P', 'text': 'Visible paragraph text.', 'markups': []},
        ])
        post['visibility'] = 'PUBLIC'
        converted = fetch_medium_posts.convert_post(post)
        self.assertEqual(converted['body_text'], 'Visible paragraph text.')
        self.assertEqual(converted['content_status'], 'excerpt')

    def test_locked_graphql_body_is_bounded_before_it_reaches_the_merge(self):
        post = self._post([
            {
                'type': 'P',
                'text': ('anonymous preview evidence ' * 100) + 'private-tail',
                'markups': [],
            },
        ])
        converted = fetch_medium_posts.convert_post(post)
        self.assertLessEqual(len(converted['body_text']), 1_200)
        self.assertNotIn('private-tail', converted['body_text'])
        self.assertTrue(converted['body_text'].endswith('…'))
        self.assertEqual(converted['content_status'], 'excerpt')
        self.assertEqual(converted['wordcount'], 0)
        self.assertEqual(
            converted['member_preview']['text'], converted['body_text']
        )
        self.assertEqual(
            converted['member_preview']['body_sha256'],
            hashlib.sha256(converted['body_text'].encode('utf-8')).hexdigest(),
        )

    def test_legacy_locked_cache_without_proof_becomes_metadata_only(self):
        legacy = {
            'audience': 'locked',
            'visibility': 'LOCKED',
            'subtitle': 'Subscriber-only subtitle from a legacy body.',
            'body_text': 'subscriber-only body ' * 500,
            'wordcount': 500,
            'content_status': 'excerpt',
        }
        carried = fetch_medium_posts.carried_cached_record(legacy)
        self.assertEqual(carried['body_text'], '')
        self.assertEqual(carried['subtitle'], '')
        self.assertEqual(carried['wordcount'], 0)
        self.assertEqual(carried['member_preview']['surface'], 'metadata-only')
        self.assertEqual(carried['member_preview']['text'], '')

    def test_tracked_locked_rows_are_bound_to_exact_anonymous_previews(self):
        rows = json.loads((ROOT / 'medium_posts.json').read_text(encoding='utf-8'))
        locked = [
            row for row in rows
            if str(row.get('audience') or '').strip().casefold() == 'locked'
        ]
        self.assertTrue(locked)
        for row in locked:
            preview = row.get('member_preview')
            self.assertIsInstance(preview, dict)
            self.assertEqual(
                fetch_medium_posts._trusted_member_preview(preview),
                row['body_text'],
            )
            self.assertLessEqual(len(row['body_text']), 1_200)
            self.assertTrue(
                not row.get('subtitle') or row['subtitle'] in row['body_text']
            )

    def test_medium_wordcount_uses_the_shared_full_body_token_rule(self):
        post = self._post([
            {
                'type': 'H3',
                'text': "A market-maker's cross-venue edge",
                'markups': [],
            },
            {
                'type': 'P',
                'text': "It isn't a risk-free trade; it's inventory-aware.",
                'markups': [],
            },
        ])
        post['visibility'] = 'PUBLIC'
        converted = fetch_medium_posts.convert_post(post)
        self.assertEqual(
            converted['wordcount'],
            fetch_medium_posts.body_word_count(converted['body_text']),
        )
        self.assertIn(
            converted['url'],
            validate_pipeline.validate_posts([converted]),
        )

    def test_graphql_fetch_is_bounded_and_accepts_canonical_json_object(self):
        payload = b'{"data":{"userResult":null}}'
        response = self.FakeResponse(
            payload,
            final_url=fetch_medium_posts.GRAPHQL_URL,
            headers={'Content-Length': str(len(payload))},
        )
        with mock.patch.object(
                fetch_medium_posts.urllib.request, 'urlopen', return_value=response):
            result = fetch_medium_posts.request_json(
                fetch_medium_posts.GRAPHQL_URL,
                {'query': 'query Test { __typename }'},
                attempts=1,
            )
        self.assertEqual(result, {'data': {'userResult': None}})
        self.assertEqual(
            response.read_size,
            fetch_medium_posts.MAX_GRAPHQL_BYTES + 1,
        )

    def test_graphql_fetch_rejects_declared_and_streamed_oversize_bodies(self):
        declared = self.FakeResponse(
            b'{}',
            final_url=fetch_medium_posts.GRAPHQL_URL,
            headers={'Content-Length': str(fetch_medium_posts.MAX_GRAPHQL_BYTES + 1)},
        )
        with mock.patch.object(
                fetch_medium_posts.urllib.request, 'urlopen', return_value=declared):
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                fetch_medium_posts.request_json(
                    fetch_medium_posts.GRAPHQL_URL, {}, attempts=1
                )
        self.assertIsNone(declared.read_size)

        streamed = self.FakeResponse(
            b'x' * 33,
            final_url=fetch_medium_posts.GRAPHQL_URL,
        )
        with mock.patch.object(fetch_medium_posts, 'MAX_GRAPHQL_BYTES', 32), \
                mock.patch.object(
                    fetch_medium_posts.urllib.request,
                    'urlopen',
                    return_value=streamed,
                ):
            with self.assertRaisesRegex(ValueError, 'exceeds 32 bytes'):
                fetch_medium_posts.request_json(
                    fetch_medium_posts.GRAPHQL_URL, {}, attempts=1
                )

    def test_graphql_fetch_rejects_noncanonical_final_urls(self):
        final_urls = (
            'http://navnoorbawa.medium.com/_/graphql',
            'https://attacker.example/_/graphql',
            'https://user@navnoorbawa.medium.com/_/graphql',
            'https://navnoorbawa.medium.com:444/_/graphql',
            'https://navnoorbawa.medium.com/_/graphql?redirected=1',
            'https://navnoorbawa.medium.com/not-graphql',
        )
        for final_url in final_urls:
            with self.subTest(final_url=final_url):
                response = self.FakeResponse(b'{}', final_url=final_url)
                with mock.patch.object(
                        fetch_medium_posts.urllib.request,
                        'urlopen',
                        return_value=response,
                ):
                    with self.assertRaisesRegex(ValueError, 'canonical HTTPS author endpoint'):
                        fetch_medium_posts.request_json(
                            fetch_medium_posts.GRAPHQL_URL, {}, attempts=1
                        )
                self.assertIsNone(response.read_size)

    def test_graphql_fetch_requires_strict_utf8_json_object(self):
        payloads = (
            b'\xff',
            b'[]',
            b'{"value":NaN}',
            b'{"value":1,"value":2}',
            b'{"errors":"not-a-list"}',
            b'{"errors":["plain error"]}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.FakeResponse(
                    payload,
                    final_url=fetch_medium_posts.GRAPHQL_URL,
                )
                with mock.patch.object(
                        fetch_medium_posts.urllib.request,
                        'urlopen',
                        return_value=response,
                ):
                    with self.assertRaises(ValueError):
                        fetch_medium_posts.request_json(
                            fetch_medium_posts.GRAPHQL_URL, {}, attempts=1
                        )

    def test_archive_pass_rejects_duplicate_post_ids_across_pages(self):
        post = self._archive_post()
        responses = (
            self._archive_page([post], next_cursor='page-2'),
            self._archive_page([dict(post)]),
        )
        with mock.patch.object(
                fetch_medium_posts, 'request_json', side_effect=responses):
            with self.assertRaisesRegex(ValueError, 'repeated a post ID'):
                fetch_medium_posts._fetch_archive_pass()

    def test_archive_pass_rejects_malformed_post_rows_before_filtering(self):
        valid = self._archive_post()
        malformed_rows = [
            None,
            {},
            dict(valid, id=123),
            dict(valid, creator=None),
            {key: value for key, value in valid.items() if key != 'isPublished'},
            dict(valid, isPublished=1),
            {
                key: value for key, value in valid.items()
                if key != 'inResponseToPostResult'
            },
            dict(valid, inResponseToPostResult='malformed'),
        ]
        for malformed in malformed_rows:
            with self.subTest(malformed=malformed):
                with mock.patch.object(
                        fetch_medium_posts,
                        'request_json',
                        return_value=self._archive_page([malformed]),
                ):
                    with self.assertRaisesRegex(ValueError, 'malformed post row'):
                        fetch_medium_posts._fetch_archive_pass()

    def test_authored_graphql_rows_require_positive_numeric_timestamps(self):
        valid = self._archive_post()
        for field in ('firstPublishedAt', 'latestPublishedAt'):
            for invalid in (
                None,
                True,
                False,
                0,
                -1,
                0.1,
                1.5,
                fetch_medium_posts.MIN_PUBLICATION_TIMESTAMP_MS - 1,
                '1750000000000',
            ):
                with self.subTest(field=field, invalid=invalid):
                    post = dict(valid, **{field: invalid})
                    with mock.patch.object(
                            fetch_medium_posts,
                            'request_json',
                            return_value=self._archive_page([post]),
                    ):
                        with self.assertRaisesRegex(
                            ValueError, 'invalid publication timestamps'):
                            fetch_medium_posts._fetch_archive_pass()

    def test_authored_graphql_latest_timestamp_cannot_precede_first(self):
        first = 1750000000000
        post = dict(
            self._archive_post(),
            firstPublishedAt=first,
            latestPublishedAt=first - 1,
            visibility='PUBLIC',
            content={'bodyModel': {'paragraphs': []}},
        )
        with mock.patch.object(
                fetch_medium_posts,
                'request_json',
                return_value=self._archive_page([post]),
        ):
            with self.assertRaisesRegex(
                    ValueError, 'invalid publication timestamps'):
                fetch_medium_posts._fetch_archive_pass()
        with self.assertRaisesRegex(
                ValueError, 'invalid publication timestamps'):
            fetch_medium_posts.convert_post(post)

    def test_graphql_item_url_identity_is_exact_and_unicode_safe(self):
        legitimate = (
            'https://medium.com/@navnoorbawa/'
            'the-cram%C3%A9r-rao-bound-killed-ltcm-54e47fbb0504',
            'https://medium.com/@navnoorbawa/'
            'color-%CE%B3-t-options-greek-b7bf066746e0',
            'https://medium.com/@navnoorbawa/'
            'how-soci%C3%A9t%C3%A9-g%C3%A9n%C3%A9rales-work-bf9a744c7898',
        )
        for url in legitimate:
            with self.subTest(url=url):
                canonical, _, post_id = (
                    fetch_medium_posts.canonical_medium_item_identity(url)
                )
                self.assertEqual(canonical, url)
                self.assertEqual(post_id, url[-12:])

        invalid = (
            'https://medium.com/@another/story-abcdef123456',
            'https://medium.com/@navnoorbawa/a/../story-abcdef123456',
            'https://medium.com/@navnoorbawa/%2e%2e-abcdef123456',
            'https://medium.com/@navnoorbawa/a%2F..%2Fstory-abcdef123456',
            'https://medium.com/@navnoorbawa/story-%c3%a9-abcdef123456',
            'https://medium.com/@navnoorbawa/story-abcdef123456?source=other',
            'https://user@medium.com/@navnoorbawa/story-abcdef123456',
            'https://medium.com:444/@navnoorbawa/story-abcdef123456',
            'https://medium.com/@navnoorbawa/story-abcdef123456#fragment',
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    fetch_medium_posts.canonical_medium_item_identity(url)

    def test_archive_rejects_inconsistent_item_url_identity(self):
        valid = self._archive_post()
        invalid_urls = (
            'https://medium.com/@navnoorbawa/../archive-post-abcdef123456',
            'https://medium.com/@navnoorbawa/%2e%2e-abcdef123456',
            'https://medium.com/@navnoorbawa/archive-post-bbbbbbbbbbbb',
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                post = dict(valid, mediumUrl=url)
                with mock.patch.object(
                        fetch_medium_posts,
                        'request_json',
                        return_value=self._archive_page([post]),
                ):
                    with self.assertRaises(ValueError):
                        fetch_medium_posts._fetch_archive_pass()

    def test_archive_requires_two_exact_complete_passes(self):
        posts = [self._archive_post()]
        with mock.patch.object(
                fetch_medium_posts,
                '_fetch_archive_pass',
                side_effect=[posts, [dict(posts[0])]],
        ) as fetch_pass:
            self.assertEqual(fetch_medium_posts.fetch_archive(), posts)
        self.assertEqual(fetch_pass.call_count, 2)

        changed = dict(posts[0], title='Changed between passes')
        with mock.patch.object(
                fetch_medium_posts,
                '_fetch_archive_pass',
                side_effect=[posts, [changed]],
        ):
            with self.assertRaisesRegex(ValueError, 'changed between verification passes'):
                fetch_medium_posts.fetch_archive()

    def test_catalogue_rejects_same_size_replacement_of_previous_ids(self):
        previous = [{
            'medium_id': 'aaaaaaaaaaaa',
            'url': 'https://medium.com/@navnoorbawa/old-aaaaaaaaaaaa',
            'post_date': '2026-07-01T00:00:00Z',
        }]
        replacement = [{
            'medium_id': 'bbbbbbbbbbbb',
            'url': 'https://medium.com/@navnoorbawa/new-bbbbbbbbbbbb',
            'post_date': '2026-07-02T00:00:00Z',
        }]
        with mock.patch.dict(fetch_medium_posts.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'omitted 1 of 1 previous'):
                fetch_medium_posts.validate_catalogue(replacement, previous)

    def test_rss_fetch_is_bounded_and_accepts_valid_medium_xml(self):
        response = self.FakeResponse(self._rss_payload())
        with mock.patch.object(
                fetch_medium_posts.urllib.request, 'urlopen', return_value=response):
            posts = fetch_medium_posts.fetch_rss_posts(attempts=1)

        self.assertEqual(response.read_size, fetch_medium_posts.MAX_RSS_BYTES + 1)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['medium_id'], 'abcdef123456')
        self.assertEqual(posts[0]['content_status'], 'excerpt')
        self.assertEqual(posts[0]['body_revision_status'], 'current')
        self.assertEqual(posts[0]['source_updated_at'], posts[0]['post_date'])
        self.assertEqual(
            posts[0]['observed_source_updated_at'], posts[0]['post_date'],
        )
        self.assertTrue(posts[0]['post_date'])

    def test_rss_sequence_rejects_duplicate_identity_and_wrong_order(self):
        newest = self._rss_record(
            'aaaaaaaaaaaa', '2026-07-18T12:00:00Z',
        )
        older = self._rss_record(
            'bbbbbbbbbbbb', '2026-07-17T12:00:00Z',
        )
        duplicate = dict(older, medium_id=newest['medium_id'])
        duplicate['source_id'] = newest['source_id']
        with self.assertRaisesRegex(ValueError, 'repeated a post ID'):
            fetch_medium_posts.validate_rss_sequence([newest, duplicate])
        with self.assertRaisesRegex(ValueError, 'newest-first order'):
            fetch_medium_posts.validate_rss_sequence([older, newest])

    def test_rss_requires_two_exact_normalized_windows(self):
        posts = [self._rss_record(
            'aaaaaaaaaaaa', '2026-07-18T12:00:00Z',
        )]
        with mock.patch.object(
            fetch_medium_posts,
            'fetch_rss_posts',
            side_effect=[posts, [dict(posts[0])]],
        ) as fetch_pass:
            self.assertEqual(
                fetch_medium_posts.fetch_stable_rss_posts(),
                posts,
            )
        self.assertEqual(fetch_pass.call_count, 2)

        changed = [dict(posts[0], post_date='2026-07-19T12:00:00Z')]
        with mock.patch.object(
            fetch_medium_posts,
            'fetch_rss_posts',
            side_effect=[posts, changed],
        ):
            with self.assertRaisesRegex(
                ValueError, 'changed between verification passes',
            ):
                fetch_medium_posts.fetch_stable_rss_posts()

    def test_incremental_rss_requires_contiguous_history_overlap(self):
        history = [
            self._rss_record('aaaaaaaaaaaa', '2026-07-17T12:00:00Z'),
            self._rss_record('bbbbbbbbbbbb', '2026-07-16T12:00:00Z'),
        ]
        new = self._rss_record('cccccccccccc', '2026-07-18T12:00:00Z')
        fetch_medium_posts.validate_incremental_rss(
            [new, history[0], history[1]],
            history,
        )

        with self.assertRaisesRegex(ValueError, 'no overlap'):
            fetch_medium_posts.validate_incremental_rss([new], history)

        with self.assertRaisesRegex(ValueError, 'newest validated history edge'):
            fetch_medium_posts.validate_incremental_rss(
                [new, history[1]],
                history,
            )

        hole = self._rss_record('dddddddddddd', '2026-07-16T18:00:00Z')
        with self.assertRaisesRegex(ValueError, 'unknown item below'):
            fetch_medium_posts.validate_incremental_rss(
                [new, history[0], hole, history[1]],
                history,
            )

        regressed = self._rss_record(
            history[1]['medium_id'], '2026-07-16T12:00:00Z',
        )
        with self.assertRaisesRegex(ValueError, 'regressed behind history'):
            fetch_medium_posts.validate_incremental_rss(
                [regressed],
                history,
            )

        changed_timestamp = dict(
            history[0], post_date='2026-07-17T12:00:01Z',
        )
        with self.assertRaisesRegex(
            ValueError, 'changed the exact publication timestamp',
        ):
            fetch_medium_posts.validate_incremental_rss(
                [changed_timestamp, history[1]], history,
            )

    def test_established_history_requires_the_full_ten_row_rss_window(self):
        history = [
            self._rss_record(
                f'{index:012x}',
                f'2026-07-{20 - index:02d}T12:00:00Z',
            )
            for index in range(10)
        ]
        with self.assertRaisesRegex(ValueError, 'exactly 10 are required'):
            fetch_medium_posts.validate_incremental_rss(
                [dict(history[0])], history,
            )

    def test_known_overlap_retains_exact_timestamp_and_history_order(self):
        history = [
            self._rss_record('aaaaaaaaaaaa', '2026-07-17T12:00:00Z'),
            self._rss_record('bbbbbbbbbbbb', '2026-07-16T12:00:00Z'),
        ]
        newest = self._rss_record(
            'cccccccccccc', '2026-07-18T12:00:00Z',
        )
        latest = [newest, dict(history[0]), dict(history[1])]
        fetch_medium_posts.validate_incremental_rss(latest, history)
        merged = fetch_medium_posts.merge_rss_with_history(latest, history)
        self.assertEqual(
            [row['medium_id'] for row in merged[:3]],
            ['cccccccccccc', 'aaaaaaaaaaaa', 'bbbbbbbbbbbb'],
        )
        self.assertEqual(
            [row['post_date'] for row in merged[1:3]],
            ['2026-07-17T12:00:00Z', '2026-07-16T12:00:00Z'],
        )

        millisecond_history = [
            dict(history[0], post_date='2026-07-17T12:00:00.000Z'),
            dict(history[1], post_date='2026-07-16T12:00:00.000Z'),
        ]
        fetch_medium_posts.validate_incremental_rss(
            latest, millisecond_history,
        )
        normalized_merge = fetch_medium_posts.merge_rss_with_history(
            latest, millisecond_history,
        )
        self.assertEqual(
            [row['post_date'] for row in normalized_merge[1:3]],
            ['2026-07-17T12:00:00.000Z', '2026-07-16T12:00:00.000Z'],
        )

    def test_archive_rss_edge_rejects_publication_timestamp_drift(self):
        latest = [
            self._rss_record(
                f'{index:012x}',
                f'2026-07-{20 - index:02d}T12:00:00Z',
            )
            for index in range(10)
        ]
        archive = [dict(post) for post in latest]
        archive[0]['post_date'] = '2026-07-20T12:00:01Z'
        with self.assertRaisesRegex(ValueError, 'RSS timestamps'):
            fetch_medium_posts.validate_archive_rss_edge(archive, latest)

    def test_reviewed_profile_bridge_is_exact_expiring_and_one_time(self):
        bridge = fetch_medium_posts.load_profile_bridge(
            now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
        )
        published = (
            '2026-08-19T17:01:27Z',
            '2026-08-19T16:12:51Z',
            '2026-08-18T15:46:22Z',
            '2026-08-18T14:16:44Z',
            '2026-08-17T15:38:09Z',
            '2026-08-17T10:14:09Z',
            '2026-08-16T13:01:24Z',
            '2026-08-16T06:41:27Z',
            '2026-08-15T09:37:43Z',
            '2026-08-15T04:46:41Z',
        )
        latest = [
            self._rss_record(post_id, stamp)
            for post_id, stamp in zip(bridge['rss_window_ids'], published)
        ]
        previous = [
            self._rss_record('4912dfd9ee85', '2026-08-14T06:29:10Z'),
            self._rss_record('c4c340597a67', '2026-08-14T04:07:37Z'),
        ]
        self.assertEqual(
            fetch_medium_posts.validate_profile_bridge(
                latest,
                previous,
                now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
            ),
            bridge,
        )

        unrelated = [dict(post) for post in latest]
        unrelated[0]['source_id'] = '111111111111'
        unrelated[0]['medium_id'] = '111111111111'
        unrelated[0]['url'] = (
            'https://medium.com/@navnoorbawa/research-111111111111'
        )
        with self.assertRaisesRegex(ValueError, 'complete live RSS window'):
            fetch_medium_posts.validate_profile_bridge(
                unrelated,
                previous,
                now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
            )

        after_first_merge = fetch_medium_posts.merge_rss_with_history(
            latest, previous,
        )
        with self.assertRaisesRegex(ValueError, 'newest trusted history prefix'):
            fetch_medium_posts.validate_profile_bridge(
                latest,
                after_first_merge,
                now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
            )

        with self.assertRaisesRegex(ValueError, 'has expired'):
            fetch_medium_posts.load_profile_bridge(
                now=datetime(2026, 8, 23, 13, 38, 49, tzinfo=timezone.utc),
            )

        invalid_schema = dict(bridge, unreviewed_extension=True)
        with mock.patch.object(
            fetch_medium_posts,
            '_strict_bridge_object',
            return_value=invalid_schema,
        ):
            with self.assertRaisesRegex(ValueError, 'exact schema'):
                fetch_medium_posts.load_profile_bridge(
                    now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
                )

    def test_rss_accepts_only_its_tracking_query_and_canonicalizes_it(self):
        payload = self._rss_payload().replace(
            b'abcdef123456</link>',
            b'abcdef123456?source=rss-3f267717a24e------2</link>',
        )
        response = self.FakeResponse(payload)
        with mock.patch.object(
                fetch_medium_posts.urllib.request,
                'urlopen',
                return_value=response,
        ):
            posts = fetch_medium_posts.fetch_rss_posts(attempts=1)
        self.assertEqual(
            posts[0]['url'],
            'https://medium.com/@navnoorbawa/'
            'bounded-medium-research-abcdef123456',
        )

        invalid_payload = self._rss_payload().replace(
            b'bounded-medium-research-abcdef123456</link>',
            b'%2e%2e-abcdef123456</link>',
        )
        response = self.FakeResponse(invalid_payload)
        with mock.patch.object(
                fetch_medium_posts.urllib.request,
                'urlopen',
                return_value=response,
        ):
            with self.assertRaises(ValueError):
                fetch_medium_posts.fetch_rss_posts(attempts=1)

    def test_rss_fetch_rejects_oversized_response_before_parsing(self):
        response = self.FakeResponse(b'x' * (fetch_medium_posts.MAX_RSS_BYTES + 1))
        with mock.patch.object(
                fetch_medium_posts.urllib.request, 'urlopen', return_value=response), \
                mock.patch.object(fetch_medium_posts.ET, 'fromstring') as parse_xml:
            with self.assertRaisesRegex(ValueError, 'exceeds 2000000 bytes'):
                fetch_medium_posts.fetch_rss_posts(attempts=1)
        parse_xml.assert_not_called()

    def test_rss_fetch_rejects_doctype_and_entity_declarations(self):
        declarations = (
            b'<!DOCTYPE rss SYSTEM "https://example.test/rss.dtd">',
            b'<!ENTITY unsafe "expanded">',
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                response = self.FakeResponse(self._rss_payload(declaration))
                with mock.patch.object(
                        fetch_medium_posts.urllib.request, 'urlopen', return_value=response), \
                        mock.patch.object(fetch_medium_posts.ET, 'fromstring') as parse_xml:
                    with self.assertRaisesRegex(ValueError, 'prohibited XML declaration'):
                        fetch_medium_posts.fetch_rss_posts(attempts=1)
                parse_xml.assert_not_called()

    def test_rss_fetch_rejects_non_utf8_xml_before_parsing(self):
        response = self.FakeResponse(self._rss_payload().decode().encode('utf-16'))
        with mock.patch.object(
                fetch_medium_posts.urllib.request, 'urlopen', return_value=response), \
                mock.patch.object(fetch_medium_posts.ET, 'fromstring') as parse_xml:
            with self.assertRaisesRegex(ValueError, 'not UTF-8 XML'):
                fetch_medium_posts.fetch_rss_posts(attempts=1)
        parse_xml.assert_not_called()

    def test_rss_fetch_rejects_non_https_and_off_origin_redirects(self):
        final_urls = (
            'http://medium.com/feed/@navnoorbawa',
            'https://attacker.example/feed/@navnoorbawa',
            'https://medium.com/feed/@another-author',
            'https://medium.com/feed/@navnoorbawa?redirected=1',
            'https://medium.com/feed/@navnoorbawa#fragment',
        )
        for final_url in final_urls:
            with self.subTest(final_url=final_url):
                response = self.FakeResponse(self._rss_payload(), final_url=final_url)
                with mock.patch.object(
                        fetch_medium_posts.urllib.request, 'urlopen', return_value=response), \
                        mock.patch.object(fetch_medium_posts.ET, 'fromstring') as parse_xml:
                    with self.assertRaisesRegex(ValueError, 'canonical HTTPS'):
                        fetch_medium_posts.fetch_rss_posts(attempts=1)
                parse_xml.assert_not_called()


class SourceMergeTests(unittest.TestCase):
    def substack(self, slug, title, date='2026-01-01'):
        revision_at = f'{date[:10]}T00:00:00Z'
        return {
            'slug': slug,
            'title': title,
            'subtitle': 'A sufficiently descriptive subtitle for matching and rendering.',
            'post_date': date,
            'url': f'https://navnoorbawa.substack.com/p/{slug}',
            'audience': 'everyone',
            'body_text': 'Substack body',
            'wordcount': 2,
            'content_status': 'full',
            'body_revision_status': 'current',
            'source_updated_at': revision_at,
            'observed_source_updated_at': revision_at,
        }

    def medium(self, post_id, title, date='2026-01-01', **extra):
        revision_at = f'{date[:10]}T00:00:00Z'
        value = {
            'medium_id': post_id,
            'source_id': post_id,
            'slug': f'{title.lower().replace(" ", "-")}-{post_id}',
            'title': title,
            'display_title': title,
            'subtitle': '',
            'post_date': date,
            'url': f'https://medium.com/@navnoorbawa/story-{post_id}',
            'body_text': 'Medium body',
            'wordcount': 2,
            'content_status': 'full',
            'body_revision_status': 'current',
            'source_updated_at': revision_at,
            'observed_source_updated_at': revision_at,
        }
        value.update(extra)
        return value

    def test_title_normalization_handles_curly_quotes_and_number_words(self):
        left = "Optiver’s Three Ideas — A ‘Neutral’ Fix"
        right = "Optiver's 3 Ideas: A 'Neutral' Fix"
        self.assertEqual(
            merge_article_sources.normalize_title(left),
            merge_article_sources.normalize_title(right),
        )

    def test_cross_post_is_collapsed_and_unique_medium_is_kept(self):
        substack = [self.substack('same-story', 'The Same Story')]
        medium = [
            self.medium('aaa111aaa111', 'Different Medium Headline',
                        mirror_substack_slug='same-story'),
            self.medium('bbb222bbb222', 'A Genuinely Unique Medium Article',
                        date='2025-01-01'),
        ]
        posts, articles, report = merge_article_sources.merge_sources(
            substack, medium, overrides=[]
        )
        self.assertEqual(len(posts), 2)
        self.assertEqual(len(articles), 2)
        self.assertEqual(report['duplicate_medium_articles'], 1)
        self.assertEqual(report['unique_medium_articles'], 1)
        self.assertEqual({article['source'] for article in articles}, {'substack', 'medium'})
        self.assertTrue(all(
            article['body_revision_status'] == 'current'
            for article in articles
        ))
        self.assertEqual(
            next(article for article in articles if article['source'] == 'substack')
            ['alternate_urls']['medium'],
            medium[0]['url'],
        )

    def test_member_sources_publish_only_the_bounded_anonymous_preview(self):
        paid = self.substack('paid-note', 'Paid note')
        paid.update({
            'audience': 'only_paid',
            'body_text': 'private subscriber body ' * 500,
            'public_preview_text': 'Exact anonymous Substack preview.',
            'public_preview_updated_at': '2026-01-01T00:00:00Z',
        })
        locked = self.medium(
            'aaa111aaa111',
            'Locked Medium note',
            audience='locked',
            body_text=('Exact anonymous Medium preview ' * 80) + 'private-tail',
            content_status='excerpt',
        )
        locked['member_preview'] = fetch_medium_posts._member_preview(
            locked['body_text']
        )

        posts, articles, _ = merge_article_sources.merge_sources(
            [paid], [locked], overrides=[]
        )

        by_source = {row['source']: row for row in posts}
        self.assertEqual(
            by_source['substack']['body_text'],
            'Exact anonymous Substack preview.',
        )
        self.assertLessEqual(len(by_source['medium']['body_text']), 1_200)
        self.assertNotIn('private-tail', by_source['medium']['body_text'])
        for row in posts:
            preview = row['member_preview']
            self.assertEqual(preview['text'], row['body_text'])
            self.assertEqual(preview['character_count'], len(row['body_text']))
            self.assertEqual(
                preview['body_sha256'],
                hashlib.sha256(row['body_text'].encode('utf-8')).hexdigest(),
            )
            article = next(
                candidate for candidate in articles
                if candidate['source'] == row['source']
            )
            self.assertEqual(article['member_preview'], preview)
            self.assertEqual(article['brief']['body_sha256'], preview['body_sha256'])

        legacy_locked = self.medium(
            'bbb222bbb222',
            'Legacy locked cache',
            audience='locked',
            body_text='legacy subscriber body ' * 500,
            content_status='excerpt',
        )
        legacy_posts, legacy_articles, _ = merge_article_sources.merge_sources(
            [], [legacy_locked], overrides=[]
        )
        self.assertEqual(legacy_posts[0]['body_text'], '')
        self.assertEqual(
            legacy_articles[0]['member_preview']['surface'],
            'metadata-only',
        )

    def test_similar_topic_with_different_date_remains_distinct(self):
        substack = [self.substack(
            'new-volatility-engine',
            "Inside Da Vinci Trading's Multi-Market Volatility Engine",
            date='2026-06-01',
        )]
        medium = [self.medium(
            'ccc333ccc333',
            'Da Vinci Trading: Volatility Arbitrage and Crypto Market Making Deconstructed',
            date='2025-10-01',
        )]
        _, articles, report = merge_article_sources.merge_sources(
            substack, medium, overrides=[]
        )
        self.assertEqual(len(articles), 2)
        self.assertEqual(report['unique_medium_articles'], 1)

    def test_same_normalized_title_in_different_year_remains_distinct(self):
        substack = [self.substack(
            'annual-market-structure-review-2025',
            'Annual Market Structure Review',
            date='2025-01-15',
        )]
        medium = [self.medium(
            'ddd444ddd444',
            'Annual Market Structure Review',
            date='2026-01-15',
        )]
        _, articles, report = merge_article_sources.merge_sources(
            substack, medium, overrides=[]
        )
        self.assertEqual(len(articles), 2)
        self.assertEqual(report['duplicate_medium_articles'], 0)
        self.assertEqual(report['unique_medium_articles'], 1)

    def test_reviewed_override_is_bound_to_immutable_source_ids(self):
        target = self.substack(
            'canonical-substack-slug',
            'Canonical Substack Research Title',
        )
        reviewed = self.medium(
            'aaa111aaa111',
            'Substantially Rewritten Medium Headline',
        )
        same_title_different_id = self.medium(
            'bbb222bbb222',
            'Substantially Rewritten Medium Headline',
            date='2025-01-01',
        )
        override = {
            'medium_id': 'aaa111aaa111',
            'substack_slug': 'canonical-substack-slug',
            'medium_title_key': 'Substantially Rewritten Medium Headline',
            'substack_title': 'Canonical Substack Research Title',
        }
        _, articles, report = merge_article_sources.merge_sources(
            [target],
            [reviewed, same_title_different_id],
            overrides=[override],
        )
        self.assertEqual(report['duplicate_medium_articles'], 1)
        self.assertEqual(report['matches'][0]['medium_id'], 'aaa111aaa111')
        self.assertEqual(report['matches'][0]['reason'], 'reviewed-override')
        self.assertEqual(report['unique_medium_ids'], ['bbb222bbb222'])
        self.assertEqual(len(articles), 2)

        invalid = dict(override, medium_title_key='A stale human label')
        with self.assertRaisesRegex(ValueError, 'title cross-check failed'):
            merge_article_sources.merge_sources(
                [target],
                [reviewed],
                overrides=[invalid],
            )

        title_only = {
            'medium_title_key': override['medium_title_key'],
            'substack_title': override['substack_title'],
        }
        with self.assertRaisesRegex(ValueError, 'immutable ID pair'):
            merge_article_sources.merge_sources(
                [target],
                [reviewed],
                overrides=[title_only],
            )

    def test_merge_rejects_full_current_body_without_revision_timestamps(self):
        medium = self.medium(
            'ccc333ccc333',
            'Timestamp Binding Regression',
            source_updated_at='',
            observed_source_updated_at='',
        )
        with self.assertRaisesRegex(
                ValueError, 'timestamp-bound current revision'):
            merge_article_sources.merge_sources(
                [],
                [medium],
                overrides=[],
            )

    def test_missing_body_provenance_is_never_promoted_to_current(self):
        substack = self.substack('legacy-substack', 'Legacy Substack Cache')
        medium = self.medium(
            'eee555eee555',
            'Legacy Medium Cache',
            date='2025-12-01',
        )
        for post in (substack, medium):
            post.pop('body_revision_status')
            post.pop('source_updated_at')
            post.pop('observed_source_updated_at')

        posts, articles, _ = merge_article_sources.merge_sources(
            [substack], [medium], overrides=[]
        )
        for row in posts + articles:
            self.assertEqual(row['body_revision_status'], 'unverified')
            self.assertEqual(row['content_status'], 'excerpt')
            self.assertEqual(row['observed_source_updated_at'], '')


if __name__ == '__main__':
    unittest.main()
