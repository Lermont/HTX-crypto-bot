import errno
import unittest
from unittest.mock import MagicMock, call, patch

from htxbot.fileio import is_transient_file_lock_error, replace_path_with_retry


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
            "src", "dst",
            attempts=5,
            initial_delay_sec=0.1,
            replace_func=replace_mock,
            sleep_func=sleep_mock
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
                "src", "dst",
                attempts=3,
                initial_delay_sec=0.1,
                replace_func=replace_mock,
                sleep_func=sleep_mock
            )

        self.assertEqual(replace_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_replace_path_with_retry_non_transient_error(self):
        replace_mock = MagicMock()
        replace_mock.side_effect = ValueError("Some other error")
        sleep_mock = MagicMock()

        with self.assertRaises(ValueError):
            replace_path_with_retry(
                "src", "dst",
                replace_func=replace_mock,
                sleep_func=sleep_mock
            )

        replace_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_is_transient_file_lock_error(self):
        # Non-OSError should return False
        self.assertFalse(is_transient_file_lock_error(ValueError("Not an OSError")))

        # PermissionError should return True
        self.assertTrue(is_transient_file_lock_error(PermissionError("Access denied")))

        # OSError with winerror 5 or 32 should return True
        exc = OSError("Access denied")
        exc.winerror = 5
        self.assertTrue(is_transient_file_lock_error(exc))

        exc = OSError("Sharing violation")
        exc.winerror = 32
        self.assertTrue(is_transient_file_lock_error(exc))

        # OSError with other winerror should return False if not nt or not in errnos
        exc = OSError("Some other error")
        exc.winerror = 123
        self.assertFalse(is_transient_file_lock_error(exc))

        # OSError with specific errno on 'nt' should return True
        exc = OSError("Access denied")
        exc.errno = errno.EACCES
        with patch('htxbot.fileio.os.name', 'nt'):
            self.assertTrue(is_transient_file_lock_error(exc))

        # OSError with specific errno on 'posix' should return False
        exc = OSError("Access denied")
        exc.errno = errno.EACCES
        with patch('htxbot.fileio.os.name', 'posix'):
            self.assertFalse(is_transient_file_lock_error(exc))

if __name__ == "__main__":
    unittest.main()
