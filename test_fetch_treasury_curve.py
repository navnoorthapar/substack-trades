import io
import json
import urllib.error
import urllib.request
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import fetch_treasury_curve


def curve_row(value):
    return {tenor: value for tenor in fetch_treasury_curve.TENORS}


class FakeResponse:
    def __init__(self, url, body=b'<feed/>', status=200):
        self.url = url
        self.body = body
        self.status = status
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self):
        return self.url

    def read(self):
        self.read_calls += 1
        return self.body


class TreasuryFetchBoundaryTests(unittest.TestCase):
    def test_feed_url_is_exact_and_year_is_strictly_bounded(self):
        self.assertEqual(
            fetch_treasury_curve._feed_url_for_year(2026),
            'https://home.treasury.gov/resource-center/data-chart-center/'
            'interest-rates/pages/xml?data=daily_treasury_yield_curve'
            '&field_tdr_date_value=2026',
        )
        hostile_values = (
            True,
            '2026&field_tdr_date_value=http://127.0.0.1/',
            999,
            10000,
            object(),
        )
        for value in hostile_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'four-digit integer'):
                    fetch_treasury_curve._feed_url_for_year(value)  # type: ignore[arg-type]

    def test_fetch_uses_proxy_free_opener_and_exact_response_url(self):
        url = fetch_treasury_curve._feed_url_for_year(2026)
        response = FakeResponse(url, body=b'official XML')
        opener = mock.Mock()
        opener.open.return_value = response

        with mock.patch.object(fetch_treasury_curve, 'TREASURY_OPENER', opener):
            self.assertEqual(fetch_treasury_curve._fetch_year(2026), b'official XML')

        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_header('User-agent'), fetch_treasury_curve.USER_AGENT)
        self.assertEqual(
            opener.open.call_args.kwargs,
            {'timeout': fetch_treasury_curve.REQUEST_TIMEOUT},
        )
        proxy_handlers = [
            handler for handler in fetch_treasury_curve.TREASURY_OPENER.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        # Passing an empty ProxyHandler suppresses urllib's environment-backed
        # default; because it has no proxy methods, build_opener does not retain
        # it in the final handler list.
        self.assertEqual(proxy_handlers, [])
        self.assertTrue(any(
            isinstance(handler, fetch_treasury_curve._RejectRedirects)
            for handler in fetch_treasury_curve.TREASURY_OPENER.handlers
        ))

    def test_redirects_are_rejected_before_urllib_can_follow_them(self):
        url = fetch_treasury_curve._feed_url_for_year(2026)
        handler = fetch_treasury_curve._RejectRedirects()
        with self.assertRaisesRegex(ValueError, 'refused HTTP 302 redirect'):
            handler.redirect_request(
                urllib.request.Request(url),
                io.BytesIO(),
                302,
                'Found',
                {},
                'http://127.0.0.1/private',
            )

    def test_fetch_fails_closed_if_a_response_claims_another_url(self):
        response = FakeResponse('https://attacker.example/redirected')
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(fetch_treasury_curve, 'TREASURY_OPENER', opener):
            with self.assertRaisesRegex(ValueError, 'did not match the official URL'):
                fetch_treasury_curve._fetch_year(2026)
        self.assertEqual(response.read_calls, 0)

    def test_parse_year_rejects_a_nonexistent_calendar_day(self):
        document = b'''<feed xmlns="http://www.w3.org/2005/Atom"
            xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
            xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
          <entry><content><m:properties>
            <d:NEW_DATE>2026-02-30T00:00:00</d:NEW_DATE>
          </m:properties></content></entry>
        </feed>'''
        with self.assertRaisesRegex(ValueError, 'invalid calendar day'):
            fetch_treasury_curve.parse_year(document, 2026, partial_year=True)

    def test_old_cli_controls_fail_before_refresh_logic(self):
        old_controls = (
            ['--years', '2025', '2026'],
            ['--current-year', '2026'],
            ['--merge', '/tmp/attacker.json'],
            ['--output', '/tmp/attacker.json'],
        )
        for arguments in old_controls:
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with mock.patch.object(fetch_treasury_curve, 'main') as main, \
                        redirect_stderr(stderr):
                    self.assertEqual(fetch_treasury_curve.cli(arguments), 2)
                main.assert_not_called()
                self.assertIn('accepts no arguments', stderr.getvalue())

    def test_main_uses_only_fixed_input_and_internal_utc_years(self):
        existing = {
            'observations': {'2024-01-02': curve_row(1.0)},
        }
        fetched = {'2026-01-02': curve_row(2.0)}
        fake_datetime = mock.Mock()
        fake_datetime.now.return_value = datetime(
            2026, 8, 11, 1, 2, tzinfo=timezone.utc,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(fetch_treasury_curve, 'datetime', fake_datetime), \
                mock.patch.object(
                    fetch_treasury_curve,
                    'load_curve_dataset',
                    return_value=existing,
                ) as load_curve, \
                mock.patch.object(
                    fetch_treasury_curve,
                    'fetch_years',
                    return_value=fetched,
                ) as fetch_years, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(fetch_treasury_curve.main(), 0)

        fixed_path = (
            Path(fetch_treasury_curve.__file__).resolve().parent
            / fetch_treasury_curve.DATASET_NAME
        )
        load_curve.assert_called_once_with(fixed_path)
        fetch_years.assert_called_once_with((2025, 2026), 2026)
        fake_datetime.now.assert_called_once_with(timezone.utc)
        dataset = json.loads(stdout.getvalue())
        self.assertEqual(dataset['first_date'], '2024-01-02')
        self.assertEqual(dataset['last_date'], '2026-01-02')
        self.assertEqual(dataset['observation_count'], 2)
        self.assertEqual(
            stdout.getvalue(),
            json.dumps(dataset, ensure_ascii=False, separators=(',', ':')) + '\n',
        )
        self.assertIn('Treasury curve: 2 trading days', stderr.getvalue())

    def test_missing_tracked_input_and_fetch_failure_emit_no_candidate(self):
        failures = (
            FileNotFoundError('missing tracked curve'),
            urllib.error.URLError('official feed unavailable'),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                stdout = io.StringIO()
                stderr = io.StringIO()
                if isinstance(failure, FileNotFoundError):
                    load_side_effect = failure
                    fetch_side_effect = None
                else:
                    load_side_effect = None
                    fetch_side_effect = failure
                with mock.patch.object(
                        fetch_treasury_curve,
                        'load_curve_dataset',
                        return_value={'observations': {'2024-01-02': curve_row(1.0)}},
                        side_effect=load_side_effect,
                ), mock.patch.object(
                        fetch_treasury_curve,
                        'fetch_years',
                        side_effect=fetch_side_effect,
                ) as fetch_years, redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(fetch_treasury_curve.main(), 1)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('Treasury curve refresh failed:', stderr.getvalue())
                if isinstance(failure, FileNotFoundError):
                    fetch_years.assert_not_called()


if __name__ == '__main__':
    unittest.main()
