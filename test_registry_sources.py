import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import registry_sources
from validate_pipeline import canonical_url_identity


def patreon_row(source_id='1001', title='A Market-Making Study',
                post_date='2026-07-20', access='paid'):
    return {
        'source_id': source_id,
        'title': title,
        'url': (
            'https://www.patreon.com/NavnoorBawa/posts/'
            f'a-market-making-study-{source_id}'
        ),
        'post_date': post_date,
        'access': access,
    }


def base_post(slug='a-market-making-study', title='A Market-Making Study',
              post_date='2026-07-19'):
    return {
        'source': 'substack',
        'source_id': slug,
        'slug': slug,
        'title': title,
        'post_date': post_date,
        'url': f'https://navnoorbawa.substack.com/p/{slug}',
    }


class RegistrySourcesTests(unittest.TestCase):
    def test_fx_url_sections_match_deployable_snapshot_validation(self):
        for section in ('forecasts', 'news', 'education'):
            with self.subTest(section=section):
                row = {
                    'source_id': '2001',
                    'title': 'A Verified FX Empire Analysis',
                    'url': (
                        f'https://www.fxempire.com/{section}/article/'
                        'a-verified-fx-empire-analysis-2001'
                    ),
                    'post_date': '2026-07-20',
                }
                validated = registry_sources.validate_registry(
                    [row], 'fxempire',
                )
                self.assertEqual(
                    canonical_url_identity('fxempire', validated[0]['url']),
                    row['source_id'],
                )

    def test_registry_rejects_non_contract_and_noncanonical_fields(self):
        row = patreon_row()
        row['teaser'] = 'must never be persisted'
        with self.assertRaisesRegex(ValueError, 'invalid keys'):
            registry_sources.validate_registry([row], 'patreon')

        row = patreon_row()
        row['url'] = 'http://www.patreon.com/NavnoorBawa/posts/x-1001'
        with self.assertRaisesRegex(ValueError, 'canonical HTTPS'):
            registry_sources.validate_registry([row], 'patreon')

    def test_registry_conversion_is_an_empty_body_index_record(self):
        post = registry_sources.registry_to_post(patreon_row(), 'patreon')
        self.assertEqual(post['source'], 'patreon')
        self.assertEqual(post['content_status'], 'registry')
        self.assertEqual(post['wordcount'], 0)
        self.assertEqual(post['body_text'], '')
        self.assertEqual(post['access'], 'paid')
        self.assertEqual(post['audience'], 'paid')
        self.assertEqual(
            post['brief']['body_sha256'], hashlib.sha256(b'').hexdigest(),
        )
        self.assertIn(post['family'], registry_sources.classify_family.__globals__[
            'ALLOWED_FAMILIES'
        ])

    def test_normalized_title_and_date_crosslink(self):
        base = base_post(title='A Market-Making Study & Review')
        record = patreon_row(title='A Market-Making Study and Review')
        merged, report = registry_sources.crosslink_registry(
            [base], [record], 'patreon', [],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['alternate_urls']['patreon'], record['url'])
        self.assertEqual(report[0]['decision'], 'normalized-title-and-date')

    def test_strict_fuzzy_match_and_ambiguous_match_fail_closed(self):
        title = 'The Illiquidity Arbitrage Playbook for Modern Hedge Funds'
        record = patreon_row(
            title='The Illiquidity Arbitrage Playbook for Hedge Funds',
        )
        merged, report = registry_sources.crosslink_registry(
            [base_post(title=title)], [record], 'patreon', [],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(report[0]['decision'], 'strict-title-similarity-and-date')

        duplicate = base_post(slug='second', title=record['title'])
        ambiguous, report = registry_sources.crosslink_registry(
            [base_post(title=record['title']), duplicate], [record], 'patreon', [],
        )
        self.assertEqual(len(ambiguous), 3)
        self.assertEqual(report[0]['decision'], 'distinct')

    def test_reviewed_override_and_unresolved_override(self):
        record = patreon_row(title='A materially rewritten title')
        override = {
            'source': 'patreon',
            'source_id': record['source_id'],
            'target_source': 'substack',
            'target_slug': 'a-market-making-study',
            'decision': 'match',
            'reason': 'Reviewed against the two public canonical pages.',
        }
        merged, report = registry_sources.crosslink_registry(
            [base_post()], [record], 'patreon', [override],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(report[0]['decision'], 'reviewed-override')

        override['target_slug'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'resolves to 0 targets'):
            registry_sources.crosslink_registry(
                [base_post()], [record], 'patreon', [override],
            )

    def test_public_twin_is_preferred_but_paid_registry_row_is_retained(self):
        paid = patreon_row('1002', post_date='2026-07-20', access='paid')
        public = patreon_row('1003', post_date='2026-07-18', access='public')
        merged, _ = registry_sources.crosslink_registry(
            [base_post()], [paid, public], 'patreon', [],
        )
        canonical = next(row for row in merged if row['source'] == 'substack')
        retained = next(row for row in merged if row['source'] == 'patreon')
        self.assertEqual(canonical['alternate_urls']['patreon'], public['url'])
        self.assertEqual(retained['source_id'], paid['source_id'])
        self.assertEqual(
            retained['alternate_urls']['substack'], canonical['url'],
        )

    def test_fx_can_crosslink_to_an_earlier_patreon_registry_article(self):
        patreon = registry_sources.registry_to_post(
            patreon_row(title='The Same Published Analysis'), 'patreon',
        )
        fx = {
            'source_id': '2001',
            'title': 'The Same Published Analysis',
            'url': (
                'https://www.fxempire.com/forecasts/article/'
                'the-same-published-analysis-2001'
            ),
            'post_date': '2026-07-20',
        }
        merged, report = registry_sources.crosslink_registry(
            [patreon], [fx], 'fxempire', [],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['alternate_urls']['fxempire'], fx['url'])
        self.assertEqual(report[0]['target'], f"patreon:{patreon['slug']}")

    def test_override_file_is_versioned_and_validated(self):
        payload = {
            'schema_version': 1,
            'overrides': [{
                'source': 'patreon',
                'source_id': '1001',
                'target_source': 'substack',
                'target_slug': 'a-market-making-study',
                'decision': 'distinct',
                'reason': 'Reviewed public titles describe different research.',
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'overrides.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            self.assertEqual(
                registry_sources.load_overrides(path)[0]['decision'], 'distinct',
            )


if __name__ == '__main__':
    unittest.main()
