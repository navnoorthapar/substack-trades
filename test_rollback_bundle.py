"""Regression tests for immutable emergency Pages rollback bundles."""

import io
import json
import tarfile
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import rollback_bundle


CURRENT_FINGERPRINT_KEYS = (
    'html_sha256',
    'brief_sha256',
    'observation_sha256',
    'support_sha256',
    'data_sha256',
    'share_sha256',
)


class FakeResponse:
    def __init__(self, url, data, *, final_url=None):
        self.status = 200
        self.headers = {'Content-Length': str(len(data))}
        self.url = final_url or url
        self.data = data
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def geturl(self):
        return self.url

    def read(self, amount):
        self.read_sizes.append(amount)
        return self.data[:amount]


class RollbackBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.site = self.root / 'site'
        self.site.mkdir()
        (self.root / 'assets').mkdir()
        (self.root / 'assets' / 'og.jpg').write_bytes(b'tracked-social-image')
        (self.site / 'index.html').write_text(
            '<!doctype html><title>Verified</title>',
            encoding='utf-8',
        )
        self.articles = self.root / 'articles.json'
        self.observations = self.root / 'observations.json'
        self.manifest = self.root / 'manifest.json'
        for path, value in (
            (self.articles, [{'source_id': 'article'}]),
            (self.observations, [{'id': 'observation'}]),
            (self.manifest, {'checked_at': '2026-07-01T00:00:00Z'}),
        ):
            path.write_text(json.dumps(value), encoding='utf-8')
        self.revision = 'a' * 40
        self.fingerprints = {
            key: str(index + 1) * 64
            for index, key in enumerate(CURRENT_FINGERPRINT_KEYS)
        }

    def create(self):
        output = self.root / 'verified-bundle'
        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
            return_value=self.fingerprints,
        ) as validator:
            rollback_bundle.create_bundle(
                self.site,
                self.articles,
                self.observations,
                self.manifest,
                self.revision,
                output,
            )
        validator.assert_called_once()
        return output

    def test_create_and_validate_bind_payload_revision_and_fingerprints(self):
        bundle = self.create()
        output = self.root / 'github-output'

        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
            return_value=self.fingerprints,
        ) as validator:
            actual = rollback_bundle.validate_bundle(
                bundle,
                self.revision,
                github_output=output,
            )

        self.assertEqual(actual, self.fingerprints)
        self.assertEqual(
            set(path.name for path in bundle.iterdir()),
            {'site', 'source', rollback_bundle.ATTESTATION_NAME},
        )
        self.assertEqual(
            (bundle / 'source' / 'assets' / 'og.jpg').read_bytes(),
            b'tracked-social-image',
        )
        attestation = json.loads(
            (bundle / rollback_bundle.ATTESTATION_NAME).read_text(
                encoding='utf-8',
            )
        )
        self.assertEqual(attestation['revision'], self.revision)
        self.assertEqual(
            attestation['schema_version'],
            rollback_bundle.BUNDLE_SCHEMA_VERSION,
        )
        self.assertEqual(
            attestation['payload_sha256'],
            rollback_bundle.payload_checksum(bundle),
        )
        self.assertEqual(attestation['fingerprints'], self.fingerprints)
        self.assertEqual(
            attestation['site_files'],
            rollback_bundle.site_file_checksums(bundle / 'site'),
        )
        self.assertIn(
            'html_sha256=' + self.fingerprints['html_sha256'],
            output.read_text(encoding='utf-8'),
        )
        self.assertEqual(validator.call_args.kwargs, {})

        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
        ) as current_validator:
            attested, site_files = rollback_bundle.verify_attestation(
                bundle,
                self.revision,
            )
        current_validator.assert_not_called()
        self.assertEqual(attested, self.fingerprints)
        self.assertEqual(
            site_files,
            rollback_bundle.site_file_checksums(bundle / 'site'),
        )

    def test_payload_or_fingerprint_mutation_fails_closed(self):
        bundle = self.create()
        (bundle / 'site' / 'index.html').write_text(
            '<!doctype html><title>Mutated</title>',
            encoding='utf-8',
        )
        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
            return_value=self.fingerprints,
        ) as validator:
            with self.assertRaisesRegex(ValueError, 'payload checksum'):
                rollback_bundle.validate_bundle(
                    bundle,
                    self.revision,
                )
        validator.assert_not_called()

        bundle = self.root / 'verified-bundle-two'
        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
            return_value=self.fingerprints,
        ):
            rollback_bundle.create_bundle(
                self.site,
                self.articles,
                self.observations,
                self.manifest,
                self.revision,
                bundle,
            )
        changed = dict(self.fingerprints)
        changed['html_sha256'] = 'f' * 64
        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
            return_value=changed,
        ):
            with self.assertRaisesRegex(ValueError, 'fingerprints'):
                rollback_bundle.validate_bundle(
                    bundle,
                    self.revision,
                )

    def test_attestation_rejects_noncanonical_site_paths(self):
        bundle = self.create()
        with self.assertRaisesRegex(ValueError, 'revision'):
            rollback_bundle.verify_attestation(bundle, 'b' * 40)

        attestation_path = bundle / rollback_bundle.ATTESTATION_NAME
        attestation = json.loads(attestation_path.read_text(encoding='utf-8'))
        attestation['site_files'] = {
            '../index.html': attestation['site_files']['index.html'],
        }
        attestation_path.write_text(
            json.dumps(attestation),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'site-file manifest'):
            rollback_bundle.verify_attestation(bundle, self.revision)

        attestation['site_files'] = rollback_bundle.site_file_checksums(
            bundle / 'site',
        )
        attestation['schema_version'] = 1
        attestation_path.write_text(
            json.dumps(attestation),
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'schema version'):
            rollback_bundle.verify_attestation(bundle, self.revision)

    def test_schema_neutral_verification_survives_fingerprint_evolution(self):
        bundle = self.create()
        attestation_path = bundle / rollback_bundle.ATTESTATION_NAME
        attestation = json.loads(attestation_path.read_text(encoding='utf-8'))
        evolved_fingerprints = {
            'future_release_digest': 'e' * 64,
        }
        attestation['fingerprints'] = evolved_fingerprints
        attestation_path.write_text(
            json.dumps(attestation),
            encoding='utf-8',
        )

        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
        ) as current_validator:
            verified, _ = rollback_bundle.verify_attestation(
                bundle,
                self.revision,
            )
        current_validator.assert_not_called()
        self.assertEqual(verified, evolved_fingerprints)

        def exact_response(request, _origin, _timeout):
            expected = (bundle / 'site' / 'index.html').read_bytes()
            return FakeResponse(request.full_url, expected)

        with mock.patch.object(
            rollback_bundle,
            '_open_same_origin',
            side_effect=exact_response,
        ):
            rollback_bundle.smoke_bundle(
                bundle,
                self.revision,
                'https://example.test/terminal/',
                retries=1,
                retry_delay=0,
                concurrency=1,
            )

        with mock.patch.object(
            rollback_bundle,
            '_validate_current_release',
            return_value=self.fingerprints,
        ):
            with self.assertRaisesRegex(ValueError, 'fingerprints'):
                rollback_bundle.validate_bundle(bundle, self.revision)

    def test_schema_neutral_smoke_checks_every_exact_file_with_revision_query(self):
        nested = self.site / 'cards'
        nested.mkdir()
        (nested / 'research note.html').write_bytes(b'card-bytes')
        bundle = self.create()
        requested = []
        responses = []

        def open_response(request, origin, timeout):
            self.assertEqual(origin, ('example.test', 443))
            self.assertEqual(timeout, 7)
            parts = urllib.parse.urlsplit(request.full_url)
            self.assertEqual(
                urllib.parse.parse_qs(parts.query),
                {'rollback-revision': [self.revision]},
            )
            prefix = '/terminal/'
            self.assertTrue(parts.path.startswith(prefix))
            relative = urllib.parse.unquote(parts.path[len(prefix):])
            data = (bundle / 'site' / relative).read_bytes()
            response = FakeResponse(request.full_url, data)
            requested.append(relative)
            responses.append(response)
            return response

        with mock.patch.object(
            rollback_bundle,
            '_open_same_origin',
            side_effect=open_response,
        ):
            rollback_bundle.smoke_bundle(
                bundle,
                self.revision,
                'https://example.test/terminal',
                retries=1,
                retry_delay=0,
                timeout=7,
                concurrency=2,
            )

        self.assertEqual(
            set(requested),
            {'index.html', 'cards/research note.html'},
        )
        for relative, response in zip(requested, responses):
            expected_size = (bundle / 'site' / relative).stat().st_size
            self.assertEqual(response.read_sizes, [expected_size + 1])

    def test_schema_neutral_smoke_rejects_wrong_bytes_and_redirect_path(self):
        bundle = self.create()

        def wrong_bytes(request, _origin, _timeout):
            return FakeResponse(request.full_url, b'x' * len(
                (bundle / 'site' / 'index.html').read_bytes()
            ))

        with mock.patch.object(
            rollback_bundle,
            '_open_same_origin',
            side_effect=wrong_bytes,
        ):
            with self.assertRaisesRegex(ValueError, 'response bytes'):
                rollback_bundle.smoke_bundle(
                    bundle,
                    self.revision,
                    'https://example.test/terminal/',
                    retries=1,
                    retry_delay=0,
                    concurrency=1,
                )

        def wrong_path(request, _origin, _timeout):
            expected = (bundle / 'site' / 'index.html').read_bytes()
            final_url = request.full_url.replace(
                '/index.html?',
                '/different.html?',
            )
            return FakeResponse(
                request.full_url,
                expected,
                final_url=final_url,
            )

        with mock.patch.object(
            rollback_bundle,
            '_open_same_origin',
            side_effect=wrong_path,
        ):
            with self.assertRaisesRegex(ValueError, 'changed origin, path'):
                rollback_bundle.smoke_bundle(
                    bundle,
                    self.revision,
                    'https://example.test/terminal/',
                    retries=1,
                    retry_delay=0,
                    concurrency=1,
                )

    def test_schema_neutral_smoke_retries_a_transient_fetch(self):
        bundle = self.create()
        attempts = []

        def transient(request, _origin, _timeout):
            attempts.append(request.full_url)
            if len(attempts) == 1:
                raise OSError('temporary network failure')
            expected = (bundle / 'site' / 'index.html').read_bytes()
            return FakeResponse(request.full_url, expected)

        with mock.patch.object(
            rollback_bundle,
            '_open_same_origin',
            side_effect=transient,
        ):
            rollback_bundle.smoke_bundle(
                bundle,
                self.revision,
                'https://example.test/terminal/',
                retries=2,
                retry_delay=0,
                concurrency=1,
            )
        self.assertEqual(len(attempts), 2)

    def test_smoke_requires_https_and_redirects_cannot_leave_origin(self):
        bundle = self.create()
        with self.assertRaisesRegex(ValueError, 'HTTPS URL'):
            rollback_bundle.smoke_bundle(
                bundle,
                self.revision,
                'http://example.test/terminal/',
            )
        handler = rollback_bundle._SameOriginRedirectHandler(
            ('example.test', 443),
        )
        with self.assertRaisesRegex(ValueError, 'off-origin redirect'):
            handler.redirect_request(
                mock.Mock(full_url='https://example.test/terminal/index.html'),
                mock.Mock(),
                302,
                'Found',
                {},
                'https://attacker.test/index.html',
            )

    def write_tar(self, path, members):
        with tarfile.open(path, mode='w') as archive:
            for name, kind, data in members:
                info = tarfile.TarInfo(name)
                if kind == 'directory':
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                elif kind == 'symlink':
                    info.type = tarfile.SYMTYPE
                    info.linkname = data.decode('utf-8')
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))

    def test_archive_extraction_accepts_regular_files_and_rejects_escape(self):
        safe_archive = self.root / 'safe.tar'
        self.write_tar(
            safe_archive,
            [
                ('./', 'directory', b''),
                ('./site/', 'directory', b''),
                ('./site/index.html', 'file', b'verified'),
            ],
        )
        safe_output = self.root / 'safe-output'
        rollback_bundle.extract_pages_archive(safe_archive, safe_output)
        self.assertEqual(
            (safe_output / 'site' / 'index.html').read_bytes(),
            b'verified',
        )

        for name, kind, data in (
            ('../escape', 'file', b'escape'),
            ('site/link', 'symlink', b'/tmp/target'),
        ):
            archive = self.root / f'{kind}.tar'
            self.write_tar(archive, [(name, kind, data)])
            output = self.root / f'{kind}-output'
            with self.assertRaisesRegex(
                ValueError,
                'unsafe path' if kind == 'file' else 'link or special',
            ):
                rollback_bundle.extract_pages_archive(archive, output)
            self.assertFalse(output.exists())

        duplicate_archive = self.root / 'duplicate.tar'
        self.write_tar(
            duplicate_archive,
            [
                ('./site/index.html', 'file', b'one'),
                ('site/index.html', 'file', b'two'),
            ],
        )
        with self.assertRaisesRegex(ValueError, 'duplicate path'):
            rollback_bundle.extract_pages_archive(
                duplicate_archive,
                self.root / 'duplicate-output',
            )


if __name__ == '__main__':
    unittest.main()
