import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fetch_all_posts
import fetch_medium_posts
from validate_pipeline import validate_manifest, validate_previous_manifest
from write_snapshot_manifest import build_manifest, data_checksum


def sample_articles():
    return [
        {
            'source': 'substack',
            'source_id': 'alpha',
            'title': 'Alpha',
            'post_date': '2026-07-14T01:00:00Z',
            'url': 'https://navnoorbawa.substack.com/p/alpha',
            'content_status': 'full',
        },
        {
            'source': 'medium',
            'source_id': 'abcdef123456',
            'title': 'Beta',
            'post_date': '2026-07-13T01:00:00Z',
            'url': 'https://medium.com/@navnoorbawa/beta-abcdef123456',
            'content_status': 'excerpt',
        },
    ]


def sample_observations():
    return [
        {
            'article_title': 'Alpha',
            'article_url': 'https://navnoorbawa.substack.com/p/alpha',
            'article_date': '2026-07-14',
            'trade_description': 'A sufficiently detailed extracted investment observation.',
            'instruments': ['equity'],
            'direction': 'long',
        }
    ]


def sample_statuses():
    return {
        'substack': {
            'source': 'substack',
            'checked_at': '2026-07-14T02:00:00Z',
            'status': 'ok',
            'mode': 'complete_api',
            'published_count': 1,
            'fetched_count': 1,
            'newest': '2026-07-14T01:00:00Z',
        },
        'medium': {
            'source': 'medium',
            'checked_at': '2026-07-14T02:00:01Z',
            'status': 'degraded',
            'mode': 'cached_archive_plus_rss',
            'published_count': 3,
            'fetched_count': 2,
            'newest': '2026-07-13T01:00:00Z',
        },
    }


class SnapshotManifestTests(unittest.TestCase):
    def _fixture(self, directory):
        article_path = directory / 'articles.json'
        trade_path = directory / 'trades.json'
        article_path.write_text(
            json.dumps(sample_articles(), ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        trade_path.write_text(
            json.dumps(sample_observations(), ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        checksum = data_checksum(article_path.read_bytes(), trade_path.read_bytes())
        manifest = build_manifest(
            sample_articles(), sample_observations(), sample_statuses(), checksum,
            checked_at='2026-07-14T02:00:02Z',
        )
        return article_path, trade_path, manifest

    def test_manifest_counts_sources_latest_publication_and_raw_checksum(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            article_path, trade_path, manifest = self._fixture(directory)
            checked_at = validate_manifest(
                manifest, sample_articles(), sample_observations(),
                article_path, trade_path,
            )
            self.assertEqual(checked_at.isoformat(), '2026-07-14T02:00:02+00:00')
            self.assertEqual(manifest['schema_version'], 1)
            self.assertEqual(manifest['latest_publication'], '2026-07-14T01:00:00Z')
            self.assertEqual(manifest['article_count'], 2)
            self.assertEqual(manifest['observation_count'], 1)
            self.assertEqual(manifest['sources']['medium']['included_count'], 1)
            self.assertRegex(manifest['data_checksum'], r'^[0-9a-f]{64}$')

    def test_checksum_detects_even_format_only_file_changes(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            article_path, trade_path, manifest = self._fixture(directory)
            article_path.write_text(
                json.dumps(sample_articles(), separators=(',', ':')),
                encoding='utf-8',
            )
            with self.assertRaisesRegex(ValueError, 'checksum does not match'):
                validate_manifest(
                    manifest, sample_articles(), sample_observations(),
                    article_path, trade_path,
                )

    def test_previous_manifest_rejects_time_rollback_and_inconsistent_counts(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _, _, current = self._fixture(directory)
            previous = json.loads(json.dumps(current))
            previous['checked_at'] = '2026-07-14T03:00:00Z'
            with self.assertRaisesRegex(ValueError, 'moved backwards'):
                validate_previous_manifest(current, previous)

            previous['checked_at'] = '2026-07-14T01:59:59Z'
            previous['article_count'] = 999
            with self.assertRaisesRegex(ValueError, 'inconsistent article_count'):
                validate_previous_manifest(current, previous)

    def test_medium_dual_outage_fails_without_overwriting_catalogue(self):
        previous = [{
            'source': 'medium',
            'source_id': 'abcdef123456',
            'medium_id': 'abcdef123456',
            'title': 'Previous',
            'post_date': '2026-07-13T01:00:00Z',
            'url': 'https://medium.com/@navnoorbawa/previous-abcdef123456',
            'content_status': 'excerpt',
        }]
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status = directory / 'status.json'
            original = b'{"sentinel": true}\n'
            output.write_bytes(original)
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(fetch_medium_posts, 'FETCH_STATUS_PATH', status),
                mock.patch.object(fetch_medium_posts, 'load_previous', return_value=previous),
                mock.patch.object(fetch_medium_posts, 'fetch_archive',
                                  side_effect=RuntimeError('archive down')),
                mock.patch.object(fetch_medium_posts, 'fetch_rss_posts',
                                  side_effect=RuntimeError('RSS down')),
            ):
                self.assertEqual(fetch_medium_posts.main(), 1)
            self.assertEqual(output.read_bytes(), original)
            provenance = json.loads(status.read_text(encoding='utf-8'))
            self.assertEqual(provenance['status'], 'failed')
            self.assertEqual(provenance['mode'], 'archive_and_rss_failed')

    def test_medium_archive_outage_with_live_rss_is_explicitly_degraded(self):
        previous = [{
            'source': 'medium',
            'source_id': 'abcdef123456',
            'medium_id': 'abcdef123456',
            'title': 'Previous',
            'post_date': '2026-07-13T01:00:00Z',
            'url': 'https://medium.com/@navnoorbawa/previous-abcdef123456',
            'visibility': 'PUBLIC',
            'content_status': 'full',
        }]
        newest = dict(
            previous[0],
            source_id='123456abcdef',
            medium_id='123456abcdef',
            title='Newest',
            post_date='2026-07-14T01:00:00Z',
            url='https://medium.com/@navnoorbawa/newest-123456abcdef',
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status_path = directory / 'status.json'
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(fetch_medium_posts, 'FETCH_STATUS_PATH', status_path),
                mock.patch.object(fetch_medium_posts, 'load_previous', return_value=previous),
                mock.patch.object(fetch_medium_posts, 'fetch_archive',
                                  side_effect=RuntimeError('archive down')),
                mock.patch.object(fetch_medium_posts, 'fetch_rss_posts',
                                  return_value=[newest]),
            ):
                self.assertEqual(fetch_medium_posts.main(), 0)
            catalogue = json.loads(output.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertEqual(len(catalogue), 2)
            self.assertEqual(status['status'], 'degraded')
            self.assertEqual(status['mode'], 'cached_archive_plus_rss')
            self.assertEqual(status['fetched_count'], 1)
            self.assertEqual(status['published_count'], 2)

    def test_fetch_status_outputs_have_required_provenance_fields(self):
        post = {'post_date': '2026-07-14T01:00:00Z'}
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            substack_status = directory / 'substack.json'
            medium_status = directory / 'medium.json'
            with mock.patch.object(fetch_all_posts, 'FETCH_STATUS_PATH', substack_status):
                fetch_all_posts.write_fetch_status('ok', 'complete_api', 1, [post])
            with mock.patch.object(fetch_medium_posts, 'FETCH_STATUS_PATH', medium_status):
                fetch_medium_posts.write_fetch_status(
                    'degraded', 'cached_archive_plus_rss', 10, [post]
                )
            for path, source in ((substack_status, 'substack'),
                                 (medium_status, 'medium')):
                status = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(status['source'], source)
                for field in ('checked_at', 'status', 'mode', 'fetched_count', 'newest'):
                    self.assertIn(field, status)


if __name__ == '__main__':
    unittest.main()
