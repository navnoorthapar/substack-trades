import subprocess
import sys
import unittest
from pathlib import Path

from validate_pipeline import validate_deployable_articles, validate_trades


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
            'post_date': '2026-07-14',
            'content_status': 'full',
        }
        duplicate = dict(article, url='https://navnoorbawa.substack.com/p/example-copy')
        with self.assertRaisesRegex(ValueError, 'duplicate source IDs'):
            validate_deployable_articles([article, duplicate])

    def test_trade_validation_remains_strict_without_post_cache(self):
        article = {
            'url': 'https://medium.com/@navnoorbawa/example-123',
            'source': 'medium',
            'source_id': '123',
            'post_date': '2026-07-14',
            'content_status': 'excerpt',
        }
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


if __name__ == '__main__':
    unittest.main()
