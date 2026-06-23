"""Fixtures for coverage unit tests.

These tests require:
- ``gcc`` and ``lcov`` installed on the dev VM
"""

from tests._fixtures.paths import ensure_sut_dirs

# Must be set before any otto imports -- configmodule reads OTTO_SUT_DIRS at
# import time to compute the module-level _repos singleton.
ensure_sut_dirs()
