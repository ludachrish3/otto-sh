"""Registry of (module, attribute) pairs that resolve in-package web artifacts.

The unit lane neutralizes every entry (root ``neutralized_webassets`` fixture,
autouse under tests/unit/) so unit tests can NEVER see a real built bundle —
absence of a monkeypatch must fail identically on every machine, not pass on
whichever dev box happens to have run ``make web`` (issue #175). The drift
guard (tests/unit/test_webassets_guard.py) pins every ``otto._webassets.ALL``
entry to at least one consumer here whose code reads its constant, imported
from ``otto._webassets.artifacts`` (the defining module, as otto's own code
spells it) or read as ``_webassets.<NAME>``.
"""

#: (import path, attribute) for every module-global that points at a
#: registered build artifact. New artifacts add their consumer(s) here.
CONSUMERS: tuple[tuple[str, str], ...] = (
    ("otto.monitor.server", "_STATIC_DIR"),
    ("otto.coverage.renderer.spa_renderer", "STATIC_DIR"),
)
