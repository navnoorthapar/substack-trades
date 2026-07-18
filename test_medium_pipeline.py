import unittest
from unittest import mock

import fetch_medium_posts
import merge_article_sources


class MediumFetchTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload, final_url=None):
            self.payload = payload
            self.final_url = final_url or fetch_medium_posts.RSS_URL
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

    def test_full_heading_replaces_profile_ellipsis(self):
        converted = fetch_medium_posts.convert_post(self._post([
            {'type': 'H3', 'text': 'A Long Medium Title With Its Complete Ending', 'markups': []},
            {'type': 'P', 'text': 'The article subtitle is here.', 'markups': []},
        ]))
        self.assertEqual(converted['title'], 'A Long Medium Title With Its Complete Ending')
        self.assertEqual(converted['subtitle'], 'The article subtitle is here.')
        self.assertEqual(converted['content_status'], 'excerpt')

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

    def test_rss_fetch_is_bounded_and_accepts_valid_medium_xml(self):
        response = self.FakeResponse(self._rss_payload())
        with mock.patch.object(
                fetch_medium_posts.urllib.request, 'urlopen', return_value=response):
            posts = fetch_medium_posts.fetch_rss_posts(attempts=1)

        self.assertEqual(response.read_size, fetch_medium_posts.MAX_RSS_BYTES + 1)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['medium_id'], 'abcdef123456')

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
        return {
            'slug': slug,
            'title': title,
            'subtitle': 'A sufficiently descriptive subtitle for matching and rendering.',
            'post_date': date,
            'url': f'https://navnoorbawa.substack.com/p/{slug}',
            'body_text': 'Substack body',
            'wordcount': 2,
        }

    def medium(self, post_id, title, date='2026-01-01', **extra):
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


if __name__ == '__main__':
    unittest.main()
