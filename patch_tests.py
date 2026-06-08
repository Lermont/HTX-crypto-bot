import re

with open('tests/test_unified_bot.py', 'r') as f:
    content = f.read()

new_test = """    def test_btc_hedge_throttle_key_hashes_message(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            combined, exchange = self.make_btc_hedge_combined(Path(raw_tmp))
            bot = combined._hedge_control_bot()
            log_events = []
            def fake_log_event(*args, **kwargs):
                log_events.append(args)

            bot._log_event = fake_log_event

            # First message
            combined._log_btc_hedge("INFO", "First message", "reason1", "event1", throttle_sec=10.0, symbol="BTC/USDT")
            self.assertEqual(len(log_events), 1)

            # Exact same message - should be throttled
            combined._log_btc_hedge("INFO", "First message", "reason1", "event1", throttle_sec=10.0, symbol="BTC/USDT")
            self.assertEqual(len(log_events), 1)

            # Different message but same reason/event - should NOT be throttled because hash is different
            combined._log_btc_hedge("INFO", "Second message", "reason1", "event1", throttle_sec=10.0, symbol="BTC/USDT")
            self.assertEqual(len(log_events), 2)

            # Verify no sensitive info in cache keys
            keys = str(list(combined._btc_hedge_log_at.keys()))
            self.assertNotIn("First message", keys)
            self.assertNotIn("Second message", keys)

"""

if "test_btc_hedge_throttle_key_hashes_message" not in content:
    # Find a good place to insert the test
    target = "def test_btc_hedge_waits_when_btc_open_orders_exist(self):"
    content = content.replace(target, new_test + target)
    with open('tests/test_unified_bot.py', 'w') as f:
        f.write(content)
        print("Test injected")
