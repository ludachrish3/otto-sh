"""pytest configuration for repo5's test suite.

When ``otto test`` (e.g. ``otto test TestKmodDemo``) runs the tests, otto has
already called ``Repo.add_libs_to_pythonpath()`` at startup, which adds
``repo5/pylib`` to ``sys.path`` before pytest collects anything.

When pytest collects tests directly (e.g. ``pytest tests/repo5/``), that
initialization is skipped.  This conftest bridges the gap by adding the
same directory at collection time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "pylib"))
