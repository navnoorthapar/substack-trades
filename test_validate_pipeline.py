import copy
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from article_briefs import build_article_brief
from snapshot_fixtures import write_rebased_manifest
from validate_pipeline import (
    validate_article_index,
    validate_article_regression,
    validate_deployable_articles,
    validate_posts,
    validate_trade_regression,
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


def current_body(article):
    if article.get('source') == 'substack':
        article.setdefault('audience', 'everyone')
    elif article.get('source') == 'medium':
        article.setdefault('audience', 'public')
    article.update({
        'body_revision_status': 'current',
        'source_updated_at': '2026-07-14T00:00:00Z',
        'observed_source_updated_at': '2026-07-14T00:00:00Z',
    })
    if article.get('content_status') == 'full':
        article.setdefault('wordcount', 100)
        article.setdefault('brief', {
            'schema_version': 1,
            'body_sha256': hashlib.sha256(b'fixture body').hexdigest(),
            'lead': None,
            'sections': [],
            'fallback_evidence': None,
            'checkpoints': [],
        })
    return article


class DeployableSnapshotValidationTests(unittest.TestCase):
    def test_tracked_snapshot_validates_without_local_post_cache(self):
        # Validate against a rebased copy of the manifest: the tracked one
        # ages past the sixteen-hour source-check contract between scheduled
        # refreshes, which would fail this test for elapsed time rather than
        # for anything wrong with the tracked snapshot.
        with tempfile.TemporaryDirectory(prefix='nrt-pipeline-manifest-') as work:
            manifest = write_rebased_manifest(
                ROOT / 'snapshot_manifest.json',
                Path(work) / 'snapshot_manifest.json',
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'validate_pipeline.py'),
                    '--articles',
                    str(ROOT / 'articles_index.json'),
                    '--trades',
                    str(ROOT / 'trades_extracted.json'),
                    '--manifest',
                    str(manifest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Validation passed:', result.stdout)

    def test_deployable_catalog_rejects_duplicate_source_identity(self):
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        })
        # The same canonical identity cannot appear twice.
        duplicate = dict(article)
        with self.assertRaisesRegex(ValueError, 'duplicate canonical URLs'):
            validate_deployable_articles([article, duplicate])

    def test_body_revision_provenance_controls_content_claims(self):
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        })
        self.assertIn(article['url'], validate_deployable_articles([article]))

        missing = dict(article)
        missing.pop('body_revision_status')
        with self.assertRaisesRegex(ValueError, 'body_revision_status'):
            validate_deployable_articles([missing])

        current_mismatch = dict(
            article,
            observed_source_updated_at='2026-07-15T00:00:00Z',
        )
        with self.assertRaisesRegex(ValueError, 'current body revision'):
            validate_deployable_articles([current_mismatch])

        prior = dict(
            current_mismatch,
            content_status='excerpt',
            body_revision_status='prior',
        )
        self.assertIn(prior['url'], validate_deployable_articles([prior]))

        unverified = dict(
            article,
            content_status='excerpt',
            body_revision_status='unverified',
            source_updated_at='',
            observed_source_updated_at='',
        )
        self.assertIn(
            unverified['url'],
            validate_deployable_articles([unverified]),
        )

        with self.assertRaisesRegex(ValueError, 'unverified body revision'):
            validate_deployable_articles([
                dict(article, body_revision_status='unverified')
            ])

        for missing_timestamp, message in (
            (
                dict(
                    article,
                    source_updated_at='',
                    observed_source_updated_at='',
                ),
                'timestamp-bound current body revision',
            ),
            (
                dict(article, source_updated_at=''),
                'current body revision is inconsistent',
            ),
        ):
            with self.assertRaisesRegex(ValueError, message):
                validate_deployable_articles([missing_timestamp])

    def test_content_articles_reject_unpublished_cache_fields(self):
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/exact-schema',
            'source': 'substack',
            'source_id': 'exact-schema',
            'slug': 'exact-schema',
            'title': 'Exact schema',
            'subtitle': '',
            'post_date': '2026-07-14',
            'audience': 'everyone',
            'content_status': 'full',
            'family': 'other',
        })
        for field in ('body_text', 'body_html', 'private_cache'):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ValueError, 'outside the public content article contract'
                ):
                    validate_deployable_articles([
                        dict(article, **{field: 'subscriber-only text'})
                    ])

    def test_content_source_audiences_fail_closed(self):
        substack = current_body({
            'url': 'https://navnoorbawa.substack.com/p/audience-boundary',
            'source': 'substack',
            'source_id': 'audience-boundary',
            'slug': 'audience-boundary',
            'title': 'Audience boundary',
            'subtitle': '',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        })
        with self.assertRaisesRegex(
            ValueError, 'unsupported substack audience'
        ):
            validate_deployable_articles([
                dict(substack, audience='new-member-enum')
            ])

        medium = current_body({
            'url': (
                'https://medium.com/@navnoorbawa/'
                'audience-boundary-abcdef123456'
            ),
            'source': 'medium',
            'source_id': 'abcdef123456',
            'slug': 'audience-boundary-abcdef123456',
            'title': 'Audience boundary',
            'subtitle': '',
            'post_date': '2026-07-14',
            'content_status': 'excerpt',
            'family': 'other',
        })
        self.assertIn(
            medium['url'],
            validate_deployable_articles([dict(medium, audience='unknown')]),
        )
        with self.assertRaisesRegex(
            ValueError, 'unsupported medium audience'
        ):
            validate_deployable_articles([
                dict(medium, audience='new-member-enum')
            ])
        with self.assertRaisesRegex(
            ValueError, 'unverified Medium audience must remain excerpt-only'
        ):
            validate_deployable_articles([
                dict(medium, audience='unknown', content_status='full')
            ])

    def test_full_content_requires_a_real_body_wordcount_and_brief(self):
        body_text = ('captured research ' * 50).strip()
        post = current_body({
            'url': 'https://navnoorbawa.substack.com/p/full-evidence',
            'source': 'substack',
            'source_id': 'full-evidence',
            'title': 'Full evidence',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'wordcount': 100,
            'body_text': body_text,
            'is_published': True,
        })
        article = {
            key: value for key, value in post.items()
            if key not in {'body_text', 'is_published'}
        }
        article['family'] = 'other'
        article['brief'] = {
            'schema_version': 1,
            'body_sha256': hashlib.sha256(body_text.encode('utf-8')).hexdigest(),
            'lead': None,
            'sections': [],
            'fallback_evidence': None,
            'checkpoints': [],
        }

        posts = validate_posts([post])
        self.assertIn(
            article['url'],
            validate_article_index([article], posts),
        )

        missing_wordcount = dict(post)
        missing_wordcount.pop('wordcount')
        invalid_posts = (
            (missing_wordcount, 'explicit word count'),
            (dict(post, wordcount=0), 'positive word count'),
            (dict(post, body_text=''), 'empty source body'),
            (
                dict(post, body_text=('captured ' * 96).strip()),
                'less than 97%',
            ),
        )
        for invalid, message in invalid_posts:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_posts([invalid])

        with self.assertRaisesRegex(ValueError, 'positive word count'):
            validate_deployable_articles([dict(article, wordcount=0)])

        empty_brief = copy.deepcopy(article)
        empty_brief['brief']['body_sha256'] = hashlib.sha256(b'').hexdigest()
        with self.assertRaisesRegex(ValueError, 'empty-body brief'):
            validate_deployable_articles([empty_brief])

        mismatched_wordcount = dict(article, wordcount=99)
        with self.assertRaisesRegex(ValueError, 'word count does not match'):
            validate_article_index([mismatched_wordcount], posts)

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
        article = current_body({
            'url': 'https://medium.com/@navnoorbawa/example-123',
            'source': 'medium',
            'source_id': '123',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'excerpt',
            'family': 'other',
        })
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
        with self.assertRaisesRegex(ValueError, 'public observation contract'):
            validate_trades([
                dict(trade, private_body='subscriber-only text')
            ], urls)

    def test_member_observations_are_bound_to_the_exact_public_preview(self):
        body = (
            'The fund established a long position in Acme shares after a '
            'documented catalyst improved the expected payoff.'
        )
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        preview = {
            'schema_version': 1,
            'surface': 'anonymous-substack-list',
            'text': body,
            'character_count': len(body),
            'body_sha256': digest,
        }
        post = current_body({
            'url': 'https://navnoorbawa.substack.com/p/member-example',
            'source': 'substack',
            'source_id': 'member-example',
            'slug': 'member-example',
            'title': 'Member example',
            'post_date': '2026-07-14',
            'audience': 'only_paid',
            'content_status': 'excerpt',
            'wordcount': 0,
            'body_text': body,
            'is_published': True,
            'member_preview': preview,
        })
        article = {
            key: value for key, value in post.items()
            if key not in {'body_text', 'is_published'}
        }
        article['family'] = 'other'
        article['brief'] = build_article_brief({
            'body_text': body,
            'title': article['title'],
            'post_date': article['post_date'],
        })
        posts = validate_posts([post])
        articles = validate_article_index([article], posts)
        trade = {
            'article_title': article['title'],
            'article_url': article['url'],
            'article_date': '2026-07-14',
            'trade_description': body,
            'description_truncated': False,
            'instruments': ['equity'],
            'direction': 'long',
            'source_body_sha256': digest,
        }
        self.assertEqual(validate_trades([trade], articles), {article['url']})

        with self.assertRaisesRegex(ValueError, 'member preview body'):
            validate_trades([
                dict(trade, source_body_sha256='0' * 64)
            ], articles)
        with self.assertRaisesRegex(ValueError, 'public source body'):
            validate_trades([
                dict(
                    trade,
                    trade_description=(
                        'A hidden subscriber-only paragraph must not validate '
                        'as a public observation.'
                    ),
                )
            ], articles)

    def test_medium_url_identity_is_exact_and_unicode_safe(self):
        valid_urls = (
            'https://medium.com/@navnoorbawa/'
            'the-cram%C3%A9r-rao-bound-54e47fbb0504',
            'https://medium.com/@navnoorbawa/'
            'color-%CE%B3-t-options-b7bf066746e0',
            'https://medium.com/@navnoorbawa/'
            'how-soci%C3%A9t%C3%A9-works-bf9a744c7898',
        )
        for url in valid_urls:
            with self.subTest(url=url):
                article = current_body({
                    'url': url,
                    'source': 'medium',
                    'source_id': url[-12:],
                    'title': 'Canonical Medium article',
                    'post_date': '2026-07-14',
                    'content_status': 'excerpt',
                    'family': 'other',
                })
                self.assertIn(
                    url,
                    validate_deployable_articles([article]),
                )

        invalid_urls = (
            'https://medium.com/@another/story-abcdef123456',
            'https://medium.com/@navnoorbawa/../story-abcdef123456',
            'https://medium.com/@navnoorbawa/%2e%2e-abcdef123456',
            'https://medium.com/@navnoorbawa/a%2F..%2Fstory-abcdef123456',
            'https://medium.com/@navnoorbawa/story-abcdef123456?source=rss-x',
            'https://user@medium.com/@navnoorbawa/story-abcdef123456',
            'https://medium.com:444/@navnoorbawa/story-abcdef123456',
            'https://medium.com/@navnoorbawa/story-abcdef123456#fragment',
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                article = current_body({
                    'url': url,
                    'source': 'medium',
                    'source_id': 'abcdef123456',
                    'title': 'Invalid Medium article',
                    'post_date': '2026-07-14',
                    'content_status': 'excerpt',
                    'family': 'other',
                })
                with self.assertRaises(ValueError):
                    validate_deployable_articles([article])

    def test_rejects_impossible_date_missing_status_and_noncanonical_url(self):
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-02-29',
            'content_status': 'full',
            'family': 'other',
        })
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
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Canonical title',
            'post_date': '2026-07-14T09:30:00Z',
            'content_status': 'full',
            'family': 'other',
        })
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
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        })
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
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        })
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
        article = current_body({
            'url': 'https://navnoorbawa.substack.com/p/example',
            'source': 'substack',
            'source_id': 'example',
            'title': 'Example',
            'post_date': '2026-07-14',
            'content_status': 'full',
            'family': 'other',
        })
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
        with self.assertRaisesRegex(ValueError, 'medium article count collapsed'):
            validate_article_regression(current, previous, 0.5)

    def test_trade_regression_guards_public_rows_not_removed_member_cache(self):
        public_url = 'https://navnoorbawa.substack.com/p/public'
        member_url = 'https://navnoorbawa.substack.com/p/member'
        previous_articles = [
            {'source': 'substack', 'audience': 'everyone', 'url': public_url},
            {'source': 'substack', 'audience': 'only_paid', 'url': member_url},
        ]
        previous_trades = [
            {'article_url': public_url, 'trade_description': 'public one'},
            {'article_url': public_url, 'trade_description': 'public two'},
            *[
                {
                    'article_url': member_url,
                    'trade_description': f'legacy member cache {index}',
                }
                for index in range(20)
            ],
        ]
        current_trade = {
            'article_url': public_url,
            'trade_description': 'current public one',
        }
        current_articles = {
            public_url: {'member_access': False},
            member_url: {'member_access': True},
        }
        validate_trade_regression(
            [current_trade],
            {public_url},
            previous_trades,
            0.5,
            current_articles,
            previous_articles,
        )
        with self.assertRaisesRegex(
            ValueError, 'public observation count collapsed'
        ):
            validate_trade_regression(
                [],
                set(),
                previous_trades,
                0.5,
                current_articles,
                previous_articles,
            )

    def test_explicit_missing_previous_snapshots_fail_closed(self):
        for option, label in (
            ('--previous-articles', 'previous article index'),
            ('--previous-trades', 'previous trade output'),
        ):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                missing = Path(directory) / 'missing-baseline.json'
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / 'validate_pipeline.py'),
                        '--articles',
                        str(ROOT / 'articles_index.json'),
                        '--trades',
                        str(ROOT / 'trades_extracted.json'),
                        option,
                        str(missing),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(result.returncode, 1)
            self.assertIn(f'{label} is not valid JSON', result.stderr)
            self.assertIn(str(missing), result.stderr)


if __name__ == '__main__':
    unittest.main()
