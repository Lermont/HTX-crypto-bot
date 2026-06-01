# -*- coding: utf-8 -*-

import errno
import os
import time
from typing import Callable, Optional


_WINDOWS_FILE_LOCK_WINERRORS = {5, 32}
_WINDOWS_FILE_LOCK_ERRNOS = {errno.EACCES, errno.EPERM}


def is_transient_file_replace_error(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in _WINDOWS_FILE_LOCK_WINERRORS:
        return True
    return os.name == "nt" and getattr(exc, "errno", None) in _WINDOWS_FILE_LOCK_ERRNOS


def replace_path_with_retry(
    src,
    dst,
    *,
    attempts: int = 30,
    initial_delay_sec: float = 0.05,
    max_delay_sec: float = 0.5,
    replace_func: Optional[Callable] = None,
    sleep_func: Optional[Callable[[float], None]] = None,
) -> None:
    attempts = max(1, int(attempts))
    initial_delay_sec = max(0.0, float(initial_delay_sec))
    max_delay_sec = max(initial_delay_sec, float(max_delay_sec))
    replace = replace_func or os.replace
    sleep = sleep_func or time.sleep

    for attempt in range(attempts):
        try:
            replace(src, dst)
            return
        except OSError as exc:
            if not is_transient_file_replace_error(exc) or attempt + 1 >= attempts:
                raise
            delay = min(initial_delay_sec * (attempt + 1), max_delay_sec)
            if delay > 0:
                sleep(delay)


__all__ = ["is_transient_file_replace_error", "replace_path_with_retry"]
