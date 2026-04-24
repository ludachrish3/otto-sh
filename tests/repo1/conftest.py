"""pytest configuration for repo1's test suite.

When tests are collected via ``otto test TestDevice``, otto calls
``Repo.addLibsToPythonpath()`` which adds ``repo1/pylib`` to ``sys.path``
before importing test files.

When pytest collects tests directly (e.g. ``pytest tests/repo1/``), that
initialization is skipped.  This conftest bridges the gap by adding the
same directory at collection time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'pylib'))
