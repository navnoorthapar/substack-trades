import json
import tempfile
import unittest
from pathlib import Path

from treasury_curve import (
    MAX_YIELD,
    SCHEMA_VERSION,
    SOURCE,
    band_for,
    build_rate_context,
    curve_as_of,
    load_curve_dataset,
    tercile_cuts,
    validate_curve_dataset,
)

ROOT = Path(__file__).parent


def dataset(observations):
    days = sorted(observations)
    return {
        'schema_version': SCHEMA_VERSION,
        'source': dict(SOURCE),
        'observation_count': len(days),
        'first_date': days[0],
        'last_date': days[-1],
        'observations': {day: observations[day] for day in days},
    }


def row(m3=4.0, y2=4.0, y5=4.2, y10=4.4, y30=4.8):
    return {'m3': m3, 'y2': y2, 'y5': y5, 'y10': y10, 'y30': y30}


class CurveDatasetContractTests(unittest.TestCase):
    def setUp(self):
        self.valid = dataset({
            '2026-01-02': row(y2=4.00, y10=4.30),
            '2026-01-05': row(y2=4.10, y10=4.50),
            '2026-01-06': row(y2=4.20, y10=4.80),
        })

    def test_a_complete_ordered_official_series_is_accepted(self):
        self.assertEqual(validate_curve_dataset(self.valid)['observation_count'], 3)

    def test_a_series_that_is_not_official_or_complete_fails_closed(self):
        cases = {
            'unsupported schema_version': {'schema_version': 2},
            'source name does not match': {
                'source': {**SOURCE, 'name': 'Some blog'},
            },
            'observation_count does not match': {'observation_count': 99},
            'first_date does not match': {'first_date': '2020-01-01'},
            'last_date does not match': {'last_date': '2030-01-01'},
        }
        for expected, patch in cases.items():
            with self.subTest(expected=expected):
                broken = {**self.valid, **patch}
                with self.assertRaisesRegex(ValueError, expected):
                    validate_curve_dataset(broken)

    def test_a_row_missing_a_tenor_or_out_of_range_fails_closed(self):
        partial = dict(self.valid)
        partial['observations'] = dict(self.valid['observations'])
        partial['observations']['2026-01-05'] = {'y2': 4.1, 'y10': 4.5}
        with self.assertRaisesRegex(ValueError, 'exactly the tracked tenors'):
            validate_curve_dataset(partial)

        absurd = dict(self.valid)
        absurd['observations'] = dict(self.valid['observations'])
        absurd['observations']['2026-01-05'] = row(y10=MAX_YIELD + 1)
        with self.assertRaisesRegex(ValueError, 'outside the published range'):
            validate_curve_dataset(absurd)

    def test_observations_must_be_stored_in_calendar_order(self):
        shuffled = dict(self.valid)
        shuffled['observations'] = {
            '2026-01-06': row(), '2026-01-02': row(), '2026-01-05': row(),
        }
        with self.assertRaisesRegex(ValueError, 'calendar order'):
            validate_curve_dataset(shuffled)

    def test_observation_keys_must_be_real_gregorian_calendar_days(self):
        for invalid_day in ('2026-02-29', '2100-02-29', '2026-04-31'):
            with self.subTest(day=invalid_day):
                with self.assertRaisesRegex(ValueError, 'ISO calendar days'):
                    validate_curve_dataset(dataset({invalid_day: row()}))

        leap_day = dataset({'2400-02-29': row()})
        self.assertEqual(validate_curve_dataset(leap_day)['first_date'], '2400-02-29')

    def test_load_rejects_unreadable_json(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'curve.json'
            path.write_text('{not json', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'not valid JSON'):
                load_curve_dataset(path)


class CurveReadingTests(unittest.TestCase):
    def setUp(self):
        self.data = dataset({
            '2026-01-02': row(y2=4.00, y10=4.30),
            '2026-01-05': row(y2=4.10, y10=4.50),
        })

    def test_a_non_trading_day_resolves_back_to_the_published_close(self):
        # 3 and 4 January 2026 are a weekend; the reading must be Friday's
        # published close, carrying the as-of date that produced it.
        weekend = curve_as_of(self.data, '2026-01-04')
        self.assertEqual(weekend['as_of'], '2026-01-02')
        self.assertEqual(weekend['y10'], 4.30)
        self.assertEqual(weekend['slope'], 0.30)

    def test_a_day_before_the_series_has_no_reading(self):
        self.assertIsNone(curve_as_of(self.data, '2024-12-31'))

    def test_a_trading_day_resolves_to_itself(self):
        self.assertEqual(curve_as_of(self.data, '2026-01-05')['as_of'], '2026-01-05')

    def test_lookup_rejects_a_nonexistent_calendar_day(self):
        with self.assertRaisesRegex(ValueError, 'ISO calendar day'):
            curve_as_of(self.data, '2026-02-30')

    def test_bands_split_a_record_into_thirds_by_value(self):
        cuts = tercile_cuts([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        self.assertEqual([band_for(value, cuts) for value in (0.1, 0.35, 0.6)],
                         ['low', 'mid', 'high'])

    def test_rate_context_omits_days_it_cannot_cover(self):
        context = build_rate_context(self.data, ['2024-01-01', '2026-01-05'])
        self.assertNotIn('2024-01-01', context['days'])
        self.assertIn('2026-01-05', context['days'])
        self.assertEqual(context['source'], dict(SOURCE))

    def test_rate_context_bands_describe_the_days_it_was_given(self):
        wide = dataset({
            '2026-01-02': row(y2=4.0, y10=4.1),
            '2026-01-05': row(y2=4.0, y10=4.5),
            '2026-01-06': row(y2=4.0, y10=4.9),
        })
        context = build_rate_context(wide, ['2026-01-02', '2026-01-05', '2026-01-06'])
        bands = [context['days'][day][4] for day in sorted(context['days'])]
        self.assertEqual(bands, ['low', 'mid', 'high'])


class TrackedCurveCoversTheResearchRecordTests(unittest.TestCase):
    """The desk labels every comparable with the curve of its publication day.

    A gap here is not cosmetic: an observation with no reading drops out of any
    rate-conditioned comparison entirely, so coverage is asserted rather than
    assumed.
    """

    def test_every_observation_date_has_a_published_curve_reading(self):
        data = load_curve_dataset(ROOT / 'treasury_curve.json')
        trades = json.loads(
            (ROOT / 'trades_extracted.json').read_text(encoding='utf-8'))
        uncovered = sorted({
            trade['article_date'] for trade in trades
            if curve_as_of(data, trade['article_date']) is None
        })
        self.assertEqual(
            uncovered, [],
            'the tracked Treasury curve starts after these observation dates; '
            'refresh it before the desk can condition on rates',
        )

    def test_the_tracked_curve_reaches_the_newest_research(self):
        data = load_curve_dataset(ROOT / 'treasury_curve.json')
        articles = json.loads(
            (ROOT / 'articles_index.json').read_text(encoding='utf-8'))
        newest = max(article['post_date'][:10] for article in articles
                     if article.get('post_date'))
        self.assertIsNotNone(
            curve_as_of(data, newest),
            'the newest research note has no curve reading at or before it',
        )


if __name__ == '__main__':
    unittest.main()
