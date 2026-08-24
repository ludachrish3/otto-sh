"""One stand-in for :class:`otto.bootstrap.BootstrapResult`, shared by the preamble tests.

Six modules stubbed ``otto.bootstrap.bootstrap`` independently, each namespace
carrying only the fields its own test happened to need. That holds until the
preamble reads a field nobody's stub declares -- which is exactly what happened
when the dependency preflight began reading ``ordered_repos``: ten tests failed
with ``AttributeError`` inside code none of them were about, in three modules
whose subject is reservations, output dirs and dispatch. One factory means the
next field is added once.

NOT the real dataclass, deliberately: ``BootstrapResult`` requires an
``OttoEnvSettings``, and building one is a heavier commitment than a preamble
unit test wants to make. What this mirrors is its FIELD SET, which is the part
the preamble reads -- and ``test_bootstrap_stub_fidelity`` is what keeps the
mirror true.
"""

from types import SimpleNamespace
from typing import Any


def bootstrap_stub(
    repos: "Any" = (),
    *,
    errors: "Any" = (),
    warnings: "Any" = (),
    ordered: "Any" = None,
) -> SimpleNamespace:
    """A ``bootstrap()`` return value carrying every field the CLI preamble reads.

    *ordered* defaults to EMPTY rather than to *repos*, so the dependency
    preflight sees nothing unless a test hands it something on purpose. Most
    callers here stub ``repos`` only to satisfy a name lookup, with stand-ins
    that carry no ``sut_dir`` -- feeding those to the preflight would fail on a
    detail the test is not about.
    """
    return SimpleNamespace(
        env=None,
        repos=list(repos),
        errors=list(errors),
        warnings=list(warnings),
        ordered_repos=[] if ordered is None else list(ordered),
    )
