"""Guard the PEP 561 `py.typed` marker in src/otto/.

uv_build embeds src/otto/**/* into the wheel by disk-walking the tree (see
the [tool.uv.build-backend] NOTE in pyproject.toml), so an empty marker file
committed to the source tree is enough to ship it — nothing else has to
change for `make wheel-check` to see it in dist/*.whl. But that also means a
stray `git rm` or an editor auto-clean would delete it silently and nothing
would notice until release time. This test fails the unit gate immediately
instead, at the cost of an assertion that (by construction) can't catch a
build-config regression — that half is `wheel-check`'s job.
"""

from tests._fixtures.paths import PROJECT_ROOT

_MARKER = PROJECT_ROOT / "src" / "otto" / "py.typed"


def test_py_typed_marker_exists_and_is_empty():
    assert _MARKER.is_file(), f"{_MARKER} is missing — otto would ship untyped (no PEP 561 marker)."
    assert _MARKER.read_bytes() == b"", f"{_MARKER} must be empty per PEP 561."
