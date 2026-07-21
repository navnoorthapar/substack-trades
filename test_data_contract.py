import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from data_contract import (
    DATA_ENDPOINT_NAMES,
    DATA_ENDPOINTS,
    FAMILIES,
    LATEST_KEYS,
    SOURCES,
    data_bundle_checksum,
    validate_data_layer,
    write_data_layer,
)


CHECKED_AT = '2026-07-21T00:00:00Z'
VALIDATION_NOW = datetime(2026, 7, 21, 1, 0, 0, tzinfo=timezone.utc)


def article_fixture(index):
    source = SOURCES[index % len(SOURCES)]
    family = FAMILIES[index % len(FAMILIES)]
    slug = f'article-{index:02d}'
    article = {
        'source': source,
        'source_id': f'{source}-{index:02d}',
        'slug': slug,
        'title': f'Common mechanism research article {index}',
        'subtitle': f'Common evidence for topic {index}',
        'post_date': f'2026-07-20T{index:02d}:00:00Z',
        'url': f'https://example.test/{source}/{slug}',
        'audience': 'public',
        'wordcount': 0 if source in {'patreon', 'fxempire'} else 1_000 + index,
        'content_status': (
            'registry' if source in {'patreon', 'fxempire'} else 'full'
        ),
        'brief': {
            'schema_version': 1,
            'body_sha256': hashlib.sha256(b'').hexdigest(),
            'lead': None,
            'sections': [],
            'fallback_evidence': None,
            'checkpoints': [],
        },
        'family': family,
    }
    if index % 3 == 0:
        article['alternate_urls'] = {
            'medium' if source != 'medium' else 'substack':
                f'https://alternate.test/{slug}',
        }
    if source == 'patreon':
        article['access'] = 'public' if index % 2 else 'paid'
        article['audience'] = article['access']
    return article


def search_fixture(articles):
    rows = []
    inverted = {}
    for index, article in enumerate(articles):
        terms = sorted(('common', f'topic-{index}'))
        rows.append({
            'slug': article['slug'],
            'source': article['source'],
            'title': article['title'],
            'post_date': article['post_date'],
            'url': article['url'],
            'entities': terms,
        })
        for term in terms:
            inverted.setdefault(term, []).append(index)
    return {
        'entities': {term: inverted[term] for term in sorted(inverted)},
        'articles': rows,
    }


def related_fixture(articles):
    related = {}
    for source_index, article in enumerate(articles):
        candidates = [
            candidate for index, candidate in enumerate(articles)
            if index != source_index
        ][:5]
        rows = []
        for rank, candidate in enumerate(candidates):
            rows.append({
                'slug': candidate['slug'],
                'source': candidate['source'],
                'title': candidate['title'],
                'url': candidate['url'],
                'score': float(round(1.0 - rank * 0.1, 6)),
                'why': ['shared: common'],
            })
        related[f'{article["source"]}:{article["slug"]}'] = rows
    return related


def families_fixture(articles):
    families = {family: [] for family in FAMILIES}
    for article in articles:
        families[article['family']].append(article['slug'])
    return families


class DataContractTests(unittest.TestCase):
    def setUp(self):
        self.case = tempfile.TemporaryDirectory(prefix='nrt-data-contract-')
        self.root = Path(self.case.name)
        self.site = self.root / 'site'
        self.source = self.root / 'articles_index.json'
        self.articles = [article_fixture(index) for index in range(24)]
        self.snapshot = {
            'schema_version': 1,
            'checked_at': CHECKED_AT,
            'article_count': len(self.articles),
            'data_checksum': 'd' * 64,
        }
        self.search = search_fixture(self.articles)
        self.related = related_fixture(self.articles)
        self.families = families_fixture(self.articles)
        self._write_source()
        self.manifest = write_data_layer(
            self.site,
            self.source,
            self.snapshot,
            self.search,
            self.related,
            self.families,
        )

    def tearDown(self):
        self.case.cleanup()

    def _write_source(self):
        self.source.write_text(
            json.dumps(self.articles, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )

    def _endpoint(self, name):
        return self.site / 'data' / name

    def _read_endpoint(self, name):
        return json.loads(self._endpoint(name).read_text(encoding='utf-8'))

    def _write_endpoint(self, name, value):
        self._endpoint(name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )

    def test_writer_and_validator_publish_one_coherent_contract(self):
        self.assertEqual(
            self._endpoint('articles_index.json').read_bytes(),
            self.source.read_bytes(),
        )
        self.assertEqual(self.manifest['schema_version'], 1)
        self.assertEqual(self.manifest['dataset_version'], 'd' * 64)
        self.assertEqual(self.manifest['generated_at'], CHECKED_AT)
        self.assertEqual(self.manifest['article_count'], 24)
        self.assertEqual(self.manifest['source_counts'], {
            source: 6 for source in SOURCES
        })
        self.assertEqual(self.manifest['endpoints'], list(DATA_ENDPOINTS))

        latest = self._read_endpoint('latest.json')
        self.assertEqual(len(latest), 20)
        self.assertEqual(
            [row['post_date'] for row in latest],
            sorted((row['post_date'] for row in self.articles), reverse=True)[:20],
        )
        self.assertTrue(all(set(row) == set(LATEST_KEYS) for row in latest))
        self.assertTrue(all(isinstance(row['alternate_urls'], dict) for row in latest))

        summary = validate_data_layer(
            self.site, self.source, self.snapshot, now=VALIDATION_NOW,
        )
        self.assertEqual(summary['article_count'], 24)
        self.assertEqual(summary['endpoint_count'], 6)
        self.assertEqual(summary['data_bundle_sha256'], data_bundle_checksum(self.site))

    def test_manifest_endpoints_resolve_inside_github_project_pages_base(self):
        base = 'https://navnoorthapar.github.io/substack-trades/'
        expected = [
            f'{base}data/{name}'
            for name in DATA_ENDPOINT_NAMES
        ]
        self.assertEqual(
            [urljoin(base, endpoint) for endpoint in self.manifest['endpoints']],
            expected,
        )
        self.assertTrue(all(
            not endpoint.startswith('/')
            for endpoint in self.manifest['endpoints']
        ))

    def test_bundle_checksum_uses_sorted_relative_paths_and_exact_bytes(self):
        digest = hashlib.sha256()
        for name in DATA_ENDPOINT_NAMES:
            relative = f'data/{name}'
            digest.update(relative.encode('utf-8'))
            digest.update(b'\0')
            digest.update(self._endpoint(name).read_bytes())
        self.assertEqual(data_bundle_checksum(self.site), digest.hexdigest())

        before = data_bundle_checksum(self.site)
        self._endpoint('latest.json').write_bytes(
            self._endpoint('latest.json').read_bytes() + b' '
        )
        self.assertNotEqual(data_bundle_checksum(self.site), before)

    def test_deliberately_corrupted_required_field_fails_validation(self):
        latest = self._read_endpoint('latest.json')
        del latest[0]['title']
        self._write_endpoint('latest.json', latest)
        with self.assertRaisesRegex(ValueError, 'minimal field set'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_recursive_normalized_private_analytics_key_is_rejected(self):
        self.articles[0]['brief']['private_metrics'] = {'subscriber_count': 12}
        self._write_source()
        write_data_layer(
            self.site,
            self.source,
            self.snapshot,
            self.search,
            self.related,
            self.families,
        )
        with self.assertRaisesRegex(ValueError, 'forbidden private-analytics key'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_paid_registry_brief_rejects_body_like_public_text(self):
        registry_index = next(
            index for index, article in enumerate(self.articles)
            if article['source'] == 'patreon'
        )
        self.articles[registry_index]['audience'] = 'paid'
        self.articles[registry_index]['access'] = 'paid'
        clean_article = copy.deepcopy(self.articles[registry_index])
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
                self.articles[registry_index] = copy.deepcopy(clean_article)
                self.articles[registry_index]['brief'].update(changes)
                self._write_source()
                write_data_layer(
                    self.site,
                    self.source,
                    self.snapshot,
                    self.search,
                    self.related,
                    self.families,
                )
                with self.assertRaisesRegex(
                    ValueError, 'exact empty-body registry contract',
                ):
                    validate_data_layer(
                        self.site, self.source, self.snapshot, now=VALIDATION_NOW,
                    )

        self.articles[registry_index] = copy.deepcopy(clean_article)
        self.articles[registry_index]['body_text'] = leaked_text
        self._write_source()
        write_data_layer(
            self.site,
            self.source,
            self.snapshot,
            self.search,
            self.related,
            self.families,
        )
        with self.assertRaisesRegex(
            ValueError, 'metadata-only registry contract',
        ):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_related_why_rejects_same_family_without_shared_features(self):
        related = self._read_endpoint('related.json')
        source = self.articles[0]
        target = self.articles[len(FAMILIES)]
        key = f'{source["source"]}:{source["slug"]}'
        related[key][0].update({
            'slug': target['slug'],
            'source': target['source'],
            'title': target['title'],
            'url': target['url'],
            'why': [f'shared: family-{source["family"]}'],
        })
        self._write_endpoint('related.json', related)
        with self.assertRaisesRegex(ValueError, 'not shared by both articles'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_format_only_change_to_public_master_fails_byte_equality(self):
        self._endpoint('articles_index.json').write_text(
            json.dumps(self.articles, ensure_ascii=False, separators=(',', ':')),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'byte-for-byte'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_malformed_or_duplicate_key_json_is_rejected(self):
        self._endpoint('latest.json').write_text('{', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'strict UTF-8 JSON'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

        self._endpoint('latest.json').write_text(
            '[{"title":"first","title":"second"}]', encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'duplicate JSON object key'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_generated_at_must_be_current_and_not_future_dated(self):
        with self.assertRaisesRegex(ValueError, 'older than 16 hours'):
            validate_data_layer(
                self.site,
                self.source,
                self.snapshot,
                now=datetime(2026, 7, 21, 17, 0, 1, tzinfo=timezone.utc),
            )
        with self.assertRaisesRegex(ValueError, 'far in the future'):
            validate_data_layer(
                self.site,
                self.source,
                self.snapshot,
                now=datetime(2026, 7, 20, 23, 49, 59, tzinfo=timezone.utc),
            )

    def test_all_four_sources_must_be_present(self):
        self.articles = [
            article for article in self.articles if article['source'] != 'fxempire'
        ]
        self.snapshot['article_count'] = len(self.articles)
        self._write_source()
        write_data_layer(
            self.site,
            self.source,
            self.snapshot,
            self.search,
            self.related,
            self.families,
        )
        with self.assertRaisesRegex(ValueError, 'all four sources'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_search_inverted_index_must_match_article_entity_lists(self):
        search = self._read_endpoint('search_index.json')
        search['entities']['common'] = search['entities']['common'][:-1]
        self._write_endpoint('search_index.json', search)
        with self.assertRaisesRegex(ValueError, 'inconsistent with article rows'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_family_index_must_partition_slugs_in_master_order(self):
        families = self._read_endpoint('families.json')
        families['other'].append(families['firm-mechanics'][0])
        self._write_endpoint('families.json', families)
        with self.assertRaisesRegex(ValueError, 'repeats a slug'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_related_rows_reject_self_links_and_untruthful_reasons(self):
        related = self._read_endpoint('related.json')
        first = self.articles[0]
        key = f'{first["source"]}:{first["slug"]}'
        related[key][0].update({
            'slug': first['slug'],
            'source': first['source'],
            'title': first['title'],
            'url': first['url'],
        })
        self._write_endpoint('related.json', related)
        with self.assertRaisesRegex(ValueError, 'points to itself'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

    def test_manifest_and_endpoint_set_must_be_exact(self):
        manifest = self._read_endpoint('manifest.json')
        manifest['article_count'] += 1
        self._write_endpoint('manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'article_count'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )

        self._endpoint('unexpected.json').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'endpoint set'):
            validate_data_layer(
                self.site, self.source, self.snapshot, now=VALIDATION_NOW,
            )


if __name__ == '__main__':
    unittest.main()
