import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from validate_pipeline import (
    validate_article_regression,
    validate_deployable_articles,
    validate_trades,
)


ROOT = Path(__file__).parent


class DeployableSnapshotValidationTests(unittest.TestCase):
    def test_tracked_snapshot_validates_without_local_post_cache(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / 'validate_pipeline.py'),
                '--articles',
                str(ROOT / 'articles_index.json'),
                '--trades',
                str(ROOT / 'trades_extracted.json'),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Validation passed:', result.stdout)

    def test_deployable_catalog_rejects_duplicate_source_identity(self):
        article = {
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
        }
        # The same canonical identity cannot appear twice.
        duplicate = dict(article)
        with self.assertRaisesRegex(ValueError, 'duplicate canonical URLs'):
            validate_deployable_articles([article, duplicate])

    def test_trade_validation_remains_strict_without_post_cache(self):
        article = {
            'url': 'https://medium.com/@navnoorbawa/example-123',
            'source': 'medium',
            'source_id': '123',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'excerpt',
        }
        article['source_id'] = 'abcdef123456'
        article['url'] = 'https://medium.com/@navnoorbawa/example-abcdef123456'
        urls = validate_deployable_articles([article])
        trade = {
            'article_title': 'Example',
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'trade_description': 'A sufficiently detailed extracted investment observation.',
            'instruments': ['equity'],
            'direction': 'long',
        }
        self.assertEqual(validate_trades([trade], urls), {article['url']})
        invalid = dict(trade, direction='buy')
        with self.assertRaisesRegex(ValueError, 'invalid direction'):
            validate_trades([invalid], urls)

    def test_rejects_impossible_date_missing_status_and_noncanonical_url(self):
        article = {
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-02-29',
            'content_status': 'full',
        }
        with self.assertRaisesRegex(ValueError, 'not a real ISO date'):
            validate_deployable_articles([article])

        missing_status = dict(article, post_date='2026-02-28')
        missing_status.pop('content_status')
        with self.assertRaisesRegex(ValueError, 'no explicit content status'):
            validate_deployable_articles([missing_status])

        noncanonical = dict(article, post_date='2026-02-28')
        noncanonical['url'] += '?utm_source=test'
        with self.assertRaisesRegex(ValueError, 'query or fragment'):
            validate_deployable_articles([noncanonical])

        wrong_identity = dict(article, post_date='2026-02-28', source_id='other')
        with self.assertRaisesRegex(ValueError, 'does not match its canonical URL'):
            validate_deployable_articles([wrong_identity])

    def test_trade_title_and_date_must_match_article(self):
        article = {
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Canonical title',
            'post_date': '2026-07-14T09:30:00Z',
            'content_status': 'full',
        }
        articles = validate_deployable_articles([article])
        trade = {
            'article_title': article['title'],
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'trade_description': 'A sufficiently detailed extracted investment observation.',
            'instruments': ['equity'],
            'direction': 'long',
        }
        with self.assertRaisesRegex(ValueError, 'title does not match'):
            validate_trades([dict(trade, article_title='Wrong title')], articles)
        with self.assertRaisesRegex(ValueError, 'date does not match'):
            validate_trades([dict(trade, article_date='2026-07-13')], articles)

    def test_previous_articles_regression_is_enforced_per_source(self):
        previous = [
            {'source': 'substack'}, {'source': 'substack'},
            {'source': 'medium'}, {'source': 'medium'},
        ]
        current = [{'source': 'substack'}, {'source': 'substack'}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'articles.json'
            path.write_text(json.dumps(previous), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'medium article count collapsed'):
                validate_article_regression(current, path, 0.5)


if __name__ == '__main__':
    unittest.main()
