import unittest
from unittest.mock import Mock, call

from htxbot.fileio import replace_path_with_retry

class TestReplacePathWithRetry(unittest.TestCase):
    def test_success_first_try(self):
        replace_mock = Mock()
        sleep_mock = Mock()

        replace_path_with_retry(
            "src.txt", "dst.txt",
            replace_func=replace_mock,
            sleep_func=sleep_mock,
            attempts=3
        )

        replace_mock.assert_called_once_with("src.txt", "dst.txt")
        sleep_mock.assert_not_called()

    def test_retry_transient_error_then_success(self):
        replace_mock = Mock()

        # Windows PermissionError is a typical transient error
        transient_error = PermissionError("Access denied")

        replace_mock.side_effect = [transient_error, transient_error, None]
        sleep_mock = Mock()

        replace_path_with_retry(
            "src.txt", "dst.txt",
            replace_func=replace_mock,
            sleep_func=sleep_mock,
            attempts=5,
            initial_delay_sec=0.1
        )

        self.assertEqual(replace_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

        # Check sleep durations (exponential backoff)
        # Attempt 0 fails: delay = min(0.1 * 2^0, 0.5) = 0.1
        # Attempt 1 fails: delay = min(0.1 * 2^1, 0.5) = 0.2
        sleep_mock.assert_has_calls([call(0.1), call(0.2)])

    def test_fail_immediately_on_non_transient_error(self):
        replace_mock = Mock()

        # FileNotFoundError is NOT a transient error
        non_transient_error = FileNotFoundError("File not found")

        replace_mock.side_effect = non_transient_error
        sleep_mock = Mock()

        with self.assertRaises(FileNotFoundError):
            replace_path_with_retry(
                "src.txt", "dst.txt",
                replace_func=replace_mock,
                sleep_func=sleep_mock,
                attempts=5
            )

        replace_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_exhaust_attempts(self):
        replace_mock = Mock()

        transient_error = PermissionError("Access denied")
        replace_mock.side_effect = transient_error
        sleep_mock = Mock()

        with self.assertRaises(PermissionError):
            replace_path_with_retry(
                "src.txt", "dst.txt",
                replace_func=replace_mock,
                sleep_func=sleep_mock,
                attempts=3,
                initial_delay_sec=0.1
            )

        self.assertEqual(replace_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

if __name__ == "__main__":
    unittest.main()
