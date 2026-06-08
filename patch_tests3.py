import re

with open('tests/test_unified_bot.py', 'r') as f:
    content = f.read()

# I need to correctly replace the test to have 4-space indentation for the method
# First, let's fix it by deleting the broken block and re-inserting it properly

# We'll just reset the file from git and re-inject carefully
import subprocess
subprocess.run(['git', 'checkout', 'tests/test_unified_bot.py'])

with open('tests/test_unified_bot.py', 'r') as f:
    content = f.read()

new_test = """    def test_btc_hedge_throttle_key_hashes_message(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            hedge = replace(
                config.HEDGE,
                btc_hedge_enabled=True,
                btc_hedge_min_rebalance_notional=1.0,
                btc_hedge_cooldown_sec=0.0,
            )
            with override_config(HEDGE=hedge):
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

target = "    def test_btc_hedge_waits_when_btc_open_orders_exist(self):"
content = content.replace(target, new_test + target)
with open('tests/test_unified_bot.py', 'w') as f:
    f.write(content)
