"""Reference samples for otto, exercised in otto's own test suite.

Copy any of these as a starting point:

- :mod:`otto.examples.lab_repository` — an in-memory host-source backend.
- :mod:`otto.examples.reservations` — an in-memory reservation backend.
- :mod:`otto.examples.options` — repo-wide ``@options`` classes for suites and
  instructions.
- :mod:`otto.examples.monitor` — a custom metric parser for ``otto monitor``.

Register the backends by name from an ``init`` module; inherit the options
classes from your own suite and instruction option classes.
"""
