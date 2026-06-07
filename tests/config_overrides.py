# -*- coding: utf-8 -*-
"""Shared helpers for test-only config overrides.

Production config sections are frozen dataclasses on purpose.  Tests that need
to exercise alternate values should use these context managers so overrides are
restored even when assertions fail.
"""

from contextlib import contextmanager

import config


@contextmanager
def override_config(**values):
    sentinel = object()
    previous = {name: config.__dict__.get(name, sentinel) for name in values}
    for name, value in values.items():
        setattr(config, name, value)
    try:
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is sentinel:
                delattr(config, name)
            else:
                setattr(config, name, old_value)


@contextmanager
def override_frozen_config_fields(settings, **updates):
    params = getattr(type(settings), "__dataclass_params__", None)
    if params is None or not getattr(params, "frozen", False):
        raise TypeError("settings must be a frozen dataclass instance")

    missing = tuple(name for name in updates if not hasattr(settings, name))
    if missing:
        raise AttributeError(f"Unknown config field(s): {', '.join(missing)}")

    previous = {name: getattr(settings, name) for name in updates}
    for name, value in updates.items():
        object.__setattr__(settings, name, value)
    try:
        yield settings
    finally:
        for name, old_value in previous.items():
            object.__setattr__(settings, name, old_value)
