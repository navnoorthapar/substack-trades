import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import smoke_test_site


REVISION = 'a' * 40
CHECKSUM = 'b' * 64


def fixture_html(
    revision=REVISION,
    articles='363',
    observations='1327',
    checksum=CHECKSUM,
    omitted_id=None,
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
</head><body>{elements}</body></html>'''


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
        smoke_test_site.validate_html(fixture_html(), REVISION, 363, 1327, CHECKSUM)

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

    def test_snapshot_counts_require_non_empty_json_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.json'
            path.write_text(json.dumps([{'id': 1}, {'id': 2}]), encoding='utf-8')
            self.assertEqual(smoke_test_site.load_list_count(path, 'records'), 2)
            path.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'non-empty JSON list'):
                smoke_test_site.load_list_count(path, 'records')

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

    @patch('smoke_test_site.time.sleep')
    @patch('smoke_test_site.fetch_html')
    def test_stale_release_is_retried_until_expected_revision_appears(
        self, fetch_html, sleep,
    ):
        fetch_html.side_effect = [
            fixture_html(revision='c' * 40),
            fixture_html(),
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            smoke_test_site.smoke_test(
                'https://example.test/research/',
                REVISION,
                363,
                1327,
                CHECKSUM,
                retries=2,
                retry_delay=0.01,
            )
        self.assertEqual(fetch_html.call_count, 2)
        sleep.assert_called_once_with(0.01)

    @patch('smoke_test_site.fetch_html', return_value=fixture_html(revision='c' * 40))
    def test_retry_exhaustion_fails_the_deployment(self, fetch_html):
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'did not become healthy'):
                smoke_test_site.smoke_test(
                    'https://example.test/',
                    REVISION,
                    363,
                    1327,
                    CHECKSUM,
                    retries=1,
                    retry_delay=0,
                )

    def test_non_https_url_is_rejected_before_fetch(self):
        with self.assertRaisesRegex(ValueError, 'absolute HTTPS URL'):
            smoke_test_site.smoke_test(
                'http://example.test/', REVISION, 1, 1, CHECKSUM, retries=1,
            )


if __name__ == '__main__':
    unittest.main()
