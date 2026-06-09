import errno
import unittest
from unittest.mock import MagicMock, call, patch

from htxbot.fileio import (
    replace_path_with_retry,
    is_transient_file_replace_error,
    retry_transient_file_operation,
)
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

    def test_retry_transient_file_operation_success(self):
        operation_mock = MagicMock(return_value="success")
        sleep_mock = MagicMock()

        result = retry_transient_file_operation(operation_mock, sleep_func=sleep_mock)

        self.assertEqual(result, "success")
        operation_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_retry_transient_file_operation_transient_error_then_success(self):
        operation_mock = MagicMock()
        call_count = [0]

        def side_effect():
            call_count[0] += 1
            if call_count[0] < 3:
                raise PermissionError("Access denied")
            return "success"

        operation_mock.side_effect = side_effect
        sleep_mock = MagicMock()

        result = retry_transient_file_operation(
            operation_mock,
            attempts=5,
            initial_delay_sec=0.1,
            max_delay_sec=0.5,
            sleep_func=sleep_mock,
        )

        self.assertEqual(result, "success")
        self.assertEqual(operation_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        sleep_mock.assert_has_calls([call(0.1), call(0.2)])

    def test_retry_transient_file_operation_transient_error_max_delay(self):
        operation_mock = MagicMock()
        call_count = [0]

        def side_effect():
            call_count[0] += 1
            if call_count[0] < 4:
                raise PermissionError("Access denied")
            return "success"

        operation_mock.side_effect = side_effect
        sleep_mock = MagicMock()

        result = retry_transient_file_operation(
            operation_mock,
            attempts=5,
            initial_delay_sec=0.1,
            max_delay_sec=0.15,
            sleep_func=sleep_mock,
        )

        self.assertEqual(result, "success")
        self.assertEqual(operation_mock.call_count, 4)
        self.assertEqual(sleep_mock.call_count, 3)
        sleep_mock.assert_has_calls([call(0.1), call(0.15), call(0.15)])

    def test_retry_transient_file_operation_exceeds_attempts(self):
        operation_mock = MagicMock(side_effect=PermissionError("Access denied"))
        sleep_mock = MagicMock()

        with self.assertRaises(PermissionError):
            retry_transient_file_operation(
                operation_mock, attempts=3, initial_delay_sec=0.1, sleep_func=sleep_mock
            )

        self.assertEqual(operation_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_retry_transient_file_operation_non_transient_error(self):
        operation_mock = MagicMock(side_effect=ValueError("Not transient"))
        sleep_mock = MagicMock()

        with self.assertRaises(ValueError):
            retry_transient_file_operation(operation_mock, sleep_func=sleep_mock)

        operation_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_retry_transient_file_operation_parameter_clamping(self):
        operation_mock = MagicMock()
        call_count = [0]

        def side_effect():
            call_count[0] += 1
            if call_count[0] < 2:
                raise PermissionError("Access denied")
            return "success"

        operation_mock.side_effect = side_effect
        sleep_mock = MagicMock()

        # attempts=0 -> max(1, 0) -> 1 -> will fail on first error
        operation_mock.side_effect = PermissionError("Access denied")
        with self.assertRaises(PermissionError):
            retry_transient_file_operation(
                operation_mock,
                attempts=0,  # Should clamp to 1
                sleep_func=sleep_mock,
            )
        self.assertEqual(operation_mock.call_count, 1)

        operation_mock.reset_mock()
        sleep_mock.reset_mock()
        call_count[0] = 0
        operation_mock.side_effect = side_effect

        # negative delays -> clamped to 0.0
        result = retry_transient_file_operation(
            operation_mock,
            attempts=3,
            initial_delay_sec=-1.0,
            max_delay_sec=-2.0,
            sleep_func=sleep_mock,
        )
        self.assertEqual(result, "success")
        self.assertEqual(operation_mock.call_count, 2)
        # Negative delays are clamped to 0.0, so the delay check `if delay > 0:` fails and sleep is never called.
        sleep_mock.assert_not_called()

    def test_retry_transient_file_operation_max_delay_less_than_initial(self):
        operation_mock = MagicMock()
        call_count = [0]

        def side_effect():
            call_count[0] += 1
            if call_count[0] < 2:
                raise PermissionError("Access denied")
            return "success"

        operation_mock.side_effect = side_effect
        sleep_mock = MagicMock()

        result = retry_transient_file_operation(
            operation_mock,
            attempts=3,
            initial_delay_sec=0.5,
            max_delay_sec=0.1,  # max < initial, should clamp max to initial (0.5)
            sleep_func=sleep_mock,
        )

        self.assertEqual(result, "success")
        self.assertEqual(operation_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
