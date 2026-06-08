# -*- coding: utf-8 -*-

import unittest
from unittest.mock import MagicMock, call

from htxbot.fileio import replace_path_with_retry, is_transient_file_replace_error
import os


class TestFileIO(unittest.TestCase):
    def test_replace_path_with_retry_success(self):
        replace_mock = MagicMock()
        sleep_mock = MagicMock()

        replace_path_with_retry(
            "src", "dst", replace_func=replace_mock, sleep_func=sleep_mock
        )

        replace_mock.assert_called_once_with("src", "dst")
        sleep_mock.assert_not_called()

    def test_replace_path_with_retry_transient_error_then_success(self):
        replace_mock = MagicMock()

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise PermissionError("Access denied")

        replace_mock.side_effect = side_effect
        sleep_mock = MagicMock()

        replace_path_with_retry(
            "src",
            "dst",
            attempts=5,
            initial_delay_sec=0.1,
            replace_func=replace_mock,
            sleep_func=sleep_mock,
        )

        self.assertEqual(call_count[0], 3)
        self.assertEqual(replace_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        sleep_mock.assert_has_calls([call(0.1), call(0.2)])

    def test_replace_path_with_retry_exceeds_attempts(self):
        replace_mock = MagicMock()
        replace_mock.side_effect = PermissionError("Access denied")
        sleep_mock = MagicMock()

        with self.assertRaises(PermissionError):
            replace_path_with_retry(
                "src",
                "dst",
                attempts=3,
                initial_delay_sec=0.1,
                replace_func=replace_mock,
                sleep_func=sleep_mock,
            )

        self.assertEqual(replace_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_replace_path_with_retry_non_transient_error(self):
        replace_mock = MagicMock()
        replace_mock.side_effect = ValueError("Some other error")
        sleep_mock = MagicMock()

        with self.assertRaises(ValueError):
            replace_path_with_retry(
                "src", "dst", replace_func=replace_mock, sleep_func=sleep_mock
            )

        replace_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_is_transient_file_replace_error(self):
        self.assertTrue(
            is_transient_file_replace_error(PermissionError("Access denied"))
        )
        self.assertFalse(is_transient_file_replace_error(ValueError("Not an OSError")))

        # Test winerror 5 and 32
        exc5 = OSError()
        exc5.winerror = 5
        self.assertTrue(is_transient_file_replace_error(exc5))

        exc32 = OSError()
        exc32.winerror = 32
        self.assertTrue(is_transient_file_replace_error(exc32))

        # Test non-transient winerror
        exc_other = OSError()
        exc_other.winerror = 123
        self.assertFalse(is_transient_file_replace_error(exc_other))

        # Test windows errno (mocking os.name)
        original_os_name = os.name
        try:
            os.name = "nt"
            import errno

            exc_eacces = OSError()
            exc_eacces.errno = errno.EACCES
            self.assertTrue(is_transient_file_replace_error(exc_eacces))

            exc_eperm = OSError()
            exc_eperm.errno = errno.EPERM
            self.assertTrue(is_transient_file_replace_error(exc_eperm))

            exc_other_errno = OSError()
            exc_other_errno.errno = errno.ENOENT
            self.assertFalse(is_transient_file_replace_error(exc_other_errno))

            os.name = "posix"
            self.assertFalse(is_transient_file_replace_error(exc_eacces))
        finally:
            os.name = original_os_name


if __name__ == "__main__":
    unittest.main()
