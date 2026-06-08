🧹 Replace empty except OSError handlers with log warnings

🎯 **What:** Replaced empty `except OSError: pass` exception handlers with proper error logging using the class logger in `htxbot/state.py`.
💡 **Why:** Silently swallowing errors with `pass` is an anti-pattern. While an `OSError` during lock cleanup or state saving may be non-fatal, hiding it makes it very difficult to diagnose intermittent file lock or permission issues. Logging these exceptions as warnings significantly improves codebase observability and maintainability.
✅ **Verification:**
- Ran `ruff check htxbot/state.py --fix` and `ruff format htxbot/state.py`.
- Checked `PYTHONPATH=. python3 -m pytest tests/` to verify that functionality isn't broken. (Note: Existent TypeError in signal_engine is a baseline failure not introduced by this change).
- Checked the patched source to ensure correct formatting and implementation.
✨ **Result:** Enhanced traceability of file manipulation issues in state handling without altering expected runtime behavior.
