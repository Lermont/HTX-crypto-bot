# -*- coding: utf-8 -*-

import os
import unittest
from contextlib import contextmanager

import config
import htxbot.config as package_config
from htxbot.config import CONFIG_WARNINGS, _add_config_warning


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

    def test_btc_hedge_enabled_by_default(self):
        with temporary_env(BTC_HEDGE_ENABLED=None, HTXBOT_BTC_HEDGE_ENABLED=None):
            hedge = config._make_hedge_settings()
            self.assertTrue(hedge.btc_hedge_enabled)
            self.assertEqual(hedge.btc_hedge_max_spread_bps, 30.0)

    def test_btc_hedge_can_be_disabled_by_env(self):
        with temporary_env(BTC_HEDGE_ENABLED="false", HTXBOT_BTC_HEDGE_ENABLED=None):
            self.assertFalse(config._make_hedge_settings().btc_hedge_enabled)

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
