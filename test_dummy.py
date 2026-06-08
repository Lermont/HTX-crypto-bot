import tempfile
import hashlib

def generate_test():
    with open('tests/test_unified_bot.py', 'a') as f:
        f.write('''
    def test_btc_hedge_throttle_key_hashes_message(self):
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

                combined._log_btc_hedge("INFO", "First message", "reason1", "event1", throttle_sec=10.0, symbol="BTC/USDT")
                self.assertEqual(len(log_events), 1)

                combined._log_btc_hedge("INFO", "First message", "reason1", "event1", throttle_sec=10.0, symbol="BTC/USDT")
                self.assertEqual(len(log_events), 1)

                combined._log_btc_hedge("INFO", "Second message", "reason1", "event1", throttle_sec=10.0, symbol="BTC/USDT")
                self.assertEqual(len(log_events), 2)

                keys = str(list(combined._btc_hedge_log_at.keys()))
                self.assertNotIn("First message", keys)
                self.assertNotIn("Second message", keys)
''')
