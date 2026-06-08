# -*- coding: utf-8 -*-

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from htxbot.fileio import write_text_path_with_retry


class TestWriteTextPathWithRetry(unittest.TestCase):
    def test_happy_path(self):
        mock_write = MagicMock(return_value=42)
        mock_sleep = MagicMock()
        path = Path("test.txt")

        result = write_text_path_with_retry(
            path, "test data", write_func=mock_write, sleep_func=mock_sleep
        )

        self.assertEqual(result, 42)
        mock_write.assert_called_once_with(path, "test data", encoding="utf-8")
        mock_sleep.assert_not_called()

    def test_transient_failure_recovery(self):
        mock_write = MagicMock()

        err = PermissionError("Access denied")
        mock_write.side_effect = [err, err, 42]

        mock_sleep = MagicMock()
        path = Path("test.txt")

        result = write_text_path_with_retry(
            path,
            "test data",
            attempts=5,
            initial_delay_sec=0.1,
            max_delay_sec=0.5,
            write_func=mock_write,
            sleep_func=mock_sleep,
        )

        self.assertEqual(result, 42)
        self.assertEqual(mock_write.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

        mock_sleep.assert_any_call(0.1)
        mock_sleep.assert_any_call(0.2)

    def test_transient_failure_exhausted(self):
        mock_write = MagicMock()

        err = PermissionError("Access denied")
        mock_write.side_effect = err

        mock_sleep = MagicMock()
        path = Path("test.txt")

        with self.assertRaises(PermissionError):
            write_text_path_with_retry(
                path,
                "test data",
                attempts=3,
                initial_delay_sec=0.1,
                max_delay_sec=0.5,
                write_func=mock_write,
                sleep_func=mock_sleep,
            )

        self.assertEqual(mock_write.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_non_transient_failure(self):
        mock_write = MagicMock()

        err = FileNotFoundError("No such file or directory")
        mock_write.side_effect = err

        mock_sleep = MagicMock()
        path = Path("test.txt")

        with self.assertRaises(FileNotFoundError):
            write_text_path_with_retry(
                path,
                "test data",
                attempts=3,
                write_func=mock_write,
                sleep_func=mock_sleep,
            )

        self.assertEqual(mock_write.call_count, 1)
        mock_sleep.assert_not_called()

    def test_real_file_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test_real.txt"

            result = write_text_path_with_retry(path, "hello world", encoding="utf-8")

            self.assertEqual(result, len("hello world"))
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "hello world")


if __name__ == "__main__":
    unittest.main()
