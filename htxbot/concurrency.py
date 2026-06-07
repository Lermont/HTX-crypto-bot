# -*- coding: utf-8 -*-

import threading
from typing import Any


def instance_rlock(owner: Any, attr_name: str) -> threading.RLock:
    lock = getattr(owner, attr_name, None)
    if lock is None:
        lock = threading.RLock()
        try:
            setattr(owner, attr_name, lock)
        except Exception:
            return threading.RLock()
    return lock


def ensure_runtime_locks(owner: Any) -> None:
    for attr_name in (
        "_state_lock",
        "_cache_lock",
        "_monitoring_lock",
        "_account_pnl_lock",
        "_exchange_host_lock",
        "_market_data_cache_lock",
    ):
        instance_rlock(owner, attr_name)


__all__ = ["ensure_runtime_locks", "instance_rlock"]
