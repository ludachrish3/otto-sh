"""Pure-unit tests for :mod:`otto.configmodule.completion_cache`.

Focus on the small guards and the option-serialization code path — the
subprocess coverage in ``test_completion_cache.py`` exercises the full stack
but is heavy; these tests run in milliseconds and pinpoint regressions.

Note: this module intentionally does NOT use ``from __future__ import
annotations`` — ``_serialize_options`` introspects ``Annotated[...]`` forms
at runtime, and PEP 563 would stringify them, making the serializer skip the
option entirely.
"""

import inspect
import json
import time
from pathlib import Path
from typing import Annotated
from unittest.mock import MagicMock

import pytest
import typer

from otto.configmodule import completion_cache as cc


def test_read_cache_returns_none_for_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Empty-repo fingerprints poison the cache if allowed; read must skip them."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    # Write a plausible-looking cache entry keyed on the empty fingerprint.
    cache_file = cc._cache_path()
    assert cache_file is not None
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                cc.compute_fingerprint([]): {
                    "schema_version": cc.SCHEMA_VERSION,
                    "generated_at": int(time.time()),
                    "instructions": [{"name": "poisoned", "options": []}],
                    "suites": [],
                },
            }
        )
    )

    assert cc.read_cache([]) is None


def test_write_cache_skips_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Writing for empty repos must be a no-op — no file, no poisoned entry."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    cc.write_cache([], instructions=[{"name": "x", "options": []}], suites=[], hosts=[])
    assert not cc._cache_path().exists()  # type: ignore[union-attr]


def test_read_cache_rejects_schema_mismatch(tmp_path: Path, monkeypatch) -> None:
    """A cache with an older schema version is not consulted."""
    from unittest.mock import MagicMock

    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    (fake_repo.sut_dir / ".otto").mkdir()
    (fake_repo.sut_dir / ".otto" / "settings.toml").write_text("")
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.labs = []

    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    cache_file = cc._cache_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    cache_file.write_text(
        json.dumps(
            {  # type: ignore[union-attr]
                cc.compute_fingerprint([fake_repo]): {
                    "schema_version": cc.SCHEMA_VERSION - 1,
                    "generated_at": int(time.time()),
                    "instructions": [],
                    "suites": [],
                },
            }
        )
    )

    assert cc.read_cache([fake_repo]) is None


def test_serialize_options_handles_supported_kinds() -> None:
    """Every kind in the type-map should produce a non-None schema."""

    def source(
        s: Annotated[str, typer.Option("--s")] = "",
        i: Annotated[int, typer.Option("--i")] = 0,
        f: Annotated[float, typer.Option("--f")] = 0.0,
        b: Annotated[bool, typer.Option("--b/--no-b")] = False,
        p: Annotated[Path, typer.Option("--p")] = Path(),
        l: Annotated[list[str] | None, typer.Option("--l")] = None,  # noqa: E741 — deliberate single-char CLI option name in type-map test
    ) -> None: ...

    schema = cc._serialize_options(source, command_name="source")
    assert schema is not None
    kinds = [entry["kind"] for entry in schema]
    assert kinds == ["str", "int", "float", "bool", "path", "str_list"]


def test_serialize_options_returns_none_on_unsupported() -> None:
    """An unsupported annotation drops the entire command schema."""
    from decimal import Decimal

    def source(
        ok: Annotated[str, typer.Option("--ok")] = "",
        bad: Annotated[Decimal, typer.Option("--bad")] = Decimal(0),
    ) -> None: ...

    assert cc._serialize_options(source, command_name="source") is None


def test_clear_cache_returns_false_when_missing(tmp_path: Path, monkeypatch) -> None:
    """clear_cache reports False when there's nothing to remove."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    assert cc.clear_cache() is False


def test_clear_cache_removes_existing(tmp_path: Path, monkeypatch) -> None:
    """clear_cache unlinks a present cache file and reports True."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    path = cc._cache_path()
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert cc.clear_cache() is True
    assert not path.exists()


def test_collect_backend_names_includes_builtins():
    from otto.configmodule import completion_cache as cc

    snap = cc.collect_backend_names()
    assert "ssh" in snap["term_backends"]
    assert "telnet" in snap["term_backends"]
    by_name = {e["name"]: e["host_families"] for e in snap["transfer_backends"]}
    assert by_name["scp"] == ["unix"]
    assert by_name["console"] == ["embedded"]


def test_write_read_cache_round_trips_backend_names(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from otto.configmodule import completion_cache as cc

    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    (fake_repo.sut_dir / ".otto").mkdir()
    (fake_repo.sut_dir / ".otto" / "settings.toml").write_text("")
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.labs = []

    cc.write_cache(
        [fake_repo],
        instructions=[],
        suites=[],
        hosts=[],
        term_backends=["ssh", "telnet"],
        transfer_backends=[{"name": "scp", "host_families": ["unix"]}],
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["term_backends"] == ["ssh", "telnet"]
    assert out["transfer_backends"] == [{"name": "scp", "host_families": ["unix"]}]


# ---------------------------------------------------------------------------
# _json_safe_default — pure function table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (inspect.Parameter.empty, None),
        ([1, 2, "x"], [1, 2, "x"]),
        (object(), None),
        ([{1, 2}], None),  # non-serializable list → json.dumps TypeError → None
    ],
)
def test_json_safe_default(value: object, expected: object) -> None:
    """_json_safe_default coerces each supported form correctly."""
    assert cc._json_safe_default(value) == expected


# ---------------------------------------------------------------------------
# _serialize_options — skip-gate tests
# ---------------------------------------------------------------------------


def test_serialize_options_non_annotated_returns_none() -> None:
    """A plain (non-Annotated) param annotation causes the whole callback to be skipped."""

    def cb(x: int) -> None: ...

    assert cc._serialize_options(cb, command_name="cb") is None


def test_serialize_options_annotated_without_option_returns_none() -> None:
    """Annotated param without a typer.Option in metadata causes the callback to be skipped."""

    def cb(x: Annotated[int, "meta-but-not-typer-Option"]) -> None: ...

    assert cc._serialize_options(cb, command_name="cb") is None


# ---------------------------------------------------------------------------
# collect_docker_capable_host_ids — hosts.json reading + docker_capable filter
# ---------------------------------------------------------------------------

_DOCKER_HOST = {
    "ip": "10.0.0.1",
    "element": "b",
    "os_type": "unix",
    "board": "seed",
    "docker_capable": True,
    "creds": {"user": "pass"},
    "resources": ["b"],
    "labs": ["lab"],
}
_NON_DOCKER_HOST = {
    "ip": "10.0.0.2",
    "element": "a",
    "os_type": "unix",
    "board": "seed",
    "docker_capable": False,
    "creds": {"user": "pass"},
    "resources": ["a"],
    "labs": ["lab"],
}


def _make_fake_repo(tmp_path: Path) -> MagicMock:
    """Build a minimal fake Repo whose lab path is tmp_path."""
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir(parents=True, exist_ok=True)
    (fake_repo.sut_dir / ".otto").mkdir(exist_ok=True)
    (fake_repo.sut_dir / ".otto" / "settings.toml").write_text("")
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    fake_repo.labs = [tmp_path / "lab"]
    return fake_repo


def test_collect_returns_only_capable_sorted(tmp_path: Path) -> None:
    """Only docker_capable hosts are returned, sorted, and non-dict entries are skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    hosts_file = lab_path / cc.HOSTS_FILENAME
    # docker_capable host "b_seed", non-docker host "a_seed", junk non-dict entry
    hosts_file.write_text(json.dumps([_DOCKER_HOST, _NON_DOCKER_HOST, "junk-string-not-a-dict"]))
    repo = _make_fake_repo(tmp_path)

    result = cc.collect_docker_capable_host_ids([repo])

    assert result == ["b_seed"]


def test_collect_skips_missing_file(tmp_path: Path) -> None:
    """A repo whose lab path has no hosts.json yields an empty list."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # Deliberately do NOT write hosts.json
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


def test_collect_skips_non_list_json(tmp_path: Path) -> None:
    """A hosts.json containing a non-list value (e.g. a dict) is skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    (lab_path / cc.HOSTS_FILENAME).write_text(json.dumps({"not": "a list"}))
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


# ---------------------------------------------------------------------------
# compute_fingerprint — init-module resolution branches + determinism
# ---------------------------------------------------------------------------


def _make_fingerprint_repo(
    tmp_path: Path,
    *,
    init: list[str],
    libs: list[Path],
    labs: list[Path] | None = None,
) -> MagicMock:
    """Build a fake Repo suitable for compute_fingerprint tests."""
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir(parents=True, exist_ok=True)
    (fake_repo.sut_dir / ".otto").mkdir(exist_ok=True)
    (fake_repo.sut_dir / ".otto" / "settings.toml").write_text("")
    fake_repo.init = init
    fake_repo.libs = libs
    fake_repo.tests = []
    fake_repo.labs = labs or []
    return fake_repo


def test_fingerprint_resolves_single_py_module(tmp_path: Path) -> None:
    """A single-file init module (lib/foo.py) is hashed via the resolved path."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "mymod.py").write_text("# init module")

    repo = _make_fingerprint_repo(
        tmp_path,
        init=["mymod"],
        libs=[lib_dir],
    )
    d1 = cc.compute_fingerprint([repo])
    assert isinstance(d1, str)
    assert len(d1) == 64  # sha256 hex


def test_fingerprint_unresolved_module_token(tmp_path: Path) -> None:
    """An unresolvable init token produces a DISTINCT fingerprint from the resolved case."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "mymod.py").write_text("# init module")

    repo_resolved = _make_fingerprint_repo(
        tmp_path / "resolved",
        init=["mymod"],
        libs=[lib_dir],
    )
    repo_unresolved = _make_fingerprint_repo(
        tmp_path / "unresolved",
        init=["no_such_module.sub.path"],
        libs=[lib_dir],
    )

    d_resolved = cc.compute_fingerprint([repo_resolved])
    d_unresolved = cc.compute_fingerprint([repo_unresolved])

    assert d_resolved != d_unresolved


def test_fingerprint_resolves_package_dir_module(tmp_path: Path) -> None:
    """A package-directory init module (lib/mypkg/__init__.py) is hashed via rglob."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    pkg_dir = lib_dir / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("# package init")
    (pkg_dir / "helpers.py").write_text("# helper")

    repo = _make_fingerprint_repo(
        tmp_path,
        init=["mypkg"],
        libs=[lib_dir],
    )
    digest = cc.compute_fingerprint([repo])
    assert isinstance(digest, str)
    assert len(digest) == 64  # sha256 hex


def test_fingerprint_is_deterministic(tmp_path: Path) -> None:
    """Calling compute_fingerprint twice on the same repo set returns equal digests."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "mymod.py").write_text("# init module")

    repo = _make_fingerprint_repo(
        tmp_path,
        init=["mymod"],
        libs=[lib_dir],
    )

    d1 = cc.compute_fingerprint([repo])
    d2 = cc.compute_fingerprint([repo])

    assert d1 == d2


def test_collect_skips_corrupt_json(tmp_path: Path) -> None:
    """A hosts.json with invalid JSON (JSONDecodeError branch) is silently skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    (lab_path / cc.HOSTS_FILENAME).write_text("not valid json }{")
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


def test_collect_skips_invalid_host_dict(tmp_path: Path) -> None:
    """A docker_capable host dict that fails validate_host_dict is silently skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # docker_capable=True but missing required fields (no 'ip', invalid os_type, etc.)
    bad_host = {"docker_capable": True, "element": "x", "os_type": "nonexistent_profile"}
    (lab_path / cc.HOSTS_FILENAME).write_text(json.dumps([bad_host]))
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []
