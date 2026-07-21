import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlsplit

import smoke_test_site
import share_cards
from data_contract import DATA_ENDPOINT_NAMES, write_data_layer
from test_data_contract import (
    article_fixture,
    families_fixture,
    related_fixture,
    search_fixture,
)


REVISION = 'a' * 40
CHECKSUM = 'b' * 64
HTML_DIGEST = 'c' * 64
BRIEF_DIGEST = 'd' * 64
OBSERVATION_DIGEST = 'e' * 64
SUPPORT_DIGEST = 'f' * 64
DATA_DIGEST = '1' * 64
SHARE_DIGEST = '2' * 64


def fixture_deferred(checksum=CHECKSUM, schema_version=1, briefs=None):
    if briefs is None:
        briefs = {'a_article': {'lead': {'text': 'Exact authored passage.'}}}
    return {
        'schema_version': schema_version,
        'data_checksum': checksum,
        'briefs': briefs,
    }


def fixture_observations(checksum=CHECKSUM, schema_version=1, count=1327):
    return {
        'schema_version': schema_version,
        'data_checksum': checksum,
        'observations': [{'id': f'i_{index}'} for index in range(count)],
    }


def fixture_html(
    revision=REVISION,
    articles='363',
    observations='1327',
    checksum=CHECKSUM,
    omitted_id=None,
    brief_digest=BRIEF_DIGEST,
    observation_digest=OBSERVATION_DIGEST,
):
    ids = smoke_test_site.REQUIRED_ELEMENT_IDS - ({omitted_id} if omitted_id else set())
    elements = ''.join(f'<div id="{element_id}"></div>' for element_id in sorted(ids))
    return f'''<!doctype html>
<html><head>
<title>Navnoor Research Terminal</title>
<meta name="nrt-revision" content="{revision}">
<meta name="nrt-article-count" content="{articles}">
<meta name="nrt-observation-count" content="{observations}">
<meta name="nrt-data-checksum" content="{checksum}">
<meta name="nrt-brief-archive-sha256" content="{brief_digest}">
<meta name="nrt-observation-archive-sha256" content="{observation_digest}">
</head><body>{elements}
</body></html>'''


def fixture_data_payloads():
    articles = [article_fixture(index) for index in range(24)]
    generated_at = datetime.now(timezone.utc).isoformat(
        timespec='seconds'
    ).replace('+00:00', 'Z')
    snapshot = {
        'schema_version': 1,
        'checked_at': generated_at,
        'article_count': len(articles),
        'data_checksum': CHECKSUM,
    }
    with tempfile.TemporaryDirectory(prefix='nrt-smoke-data-fixture-') as directory:
        root = Path(directory)
        source = root / 'articles.json'
        site = root / 'site'
        source.write_text(
            json.dumps(articles, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        write_data_layer(
            site,
            source,
            snapshot,
            search_fixture(articles),
            related_fixture(articles),
            families_fixture(articles),
        )
        payloads = {
            name: (site / 'data' / name).read_bytes()
            for name in DATA_ENDPOINT_NAMES
        }
    content_count = sum(
        article['content_status'] != 'registry' for article in articles
    )
    return payloads, content_count


def fixture_share_payloads(page_url='https://example.test/research/'):
    data_payloads, _ = fixture_data_payloads()
    articles = json.loads(data_payloads['articles_index.json'])
    root = smoke_test_site._site_root(page_url)
    payloads = {}
    for article in smoke_test_site.representative_share_articles(articles):
        slug = article['slug']
        payloads[f'cards/{slug}.png'] = share_cards.render_share_card(
            article['title'], article['source'], article['post_date'],
        )
        payloads[f'a/{slug}.html'] = share_cards.render_article_stub(
            article,
            smoke_test_site._stable_article_id(article),
            root,
        ).encode('utf-8')
    return payloads, articles


class SmokeTestSiteTests(unittest.TestCase):
    @patch('smoke_test_site.ssl.create_default_context')
    def test_verified_ssl_context_uses_certifi_when_available(self, create_context):
        certifi = types.SimpleNamespace(where=lambda: '/trusted/certifi-ca.pem')
        with patch.dict(sys.modules, {'certifi': certifi}):
            context = smoke_test_site.verified_ssl_context()
        self.assertIs(context, create_context.return_value)
        create_context.assert_called_once_with(cafile='/trusted/certifi-ca.pem')

    @patch('smoke_test_site.ssl.create_default_context')
    def test_verified_ssl_context_falls_back_to_platform_store(self, create_context):
        real_import = __import__

        def import_without_certifi(name, *args, **kwargs):
            if name == 'certifi':
                raise ImportError('certifi intentionally unavailable')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=import_without_certifi):
            context = smoke_test_site.verified_ssl_context()
        self.assertIs(context, create_context.return_value)
        create_context.assert_called_once_with()

    def test_valid_release_metadata_and_core_shell_pass(self):
        self.assertEqual(
            smoke_test_site.validate_html(
                fixture_html(), REVISION, 363, 1327, CHECKSUM,
            ),
            {
                smoke_test_site.DEFERRED_ASSET_NAME: BRIEF_DIGEST,
                smoke_test_site.OBSERVATION_ASSET_NAME: OBSERVATION_DIGEST,
            },
        )

    def test_wrong_revision_or_counts_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'deployed revision'):
            smoke_test_site.validate_html(
                fixture_html(revision='c' * 40), REVISION, 363, 1327, CHECKSUM,
            )
        with self.assertRaisesRegex(ValueError, 'expected 363'):
            smoke_test_site.validate_html(
                fixture_html(articles='362'), REVISION, 363, 1327, CHECKSUM,
            )

    def test_invalid_checksum_and_missing_core_element_fail(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            smoke_test_site.validate_html(
                fixture_html(checksum='not-a-digest'), REVISION, 363, 1327, CHECKSUM,
            )
        with self.assertRaisesRegex(ValueError, 'data-table'):
            smoke_test_site.validate_html(
                fixture_html(omitted_id='data-table'), REVISION, 363, 1327, CHECKSUM,
            )

    def test_wrong_data_checksum_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'expected'):
            smoke_test_site.validate_html(
                fixture_html(), REVISION, 363, 1327, 'c' * 64,
            )

    def test_missing_or_invalid_deferred_asset_digest_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'article_briefs.json'):
            smoke_test_site.validate_html(
                fixture_html(brief_digest='not-a-digest'),
                REVISION,
                363,
                1327,
                CHECKSUM,
            )

    def test_duplicate_or_decoy_asset_digest_declaration_fails_closed(self):
        duplicate = fixture_html().replace(
            '</head>',
            f'<meta name="nrt-brief-archive-sha256" content="{BRIEF_DIGEST}">\n</head>',
        )
        with self.assertRaisesRegex(ValueError, 'found 2'):
            smoke_test_site.validate_html(
                duplicate, REVISION, 363, 1327, CHECKSUM,
            )
        decoy_only = fixture_html().replace(
            f'<meta name="nrt-brief-archive-sha256" content="{BRIEF_DIGEST}">',
            f'<!--\n<meta name="nrt-brief-archive-sha256" content="{BRIEF_DIGEST}">\n-->',
        )
        with self.assertRaisesRegex(ValueError, 'found 0'):
            smoke_test_site.embedded_asset_digests(decoy_only)
        with self.assertRaisesRegex(ValueError, 'observations.json'):
            smoke_test_site.validate_html(
                fixture_html(observation_digest='ABC'),
                REVISION,
                363,
                1327,
                CHECKSUM,
            )

    def test_deferred_dossier_must_match_exact_release(self):
        smoke_test_site.validate_deferred_payload(fixture_deferred(), CHECKSUM)
        with self.assertRaisesRegex(ValueError, 'schema_version must be 1'):
            smoke_test_site.validate_deferred_payload(
                fixture_deferred(schema_version=2), CHECKSUM,
            )
        with self.assertRaisesRegex(ValueError, 'expected'):
            smoke_test_site.validate_deferred_payload(
                fixture_deferred(checksum='c' * 64), CHECKSUM,
            )
        with self.assertRaisesRegex(ValueError, 'non-empty object'):
            smoke_test_site.validate_deferred_payload(
                fixture_deferred(briefs={}), CHECKSUM,
            )

    def test_observation_archive_must_match_exact_release(self):
        smoke_test_site.validate_observation_payload(
            fixture_observations(), CHECKSUM, 1327,
        )
        with self.assertRaisesRegex(ValueError, 'schema_version must be 1'):
            smoke_test_site.validate_observation_payload(
                fixture_observations(schema_version=2), CHECKSUM, 1327,
            )
        with self.assertRaisesRegex(ValueError, 'expected'):
            smoke_test_site.validate_observation_payload(
                fixture_observations(checksum='c' * 64), CHECKSUM, 1327,
            )
        with self.assertRaisesRegex(ValueError, 'count is 1, expected 1327'):
            smoke_test_site.validate_observation_payload(
                fixture_observations(count=1), CHECKSUM, 1327,
            )
        duplicate_ids = fixture_observations(count=2)
        duplicate_ids['observations'][1]['id'] = duplicate_ids['observations'][0]['id']
        with self.assertRaisesRegex(ValueError, 'missing or duplicated'):
            smoke_test_site.validate_observation_payload(
                duplicate_ids, CHECKSUM, 2,
            )
        malformed_row = fixture_observations(count=1)
        malformed_row['observations'][0] = 'not-an-object'
        with self.assertRaisesRegex(ValueError, 'missing or duplicated'):
            smoke_test_site.validate_observation_payload(
                malformed_row, CHECKSUM, 1,
            )

    def test_deferred_asset_resolves_beside_page_without_inheriting_query(self):
        self.assertEqual(
            smoke_test_site.deferred_asset_url(
                'https://example.test/research/?old=1#fragment'
            ),
            'https://example.test/research/article_briefs.json',
        )
        self.assertEqual(
            smoke_test_site.deferred_asset_url(
                'https://example.test/research/index.html?old=1'
            ),
            'https://example.test/research/article_briefs.json',
        )
        self.assertEqual(
            smoke_test_site.deferred_asset_url(
                'https://example.test/research/?old=1',
                smoke_test_site.OBSERVATION_ASSET_NAME,
            ),
            'https://example.test/research/observations.json',
        )
        with self.assertRaisesRegex(ValueError, 'unsupported deferred asset'):
            smoke_test_site.deferred_asset_url(
                'https://example.test/research/', 'other.json',
            )

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_deferred_fetch_rejects_off_origin_redirect(self, urlopen, ssl_context):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://attacker.test/article_briefs.json'
        urlopen.return_value = response
        with self.assertRaisesRegex(ValueError, 'redirected off-origin'):
            smoke_test_site.fetch_deferred_briefs(
                'https://example.test/research/',
                REVISION,
                1,
                20,
                expected_sha256=BRIEF_DIGEST,
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            'https://example.test/research/article_briefs.json?'
            f'nrt_smoke_revision={REVISION}&nrt_smoke_attempt=1',
        )
        ssl_context.assert_called_once_with()

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_page_fetch_rejects_https_off_origin_redirect(self, urlopen, ssl_context):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://attacker.test/research/'
        urlopen.return_value = response
        with self.assertRaisesRegex(ValueError, 'redirected off-origin'):
            smoke_test_site.fetch_html(
                'https://example.test/research/',
                REVISION,
                1,
                20,
                expected_sha256=HTML_DIGEST,
            )
        ssl_context.assert_called_once_with()

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_page_fetch_requires_the_exact_tested_html_bytes(self, urlopen, ssl_context):
        import hashlib
        payload = fixture_html().encode('utf-8')
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://example.test/research/'
        response.status = 200
        response.headers.get_content_type.return_value = 'text/html'
        response.read.return_value = payload
        urlopen.return_value = response
        expected = hashlib.sha256(payload).hexdigest()
        self.assertEqual(
            smoke_test_site.fetch_html(
                'https://example.test/research/',
                REVISION,
                1,
                20,
                expected_sha256=expected,
            ),
            (fixture_html(), 'https://example.test/research/'),
        )
        with self.assertRaisesRegex(ValueError, 'index.html SHA-256'):
            smoke_test_site.fetch_html(
                'https://example.test/research/',
                REVISION,
                2,
                20,
                expected_sha256=HTML_DIGEST,
            )
        self.assertEqual(ssl_context.call_count, 2)

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_deferred_fetch_rejects_bytes_that_do_not_match_html_digest(
        self, urlopen, ssl_context,
    ):
        payload = json.dumps(fixture_deferred()).encode('utf-8')
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://example.test/research/article_briefs.json'
        response.status = 200
        response.headers.get_content_type.return_value = 'application/json'
        response.read.return_value = payload
        urlopen.return_value = response
        with self.assertRaisesRegex(ValueError, 'SHA-256 is'):
            smoke_test_site.fetch_deferred_briefs(
                'https://example.test/research/',
                REVISION,
                1,
                20,
                expected_sha256=BRIEF_DIGEST,
            )
        ssl_context.assert_called_once_with()

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_deferred_fetch_accepts_only_the_exact_build_bytes(
        self, urlopen, ssl_context,
    ):
        import hashlib
        payload = json.dumps(fixture_deferred()).encode('utf-8')
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://example.test/research/article_briefs.json'
        response.status = 200
        response.headers.get_content_type.return_value = 'application/json'
        response.read.return_value = payload
        urlopen.return_value = response
        self.assertEqual(
            smoke_test_site.fetch_deferred_briefs(
                'https://example.test/research/',
                REVISION,
                1,
                20,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            ),
            fixture_deferred(),
        )
        ssl_context.assert_called_once_with()

    @patch('smoke_test_site.urlopen')
    def test_invalid_expected_asset_digest_is_rejected_before_network(self, urlopen):
        with self.assertRaisesRegex(ValueError, 'expected digest'):
            smoke_test_site.fetch_deferred_observations(
                'https://example.test/research/',
                REVISION,
                1,
                20,
                expected_sha256='invalid',
            )
        urlopen.assert_not_called()

    def test_snapshot_counts_require_non_empty_json_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.json'
            path.write_text(json.dumps([{'id': 1}, {'id': 2}]), encoding='utf-8')
            self.assertEqual(smoke_test_site.load_list_count(path, 'records'), 2)
            path.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'non-empty JSON list'):
                smoke_test_site.load_list_count(path, 'records')

    def test_terminal_article_count_excludes_metadata_only_registry_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'articles.json'
            path.write_text(json.dumps([
                {'content_status': 'full'},
                {'content_status': 'excerpt'},
                {'content_status': 'registry'},
                {'content_status': 'registry'},
            ]), encoding='utf-8')
            self.assertEqual(smoke_test_site.load_content_article_count(path), 2)
            path.write_text(json.dumps([{'title': 'missing status'}]), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'without content_status'):
                smoke_test_site.load_content_article_count(path)

    def test_snapshot_checksum_uses_exact_input_bytes_with_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            articles = Path(directory) / 'articles.json'
            observations = Path(directory) / 'observations.json'
            articles.write_bytes(b'[1]\n')
            observations.write_bytes(b'[2]\n')
            import hashlib
            expected = hashlib.sha256(b'[1]\n\0[2]\n').hexdigest()
            self.assertEqual(
                smoke_test_site.snapshot_checksum(articles, observations), expected,
            )

    def test_support_bundle_checksum_binds_name_order_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payloads = {}
            for index, name in enumerate(smoke_test_site.SUPPORT_ASSET_NAMES):
                payload = f'asset-{index}-{name}'.encode('utf-8')
                (root / name).write_bytes(payload)
                payloads[name] = payload
            self.assertEqual(
                smoke_test_site.support_bundle_checksum(root),
                smoke_test_site.support_payload_checksum(payloads),
            )
            payloads[smoke_test_site.SUPPORT_ASSET_NAMES[0]] += b'changed'
            self.assertNotEqual(
                smoke_test_site.support_bundle_checksum(root),
                smoke_test_site.support_payload_checksum(payloads),
            )

    def test_support_asset_names_resolve_beside_page_without_path_traversal(self):
        self.assertEqual(
            smoke_test_site.sibling_asset_url(
                'https://example.test/research/?old=1', 'robots.txt',
            ),
            'https://example.test/research/robots.txt',
        )
        with self.assertRaisesRegex(ValueError, 'plain basename'):
            smoke_test_site.sibling_asset_url(
                'https://example.test/research/', '../robots.txt',
            )

    def test_data_endpoint_urls_are_nested_and_strictly_allowlisted(self):
        self.assertEqual(
            smoke_test_site.data_asset_url(
                'https://example.test/research/?old=1#fragment', 'manifest.json',
            ),
            'https://example.test/research/data/manifest.json',
        )
        self.assertEqual(
            smoke_test_site.data_asset_url(
                'https://example.test/research/index.html?old=1', 'latest.json',
            ),
            'https://example.test/research/data/latest.json',
        )
        for unsafe in ('../manifest.json', 'data/manifest.json', 'other.json'):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, 'unsupported data endpoint'):
                    smoke_test_site.data_asset_url(
                        'https://example.test/research/', unsafe,
                    )

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_data_fetch_validates_every_nested_endpoint_and_exact_bytes(
        self, urlopen, ssl_context,
    ):
        payloads, _ = fixture_data_payloads()

        def response_for_request(request, **_kwargs):
            asset_name = Path(request.full_url.split('?', 1)[0]).name
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = request.full_url.split('?', 1)[0]
            response.status = 200
            response.headers.get_content_type.return_value = 'application/json'
            response.read.return_value = payloads[asset_name]
            return response

        urlopen.side_effect = response_for_request
        actual = smoke_test_site.fetch_data_bundle(
            'https://example.test/research/', REVISION, 3, 20,
        )
        self.assertEqual(actual, payloads)
        self.assertEqual(urlopen.call_count, len(DATA_ENDPOINT_NAMES))
        self.assertEqual(ssl_context.call_count, len(DATA_ENDPOINT_NAMES))
        self.assertEqual(
            {request_call.args[0].full_url for request_call in urlopen.call_args_list},
            {
                f'https://example.test/research/data/{name}?'
                f'nrt_smoke_revision={REVISION}&nrt_smoke_attempt=3'
                for name in DATA_ENDPOINT_NAMES
            },
        )

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_data_fetch_rejects_html_404_fallback_and_off_origin_redirect(
        self, urlopen, _ssl_context,
    ):
        def html_response(request, **_kwargs):
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = request.full_url.split('?', 1)[0]
            response.status = 200
            response.headers.get_content_type.return_value = 'text/html'
            response.read.return_value = b'<html>GitHub Pages 404</html>'
            return response

        urlopen.side_effect = html_response
        with self.assertRaisesRegex(ValueError, 'not application/json'):
            smoke_test_site.fetch_data_bundle(
                'https://example.test/research/', REVISION, 1, 20,
            )

        def redirect_response(request, **_kwargs):
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = 'https://attacker.test/data/articles_index.json'
            return response

        urlopen.side_effect = redirect_response
        with self.assertRaisesRegex(ValueError, 'redirected off-origin'):
            smoke_test_site.fetch_data_bundle(
                'https://example.test/research/', REVISION, 2, 20,
            )

    def test_live_data_semantics_reject_corrupt_cross_endpoint_counts(self):
        payloads, content_count = fixture_data_payloads()
        summary = smoke_test_site.validate_live_data_bundle(
            payloads, content_count, CHECKSUM,
        )
        self.assertEqual(summary['article_count'], 24)
        self.assertEqual(
            summary['data_bundle_sha256'],
            smoke_test_site.data_payload_checksum(payloads),
        )

        corrupt = dict(payloads)
        manifest = json.loads(corrupt['manifest.json'])
        manifest['article_count'] += 1
        corrupt['manifest.json'] = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
        ).encode('utf-8')
        with self.assertRaisesRegex(ValueError, 'article_count'):
            smoke_test_site.validate_live_data_bundle(
                corrupt, content_count, CHECKSUM,
            )

    def test_share_proof_selects_and_hashes_content_and_registry_pairs(self):
        payloads, articles = fixture_share_payloads()
        selected = smoke_test_site.representative_share_articles(articles)
        self.assertNotEqual(selected[0]['content_status'], 'registry')
        self.assertEqual(selected[1]['content_status'], 'registry')
        self.assertEqual(
            set(payloads), set(smoke_test_site.share_proof_asset_names(articles)),
        )
        expected = smoke_test_site.share_proof_payload_checksum(payloads, articles)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, payload in payloads.items():
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(payload)
            catalogue = root / 'articles.json'
            catalogue.write_text(json.dumps(articles), encoding='utf-8')
            self.assertEqual(
                smoke_test_site.share_proof_bundle_checksum(root, catalogue),
                expected,
            )

        corrupt = dict(payloads)
        first_name = smoke_test_site.share_proof_asset_names(articles)[0]
        corrupt[first_name] += b'changed'
        self.assertNotEqual(
            smoke_test_site.share_proof_payload_checksum(corrupt, articles),
            expected,
        )

    def test_share_asset_urls_are_nested_encoded_and_allowlisted(self):
        _payloads, articles = fixture_share_payloads()
        asset_name = smoke_test_site.share_proof_asset_names(articles)[0]
        self.assertEqual(
            smoke_test_site.share_asset_url(
                'https://example.test/research/index.html?old=1',
                asset_name,
                articles,
            ),
            f'https://example.test/research/{asset_name}',
        )
        with self.assertRaisesRegex(ValueError, 'unsupported share-proof asset'):
            smoke_test_site.share_asset_url(
                'https://example.test/research/', '../index.html', articles,
            )

    def test_share_proof_validates_exact_png_stub_and_redirect_contracts(self):
        page_url = 'https://example.test/research/'
        payloads, articles = fixture_share_payloads(page_url)
        summary = smoke_test_site.validate_share_proof(
            payloads, articles, page_url,
        )
        self.assertEqual(summary['article_count'], 2)
        self.assertEqual(summary['asset_count'], 4)

        first_card = smoke_test_site.share_proof_asset_names(articles)[0]
        invalid_card = dict(payloads)
        invalid_card[first_card] = b'not-a-png'
        with self.assertRaisesRegex(ValueError, '1200x630 indexed PNG'):
            smoke_test_site.validate_share_proof(
                invalid_card, articles, page_url,
            )

        registry = smoke_test_site.representative_share_articles(articles)[1]
        registry_stub = f'a/{registry["slug"]}.html'
        invalid_stub = dict(payloads)
        invalid_stub[registry_stub] = invalid_stub[registry_stub].replace(
            registry['url'].encode('utf-8'), b'https://attacker.test/',
        )
        with self.assertRaisesRegex(ValueError, 'trusted build bytes'):
            smoke_test_site.validate_share_proof(
                invalid_stub, articles, page_url,
            )

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_share_proof_fetches_both_exact_pairs_with_strict_types(
        self, urlopen, ssl_context,
    ):
        page_url = 'https://example.test/research/'
        payloads, articles = fixture_share_payloads(page_url)

        def response_for_request(request, **_kwargs):
            path = urlsplit(request.full_url).path
            asset_name = path.split('/research/', 1)[1]
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = request.full_url.split('?', 1)[0]
            response.status = 200
            response.headers.get_content_type.return_value = (
                'image/png' if asset_name.endswith('.png') else 'text/html'
            )
            response.read.return_value = payloads[asset_name]
            return response

        urlopen.side_effect = response_for_request
        self.assertEqual(
            smoke_test_site.fetch_share_proof(
                page_url, articles, REVISION, 4, 20,
            ),
            payloads,
        )
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(ssl_context.call_count, 4)

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_support_fetch_validates_and_hashes_every_exact_asset(
        self, urlopen, ssl_context,
    ):
        payloads = {}
        responses = {}
        for name in smoke_test_site.SUPPORT_ASSET_NAMES:
            payloads[name] = f'exact-{name}'.encode('utf-8')
            response = MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = f'https://example.test/research/{name}'
            response.status = 200
            response.headers.get_content_type.return_value = next(
                iter(smoke_test_site.SUPPORT_CONTENT_TYPES[name])
            )
            response.read.return_value = payloads[name]
            responses[name] = response

        def response_for_request(request, **_kwargs):
            name = Path(request.full_url.split('?', 1)[0]).name
            return responses[name]

        urlopen.side_effect = response_for_request
        self.assertEqual(
            smoke_test_site.fetch_support_bundle(
                'https://example.test/research/', REVISION, 2, 20,
            ),
            smoke_test_site.support_payload_checksum(payloads),
        )
        self.assertEqual(urlopen.call_count, len(smoke_test_site.SUPPORT_ASSET_NAMES))
        self.assertEqual(ssl_context.call_count, len(smoke_test_site.SUPPORT_ASSET_NAMES))
        self.assertEqual(
            {request_call.args[0].full_url for request_call in urlopen.call_args_list},
            {
                f'https://example.test/research/{name}?'
                f'nrt_smoke_revision={REVISION}&nrt_smoke_attempt=2'
                for name in smoke_test_site.SUPPORT_ASSET_NAMES
            },
        )

    @patch('smoke_test_site.verified_ssl_context')
    @patch('smoke_test_site.urlopen')
    def test_support_fetch_rejects_unexpected_content_type(self, urlopen, _ssl_context):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://example.test/research/favicon.svg'
        response.status = 200
        response.headers.get_content_type.return_value = 'text/html'
        urlopen.return_value = response
        with self.assertRaisesRegex(ValueError, 'unexpected content type'):
            smoke_test_site.fetch_support_bundle(
                'https://example.test/research/', REVISION, 1, 20,
            )

    @patch('smoke_test_site.fetch_support_bundle', return_value=SUPPORT_DIGEST)
    @patch(
        'smoke_test_site.fetch_deferred_observations',
        return_value=fixture_observations(),
    )
    @patch('smoke_test_site.fetch_deferred_briefs', return_value=fixture_deferred())
    @patch(
        'smoke_test_site.fetch_html',
        return_value=(fixture_html(), 'https://example.test/research/'),
    )
    def test_smoke_requires_expected_support_bundle_when_supplied(
        self, _fetch_html, _fetch_deferred, _fetch_observations, fetch_support,
    ):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            smoke_test_site.smoke_test(
                'https://example.test/research/', REVISION, 363, 1327,
                CHECKSUM, HTML_DIGEST, BRIEF_DIGEST, OBSERVATION_DIGEST,
                retries=1, expected_support_sha256=SUPPORT_DIGEST,
            )
        fetch_support.assert_called_once_with(
            'https://example.test/research/', REVISION, 1, 20.0,
        )

    def test_smoke_requires_exact_live_data_bundle_when_supplied(self):
        payloads, content_count = fixture_data_payloads()
        expected_digest = smoke_test_site.data_payload_checksum(payloads)
        with (
            patch(
                'smoke_test_site.fetch_html',
                return_value=(
                    fixture_html(articles=str(content_count)),
                    'https://example.test/research/',
                ),
            ),
            patch(
                'smoke_test_site.fetch_deferred_briefs',
                return_value=fixture_deferred(),
            ),
            patch(
                'smoke_test_site.fetch_deferred_observations',
                return_value=fixture_observations(),
            ),
            patch('smoke_test_site.fetch_data_bundle', return_value=payloads) as fetch_data,
            patch('smoke_test_site.validate_live_data_bundle') as validate_data,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            smoke_test_site.smoke_test(
                'https://example.test/research/',
                REVISION,
                content_count,
                1327,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=1,
                expected_data_sha256=expected_digest,
            )
        fetch_data.assert_called_once_with(
            'https://example.test/research/', REVISION, 1, 20.0,
        )
        validate_data.assert_called_once_with(payloads, content_count, CHECKSUM)

        with (
            patch(
                'smoke_test_site.fetch_html',
                return_value=(
                    fixture_html(articles=str(content_count)),
                    'https://example.test/research/',
                ),
            ),
            patch(
                'smoke_test_site.fetch_deferred_briefs',
                return_value=fixture_deferred(),
            ),
            patch(
                'smoke_test_site.fetch_deferred_observations',
                return_value=fixture_observations(),
            ),
            patch('smoke_test_site.fetch_data_bundle', return_value=payloads),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(ValueError, 'data endpoint bundle SHA-256'):
                smoke_test_site.smoke_test(
                    'https://example.test/research/',
                    REVISION,
                    content_count,
                    1327,
                    CHECKSUM,
                    HTML_DIGEST,
                    BRIEF_DIGEST,
                    OBSERVATION_DIGEST,
                    retries=1,
                    expected_data_sha256=DATA_DIGEST,
                )

    def test_smoke_requires_exact_semantic_share_proof_when_supplied(self):
        data_payloads, content_count = fixture_data_payloads()
        articles = json.loads(data_payloads['articles_index.json'])
        share_payloads, _ = fixture_share_payloads()
        data_digest = smoke_test_site.data_payload_checksum(data_payloads)
        share_digest = smoke_test_site.share_proof_payload_checksum(
            share_payloads, articles,
        )
        with (
            patch(
                'smoke_test_site.fetch_html',
                return_value=(
                    fixture_html(articles=str(content_count)),
                    'https://example.test/research/',
                ),
            ),
            patch(
                'smoke_test_site.fetch_deferred_briefs',
                return_value=fixture_deferred(),
            ),
            patch(
                'smoke_test_site.fetch_deferred_observations',
                return_value=fixture_observations(),
            ),
            patch(
                'smoke_test_site.fetch_data_bundle', return_value=data_payloads,
            ),
            patch(
                'smoke_test_site.fetch_share_proof', return_value=share_payloads,
            ) as fetch_share,
            patch('smoke_test_site.validate_live_data_bundle'),
            patch('smoke_test_site.validate_share_proof') as validate_share,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            smoke_test_site.smoke_test(
                'https://example.test/research/',
                REVISION,
                content_count,
                1327,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=1,
                expected_data_sha256=data_digest,
                expected_share_sha256=share_digest,
            )
        fetch_share.assert_called_once_with(
            'https://example.test/research/', articles, REVISION, 1, 20.0,
        )
        validate_share.assert_called_once_with(
            share_payloads, articles, 'https://example.test/research/',
        )

        with self.assertRaisesRegex(ValueError, 'requires the exact live data bundle'):
            smoke_test_site.smoke_test(
                'https://example.test/research/',
                REVISION,
                content_count,
                1327,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=1,
                expected_share_sha256=SHARE_DIGEST,
            )

    @patch(
        'smoke_test_site.fetch_deferred_observations',
        return_value=fixture_observations(),
    )
    @patch('smoke_test_site.fetch_deferred_briefs', return_value=fixture_deferred())
    @patch(
        'smoke_test_site.fetch_html',
        return_value=(fixture_html(), 'https://example.test/canonical/'),
    )
    def test_same_origin_canonical_page_url_is_used_as_the_asset_base(
        self, fetch_html, fetch_deferred, fetch_observations,
    ):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            smoke_test_site.smoke_test(
                'https://example.test/research',
                REVISION,
                363,
                1327,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=1,
            )
        fetch_deferred.assert_called_once_with(
            'https://example.test/canonical/',
            REVISION,
            1,
            20.0,
            expected_sha256=BRIEF_DIGEST,
        )
        fetch_observations.assert_called_once_with(
            'https://example.test/canonical/',
            REVISION,
            1,
            20.0,
            expected_sha256=OBSERVATION_DIGEST,
        )

    @patch(
        'smoke_test_site.fetch_deferred_observations',
        return_value=fixture_observations(),
    )
    @patch('smoke_test_site.fetch_deferred_briefs', return_value=fixture_deferred())
    @patch('smoke_test_site.time.sleep')
    @patch('smoke_test_site.fetch_html')
    def test_stale_release_is_retried_until_expected_revision_appears(
        self, fetch_html, sleep, fetch_deferred, fetch_observations,
    ):
        fetch_html.side_effect = [
            (fixture_html(revision='c' * 40), 'https://example.test/research/'),
            (fixture_html(), 'https://example.test/research/'),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            smoke_test_site.smoke_test(
                'https://example.test/research/',
                REVISION,
                363,
                1327,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=2,
                retry_delay=0.01,
            )
        self.assertEqual(fetch_html.call_count, 2)
        self.assertEqual(
            fetch_html.call_args_list,
            [
                call(
                    'https://example.test/research/',
                    REVISION,
                    1,
                    20.0,
                    expected_sha256=HTML_DIGEST,
                ),
                call(
                    'https://example.test/research/',
                    REVISION,
                    2,
                    20.0,
                    expected_sha256=HTML_DIGEST,
                ),
            ],
        )
        fetch_deferred.assert_called_once_with(
            'https://example.test/research/',
            REVISION,
            2,
            20.0,
            expected_sha256=BRIEF_DIGEST,
        )
        fetch_observations.assert_called_once_with(
            'https://example.test/research/',
            REVISION,
            2,
            20.0,
            expected_sha256=OBSERVATION_DIGEST,
        )
        sleep.assert_called_once_with(0.01)

    @patch(
        'smoke_test_site.fetch_deferred_observations',
        return_value=fixture_observations(),
    )
    @patch('smoke_test_site.time.sleep')
    @patch('smoke_test_site.fetch_deferred_briefs')
    @patch(
        'smoke_test_site.fetch_html',
        return_value=(fixture_html(), 'https://example.test/research/'),
    )
    def test_stale_deferred_asset_is_retried_with_page(
        self, fetch_html, fetch_deferred, sleep, fetch_observations,
    ):
        fetch_deferred.side_effect = [
            fixture_deferred(checksum='c' * 64),
            fixture_deferred(),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            smoke_test_site.smoke_test(
                'https://example.test/research/',
                REVISION,
                363,
                1327,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=2,
                retry_delay=0.01,
            )
        self.assertEqual(fetch_html.call_count, 2)
        self.assertEqual(fetch_deferred.call_count, 2)
        fetch_observations.assert_called_once_with(
            'https://example.test/research/',
            REVISION,
            2,
            20.0,
            expected_sha256=OBSERVATION_DIGEST,
        )
        sleep.assert_called_once_with(0.01)

    @patch('smoke_test_site.time.sleep')
    @patch('smoke_test_site.fetch_deferred_observations')
    @patch('smoke_test_site.fetch_deferred_briefs', return_value=fixture_deferred())
    @patch(
        'smoke_test_site.fetch_html',
        return_value=(fixture_html(), 'https://example.test/research/'),
    )
    def test_stale_observation_asset_retries_the_entire_release(
        self, fetch_html, fetch_deferred, fetch_observations, sleep,
    ):
        fetch_observations.side_effect = [
            fixture_observations(checksum='c' * 64),
            fixture_observations(),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            smoke_test_site.smoke_test(
                'https://example.test/research/',
                REVISION,
                363,
                1327,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=2,
                retry_delay=0.01,
            )
        self.assertEqual(fetch_html.call_count, 2)
        self.assertEqual(fetch_deferred.call_count, 2)
        self.assertEqual(fetch_observations.call_count, 2)
        sleep.assert_called_once_with(0.01)

    @patch(
        'smoke_test_site.fetch_html',
        return_value=(fixture_html(), 'https://example.test/research/'),
    )
    def test_html_asset_digest_must_match_independent_build_digest(self, fetch_html):
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'trusted build digest'):
                smoke_test_site.smoke_test(
                    'https://example.test/research/',
                    REVISION,
                    363,
                    1327,
                    CHECKSUM,
                    HTML_DIGEST,
                    'f' * 64,
                    OBSERVATION_DIGEST,
                    retries=1,
                    retry_delay=0,
                )
        fetch_html.assert_called_once()

    @patch(
        'smoke_test_site.fetch_html',
        return_value=(fixture_html(revision='c' * 40), 'https://example.test/'),
    )
    def test_retry_exhaustion_fails_the_deployment(self, fetch_html):
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'did not become healthy'):
                smoke_test_site.smoke_test(
                    'https://example.test/',
                    REVISION,
                    363,
                    1327,
                    CHECKSUM,
                    HTML_DIGEST,
                    BRIEF_DIGEST,
                    OBSERVATION_DIGEST,
                    retries=1,
                    retry_delay=0,
                )

    def test_non_https_url_is_rejected_before_fetch(self):
        with self.assertRaisesRegex(ValueError, 'absolute HTTPS URL'):
            smoke_test_site.smoke_test(
                'http://example.test/',
                REVISION,
                1,
                1,
                CHECKSUM,
                HTML_DIGEST,
                BRIEF_DIGEST,
                OBSERVATION_DIGEST,
                retries=1,
            )


if __name__ == '__main__':
    unittest.main()
