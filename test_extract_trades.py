import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import extract_trades


class ExtractTradesHardeningTests(unittest.TestCase):
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
