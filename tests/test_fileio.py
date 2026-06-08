import unittest
from unittest.mock import MagicMock, call
import os

from htxbot.fileio import replace_path_with_retry

class TestReplacePathWithRetry(unittest.TestCase):
    def test_replace_path_with_retry_success(self):
        replace_func = MagicMock()
        sleep_func = MagicMock()

        replace_path_with_retry(
            "src", "dst",
            replace_func=replace_func,
            sleep_func=sleep_func
        )

        replace_func.assert_called_once_with("src", "dst")
        sleep_func.assert_not_called()

    def test_replace_path_with_retry_transient_error(self):
        replace_func = MagicMock(side_effect=[PermissionError("access denied"), None])
        sleep_func = MagicMock()

        replace_path_with_retry(
            "src", "dst",
            attempts=3,
            initial_delay_sec=0.1,
            replace_func=replace_func,
            sleep_func=sleep_func
        )

        self.assertEqual(replace_func.call_count, 2)
        replace_func.assert_has_calls([call("src", "dst"), call("src", "dst")])
        sleep_func.assert_called_once_with(0.1)

    def test_replace_path_with_retry_max_attempts(self):
        replace_func = MagicMock(side_effect=PermissionError("access denied"))
        sleep_func = MagicMock()

        with self.assertRaises(PermissionError):
            replace_path_with_retry(
                "src", "dst",
                attempts=3,
                initial_delay_sec=0.1,
                replace_func=replace_func,
                sleep_func=sleep_func
            )

        self.assertEqual(replace_func.call_count, 3)
        self.assertEqual(sleep_func.call_count, 2)

    def test_replace_path_with_retry_non_transient_error(self):
        replace_func = MagicMock(side_effect=FileNotFoundError("not found"))
        sleep_func = MagicMock()

        with self.assertRaises(FileNotFoundError):
            replace_path_with_retry(
                "src", "dst",
                attempts=3,
                replace_func=replace_func,
                sleep_func=sleep_func
            )

        self.assertEqual(replace_func.call_count, 1)
        sleep_func.assert_not_called()

if __name__ == '__main__':
    unittest.main()
