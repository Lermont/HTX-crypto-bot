import unittest
from htxbot.indicators import clamp

class TestClamp(unittest.TestCase):
    def test_clamp_below_lower(self):
        self.assertEqual(clamp(5.0, 10.0, 20.0), 10.0)

    def test_clamp_within_bounds(self):
        self.assertEqual(clamp(15.0, 10.0, 20.0), 15.0)

    def test_clamp_above_upper(self):
        self.assertEqual(clamp(25.0, 10.0, 20.0), 20.0)

    def test_clamp_at_lower_bound(self):
        self.assertEqual(clamp(10.0, 10.0, 20.0), 10.0)

    def test_clamp_at_upper_bound(self):
        self.assertEqual(clamp(20.0, 10.0, 20.0), 20.0)

if __name__ == '__main__':
    unittest.main()
