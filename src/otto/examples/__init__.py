"""Reference backend implementations for otto's pluggable interfaces.

Small, dependency-free, copyable samples that satisfy otto's backend contracts
and are conformance-verified in otto's own test suite:

- :mod:`otto.examples.lab_repository` — an in-memory host source.
- :mod:`otto.examples.reservations` — an in-memory reservation backend.

Copy one as a starting point for your own backend, or register it by name from
an ``init`` module to use it directly.
"""
