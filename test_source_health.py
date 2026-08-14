import contextlib
import io
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from source_health import (
    PERSISTENT_DEGRADATION_HOURS,
    degradation_state,
    evaluate_manifest,
    main,
    report_manifest,
    track_source_health,
)


def source_row(status='degraded', checked_at='2026-08-01T12:00:00Z'):
    return {
        'checked_at': checked_at,
        'status': status,
        'mode': 'cached_archive_plus_rss' if status == 'degraded' else 'complete_archive',
        'published_count': 10,
        'fetched_count': 2 if status == 'degraded' else 10,
        'included_count': 8,
        'newest': '2026-08-01T10:00:00Z',
    }


def manifest(item):
    return {'sources': {'medium': item}}


class SourceHealthTests(unittest.TestCase):
    def test_first_degraded_observation_and_healthy_source_are_explicit(self):
        tracked = track_source_health({
            'medium': source_row(),
            'substack': source_row('ok'),
        })
        self.assertEqual(
            tracked['medium']['degraded_since'],
            '2026-08-01T12:00:00Z',
        )
        self.assertEqual(tracked['medium']['consecutive_degraded_checks'], 1)
        self.assertIsNone(tracked['substack']['degraded_since'])
        self.assertEqual(tracked['substack']['consecutive_degraded_checks'], 0)

    def test_legacy_degraded_manifest_migrates_without_inventing_history(self):
        previous = manifest(source_row(checked_at='2026-08-01T08:00:00Z'))
        current = track_source_health(
            {'medium': source_row(checked_at='2026-08-01T12:00:00Z')},
            previous,
        )
        self.assertEqual(current['medium']['degraded_since'], '2026-08-01T08:00:00Z')
        self.assertEqual(current['medium']['consecutive_degraded_checks'], 2)

    def test_annotated_degraded_streak_continues_and_recovery_resets_it(self):
        previous_item = source_row(checked_at='2026-08-01T08:00:00Z')
        previous_item.update({
            'degraded_since': '2026-07-31T08:00:00Z',
            'consecutive_degraded_checks': 4,
        })
        current = track_source_health(
            {'medium': source_row(checked_at='2026-08-01T12:00:00Z')},
            manifest(previous_item),
        )
        self.assertEqual(current['medium']['degraded_since'], '2026-07-31T08:00:00Z')
        self.assertEqual(current['medium']['consecutive_degraded_checks'], 5)

        recovered = track_source_health(
            {'medium': source_row('ok', checked_at='2026-08-01T16:00:00Z')},
            manifest(current['medium']),
        )
        self.assertIsNone(recovered['medium']['degraded_since'])
        self.assertEqual(recovered['medium']['consecutive_degraded_checks'], 0)

    def test_persistence_threshold_is_exactly_48_hours(self):
        item = source_row(checked_at='2026-08-01T12:00:00Z')
        item.update({
            'degraded_since': '2026-08-01T00:00:00Z',
            'consecutive_degraded_checks': 3,
        })
        before = evaluate_manifest(
            manifest(item),
            now=datetime(2026, 8, 2, 23, 59, 59, tzinfo=timezone.utc),
        )
        at_threshold = evaluate_manifest(
            manifest(item),
            now=datetime(2026, 8, 3, 0, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(PERSISTENT_DEGRADATION_HOURS, 48)
        self.assertFalse(before[0].persistent)
        self.assertTrue(at_threshold[0].persistent)

    def test_publish_status_and_watchdog_policies_have_distinct_exit_contracts(self):
        item = source_row(checked_at='2026-08-01T12:00:00Z')
        item.update({
            'degraded_since': '2026-08-01T00:00:00Z',
            'consecutive_degraded_checks': 4,
        })
        transient_now = datetime(2026, 8, 2, 0, 0, 0, tzinfo=timezone.utc)
        persistent_now = datetime(2026, 8, 4, 0, 0, 0, tzinfo=timezone.utc)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                report_manifest(manifest(item), policy='publish', now=persistent_now),
                0,
            )
            self.assertEqual(
                report_manifest(manifest(item), policy='status', now=transient_now),
                1,
            )
            self.assertEqual(
                report_manifest(manifest(item), policy='watchdog', now=transient_now),
                0,
            )
            self.assertEqual(
                report_manifest(manifest(item), policy='watchdog', now=persistent_now),
                1,
            )
        rendered = output.getvalue()
        self.assertIn('Validated fallback data remains publishable', rendered)
        self.assertIn('::warning title=Transient source degradation::', rendered)
        self.assertIn('::error title=Persistent source degradation::', rendered)

    def test_cli_handles_legacy_manifest_as_a_safe_first_observation(self):
        output = io.StringIO()
        input_stream = io.StringIO(json.dumps(manifest(source_row())))
        with mock.patch('sys.stdin', input_stream), contextlib.redirect_stdout(output):
            result = main([
                '--policy', 'watchdog',
                '--now', '2026-08-01T12:01:00Z',
            ])
        self.assertEqual(result, 0)
        self.assertIn('legacy manifest treated as first observation', output.getvalue())

    def test_cli_rejects_removed_path_control_before_reading_stdin(self):
        input_stream = io.StringIO(json.dumps(manifest(source_row())))
        with (
            mock.patch('sys.stdin', input_stream),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            main([
                '--manifest', '/tmp/untrusted.json',
                '--policy', 'watchdog',
            ])
        self.assertEqual(caught.exception.code, 2)

    def test_partial_or_contradictory_tracking_state_fails_closed(self):
        partial = source_row()
        partial['degraded_since'] = '2026-08-01T12:00:00Z'
        with self.assertRaisesRegex(ValueError, 'tracking fields are incomplete'):
            degradation_state('medium', partial, allow_legacy=True)

        healthy = source_row('ok')
        healthy.update({
            'degraded_since': '2026-08-01T12:00:00Z',
            'consecutive_degraded_checks': 1,
        })
        with self.assertRaisesRegex(ValueError, 'retains a degraded streak'):
            degradation_state('medium', healthy, allow_legacy=True)


if __name__ == '__main__':
    unittest.main()
