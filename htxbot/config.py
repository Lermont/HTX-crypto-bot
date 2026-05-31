# -*- coding: utf-8 -*-
"""Compatibility wrapper for the project-level config module.

The bot imports ``config`` from the repository root.  Keeping a second full
copy under ``htxbot`` lets profile defaults drift, so this module deliberately
re-exports the root module instead.
"""

from importlib import import_module

_root_config = import_module("config")


def __getattr__(name):
    return getattr(_root_config, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_root_config)))


for _name in dir(_root_config):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_root_config, _name)
