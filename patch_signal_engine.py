import re

with open('htxbot/signal_engine.py', 'r') as f:
    content = f.read()

diff1 = """import time
import math
import concurrent.futures
from typing import List, Optional, Tuple

import config"""

old1 = """import time
import math
from typing import List, Optional

import config"""

content = content.replace(old1, diff1)

with open('htxbot/signal_engine.py', 'w') as f:
    f.write(content)
