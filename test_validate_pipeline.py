import copy
import hashlib
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
from extract_trades import (
    extract_fund_name,
    extract_outcome,
    extract_quant_details,
    extract_thesis,
    extract_underlying,
)
from filter_trades import clean_underlying


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
            'family': 'other',
        }
        # The same canonical identity cannot appear twice.
        duplicate = dict(article)
        with self.assertRaisesRegex(ValueError, 'duplicate canonical URLs'):
            validate_deployable_articles([article, duplicate])

    def test_paid_registry_brief_must_preserve_exact_empty_body_boundary(self):
        article = {
            'url': 'https://www.patreon.com/NavnoorBawa/posts/example-123456789',
            'source': 'patreon',
            'source_id': '123456789',
            'slug': 'example-123456789',
            'title': 'Paid research metadata title',
            'subtitle': '',
            'post_date': '2026-07-14',
            'audience': 'paid',
            'access': 'paid',
            'wordcount': 0,
            'content_status': 'registry',
            'family': 'other',
            'brief': {
                'schema_version': 1,
                'body_sha256': hashlib.sha256(b'').hexdigest(),
                'lead': None,
                'sections': [],
                'fallback_evidence': None,
                'checkpoints': [],
            },
        }
        self.assertIn(article['url'], validate_deployable_articles([article]))

        leaked_text = 'Paid article body must never enter the public registry.'
        corruptions = {
            'body digest': {
                'body_sha256': hashlib.sha256(leaked_text.encode('utf-8')).hexdigest(),
            },
            'lead': {'lead': {'text': leaked_text}},
            'fallback': {'fallback_evidence': {'text': leaked_text}},
            'sections': {'sections': [{'text': leaked_text}]},
            'checkpoints': {'checkpoints': [{'text': leaked_text}]},
            'extra brief body field': {'body_text': leaked_text},
        }
        for label, changes in corruptions.items():
            with self.subTest(label=label):
                corrupted = copy.deepcopy(article)
                corrupted['brief'].update(changes)
                with self.assertRaisesRegex(ValueError, 'exact empty-body contract'):
                    validate_deployable_articles([corrupted])

        top_level_body = copy.deepcopy(article)
        top_level_body['body_text'] = leaked_text
        with self.assertRaisesRegex(ValueError, 'metadata-only registry contract'):
            validate_deployable_articles([top_level_body])

    def test_trade_validation_remains_strict_without_post_cache(self):
        article = {
            'url': 'https://medium.com/@navnoorbawa/example-123',
            'source': 'medium',
            'source_id': '123',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'excerpt',
            'family': 'other',
        }
        article['source_id'] = 'abcdef123456'
        article['url'] = 'https://medium.com/@navnoorbawa/example-abcdef123456'
        urls = validate_deployable_articles([article])
        trade = {
            'article_title': 'Example',
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'trade_description': 'A sufficiently detailed extracted equity investment observation.',
            'description_truncated': False,
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
            'family': 'other',
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
            'family': 'other',
        }
        articles = validate_deployable_articles([article])
        trade = {
            'article_title': article['title'],
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'trade_description': 'A sufficiently detailed extracted investment observation.',
            'description_truncated': False,
            'instruments': ['equity'],
            'direction': 'long',
        }
        with self.assertRaisesRegex(ValueError, 'title does not match'):
            validate_trades([dict(trade, article_title='Wrong title')], articles)
        with self.assertRaisesRegex(ValueError, 'date does not match'):
            validate_trades([dict(trade, article_date='2026-07-13')], articles)

    def test_trade_requires_boolean_truncation_provenance(self):
        article = {
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        }
        articles = validate_deployable_articles([article])
        trade = {
            'article_title': article['title'],
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'trade_description': 'A sufficiently detailed extracted investment observation.',
            'instruments': ['equity'],
            'direction': 'unspecified',
        }
        with self.assertRaisesRegex(ValueError, 'missing fields: description_truncated'):
            validate_trades([trade], articles)
        with self.assertRaisesRegex(ValueError, 'description_truncated is not a boolean'):
            validate_trades([dict(trade, description_truncated=0)], articles)

    def test_direction_rejects_negated_signal_and_regex_override(self):
        article = {
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        }
        articles = validate_deployable_articles([article])
        base = {
            'article_title': article['title'],
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'description_truncated': False,
            'instruments': ['equity'],
        }
        negated = dict(
            base,
            trade_description=(
                'The fund did not establish a short position in the company shares '
                'during the review period.'
            ),
            direction='short',
        )
        with self.assertRaisesRegex(ValueError, 'explicitly negated trade signal'):
            validate_trades([negated], articles)

        explicit_short = dict(
            base,
            trade_description=(
                'The fund established a short position in the company shares after '
                'completing its diligence.'
            ),
            direction='long',
        )
        with self.assertRaisesRegex(ValueError, 'direction is not derived'):
            validate_trades([explicit_short], articles)

        affirmative_contrast = dict(
            base,
            trade_description=(
                'The fund did not establish a short position, but instead went long '
                'the company shares after completing its diligence.'
            ),
            direction='long',
        )
        self.assertEqual(
            validate_trades([affirmative_contrast], articles),
            {article['url']},
        )

    def test_evidence_fields_are_recomputed_from_exact_visible_passage(self):
        article = {
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        }
        articles = validate_deployable_articles([article])
        description = (
            'The fund bought Acme Capital shares because earnings would accelerate '
            'by 25%. It made $20 million on the position.'
        )
        expected = {
            'underlying': clean_underlying(extract_underlying(description)),
            'edge_or_thesis': extract_thesis(description),
            'any_quant_detail': extract_quant_details(description),
            'outcome_if_mentioned': extract_outcome(description),
            'fund_name_if_mentioned': (
                extract_fund_name(description) or extract_fund_name(article['title'])
            ),
        }
        trade = {
            'article_title': article['title'],
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'trade_description': description,
            'description_truncated': False,
            'instruments': ['equity'],
            'direction': 'long',
            **expected,
        }
        self.assertEqual(validate_trades([trade], articles), {article['url']})

        for field in expected:
            with self.subTest(field=field):
                unsupported = dict(trade, **{field: 'Evidence from a hidden paragraph.'})
                with self.assertRaisesRegex(ValueError, f'field {field} is not derived'):
                    validate_trades([unsupported], articles)

        with self.assertRaisesRegex(ValueError, 'instruments are not derived'):
            validate_trades([dict(trade, instruments=['bond'])], articles)

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
