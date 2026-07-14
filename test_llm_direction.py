import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import llm_direction


class DirectionResolverSafetyTests(unittest.TestCase):
    def test_cache_cannot_reverse_negation_or_promote_a_reference(self):
        negated = 'There is no verified, current spot-price arbitrage to chase in bitcoin.'
        reference = (
            '[1] Doe, J. (2025). "Treasury Basis Trade." Journal of Finance. '
            'https://example.com/paper'
        )
        eligible = 'The options exposure is described, but its direction is ambiguous.'
        trades = [
            {'trade_description': negated, 'direction': 'unspecified'},
            {'trade_description': reference, 'direction': 'unspecified'},
            {'trade_description': eligible, 'direction': 'unspecified'},
        ]
        cache = {
            hashlib.sha256(text.encode('utf-8')).hexdigest(): 'long'
            for text in (negated, reference, eligible)
        }

        with tempfile.TemporaryDirectory() as directory:
            trades_path = Path(directory) / 'trades.json'
            cache_path = Path(directory) / 'cache.json'
            trades_path.write_text(json.dumps(trades), encoding='utf-8')
            cache_path.write_text(json.dumps(cache), encoding='utf-8')
            with (
                mock.patch.object(llm_direction, 'TRADES', trades_path),
                mock.patch.object(llm_direction, 'CACHE', cache_path),
                mock.patch.dict(os.environ, {'DIRECTION_LLM_ENABLE': '1'}),
                mock.patch.object(llm_direction, '_ollama_models', return_value=None),
            ):
                llm_direction.run()

            resolved = json.loads(trades_path.read_text(encoding='utf-8'))

        self.assertEqual(resolved[0]['direction'], 'unspecified')
        self.assertEqual(resolved[1]['direction'], 'unspecified')
        self.assertEqual(resolved[2]['direction'], 'long')

    def test_eligibility_helper_is_precision_first(self):
        self.assertFalse(llm_direction._eligible_for_direction_resolution({
            'trade_description': 'The fund did not establish a long position in stock.'
        }))
        self.assertFalse(llm_direction._eligible_for_direction_resolution({
            'trade_description': 'https://example.com/long-equity-options'
        }))
        self.assertTrue(llm_direction._eligible_for_direction_resolution({
            'trade_description': 'The article discusses an equity option without a stated side.'
        }))


if __name__ == '__main__':
    unittest.main()
