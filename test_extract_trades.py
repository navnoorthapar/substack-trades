import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import extract_trades


class ExtractTradesHardeningTests(unittest.TestCase):
    def test_explicitly_negated_direction_is_not_classified(self):
        examples = [
            'There is no verified, current spot-versus-perpetual arbitrage to chase.',
            'The review found no evidence that the fund went short the dollar.',
            'The mandate does not hold a long position in equities.',
            'The desk never went long oil during the shock.',
            'The portfolio operates without taking a short position in bonds.',
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(extract_trades.classify_direction(text), 'unspecified')

        self.assertTrue(
            extract_trades.has_negated_trade_signal(
                'There is no verified arbitrage opportunity in this market.'
            )
        )
        self.assertFalse(
            extract_trades.has_negated_trade_signal(
                'The fund was not only long oil but also short airline stocks.'
            )
        )

        institutional_false_positives = [
            'Three checks would falsify the no live price arb thesis in stablecoins.',
            'The VIX moved because quotes widened, not because of genuine volatility.',
            "Volatility arbitrage isn't about being long or short volatility.",
            'Traditional arbitrage-based pricing breaks down in an incomplete market.',
            'No primary filing confirms that the bank acquired a long bond position.',
        ]
        for text in institutional_false_positives:
            with self.subTest(text=text):
                self.assertEqual(extract_trades.classify_direction(text), 'unspecified')

    def test_negation_scope_preserves_affirmative_contrast_and_not_only(self):
        self.assertEqual(
            extract_trades.classify_direction(
                'The fund was not only long oil but also short airline stocks.'
            ),
            'long/short',
        )
        self.assertEqual(
            extract_trades.classify_direction(
                'The fund was not long oil but went short airline stocks.'
            ),
            'short',
        )
        self.assertEqual(
            extract_trades.classify_direction(
                'This was not speculation—it was a long oil position.'
            ),
            'long',
        )

    def test_prediction_market_no_token_is_not_treated_as_negation(self):
        self.assertTrue(
            extract_trades.is_trade_block(
                'Buy $4,300 YES on Polymarket and $5,700 NO on Kalshi for a 10% return.'
            )
        )

    def test_negated_trade_language_does_not_create_trade_block(self):
        self.assertFalse(
            extract_trades.is_trade_block(
                'There is no verified NDF arbitrage to chase in the currency market.'
            )
        )
        self.assertFalse(
            extract_trades.is_trade_block(
                'The fund did not establish a long position in common stock.'
            )
        )

    def test_reference_and_url_only_blocks_are_not_observations(self):
        references = [
            '[1] Doe, J. (2025). "Treasury Basis Trade." Journal of Finance. '
            'https://example.com/paper',
            'Federal Reserve Board — Hedge Fund Treasury Futures and Repo Positions: '
            'https://example.com/feds-note',
            'https://example.com/research/long-equity-options-and-bonds',
        ]
        for text in references:
            with self.subTest(text=text):
                self.assertTrue(extract_trades.is_reference_only_block(text))
                self.assertFalse(extract_trades.is_trade_block(text))

    def test_process_article_keeps_analysis_but_drops_reference_entry(self):
        analysis = (
            'The portfolio established a long position in Acme Capital common stock '
            'because improving cash flow supported the valuation. The position was '
            'sized conservatively and monitored against the stated downside case.'
        )
        reference = (
            '[1] Doe, J. (2025). "Long Equity and Options Trading." Journal of Finance. '
            'https://example.com/paper'
        )
        post = {
            'title': 'A documented position',
            'url': 'https://example.com/article',
            'post_date': '2026-07-14T00:00:00Z',
            'body_text': analysis + '\n\n' + reference,
        }

        trades = extract_trades.process_article(post)

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['trade_description'], analysis)
        self.assertEqual(trades[0]['direction'], 'long')
        self.assertFalse(trades[0]['description_truncated'])

    def test_displayed_fields_never_bleed_from_adjacent_paragraphs(self):
        visible = (
            'The fund established a long position in Acme Capital common stock '
            'because recurring revenue improved the base case by 12%.'
        )
        adjacent = (
            'A separate swaptions appendix discusses Vega Holdings, a $30 billion '
            'portfolio, and a thesis because volatility will mean revert.'
        )
        post = {
            'title': 'Passage-level evidence',
            'url': 'https://example.com/passage-evidence',
            'post_date': '2026-07-14T00:00:00Z',
            'body_text': visible + '\n\n' + adjacent,
        }

        first = extract_trades.process_article(post)[0]

        self.assertEqual(first['trade_description'], visible)
        self.assertNotIn('Vega', first['underlying'] or '')
        self.assertNotIn('$30 billion', first['any_quant_detail'] or '')
        self.assertNotIn('volatility will mean revert', first['edge_or_thesis'] or '')

    def test_truncation_is_explicit_provenance(self):
        paragraph = (
            'The fund established a long position in Acme Capital common stock '
            'because recurring revenue improved. ' + ('Supporting detail ' * 80)
        )
        post = {
            'title': 'A long bounded passage',
            'url': 'https://example.com/truncated',
            'post_date': '2026-07-14T00:00:00Z',
            'body_text': paragraph,
        }

        trade = extract_trades.process_article(post)[0]

        self.assertLessEqual(len(trade['trade_description']), 800)
        self.assertTrue(trade['description_truncated'])

    def test_labels_cannot_be_inferred_beyond_published_passage(self):
        visible = (
            'The paper discusses option pricing and equity volatility, but there is '
            'no verified arbitrage opportunity or current position to execute. '
        )
        paragraph = visible + ('Background model discussion. ' * 40) + (
            'A separate appendix says another fund established a long position in stock.'
        )
        post = {
            'title': 'Evidence boundary test',
            'url': 'https://example.com/evidence-boundary',
            'post_date': '2026-07-14T00:00:00Z',
            'body_text': paragraph,
        }

        self.assertGreater(len(paragraph), 800)
        self.assertFalse(extract_trades.is_trade_block(extract_trades.excerpt(paragraph)))
        trades = extract_trades.process_article(post)
        self.assertTrue(trades)
        self.assertTrue(all(
            extract_trades.classify_direction(trade['trade_description'])
            == trade['direction']
            for trade in trades
        ))
        self.assertTrue(all(
            not trade['trade_description'].startswith(visible)
            for trade in trades
        ))

    def test_accumulated_metric_is_not_a_long_signal(self):
        text = (
            "LJM's short put positions accumulated negative gamma. "
            "Each market drop required the desk to buy futures to hedge."
        )
        self.assertEqual(extract_trades.classify_direction(text), 'unspecified')

    def test_accumulated_owned_asset_remains_a_long_signal(self):
        self.assertEqual(
            extract_trades.classify_direction('The fund accumulated Acme shares.'),
            'long',
        )
        self.assertEqual(
            extract_trades.classify_direction('The fund accumulated a large position.'),
            'long',
        )
        self.assertEqual(
            extract_trades.classify_direction('The fund accumulated a massive short position.'),
            'short',
        )

    def test_company_underlying_requires_capitalized_legal_name(self):
        false_company = 'The risk sits against Vegas on its co-terminal swaptions.'
        self.assertIsNone(extract_trades.extract_underlying(false_company))
        self.assertEqual(
            extract_trades.extract_underlying('The fund bought Acme Capital shares.'),
            'Acme Capital',
        )

    def test_thesis_as_is_a_whole_word(self):
        self.assertIsNone(
            extract_trades.extract_thesis(
                'Vegas on its co-terminal European swaptions ranged from 1% to 2%.'
            )
        )
        self.assertEqual(
            extract_trades.extract_thesis(
                'The fund bought shares because earnings were improving rapidly.'
            ),
            'earnings were improving rapidly.',
        )

    def test_excerpt_drops_partial_final_word(self):
        text = ('word ' * 159) + 'tailword'
        result = extract_trades.excerpt(text)
        self.assertLessEqual(len(result), 800)
        self.assertFalse(result.endswith('tailw'))
        self.assertEqual(result.split()[-1], 'word')

    def test_paths_are_env_overridable_and_output_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            posts_path = tmp_path / 'posts.json'
            trades_path = tmp_path / 'trades.json'
            posts_path.write_text('[]', encoding='utf-8')
            trades_path.write_text('["stale"]', encoding='utf-8')

            env = os.environ.copy()
            env['POSTS_INPUT'] = str(posts_path)
            env['TRADES_OUTPUT'] = str(trades_path)
            subprocess.run(
                [sys.executable, str(Path(extract_trades.__file__))],
                check=True,
                cwd=Path(extract_trades.__file__).parent,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(json.loads(trades_path.read_text(encoding='utf-8')), [])
            self.assertFalse((tmp_path / 'trades.json.tmp').exists())

    def test_atomic_write_failure_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'trades.json'
            output.write_text('["known-good"]', encoding='utf-8')

            with self.assertRaises(TypeError):
                extract_trades.atomic_write_json(output, {'not_json': object()})

            self.assertEqual(
                json.loads(output.read_text(encoding='utf-8')),
                ['known-good'],
            )
            self.assertFalse((Path(tmp) / 'trades.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
