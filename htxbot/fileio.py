# -*- coding: utf-8 -*-

import errno
import os
from pathlib import Path
import time
from typing import Callable, Optional, TypeVar


_WINDOWS_FILE_LOCK_WINERRORS = {5, 32}
_WINDOWS_FILE_LOCK_ERRNOS = {errno.EACCES, errno.EPERM}
_T = TypeVar("_T")


def is_transient_file_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in _WINDOWS_FILE_LOCK_WINERRORS:
        return True
    return os.name == "nt" and getattr(exc, "errno", None) in _WINDOWS_FILE_LOCK_ERRNOS


def is_transient_file_replace_error(exc: BaseException) -> bool:
    return is_transient_file_lock_error(exc)


def retry_transient_file_operation(
    operation: Callable[[], _T],
    *,
    attempts: int = 30,
    initial_delay_sec: float = 0.1,
    max_delay_sec: float = 0.5,
    sleep_func: Optional[Callable[[float], None]] = None,
) -> _T:
    attempts = max(1, int(attempts))
    initial_delay_sec = max(0.0, float(initial_delay_sec))
    max_delay_sec = max(0.0, float(max_delay_sec))
    if max_delay_sec < initial_delay_sec:
        max_delay_sec = initial_delay_sec
    sleep = sleep_func or time.sleep

    for attempt in range(attempts):
        try:
            return operation()
        except OSError as exc:
            if not is_transient_file_lock_error(exc) or attempt + 1 >= attempts:
                raise
            delay = min(initial_delay_sec * (2 ** attempt), max_delay_sec)
            if delay > 0:
                sleep(delay)

    raise RuntimeError("unreachable retry state")


def replace_path_with_retry(
    src,
    dst,
    *,
    attempts: int = 30,
    initial_delay_sec: float = 0.1,
    max_delay_sec: float = 0.5,
    replace_func: Optional[Callable] = None,
    sleep_func: Optional[Callable[[float], None]] = None,
) -> None:
    replace = replace_func or os.replace

    def operation() -> None:
        replace(src, dst)

    retry_transient_file_operation(
        operation,
        attempts=attempts,
        initial_delay_sec=initial_delay_sec,
        max_delay_sec=max_delay_sec,
        sleep_func=sleep_func,
    )


def write_text_path_with_retry(
    path: Path,
    data: str,
    *,
    encoding: str = "utf-8",
    attempts: int = 30,
    initial_delay_sec: float = 0.1,
    max_delay_sec: float = 0.5,
    write_func: Optional[Callable] = None,
    sleep_func: Optional[Callable[[float], None]] = None,
) -> int:
    writer = write_func or Path.write_text

    def operation() -> int:
        return writer(path, data, encoding=encoding)

    return retry_transient_file_operation(
        operation,
        attempts=attempts,
        initial_delay_sec=initial_delay_sec,
        max_delay_sec=max_delay_sec,
        sleep_func=sleep_func,
    )


__all__ = [
    "is_transient_file_lock_error",
    "is_transient_file_replace_error",
    "replace_path_with_retry",
    "retry_transient_file_operation",
    "write_text_path_with_retry",
]
