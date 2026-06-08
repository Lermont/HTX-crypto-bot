# -*- coding: utf-8 -*-
import unittest
from htxbot.exchange import _compute_gold_coin_candidates

class TestMacroGoldCoinCandidates(unittest.TestCase):
    def test_default_behavior(self):
        # Default/static behavior is unchanged
        self.assertEqual(_compute_gold_coin_candidates(("xaut",)), ("xaut", "xault"))
        self.assertEqual(_compute_gold_coin_candidates(("xault",)), ("xault", "xaut"))

    def test_aliases_resolution(self):
        # Unknown coins have no alias, just return themselves
        self.assertEqual(_compute_gold_coin_candidates(("btc",)), ("btc",))
        # Known aliases
        self.assertEqual(_compute_gold_coin_candidates(("XAUT",)), ("xaut", "xault"))

    def test_duplicates_empty_normalized(self):
        # Empty string and None
        self.assertEqual(_compute_gold_coin_candidates(("",)), ())
        self.assertEqual(_compute_gold_coin_candidates((None,)), ())

        # Duplicates across aliases
        self.assertEqual(
            _compute_gold_coin_candidates(("xaut", "xault")),
            ("xaut", "xault") # The second 'xault' adds nothing new
        )

        # Deduplication
        self.assertEqual(_compute_gold_coin_candidates(("btc", "btc")), ("btc",))

    def test_cache_separation(self):
        # Different inputs should not leak cache
        t1 = ("xaut",)
        t2 = ("btc",)

        # Call first
        r1 = _compute_gold_coin_candidates(t1)
        # Call second
        r2 = _compute_gold_coin_candidates(t2)

        self.assertEqual(r1, ("xaut", "xault"))
        self.assertEqual(r2, ("btc",))

        # Verify cache works by checking memory id (since lru_cache returns identical object if unmodified)
        r1_again = _compute_gold_coin_candidates(t1)
        self.assertIs(r1, r1_again)

if __name__ == '__main__':
    unittest.main()
