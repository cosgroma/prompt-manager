"""Test suite for PromptManager.

Importing this package points the application's data directory at a throwaway
location, so tests never touch ~/.promptmanager, and makes the ``src`` layout
importable when the package has not been pip-installed.
"""

import atexit
import os
import shutil
import sys
import tempfile

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_TEST_HOME = tempfile.mkdtemp(prefix="promptmanager-tests-")
os.environ["PROMPT_MANAGER_HOME"] = _TEST_HOME
for _var in (
    "PROMPT_MANAGER_DB",
    "PROMPT_MANAGER_OUTPUT_DIRS",
    "PROMPT_MANAGER_MONITORING",
):
    os.environ.pop(_var, None)

atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)
