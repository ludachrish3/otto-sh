"""Drift guard: gitignored-inside-the-package == registered build artifact (#175).

.gitignore is the source of truth for "built, not committed". Any such path
must live under src/otto/_webassets/ (one glob), appear in otto._webassets.ALL,
and have a neutralized consumer registered in tests/_fixtures/webassets.py —
otherwise the next artifact silently re-creates the #175 class.
"""

import importlib
import importlib.util
import inspect
import re
from pathlib import Path

import pytest

from otto import _webassets
from tests._fixtures.webassets import CONSUMERS

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEBASSETS_GLOB = "src/otto/_webassets/*/"


def in_package_ignores(gitignore_text: str) -> list[str]:
    """Non-comment .gitignore entries that point inside src/otto/.

    Normalizes a leading ``/`` (this repo's dominant .gitignore convention —
    see ``/test/``, ``/host/``, ``/cov/``, etc.) before the ``startswith``
    test, so an entry written as ``/src/otto/newthing/`` is not invisible to
    this pin merely for following house style.
    """
    entries = []
    for line in gitignore_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        normalized = stripped.lstrip("/")
        if normalized.startswith("src/otto/"):
            entries.append(normalized)
    return entries


def _webassets_attr_name(path: Path) -> str:
    """The otto._webassets module-level constant name bound to ``path``."""
    for attr, value in vars(_webassets).items():
        if attr.isupper() and value == path:
            return attr
    raise AssertionError(f"no otto._webassets module-level constant equals {path!r}")


def test_gitignore_in_package_entries_are_exactly_the_webassets_glob():
    text = (REPO_ROOT / ".gitignore").read_text()
    assert in_package_ignores(text) == [WEBASSETS_GLOB]


def test_fake_extra_artifact_entry_would_be_caught():
    text = "src/otto/_webassets/*/\nsrc/otto/newthing/static/dist/\n"
    assert in_package_ignores(text) != [WEBASSETS_GLOB]


def test_registry_paths_live_under_the_glob():
    root = Path(_webassets.__file__).parent
    assert _webassets.ALL, "fixture sanity: an empty registry would make this guard vacuous"
    for name, path in _webassets.ALL.items():
        assert path == root / name


def test_every_consumer_is_neutralized_by_default():
    """Every CONSUMERS global is blind to the real bundle under the unit lane.

    This is the load-bearing pin for issue #175: it proves the autouse
    ``_no_ambient_webassets`` fixture (``tests/unit/conftest.py``) actually
    fired for THIS test, not merely that it exists somewhere in the tree. On
    this dev box the real bundle is built (every ``otto._webassets.ALL`` path
    exists on disk), so "does not exist" is a genuine discriminator, not a
    tautology of a clean checkout. Delete the autouse fixture (or its
    ``neutralized_webassets`` request) and this test goes red here, even
    though every other test in the repo may still pass. The
    ``value not in _webassets.ALL.values()`` clause is NOT redundant with the
    existence check above it: on a bare checkout / hostless CI the real
    paths don't exist either, so that clause is the only one still able to
    tell "neutralized" apart from "never built" — do not delete it as
    dead weight.
    """
    for module_name, attr in CONSUMERS:
        module = importlib.import_module(module_name)
        value = getattr(module, attr)
        assert isinstance(value, Path)
        assert not value.exists()
        assert value not in _webassets.ALL.values()


def test_every_artifact_has_a_registered_consumer():
    """Every otto._webassets.ALL entry is claimed by a CONSUMERS module's source.

    Reads each consumer module's UNPATCHED source text rather than its live
    (neutralized, under this test lane) attribute value: a monkeypatch only
    overwrites the module's runtime attribute, never its source, so a
    source-text check can't be fooled either way — not by the neutralizer
    masking a missing registration, and not by a coincidental resolved-value
    match with no real reference in code. This is what actually catches a new
    artifact added to ``otto._webassets.ALL`` without a matching consumer.
    """
    claimed = set()
    for module_name, _attr in CONSUMERS:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        for name, path in _webassets.ALL.items():
            if re.search(rf"_webassets\.{_webassets_attr_name(path)}\b", source):
                claimed.add(name)
    assert claimed == set(_webassets.ALL)


def test_all_covers_every_registry_constant():
    """A Path constant added to otto._webassets without an ALL entry is invisible
    to every other pin here — this closes the add-artifact-forget-ALL path (the
    #175 class recurring with zero red tests locally: e.g. adding
    ``COVAPP2 = _ROOT / "covapp2"`` without also adding it to ``ALL``).
    """
    constants = {
        value
        for attr, value in vars(_webassets).items()
        if attr.isupper() and not attr.startswith("_") and isinstance(value, Path)
    }
    assert constants == set(_webassets.ALL.values())
    root = Path(_webassets.__file__).parent
    for value in _webassets.ALL.values():
        assert value.parent == root


def _load_build_backend_shim():
    """Import scripts/otto_build_backend.py by path (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "otto_build_backend", REPO_ROOT / "scripts" / "otto_build_backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_backend_is_the_guarded_shim():
    """pyproject must wire the in-tree shim, or bare `uv build` ships asset-less.

    Reverting ``build-backend`` to plain ``"uv_build"`` silently reopens the
    hole this whole guard family exists for: a wheel built on a checkout that
    never ran ``make web`` installs fine and fails at first use in an
    air-gapped lab. String pins on purpose — tomllib parsing adds nothing the
    literal text doesn't already prove, and the shim file must ride inside the
    sdist for wheel-from-sdist builds to find it.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'build-backend = "otto_build_backend"' in text
    assert 'backend-path = ["scripts"]' in text
    assert 'source-include = ["scripts/otto_build_backend.py"]' in text


def test_build_backend_required_files_match_the_registry():
    """The shim's required-file list stays coupled to otto._webassets.ALL.

    Same per-artifact sentinel files the Makefile wheel-check loop asserts:
    the monitor bundle's dist/index.html, the covapp bundle's index.html. A
    new ALL entry must grow both this tuple and the wheel-check loop.
    """
    shim = _load_build_backend_shim()
    sentinel = {"monitor": "dist/index.html", "covapp": "index.html"}
    assert set(sentinel) == set(_webassets.ALL)
    expected = {f"src/otto/_webassets/{name}/{sentinel[name]}" for name in _webassets.ALL}
    assert set(shim.REQUIRED_WEBASSETS) == expected


def test_build_backend_guard_refuses_missing_assets(tmp_path):
    """Red-provable core: an asset-less tree raises, naming make web; a
    populated one passes."""
    shim = _load_build_backend_shim()
    with pytest.raises(RuntimeError, match="make web"):
        shim.assert_webassets_present("wheel", root=tmp_path)
    for rel in shim.REQUIRED_WEBASSETS:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    shim.assert_webassets_present("wheel", root=tmp_path)
