"""Embedded (Zephyr) coverage OttoSuite — SKELETON, not implemented.

Will mirror ``tests/repo1/tests/test_coverage_product.py`` (``TestCoverageProduct``):
exercise each LLEXT operation over the console on the embedded host(s), trigger and
capture the ``cov_dump`` during ``otto test --cov``, and run one branch (e.g.
divide-by-zero / clamp) on a single instance so *merged* coverage across instances
exceeds any single instance — proving cross-host merge works for embedded too.

Deferred until the LLEXT product, the ``EmbeddedGcdaCollector``
(``src/otto/coverage/fetcher/embedded.py``), and the console-dump decoder exist and
the feasibility gate has passed. See ``../../../todo/embedded_coverage.md``
("The three pieces to build", part 3) and the referenced repo1 suite.

This module intentionally defines no tests yet.
"""
