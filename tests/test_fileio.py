# -*- coding: utf-8 -*-

import errno
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from htxbot.fileio import write_text_path_with_retry


class TestFileIO(unittest.TestCase):
    def test_write_text_path_with_retry_success(self):
        write_mock = MagicMock(return_value=42)
        sleep_mock = MagicMock()
        path = Path("/fake/path")

        result = write_text_path_with_retry(
            path,
            "test data",
            encoding="utf-8",
            write_func=write_mock,
            sleep_func=sleep_mock
        )

        self.assertEqual(result, 42)
        write_mock.assert_called_once_with(path, "test data", encoding="utf-8")
        sleep_mock.assert_not_called()

    def test_write_text_path_with_retry_transient_error_recovers(self):
        err = PermissionError("lock")
        write_mock = MagicMock(side_effect=[err, err, 42])
        sleep_mock = MagicMock()
        path = Path("/fake/path")

        result = write_text_path_with_retry(
            path,
            "test data",
            attempts=5,
            initial_delay_sec=0.1,
            write_func=write_mock,
            sleep_func=sleep_mock
        )

        self.assertEqual(result, 42)
        self.assertEqual(write_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        # Sleep delays: 0.1, 0.2
        self.assertEqual(sleep_mock.call_args_list[0][0][0], 0.1)
        self.assertEqual(sleep_mock.call_args_list[1][0][0], 0.2)

    def test_write_text_path_with_retry_transient_error_exhausted(self):
        err = PermissionError("lock")
        write_mock = MagicMock(side_effect=err)
        sleep_mock = MagicMock()
        path = Path("/fake/path")

        with self.assertRaises(PermissionError):
            write_text_path_with_retry(
                path,
                "test data",
                attempts=3,
                initial_delay_sec=0.1,
                write_func=write_mock,
                sleep_func=sleep_mock
            )

        self.assertEqual(write_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_write_text_path_with_retry_non_transient_error(self):
        err = FileNotFoundError("not found")
        write_mock = MagicMock(side_effect=err)
        sleep_mock = MagicMock()
        path = Path("/fake/path")

        with self.assertRaises(FileNotFoundError):
            write_text_path_with_retry(
                path,
                "test data",
                attempts=5,
                write_func=write_mock,
                sleep_func=sleep_mock
            )

        self.assertEqual(write_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_write_text_path_with_retry_windows_lock_error(self):
        err = OSError("windows lock")
        err.winerror = 32
        write_mock = MagicMock(side_effect=[err, 100])
        sleep_mock = MagicMock()
        path = Path("/fake/path")

        result = write_text_path_with_retry(
            path,
            "test data",
            attempts=3,
            initial_delay_sec=0.1,
            write_func=write_mock,
            sleep_func=sleep_mock
        )

        self.assertEqual(result, 100)
        self.assertEqual(write_mock.call_count, 2)
        self.assertEqual(sleep_mock.call_count, 1)

if __name__ == "__main__":
    unittest.main()
