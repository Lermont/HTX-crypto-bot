# -*- coding: utf-8 -*-

import os
import unittest
from contextlib import contextmanager
from dataclasses import FrozenInstanceError

import config
import htxbot.config as package_config
from htxbot.config import CONFIG_WARNINGS, _add_config_warning
from tests.config_overrides import override_frozen_config_fields


EXPECTED_PRIMARY_COINS = (
    "aave", "ada", "algo", "apt", "arb", "atom", "avax", "bch", "bnb", "bonk",
    "btc", "cake", "comp", "doge", "dot", "ena", "etc", "eth", "hbar", "htx",
    "hype", "icp", "inj", "jup", "kas", "ldo", "link", "ltc", "near", "ondo",
    "orca", "pendle", "pengu", "people", "pepe", "pol", "sei", "shib", "sol", "ssv",
    "sui", "sushi", "ton", "trx", "uni", "xaut", "xlm", "xmr", "xrp", "zec",
)
EXPECTED_SECONDARY_COINS = ("1inch", "aixbt", "akt")


@contextmanager
def temporary_env(**updates):
    sentinel = object()
    previous = {name: os.environ.get(name, sentinel) for name in updates}
    for name, value in updates.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is sentinel:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ConfigTests(unittest.TestCase):
    def test_package_config_aliases_root_config(self):
        self.assertIs(package_config.CONFIG_WARNINGS, config.CONFIG_WARNINGS)
        self.assertIs(package_config.resolve_profile("long"), config.resolve_profile("long"))

    def test_btc_hedge_disabled_by_default(self):
        with temporary_env(BTC_HEDGE_ENABLED=None, HTXBOT_BTC_HEDGE_ENABLED=None):
            hedge = config._make_hedge_settings()
            self.assertFalse(hedge.btc_hedge_enabled)
            self.assertEqual(hedge.btc_hedge_max_spread_bps, 30.0)

    def test_btc_hedge_can_be_enabled_by_env(self):
        with temporary_env(BTC_HEDGE_ENABLED="true", HTXBOT_BTC_HEDGE_ENABLED=None):
            self.assertTrue(config._make_hedge_settings().btc_hedge_enabled)

    def test_short_profile_markets_cache_filename_matches_profile(self):
        cache_path = config.resolve_profile("short").runtime.markets_cache_file.replace("\\", "/")

        self.assertTrue(cache_path.endswith("/short/bot_futures_short_markets_cache.json"))
        self.assertNotIn("short_state_markets_cache", cache_path)

    def test_coin_universe_can_be_empty_when_env_is_not_set(self):
        with temporary_env(
            COINS=None,
            HTX_COINS=None,
            COINS_2=None,
            HTX_COINS_2=None,
            LONG_COINS=None,
            HTXBOT_LONG_COINS=None,
            LONG_COINS_2=None,
            HTXBOT_LONG_COINS_2=None,
        ):
            profile = config._make_profile("long", "long", ())

        self.assertEqual(profile.coins, ())

    def test_profile_reads_coin_universe_and_api_accounts_from_env(self):
        with temporary_env(
            COINS=",".join(EXPECTED_PRIMARY_COINS),
            COINS_2=",".join(EXPECTED_SECONDARY_COINS),
            HTX_API_KEY="primary_key",
            HTX_API_SECRET="primary_secret",
            HTX_API_KEY_2="secondary_key",
            HTX_API_SECRET_2="secondary_secret",
        ):
            profile = config._make_profile("long", "long", ())

        self.assertEqual(profile.coins, EXPECTED_PRIMARY_COINS + EXPECTED_SECONDARY_COINS)
        self.assertEqual(profile.api_accounts[0].name, "primary")
        self.assertEqual(profile.api_accounts[0].coins, EXPECTED_PRIMARY_COINS)
        self.assertEqual(profile.api_accounts[1].name, "secondary")
        self.assertEqual(profile.api_accounts[1].coins, EXPECTED_SECONDARY_COINS)
        self.assertEqual(profile.api_accounts[1].api_credentials.api_key, "secondary_key")

    def test_duplicate_coin_across_api_accounts_is_rejected(self):
        with temporary_env(COINS="doge,ada", COINS_2="1inch,doge"):
            with self.assertRaisesRegex(ValueError, "assigned to multiple HTX API accounts"):
                config._make_profile("long", "long", ())

    def test_market_data_max_workers_is_profile_runtime_setting(self):
        with temporary_env(
            MARKET_DATA_MAX_WORKERS="3",
            HTXBOT_MARKET_DATA_MAX_WORKERS=None,
            LONG_MARKET_DATA_MAX_WORKERS=None,
            HTXBOT_LONG_MARKET_DATA_MAX_WORKERS=None,
        ):
            profile = config._make_profile("long", "long", ("test",))

        self.assertEqual(profile.runtime.market_data_max_workers, 3)

    def test_frozen_runtime_rejects_direct_field_assignment(self):
        with self.assertRaises(FrozenInstanceError):
            config.RUNTIME.poll_interval_sec = config.RUNTIME.poll_interval_sec + 1

    def test_override_frozen_config_fields_temporarily_bypasses_dataclass_lock(self):
        original_poll_interval = config.RUNTIME.poll_interval_sec
        original_leverage = config.RISK.leverage

        with override_frozen_config_fields(config.RUNTIME, poll_interval_sec=original_poll_interval + 7):
            self.assertEqual(config.RUNTIME.poll_interval_sec, original_poll_interval + 7)

        self.assertEqual(config.RUNTIME.poll_interval_sec, original_poll_interval)

        with self.assertRaises(RuntimeError):
            with override_frozen_config_fields(config.RISK, leverage=original_leverage + 2):
                self.assertEqual(config.RISK.leverage, original_leverage + 2)
                raise RuntimeError("force restore")

        self.assertEqual(config.RISK.leverage, original_leverage)

    def test_override_frozen_config_fields_rejects_unknown_field(self):
        with self.assertRaises(AttributeError):
            with override_frozen_config_fields(config.RUNTIME, does_not_exist=True):
                pass

    def test_ema_max_averaging_stages_is_capped(self):
        initial_warnings = list(config.CONFIG_WARNINGS)
        with temporary_env(
            EMA_MAX_AVERAGING_STAGES="4",
            HTXBOT_EMA_MAX_AVERAGING_STAGES=None,
            LONG_EMA_MAX_AVERAGING_STAGES=None,
            HTXBOT_LONG_EMA_MAX_AVERAGING_STAGES=None,
        ):
            try:
                profile = config._make_profile("long", "long", ("test",))
            finally:
                config.CONFIG_WARNINGS[:] = initial_warnings

        self.assertEqual(profile.strategy.ema_max_averaging_stages, 2)
        self.assertEqual(len(profile.strategy.averaging_drawdown_steps), 2)

    def test_add_config_warning(self):
        # Record initial length to avoid side effects from other tests
        initial_len = len(CONFIG_WARNINGS)

        test_message = "This is a test warning message."
        _add_config_warning(test_message)

        self.assertIn(test_message, CONFIG_WARNINGS)
        self.assertEqual(len(CONFIG_WARNINGS), initial_len + 1)

        # Cleanup so we don't affect other tests (or future tests)
        if test_message in CONFIG_WARNINGS:
            CONFIG_WARNINGS.remove(test_message)
