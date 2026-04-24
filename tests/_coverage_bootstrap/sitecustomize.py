"""Subprocess coverage bootstrap.

Prepended to ``PYTHONPATH`` by tests that spawn ``otto`` as a subprocess and
want the child's line execution captured by coverage.py. Relies on the env
var ``COVERAGE_PROCESS_START`` (set by the test) to point at the project's
``.coveragerc`` — if that's unset, ``coverage.process_startup()`` is a no-op
and this file adds only a cheap ``import coverage``.

Kept out of a global ``.pth`` file intentionally: enabling subprocess coverage
project-wide deadlocks ``test_coverage_e2e.py`` because every asyncssh
subprocess would also try to start coverage.
"""
import coverage
coverage.process_startup()
