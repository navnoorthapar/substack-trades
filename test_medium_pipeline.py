import unittest

import fetch_medium_posts
import merge_article_sources


class MediumFetchTests(unittest.TestCase):
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
