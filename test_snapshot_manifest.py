import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import fetch_all_posts
import fetch_medium_posts
from validate_pipeline import validate_manifest, validate_previous_manifest
from write_snapshot_manifest import build_manifest, data_checksum


FIXTURE_NOW = datetime(2026, 7, 14, 2, 5, 0, tzinfo=timezone.utc)


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


def sample_medium_bridge_provenance():
    return {
        'surface': 'operator-reviewed-direct-public-profile-sequence',
        'profile_url': 'https://medium.com/@navnoorbawa',
        'reviewed_at': '2026-07-14T01:59:00Z',
        'expires_at': '2026-07-17T01:59:00Z',
        'rss_window_ids': [f'{index:012x}' for index in range(10)],
        'previous_history_prefix_ids': [
            'aaaaaaaaaaaa', 'bbbbbbbbbbbb',
        ],
    }


def sample_medium_row(post_id, published):
    return {
        'source': 'medium',
        'source_id': post_id,
        'medium_id': post_id,
        'title': f'Medium row {post_id}',
        'post_date': published,
        'url': f'https://medium.com/@navnoorbawa/research-{post_id}',
        'visibility': 'PUBLIC',
        'content_status': 'excerpt',
        'body_revision_status': 'unverified',
        'source_updated_at': published,
        'observed_source_updated_at': '',
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
                now=FIXTURE_NOW,
            )
            self.assertEqual(checked_at.isoformat(), '2026-07-14T02:00:02+00:00')
            self.assertEqual(manifest['schema_version'], 2)
            self.assertEqual(manifest['latest_publication'], '2026-07-14T01:00:00Z')
            self.assertEqual(
                manifest['catalog_latest_publication'], '2026-07-14T01:00:00Z',
            )
            self.assertEqual(manifest['article_count'], 2)
            self.assertEqual(manifest['catalog_count'], 2)
            self.assertEqual(manifest['registry_count'], 0)
            self.assertEqual(manifest['observation_count'], 1)
            self.assertEqual(manifest['sources']['medium']['included_count'], 1)
            self.assertEqual(
                manifest['sources']['medium']['degraded_since'],
                '2026-07-14T02:00:01Z',
            )
            self.assertEqual(
                manifest['sources']['medium']['consecutive_degraded_checks'],
                1,
            )
            self.assertIsNone(manifest['sources']['substack']['degraded_since'])
            self.assertEqual(
                manifest['sources']['substack']['consecutive_degraded_checks'],
                0,
            )
            self.assertRegex(manifest['data_checksum'], r'^[0-9a-f]{64}$')

    def test_reviewed_medium_provenance_round_trips_and_validates_exactly(self):
        statuses = sample_statuses()
        statuses['medium'] = dict(
            statuses['medium'],
            status='ok',
            mode='operator_reviewed_profile_bridge_plus_current_rss',
            provenance=sample_medium_bridge_provenance(),
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            article_path = directory / 'articles.json'
            trade_path = directory / 'trades.json'
            article_path.write_text(
                json.dumps(sample_articles(), ensure_ascii=False, indent=2)
                + '\n',
                encoding='utf-8',
            )
            trade_path.write_text(
                json.dumps(sample_observations(), ensure_ascii=False, indent=2)
                + '\n',
                encoding='utf-8',
            )
            manifest = build_manifest(
                sample_articles(),
                sample_observations(),
                statuses,
                data_checksum(
                    article_path.read_bytes(), trade_path.read_bytes(),
                ),
                checked_at='2026-07-14T02:00:02Z',
            )
            self.assertEqual(
                manifest['sources']['medium']['provenance'],
                sample_medium_bridge_provenance(),
            )
            validate_manifest(
                manifest,
                sample_articles(),
                sample_observations(),
                article_path,
                trade_path,
                now=FIXTURE_NOW,
            )

            invalid_manifests = []

            missing = json.loads(json.dumps(manifest))
            missing['sources']['medium']['provenance'].pop('reviewed_at')
            invalid_manifests.append(('missing key', missing, 'exact schema'))

            extra = json.loads(json.dumps(manifest))
            extra['sources']['medium']['provenance']['extra'] = True
            invalid_manifests.append(('extra key', extra, 'exact schema'))

            surface = json.loads(json.dumps(manifest))
            surface['sources']['medium']['provenance']['surface'] = 'rss'
            invalid_manifests.append(('surface', surface, 'wrong source surface'))

            profile = json.loads(json.dumps(manifest))
            profile['sources']['medium']['provenance']['profile_url'] = (
                'https://medium.com/@another-author'
            )
            invalid_manifests.append(('profile', profile, 'wrong source surface'))

            noncanonical = json.loads(json.dumps(manifest))
            noncanonical['sources']['medium']['provenance']['reviewed_at'] = (
                '2026-07-14T01:59:00+00:00'
            )
            invalid_manifests.append(
                ('noncanonical time', noncanonical, 'canonical UTC-second')
            )

            review_after_check = json.loads(json.dumps(manifest))
            review_after_check['sources']['medium']['provenance'][
                'reviewed_at'
            ] = '2026-07-14T02:00:02Z'
            invalid_manifests.append(
                ('future review', review_after_check, 'outside the review window')
            )

            check_after_expiry = json.loads(json.dumps(manifest))
            check_after_expiry['sources']['medium']['provenance'][
                'expires_at'
            ] = '2026-07-14T02:00:00Z'
            invalid_manifests.append(
                ('expired check', check_after_expiry, 'outside the review window')
            )

            long_lifetime = json.loads(json.dumps(manifest))
            long_lifetime['sources']['medium']['provenance']['expires_at'] = (
                '2026-07-17T01:59:01Z'
            )
            invalid_manifests.append(
                ('long lifetime', long_lifetime, 'invalid review lifetime')
            )

            short_rss = json.loads(json.dumps(manifest))
            short_rss['sources']['medium']['provenance'][
                'rss_window_ids'
            ].pop()
            invalid_manifests.append(('short RSS', short_rss, 'RSS window IDs'))

            uppercase = json.loads(json.dumps(manifest))
            uppercase['sources']['medium']['provenance'][
                'rss_window_ids'
            ][0] = 'ABCDEFABCDEF'
            invalid_manifests.append(('uppercase ID', uppercase, 'RSS window IDs'))

            duplicate = json.loads(json.dumps(manifest))
            duplicate['sources']['medium']['provenance'][
                'rss_window_ids'
            ][1] = duplicate['sources']['medium']['provenance'][
                'rss_window_ids'
            ][0]
            invalid_manifests.append(('duplicate ID', duplicate, 'RSS window IDs'))

            overlap = json.loads(json.dumps(manifest))
            overlap['sources']['medium']['provenance'][
                'previous_history_prefix_ids'
            ][0] = overlap['sources']['medium']['provenance'][
                'rss_window_ids'
            ][0]
            invalid_manifests.append(('overlap', overlap, 'IDs overlap'))

            for label, candidate, error in invalid_manifests:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(ValueError, error):
                        validate_manifest(
                            candidate,
                            sample_articles(),
                            sample_observations(),
                            article_path,
                            trade_path,
                            now=FIXTURE_NOW,
                        )

    def test_manifest_writer_mode_gates_medium_provenance(self):
        unrelated = sample_statuses()
        unrelated['medium']['provenance'] = sample_medium_bridge_provenance()
        with self.assertRaisesRegex(ValueError, 'not valid for mode'):
            build_manifest(
                sample_articles(), sample_observations(), unrelated, '0' * 64,
                checked_at='2026-07-14T02:00:02Z',
            )

        missing = sample_statuses()
        missing['medium'].update({
            'status': 'ok',
            'mode': 'operator_reviewed_profile_bridge_plus_current_rss',
        })
        with self.assertRaisesRegex(ValueError, 'has no provenance'):
            build_manifest(
                sample_articles(), sample_observations(), missing, '0' * 64,
                checked_at='2026-07-14T02:00:02Z',
            )

    def test_quarantine_path_never_dirties_the_trusted_repository_output(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            trusted = directory / 'medium_posts.json'
            candidate = directory / 'medium.candidate.json'
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', trusted),
                mock.patch.object(fetch_medium_posts, 'PREVIOUS_PATH', trusted),
            ):
                self.assertIsNone(fetch_medium_posts.quarantine_output_path())
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', candidate),
                mock.patch.object(fetch_medium_posts, 'PREVIOUS_PATH', trusted),
            ):
                self.assertEqual(
                    fetch_medium_posts.quarantine_output_path(),
                    directory / 'medium.candidate.rss-quarantine.json',
                )

    def test_direct_no_overlap_preserves_exact_bytes_without_quarantine(self):
        previous = [{
            'source': 'medium',
            'source_id': 'abcdef123456',
            'medium_id': 'abcdef123456',
            'title': 'Trusted history',
            'post_date': '2026-07-13T01:00:00Z',
            'url': 'https://medium.com/@navnoorbawa/trusted-abcdef123456',
            'visibility': 'PUBLIC',
            'content_status': 'excerpt',
        }]
        untrusted = dict(
            previous[0],
            source_id='123456abcdef',
            medium_id='123456abcdef',
            title='Untrusted gap row',
            post_date='2026-07-14T01:00:00Z',
            url='https://medium.com/@navnoorbawa/gap-123456abcdef',
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            trusted = directory / 'medium_posts.json'
            status_path = directory / 'status.json'
            missing_bridge = directory / 'no-reviewed-bridge.json'
            original = (
                json.dumps(previous, separators=(',', ':')) + '\n'
            ).encode('utf-8')
            trusted.write_bytes(original)
            errors = io.StringIO()
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', trusted),
                mock.patch.object(fetch_medium_posts, 'PREVIOUS_PATH', trusted),
                mock.patch.object(
                    fetch_medium_posts, 'FETCH_STATUS_PATH', status_path,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'PROFILE_BRIDGE_PATH', missing_bridge,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'load_previous', return_value=previous,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_archive',
                    side_effect=RuntimeError('archive down'),
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_rss_posts',
                    return_value=[untrusted],
                ),
                mock.patch.object(fetch_medium_posts.sys, 'stderr', errors),
            ):
                self.assertEqual(fetch_medium_posts.main(), 0)
            self.assertEqual(trusted.read_bytes(), original)
            self.assertFalse(
                (directory / 'medium_posts.rss-quarantine.json').exists()
            )
            self.assertIn(
                'did not write the unproven RSS merge', errors.getvalue(),
            )

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
                    now=FIXTURE_NOW,
                )

    def test_manifest_rejects_a_future_dated_refresh_clock(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            article_path, trade_path, manifest = self._fixture(directory)
            manifest['checked_at'] = '2026-07-14T02:11:00Z'
            with self.assertRaisesRegex(ValueError, 'far in the future'):
                validate_manifest(
                    manifest,
                    sample_articles(),
                    sample_observations(),
                    article_path,
                    trade_path,
                    now=datetime(2026, 7, 14, 2, 0, 0, tzinfo=timezone.utc),
                )

    def test_manifest_rejects_lagging_and_stale_source_checks(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            article_path, trade_path, manifest = self._fixture(directory)

            lagging = json.loads(json.dumps(manifest))
            lagging['sources']['substack']['checked_at'] = (
                '2026-07-14T00:59:00Z'
            )
            with self.assertRaisesRegex(ValueError, 'too far behind'):
                validate_manifest(
                    lagging,
                    sample_articles(),
                    sample_observations(),
                    article_path,
                    trade_path,
                    now=FIXTURE_NOW,
                )

            with self.assertRaisesRegex(ValueError, 'source check is stale'):
                validate_manifest(
                    manifest,
                    sample_articles(),
                    sample_observations(),
                    article_path,
                    trade_path,
                    now=datetime(
                        2026, 7, 14, 18, 1, 0, tzinfo=timezone.utc
                    ),
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

    def test_degraded_streak_continuity_is_bound_to_the_previous_manifest(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            _, _, previous = self._fixture(directory)
            # The currently deployed manifest predates streak tracking. Its
            # source check is the earliest degradation instant we can prove.
            previous['sources']['medium'].pop('degraded_since')
            previous['sources']['medium'].pop('consecutive_degraded_checks')
            statuses = sample_statuses()
            statuses['substack']['checked_at'] = '2026-07-14T03:00:00Z'
            statuses['medium']['checked_at'] = '2026-07-14T03:00:01Z'
            current = build_manifest(
                sample_articles(),
                sample_observations(),
                statuses,
                previous['data_checksum'],
                checked_at='2026-07-14T03:00:02Z',
                previous_manifest=previous,
            )
            self.assertEqual(
                current['sources']['medium']['degraded_since'],
                '2026-07-14T02:00:01Z',
            )
            self.assertEqual(
                current['sources']['medium']['consecutive_degraded_checks'],
                2,
            )
            validate_previous_manifest(current, previous)

            current['sources']['medium']['consecutive_degraded_checks'] = 1
            with self.assertRaisesRegex(ValueError, 'does not continue'):
                validate_previous_manifest(current, previous)

            validate_previous_manifest(previous, previous)
            stripped = json.loads(json.dumps(current))
            stripped['sources']['medium'].pop('degraded_since')
            stripped['sources']['medium'].pop('consecutive_degraded_checks')
            with self.assertRaisesRegex(
                ValueError, 'legacy source-health row changed without tracking fields'
            ):
                validate_previous_manifest(stripped, previous)

            tracked_previous = build_manifest(
                sample_articles(),
                sample_observations(),
                statuses,
                previous['data_checksum'],
                checked_at='2026-07-14T03:00:02Z',
                previous_manifest=previous,
            )
            stripped_tracked = json.loads(json.dumps(tracked_previous))
            stripped_tracked['sources']['medium'].pop('degraded_since')
            stripped_tracked['sources']['medium'].pop(
                'consecutive_degraded_checks'
            )
            with self.assertRaisesRegex(ValueError, 'tracking fields were removed'):
                validate_previous_manifest(stripped_tracked, tracked_previous)

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

    def test_medium_archive_outage_without_rss_overlap_is_degraded(self):
        previous = [{
            'source': 'medium',
            'source_id': 'abcdef123456',
            'medium_id': 'abcdef123456',
            'title': 'Previous',
            'post_date': '2026-07-13T01:00:00Z',
            'url': 'https://medium.com/@navnoorbawa/previous-abcdef123456',
            'visibility': 'PUBLIC',
            'content_status': 'full',
            'body_revision_status': 'current',
            'source_updated_at': '2026-07-13T01:00:00Z',
            'observed_source_updated_at': '2026-07-13T01:00:00Z',
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
            missing_bridge = directory / 'no-reviewed-bridge.json'
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(fetch_medium_posts, 'FETCH_STATUS_PATH', status_path),
                mock.patch.object(
                    fetch_medium_posts, 'PROFILE_BRIDGE_PATH', missing_bridge,
                ),
                mock.patch.object(fetch_medium_posts, 'load_previous', return_value=previous),
                mock.patch.object(fetch_medium_posts, 'fetch_archive',
                                  side_effect=RuntimeError('archive down')),
                mock.patch.object(fetch_medium_posts, 'fetch_rss_posts',
                                  return_value=[newest]),
            ):
                self.assertEqual(fetch_medium_posts.main(), 0)
            catalogue = json.loads(output.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertEqual(catalogue, previous)
            self.assertEqual(status['status'], 'degraded')
            self.assertEqual(
                status['mode'],
                'trusted_history_rss_gap_quarantined',
            )
            self.assertEqual(status['fetched_count'], 1)
            self.assertEqual(status['published_count'], 1)
            self.assertNotIn('provenance', status)
            quarantine = directory / 'medium.rss-quarantine.json'
            quarantined = json.loads(quarantine.read_text(encoding='utf-8'))
            self.assertEqual(quarantined['status'], 'quarantined')
            self.assertEqual(quarantined['trusted_history_count'], 1)
            self.assertEqual(quarantined['untrusted_merged_count'], 2)
            self.assertEqual(
                quarantined['rss_window_ids'], ['123456abcdef'],
            )

    def test_medium_no_overlap_cannot_launder_on_the_next_refresh(self):
        previous = [{
            'source': 'medium',
            'source_id': 'abcdef123456',
            'medium_id': 'abcdef123456',
            'title': 'Trusted history',
            'post_date': '2026-07-13T01:00:00Z',
            'url': 'https://medium.com/@navnoorbawa/trusted-abcdef123456',
            'visibility': 'PUBLIC',
            'content_status': 'excerpt',
            'body_revision_status': 'unverified',
            'source_updated_at': '2026-07-13T01:00:00Z',
            'observed_source_updated_at': '',
        }]
        untrusted = dict(
            previous[0],
            source_id='123456abcdef',
            medium_id='123456abcdef',
            title='Untrusted gap row',
            post_date='2026-07-14T01:00:00Z',
            url='https://medium.com/@navnoorbawa/gap-123456abcdef',
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status_path = directory / 'status.json'
            missing_bridge = directory / 'no-reviewed-bridge.json'
            current_previous = previous
            for _ in range(2):
                with (
                    mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                    mock.patch.object(
                        fetch_medium_posts, 'FETCH_STATUS_PATH', status_path,
                    ),
                    mock.patch.object(
                        fetch_medium_posts, 'PROFILE_BRIDGE_PATH', missing_bridge,
                    ),
                    mock.patch.object(
                        fetch_medium_posts, 'load_previous',
                        return_value=current_previous,
                    ),
                    mock.patch.object(
                        fetch_medium_posts, 'fetch_archive',
                        side_effect=RuntimeError('archive down'),
                    ),
                    mock.patch.object(
                        fetch_medium_posts, 'fetch_rss_posts',
                        return_value=[untrusted],
                    ),
                ):
                    self.assertEqual(fetch_medium_posts.main(), 0)
                current_previous = json.loads(
                    output.read_text(encoding='utf-8')
                )
                status = json.loads(status_path.read_text(encoding='utf-8'))
                self.assertEqual(current_previous, previous)
                self.assertEqual(status['status'], 'degraded')
                self.assertEqual(
                    status['mode'],
                    'trusted_history_rss_gap_quarantined',
                )
                self.assertNotIn(
                    '123456abcdef',
                    {row['medium_id'] for row in current_previous},
                )

    def test_medium_archive_outage_with_contiguous_rss_is_healthy(self):
        previous = [{
            'source': 'medium',
            'source_id': 'abcdef123456',
            'medium_id': 'abcdef123456',
            'title': 'Previous',
            'post_date': '2026-07-13T01:00:00Z',
            'url': 'https://medium.com/@navnoorbawa/previous-abcdef123456',
            'visibility': 'PUBLIC',
            'content_status': 'excerpt',
            'body_revision_status': 'unverified',
            'source_updated_at': '2026-07-13T01:00:00Z',
            'observed_source_updated_at': '',
        }]
        newest = dict(
            previous[0],
            source_id='123456abcdef',
            medium_id='123456abcdef',
            title='Newest',
            post_date='2026-07-14T01:00:00Z',
            url='https://medium.com/@navnoorbawa/newest-123456abcdef',
        )
        overlap = dict(previous[0])
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status_path = directory / 'status.json'
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(
                    fetch_medium_posts, 'FETCH_STATUS_PATH', status_path,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'load_previous', return_value=previous,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_archive',
                    side_effect=RuntimeError('archive down'),
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_rss_posts',
                    return_value=[newest, overlap],
                ),
            ):
                self.assertEqual(fetch_medium_posts.main(), 0)
            catalogue = json.loads(output.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertEqual(len(catalogue), 2)
            self.assertEqual(status['status'], 'ok')
            self.assertEqual(
                status['mode'],
                'validated_history_plus_current_rss',
            )
            self.assertEqual(status['fetched_count'], 2)

    def test_stale_archive_is_not_published_over_a_newer_stable_rss_head(self):
        previous = [
            sample_medium_row(
                f'{index + 16:012x}',
                f'2026-07-{19 - index:02d}T12:00:00Z',
            )
            for index in range(10)
        ]
        new = sample_medium_row('ffffffffffff', '2026-07-20T12:00:00Z')
        latest = [new] + [dict(post) for post in previous[:9]]
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status_path = directory / 'status.json'
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(
                    fetch_medium_posts, 'FETCH_STATUS_PATH', status_path,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'load_previous', return_value=previous,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_archive', return_value=previous,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'convert_post',
                    side_effect=lambda post: dict(post),
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_rss_posts', return_value=latest,
                ) as rss_fetch,
            ):
                self.assertEqual(fetch_medium_posts.main(), 0)
            catalogue = json.loads(output.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertEqual(rss_fetch.call_count, 2)
            self.assertEqual(catalogue[0]['medium_id'], 'ffffffffffff')
            self.assertEqual(len(catalogue), 11)
            self.assertEqual(status['status'], 'ok')
            self.assertEqual(
                status['mode'], 'validated_history_plus_current_rss',
            )
            self.assertNotEqual(status['mode'], 'complete_archive')

    def test_matching_archive_and_stable_rss_edge_publish_complete_archive(self):
        previous = [
            sample_medium_row(
                f'{index + 16:012x}',
                f'2026-07-{19 - index:02d}T12:00:00Z',
            )
            for index in range(10)
        ]
        new = sample_medium_row('ffffffffffff', '2026-07-20T12:00:00Z')
        archive = [new] + [dict(post) for post in previous]
        latest = [dict(post) for post in archive[:10]]
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status_path = directory / 'status.json'
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(
                    fetch_medium_posts, 'FETCH_STATUS_PATH', status_path,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'load_previous', return_value=previous,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_archive', return_value=archive,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'convert_post',
                    side_effect=lambda post: dict(post),
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_rss_posts', return_value=latest,
                ) as rss_fetch,
            ):
                self.assertEqual(fetch_medium_posts.main(), 0)
            catalogue = json.loads(output.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertEqual(rss_fetch.call_count, 2)
            self.assertEqual(catalogue, archive)
            self.assertEqual(status['status'], 'ok')
            self.assertEqual(status['mode'], 'complete_archive')
            self.assertEqual(status['fetched_count'], 11)

    def test_unverifiable_archive_is_never_written_when_rss_is_unavailable(self):
        previous = [
            sample_medium_row(
                f'{index + 16:012x}',
                f'2026-07-{19 - index:02d}T12:00:00Z',
            )
            for index in range(10)
        ]
        archive = [
            sample_medium_row('ffffffffffff', '2026-07-20T12:00:00Z')
        ] + [dict(post) for post in previous]
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status_path = directory / 'status.json'
            original = b'{"sentinel":"trusted candidate bytes"}\n'
            output.write_bytes(original)
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(
                    fetch_medium_posts, 'FETCH_STATUS_PATH', status_path,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'load_previous', return_value=previous,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_archive', return_value=archive,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'convert_post',
                    side_effect=lambda post: dict(post),
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_rss_posts',
                    side_effect=RuntimeError('RSS unavailable'),
                ) as rss_fetch,
            ):
                self.assertEqual(fetch_medium_posts.main(), 1)
            self.assertEqual(rss_fetch.call_count, 1)
            self.assertEqual(output.read_bytes(), original)
            status = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(status['mode'], 'archive_and_rss_failed')

    def test_medium_reviewed_profile_bridge_records_explicit_provenance(self):
        bridge = fetch_medium_posts.load_profile_bridge(
            now=datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc),
        )
        previous = [
            {
                'source': 'medium',
                'source_id': post_id,
                'medium_id': post_id,
                'title': f'Trusted {post_id}',
                'post_date': stamp,
                'url': f'https://medium.com/@navnoorbawa/trusted-{post_id}',
                'visibility': 'PUBLIC',
                'content_status': 'excerpt',
                'body_revision_status': 'unverified',
                'source_updated_at': stamp,
                'observed_source_updated_at': '',
            }
            for post_id, stamp in (
                ('4912dfd9ee85', '2026-08-14T06:29:10Z'),
                ('c4c340597a67', '2026-08-14T04:07:37Z'),
            )
        ]
        stamps = (
            '2026-08-19T17:01:27Z',
            '2026-08-19T16:12:51Z',
            '2026-08-18T15:46:22Z',
            '2026-08-18T14:16:44Z',
            '2026-08-17T15:38:09Z',
            '2026-08-17T10:14:09Z',
            '2026-08-16T13:01:24Z',
            '2026-08-16T06:41:27Z',
            '2026-08-15T09:37:43Z',
            '2026-08-15T04:46:41Z',
        )
        latest = [
            {
                'source': 'medium',
                'source_id': post_id,
                'medium_id': post_id,
                'title': f'Live {post_id}',
                'post_date': stamp,
                'url': f'https://medium.com/@navnoorbawa/live-{post_id}',
                'visibility': 'UNKNOWN',
                'content_status': 'excerpt',
            }
            for post_id, stamp in zip(bridge['rss_window_ids'], stamps)
        ]
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            output = directory / 'medium.json'
            status_path = directory / 'status.json'
            with (
                mock.patch.object(fetch_medium_posts, 'OUTPUT_PATH', output),
                mock.patch.object(
                    fetch_medium_posts, 'FETCH_STATUS_PATH', status_path,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'load_previous', return_value=previous,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_archive',
                    side_effect=RuntimeError('archive down'),
                ),
                mock.patch.object(
                    fetch_medium_posts, 'fetch_rss_posts', return_value=latest,
                ),
                mock.patch.object(
                    fetch_medium_posts, 'utc_now',
                    return_value='2026-08-20T14:00:00Z',
                ),
            ):
                self.assertEqual(fetch_medium_posts.main(), 0)
            catalogue = json.loads(output.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertEqual(len(catalogue), 12)
            self.assertEqual(status['status'], 'ok')
            self.assertEqual(
                status['mode'],
                'operator_reviewed_profile_bridge_plus_current_rss',
            )
            self.assertEqual(
                status['provenance']['surface'],
                fetch_medium_posts.PROFILE_BRIDGE_SURFACE,
            )
            self.assertEqual(
                status['provenance']['rss_window_ids'],
                bridge['rss_window_ids'],
            )
            self.assertEqual(
                status['provenance']['previous_history_prefix_ids'],
                bridge['previous_history_prefix_ids'],
            )

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
