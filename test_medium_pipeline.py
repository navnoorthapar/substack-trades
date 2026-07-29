import unittest
from unittest import mock

import fetch_medium_posts
import merge_article_sources
import validate_pipeline


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

    def test_nonempty_public_graphql_body_is_still_only_a_proven_excerpt(self):
        post = self._post([
            {'type': 'P', 'text': 'Visible paragraph text.', 'markups': []},
        ])
        post['visibility'] = 'PUBLIC'
        converted = fetch_medium_posts.convert_post(post)
        self.assertEqual(converted['body_text'], 'Visible paragraph text.')
        self.assertEqual(converted['content_status'], 'excerpt')

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
        self.assertEqual(posts[0]['source_updated_at'], '')
        self.assertEqual(posts[0]['observed_source_updated_at'], '')

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
