import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import filter_trades


class FilterTradesTests(unittest.TestCase):
    @staticmethod
    def trade(description, **overrides):
        row = {
            'article_url': 'https://example.com/research-one',
            'article_title': 'Research one',
            'article_date': '2026-07-31',
            'trade_description': description,
            'direction': 'unspecified',
            'instruments': ['unspecified'],
            'underlying': None,
        }
        row.update(overrides)
        return row

    def test_meta_content_is_rejected_even_when_it_mentions_a_trade(self):
        row = self.trade(
            'This article explains how Citadel bought a large equity position '
            'and summarizes the supporting evidence for readers in detail.'
        )

        self.assertTrue(filter_trades.is_false_positive(row))

    def test_missing_trade_evidence_is_rejected(self):
        row = self.trade(
            'Market liquidity changed throughout the quarter while volatility '
            'and cross-asset correlations remained unusually elevated.'
        )

        self.assertTrue(filter_trades.is_false_positive(row))

    def test_quant_direction_and_instrument_form_the_strict_fallback(self):
        description = (
            'The measured exposure was 12.5% after the rebalance, with the '
            'documented risk concentrated in the named equity basket.'
        )
        accepted = self.trade(
            description,
            direction='long',
            instruments=['equity'],
        )
        self.assertFalse(filter_trades.is_false_positive(accepted))

        missing_components = (
            self.trade(description, direction='unspecified', instruments=['equity']),
            self.trade(description, direction='long', instruments=['unspecified']),
            self.trade(
                description.replace('12.5%', 'a material amount'),
                direction='long',
                instruments=['equity'],
            ),
        )
        for row in missing_components:
            with self.subTest(row=row):
                self.assertTrue(filter_trades.is_false_positive(row))

    def test_real_trade_indicator_still_requires_a_meaningful_description(self):
        self.assertTrue(filter_trades.is_false_positive(
            self.trade('Citadel bought bonds.'),
        ))
        self.assertFalse(filter_trades.is_false_positive(self.trade(
            'Citadel bought investment-grade bonds after spreads widened, '
            'building the position over several sessions while liquidity held.'
        )))

    def test_merge_duplicates_is_article_scoped_and_uses_the_first_150_chars(self):
        prefix = 'Citadel bought equity index futures while volatility rose. ' * 4
        first = self.trade(prefix + 'first ending')
        same_prefix = self.trade(prefix + 'different ending', direction='long')
        other_article = self.trade(
            prefix + 'first ending',
            article_url='https://example.com/research-two',
        )
        distinct_passage = self.trade(
            'Bridgewater sold duration and added inflation hedges after real '
            'yields moved higher across the curve for several sessions.'
        )

        result = filter_trades.merge_duplicates([
            first, same_prefix, other_article, distinct_passage,
        ])

        self.assertEqual(result, [first, other_article, distinct_passage])
        self.assertIs(result[0], first)

    def test_clean_underlying_removes_noise_and_normalizes_empty_values(self):
        cases = {
            None: None,
            '': None,
            'global hedge fund; WTI crude oil; ': 'WTI crude oil',
            'US hedge fund exposure; WTI crude oil': 'WTI crude oil',
            'small fraction of the fund held; gold': 'gold',
            'hedge fund': None,
            'S&P 500;; ': 'S&P 500',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(filter_trades.clean_underlying(value), expected)

    def test_main_filters_cleans_deduplicates_sorts_and_replaces_output(self):
        valid_new = self.trade(
            'Citadel bought investment-grade bonds after spreads widened, '
            'building the position over several sessions while liquidity held.',
            article_date='2026-08-01',
            underlying='global hedge fund; US corporate bonds',
        )
        duplicate = dict(valid_new, direction='long')
        valid_old = self.trade(
            'The measured exposure was 8.5% after the rebalance, with the '
            'documented risk concentrated in the named commodity basket.',
            article_url='https://example.com/research-two',
            article_title='Research two',
            article_date=None,
            direction='short',
            instruments=['commodity'],
            underlying='WTI crude oil; ',
        )
        meta = self.trade(
            'This article explains how Citadel bought a large equity position '
            'and summarizes the supporting evidence for readers in detail.',
            article_url='https://example.com/meta',
        )
        unsupported = self.trade(
            'Liquidity changed throughout the quarter while volatility and '
            'cross-asset correlations remained unusually elevated.',
            article_url='https://example.com/unsupported',
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / 'input.json'
            output_path = root / 'output.json'
            input_path.write_text(
                json.dumps([valid_old, meta, duplicate, unsupported, valid_new]),
                encoding='utf-8',
            )
            output_path.write_text('[{"old": true}]\n', encoding='utf-8')

            with (
                mock.patch.object(filter_trades, 'INPUT_PATH', input_path),
                mock.patch.object(filter_trades, 'OUTPUT_PATH', output_path),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                result = filter_trades.main()

            stored = json.loads(output_path.read_text(encoding='utf-8'))
            self.assertEqual(stored, result)
            self.assertEqual(
                [row['article_url'] for row in stored],
                [valid_new['article_url'], valid_old['article_url']],
            )
            self.assertEqual(stored[0]['underlying'], 'US corporate bonds')
            self.assertEqual(stored[1]['underlying'], 'WTI crude oil')
            self.assertFalse((root / 'output.json.tmp').exists())
            self.assertIn('After false-positive filter: 3', stdout.getvalue())
            self.assertIn('After deduplication: 2', stdout.getvalue())

    def test_atomic_write_replaces_exact_output_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trades.json'
            path.write_text('[{"old": true}]\n', encoding='utf-8')

            filter_trades.atomic_write_json(path, [{'new': True}])

            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), [{'new': True}])
            self.assertFalse((path.parent / 'trades.json.tmp').exists())

    def test_atomic_write_failure_preserves_output_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trades.json'
            original = b'[{"old": true}]\n'
            path.write_bytes(original)

            with self.assertRaises(TypeError):
                filter_trades.atomic_write_json(path, [{'invalid': object()}])

            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((path.parent / 'trades.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
