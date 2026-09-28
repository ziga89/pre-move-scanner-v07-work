import unittest

from tests.helpers import ROOT  # noqa: F401
from server.engine.book import BandBook


def lv(*pairs):
    return [[p, q] for p, q in pairs]


class BandBookTest(unittest.TestCase):
    def test_first_update_is_snapshot_no_flows(self):
        b = BandBook(2.0)
        self.assertIsNone(b.update(lv((99, 1)), lv((101, 1))))
        self.assertTrue(b.ready)
        self.assertAlmostEqual(b.mid, 100.0)

    def test_add_and_remove_notional(self):
        b = BandBook(2.0)
        b.update(lv((99.5, 2), (99, 3)), lv((100.5, 2), (101, 3)))
        # ask at 100.5 fully removed (consumed), 101 grows by 1; bid 99 shrinks by 1
        flows = b.update(lv((99.5, 2), (99, 2)), lv((101, 4)))
        b_add, b_rem, a_add, a_rem = flows
        self.assertAlmostEqual(a_rem, 2 * 100.5)
        self.assertAlmostEqual(a_add, 1 * 101)
        self.assertAlmostEqual(b_rem, 1 * 99)
        self.assertAlmostEqual(b_add, 0.0)

    def test_levels_outside_band_ignored(self):
        b = BandBook(2.0)
        b.update(lv((99.9, 1), (90, 100)), lv((100.1, 1), (110, 100)))
        flows = b.update(lv((99.9, 1), (90, 500)), lv((100.1, 1), (110, 1)))
        self.assertEqual(flows, (0.0, 0.0, 0.0, 0.0))

    def test_recentering_edges_not_counted(self):
        # Levels that only enter the band because mid moved are not "adds".
        b = BandBook(1.0)
        b.update(lv((99.9, 1), (99.2, 1)), lv((100.1, 1), (100.9, 1), (101.5, 5)))
        # mid moves up ~0.5%; 101.5 now inside the new band but was outside the old one
        flows = b.update(lv((100.4, 1), (99.9, 1), (99.2, 1)), lv((100.6, 1), (100.9, 1), (101.5, 5)))
        _, _, a_add, a_rem = flows
        self.assertAlmostEqual(a_add, 100.6)          # genuinely new ask inside both bands
        self.assertAlmostEqual(a_rem, 100.1)          # consumed ask
        self.assertNotAlmostEqual(a_add, 100.6 + 5 * 101.5)

    def test_truncated_book_not_counted_beyond_visible_depth(self):
        b = BandBook(2.0)
        b.update(lv((99.9, 1)), lv((100.1, 1), (100.2, 1)))  # visible asks end at 100.2
        flows = b.update(lv((99.9, 1)), lv((100.1, 1), (100.2, 1), (100.3, 9)))
        _, _, a_add, _ = flows
        self.assertEqual(a_add, 0.0)

    def test_crossed_book_rejected(self):
        b = BandBook(2.0)
        b.update(lv((99, 1)), lv((101, 1)))
        self.assertIsNone(b.update(lv((101, 1)), lv((100, 1))))
        self.assertAlmostEqual(b.mid, 100.0)  # state unchanged

    def test_snapshot_flag_resets_diff(self):
        b = BandBook(2.0)
        b.update(lv((99, 1)), lv((101, 1)))
        self.assertIsNone(b.update(lv((99, 50)), lv((101, 50)), snapshot=True))

    def test_depth_bands_and_slippage(self):
        b = BandBook(2.0)
        b.update(lv((99.8, 1), (99.2, 1), (98.5, 1)), lv((100.2, 1), (100.8, 1), (101.5, 1)))
        bd05, ad05, bd1, ad1, bd2, ad2 = b.depths()
        self.assertAlmostEqual(ad05, 100.2)
        self.assertAlmostEqual(ad1, 100.2 + 100.8)
        self.assertAlmostEqual(ad2, 100.2 + 100.8 + 101.5)
        self.assertAlmostEqual(bd1, 99.8 + 99.2)
        s = b.buy_slippage_bps(100.2)
        self.assertAlmostEqual(s, (100.2 / 100.0 - 1) * 1e4, places=6)
        self.assertIsNone(b.buy_slippage_bps(1e9))
        self.assertAlmostEqual(b.spread_bps(), 40.0, places=6)

    def test_histogram_shape(self):
        b = BandBook(2.0)
        b.update(lv((99.9, 1)), lv((100.1, 1)))
        h = b.histogram(10)
        self.assertEqual(len(h["bid"]), 10)
        self.assertGreater(h["ask"][0], 0)


if __name__ == "__main__":
    unittest.main()
