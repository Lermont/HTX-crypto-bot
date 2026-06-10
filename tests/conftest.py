# -*- coding: utf-8 -*-
"""Test-session bootstrap.

The bot reads the operator's local ``.env`` at ``config`` import time.  Unit
tests must assert documented defaults regardless of local overrides, so the
dotenv layer is disabled before anything imports ``config``.
"""

import os

os.environ["HTXBOT_DISABLE_DOTENV"] = "1"
