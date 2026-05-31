# -*- coding: utf-8 -*-

import unittest
import config
import htxbot.config as package_config
from htxbot.config import CONFIG_WARNINGS, _add_config_warning

class ConfigTests(unittest.TestCase):
    def test_package_config_aliases_root_config(self):
        self.assertIs(package_config.CONFIG_WARNINGS, config.CONFIG_WARNINGS)
        self.assertIs(package_config.resolve_profile("long"), config.resolve_profile("long"))

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
