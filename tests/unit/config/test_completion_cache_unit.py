"""Pure-unit tests for :mod:`otto.config.completion_cache`.

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
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated
from unittest.mock import MagicMock

import pytest
import typer

from otto.config import completion_cache as cc
from otto.config.repo import PYTEST_CONFIG_NAMES, configured_python_files
from otto.labs.json_repository import LAB_FILENAME
from otto.labs.sources import CompiledLabSource
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.sutrepo import touch_settings


def test_read_cache_returns_none_for_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Empty-repo fingerprints poison the cache if allowed; read must skip them."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
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


# ── Effective TTL: a non-file host source has no invalidation signal ─────────


def _ttl_repo(tmp_path: Path, backend: str | None = None) -> MagicMock:
    """A repo declaring one [[lab.sources]] entry on *backend* (None = none)."""
    repo = MagicMock()
    repo.sut_dir = tmp_path / "sut"
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.lab_sources = (
        []
        if backend is None
        else [CompiledLabSource(label=f"ttl/{backend}#1", backend=backend, repo_dir=repo.sut_dir)]
    )
    repo.reservation_settings = {}
    return repo


def test_json_backends_keep_the_long_ttl(tmp_path: Path) -> None:
    """No source at all, or an explicit json one, means a lab.json fingerprint."""
    assert cc._cache_ttl_seconds([]) == cc.CACHE_TTL_SECONDS
    assert cc._cache_ttl_seconds([_ttl_repo(tmp_path)]) == cc.CACHE_TTL_SECONDS
    assert cc._cache_ttl_seconds([_ttl_repo(tmp_path, "json")]) == cc.CACHE_TTL_SECONDS


def test_a_reservation_backend_also_shortens_the_ttl(tmp_path: Path) -> None:
    """The `usernames` field has the identical constant-digest problem.

    The built-in json reservation backend implements no username completion,
    so that field is populated exclusively by custom — typically networked —
    backends, and the fingerprint tracks only settings.toml for them.
    """
    repo = _ttl_repo(tmp_path)
    repo.reservation_settings = {"backend": "acme"}
    assert cc._cache_ttl_seconds([repo]) == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS


def test_a_custom_backend_shortens_the_ttl(tmp_path: Path) -> None:
    """A non-file host source's digest never moves, so the TTL is the only bound."""
    repos = [_ttl_repo(tmp_path, "cmdb")]
    assert cc._cache_ttl_seconds(repos) == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS
    assert cc.UNFINGERPRINTED_CACHE_TTL_SECONDS < cc.CACHE_TTL_SECONDS

    # One custom repo among json ones is enough — the cache entry is shared.
    mixed = [_ttl_repo(tmp_path), _ttl_repo(tmp_path, "cmdb")]
    assert cc._cache_ttl_seconds(mixed) == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS


def test_a_repo_double_without_lab_sources_reads_as_json(tmp_path: Path) -> None:
    """A double that declares no source must not be mistaken for a custom backend.

    Guards the getattr(...) default and the reservations isinstance(...) check in
    _has_unfingerprinted_source: without them, every mock-based repo double in
    the suite would silently take the short TTL.
    """
    bare = MagicMock(spec=["sut_dir"])
    assert cc._cache_ttl_seconds([bare]) == cc.CACHE_TTL_SECONDS

    automocked = MagicMock()  # .lab_sources iterates empty; .reservation_settings is a Mock
    assert cc._cache_ttl_seconds([automocked]) == cc.CACHE_TTL_SECONDS


def test_read_cache_applies_the_short_ttl_to_a_custom_backend(tmp_path: Path, monkeypatch) -> None:
    """An entry inside the long TTL but past the short one is served or not, by backend."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    json_repo = _ttl_repo(tmp_path)
    cache_file = cc._cache_path()
    assert cache_file is not None
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    six_hours_ago = int(time.time()) - 6 * 60 * 60
    entry = {
        "schema_version": cc.SCHEMA_VERSION,
        "generated_at": six_hours_ago,
        "instructions": [],
        "suites": [],
        "hosts": [],
        "hosts_by_lab": {},
        "docker_hosts": [],
        "term_backends": [],
        "transfer_backends": [],
        "usernames": [],
        "commands": [],
        "labs": [],
        "tests": [],
    }
    cache_file.write_text(json.dumps({cc.compute_fingerprint([json_repo]): entry}))

    # Same fingerprint inputs (labs=[] either way), different backend verdict.
    assert cc.read_cache([json_repo]) is not None, "6h is well inside the 24h TTL"

    custom = _ttl_repo(tmp_path, {"backend": "cmdb"})
    assert cc.compute_fingerprint([custom]) == cc.compute_fingerprint([json_repo]), (
        "positive control: the digest is identical — only the TTL differs"
    )
    assert cc.read_cache([custom]) is None, "6h is past the short TTL"

    # ...and a FRESH entry is still served to that same custom repo, so the
    # short TTL bounds staleness rather than disabling the cache (which would
    # put a full bootstrap behind every TAB).
    entry["generated_at"] = int(time.time()) - 60
    cache_file.write_text(json.dumps({cc.compute_fingerprint([custom]): entry}))
    assert cc.read_cache([custom]) is not None, "a 1-minute-old entry must still serve"


# ── Fingerprint coverage of the --tests sources ──────────────────────────────


def _tests_repo(tmp_path: Path) -> MagicMock:
    """A repo rooted at *tmp_path* whose only tests dir is ``<tmp>/tests``."""
    repo = MagicMock()
    repo.sut_dir = tmp_path
    repo.init = []
    repo.libs = []
    repo.lab_sources = []
    repo.tests = [tmp_path / "tests"]
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    return repo


@pytest.mark.parametrize(
    "relpath",
    [
        "tests/test_top.py",  # the one shape the pre-fix top-level glob caught
        "tests/unit/test_nested.py",  # otto's layout: 405 test files, 0 at the top
        "tests/unit/b_test.py",  # pytest's second default python_files pattern
        "tests/unit/conftest.py",  # parametrization the collected set can see
        "conftest.py",  # ABOVE the tests dir — pytest loads it, rootdir is the SUT
    ],
)
def test_fingerprint_moves_when_a_test_source_appears(tmp_path: Path, relpath: str) -> None:
    """A file the ``--tests`` completer can learn a name from must be hashed.

    Otherwise the cached name-set outlives the files it was derived from: the
    digest never moves, so the entry is served until its 24h TTL expires, and
    the shell offers tests that no longer exist (or omits ones that do).
    """
    repo = _tests_repo(tmp_path)
    before = cc.compute_fingerprint([repo])

    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_new(): pass\n")

    assert cc.compute_fingerprint([repo]) != before, (
        f"adding {relpath} left the digest unchanged — the cache cannot self-invalidate"
    )


def test_fingerprint_ignores_non_test_files(tmp_path: Path) -> None:
    """The digest tracks test SOURCES, not the whole tree — an accepted trade.

    Hashing everything under ``tests/`` would invalidate completion on any
    fixture-data churn, which is the opposite failure: a full bootstrap behind
    a TAB keystroke that had a perfectly good entry. The cost of the trade is
    real — a helper module defining a base class that ``Test*`` inherits does
    change what pytest collects, and no digest here moves for it.
    """
    repo = _tests_repo(tmp_path)
    before = cc.compute_fingerprint([repo])

    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "helper.py").write_text("def test_ignored(): pass\n")
    (tmp_path / "tests" / "fixture.json").write_text("{}")

    assert cc.compute_fingerprint([repo]) == before


def test_fingerprint_and_static_scan_read_the_same_patterns(tmp_path: Path) -> None:
    """Lockstep: every file the scan parses for a name also moves the digest.

    The two used to disagree (``glob("test_*.py")`` vs a recursive walk over
    two patterns), which is exactly how a name-set outlives its source. This
    asserts through ``compute_fingerprint`` itself, not through the helper
    they share: pointing the digest back at a narrower glob while leaving the
    helper in place is the cheapest possible regression, and asserting on the
    helper would sail straight past it.
    """
    repo = _tests_repo(tmp_path)
    (tmp_path / "tests" / "unit").mkdir(parents=True)

    for i, pattern in enumerate(configured_python_files(tmp_path)):
        before = cc.compute_fingerprint([repo])
        path = tmp_path / "tests" / "unit" / pattern.replace("*", f"case{i}")
        path.write_text(f"def test_case{i}(): pass\n")

        # The scan takes a name from this file...
        assert f"test_case{i}" in cc.collect_test_names([repo]), (
            f"{pattern} yields no name — the digest assertion below would be vacuous"
        )
        # ...so writing it must move the digest.
        assert cc.compute_fingerprint([repo]) != before, (
            f"the scan reads {pattern} but the fingerprint does not stat it"
        )


# ── Fingerprint coverage of the [[lab.sources]] host data ────────────────────


@pytest.mark.parametrize(
    ("spelling", "path_of"),
    [
        pytest.param("directory", lambda lab_dir: lab_dir, id="directory"),
        pytest.param("json file", lambda lab_dir: lab_dir / "lab.json", id="json-file"),
    ],
)
def test_fingerprint_moves_when_a_source_lab_file_is_edited(
    tmp_path: Path, spelling: str, path_of
) -> None:
    """Editing a json source's lab file must move the digest.

    The host ids behind ``otto host <TAB>`` come from these files, so a digest
    that ignores them serves a stale host list until the 24h TTL expires — the
    exact staleness the file-backed source is entitled to escape (a backend
    with no file signal falls back to the short TTL instead).

    Parametrized over BOTH ``paths`` spellings because the digest reads them
    through :meth:`~otto.labs.sources.CompiledLabSource.lab_files`: a digest
    wired to the directory form alone would silently stop tracking a source
    that names its ``.json`` file directly.
    """
    sut_dir = tmp_path / "sut"
    sut_dir.mkdir(parents=True)
    touch_settings(sut_dir)
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir(parents=True)
    lab_file = write_lab_json(lab_dir / "lab.json", [{"ip": "10.0.0.1", "element": "test1"}])

    repo = MagicMock()
    repo.sut_dir = sut_dir
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.lab_sources = [
        CompiledLabSource(
            label=f"fp/{spelling}", backend="json", repo_dir=sut_dir, paths=[path_of(lab_dir)]
        )
    ]

    before = cc.compute_fingerprint([repo])
    write_lab_json(
        lab_file,
        [
            {"ip": "10.0.0.1", "element": "test1"},
            {"ip": "10.0.0.2", "element": "test2"},
        ],
    )

    assert cc.compute_fingerprint([repo]) != before, (
        f"editing the lab file of a {spelling} source left the digest unchanged — "
        "the cache cannot self-invalidate on host-data edits"
    )


def test_fingerprint_ignores_a_custom_source_with_no_files(tmp_path: Path) -> None:
    """A non-json source contributes nothing — the TTL fallback covers it.

    Pins the other half of the rule: ``lab_files()`` is empty for a custom
    backend, so its digest is constant and ``_has_unfingerprinted_source``
    is what shortens the TTL. Hashing the repo_dir here would give a custom
    source a false invalidation signal.
    """
    sut_dir = tmp_path / "sut"
    sut_dir.mkdir(parents=True)
    touch_settings(sut_dir)

    repo = MagicMock()
    repo.sut_dir = sut_dir
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.lab_sources = [
        CompiledLabSource(label="fp/cmdb#1", backend="cmdb", repo_dir=sut_dir, paths=[])
    ]

    before = cc.compute_fingerprint([repo])
    (sut_dir / "anything.json").write_text("{}")
    assert cc.compute_fingerprint([repo]) == before
    assert cc._has_unfingerprinted_source([repo]) is True


def test_write_cache_skips_empty_repos(tmp_path: Path, monkeypatch) -> None:
    """Writing for empty repos must be a no-op — no file, no poisoned entry."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    cc.write_cache([], instructions=[{"name": "x", "options": []}], suites=[], hosts=[])
    assert not cc._cache_path().exists()  # type: ignore[union-attr]


def test_read_cache_rejects_schema_mismatch(tmp_path: Path, monkeypatch) -> None:
    """A cache with an older schema version is not consulted."""
    from unittest.mock import MagicMock

    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
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
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    assert cc.clear_cache() is False


# ---------------------------------------------------------------------------
# collect_current_commands — reads otto.cli.run.INSTRUCTIONS + otto.suite.register.SUITES
# ---------------------------------------------------------------------------


class TestCollectCurrentCommands:
    """collect_current_commands() reads the live INSTRUCTIONS/SUITES registries."""

    def test_nothing_registered_yields_empty_instructions(self, monkeypatch) -> None:
        """A run where no init module registered anything reports [], never an error.

        THE EMPTINESS IS INJECTED, not inherited. This test used to delete
        ``otto.cli.run`` from ``sys.modules`` and assert the result was empty —
        which stopped meaning anything when the registry moved to
        ``otto.instructions``: deleting a module that merely re-exports
        ``INSTRUCTIONS`` leaves the registry object, and its contents, exactly
        where they were. What made it pass was collection ORDER. Run alone, or
        after tests that never bootstrap, ``otto.project.instructions`` had not
        been imported and the registry really was empty; run after anything that
        calls ``bootstrap()`` — ``tests/unit/config/test_scope.py`` is one — the
        six first-party instructions are registered as an import side effect and
        the assertion failed. Nothing leaked: those six are otto's own
        import-time baseline, and ``importlib.import_module`` is a no-op on the
        second call, so no snapshot/restore guard may remove them either.

        Replacing the registry with an empty one states the condition the test
        is actually about and holds under every seed.
        """
        from otto.registry import Registry

        monkeypatch.setattr(
            "otto.instructions.INSTRUCTIONS",
            Registry("instruction", register_hint="@otto.instruction()"),
        )
        instructions, _suites = cc.collect_current_commands()
        assert instructions == []

    def test_collects_registered_instruction_with_options(self) -> None:
        import typer

        from otto.cli.run import INSTRUCTIONS, InstructionEntry

        sub_app = typer.Typer()

        def _probe_instr(name: Annotated[str, typer.Option("--name")] = "x") -> None: ...

        sub_app.command("_cc_probe_instr")(_probe_instr)
        INSTRUCTIONS.register(
            "_cc_probe_instr",
            InstructionEntry(name="_cc_probe_instr", sub_app=sub_app, module=__name__),
            origin=__name__,
        )
        try:
            instructions, _suites = cc.collect_current_commands()
        finally:
            INSTRUCTIONS.unregister("_cc_probe_instr")

        entry = next(e for e in instructions if e["name"] == "_cc_probe_instr")
        assert entry["options"]
        assert entry["options"][0]["kind"] == "str"

    def test_collects_registered_suite_with_options(self) -> None:
        import typer

        from otto.suite.register import SUITES, SuiteEntry

        sub_app = typer.Typer()

        def _probe_suite(count: Annotated[int, typer.Option("--count")] = 1) -> None: ...

        sub_app.command("_CcProbeSuite")(_probe_suite)
        SUITES.register(
            "_CcProbeSuite",
            SuiteEntry(name="_CcProbeSuite", sub_app=sub_app, file=__file__, cls=object),
            origin=__name__,
        )
        try:
            _instructions, suites = cc.collect_current_commands()
        finally:
            SUITES.unregister("_CcProbeSuite")

        entry = next(e for e in suites if e["name"] == "_CcProbeSuite")
        assert entry["options"]
        assert entry["options"][0]["kind"] == "int"

    def test_auto_registered_suite_appears_with_serialized_options(self) -> None:
        """A Test* OttoSuite subclass defined with NO decorator/manual registration
        still surfaces in collect_current_commands() — pins that the completion
        cache reads the live SUITES registry, which OttoSuite.__init_subclass__
        populates automatically (register_suite() was deleted; see
        tests/unit/suite/test_auto_registration.py for the isolation idiom)."""
        import typer

        from otto import options
        from otto.suite import OttoSuite
        from otto.suite.register import SUITES

        @options
        class _AutoRegProbeOpts:
            retries: Annotated[int, typer.Option(help="n")] = 3

        class TestAutoRegProbe(OttoSuite[_AutoRegProbeOpts]):
            Options = _AutoRegProbeOpts

            async def test_something(self) -> None: ...

        try:
            assert "TestAutoRegProbe" in SUITES  # sanity: __init_subclass__ registered it
            _instructions, suites = cc.collect_current_commands()
        finally:
            SUITES.unregister("TestAutoRegProbe")

        entry = next(e for e in suites if e["name"] == "TestAutoRegProbe")
        assert entry["options"]
        assert entry["options"][0]["kind"] == "int"

    def test_unserializable_options_cache_with_empty_options_list(self) -> None:
        """A command whose options can't be serialized still completes by name."""
        from decimal import Decimal

        import typer

        from otto.suite.register import SUITES, SuiteEntry

        sub_app = typer.Typer()

        def _probe_bad(bad: Annotated[Decimal, typer.Option("--bad")] = Decimal(0)) -> None: ...

        sub_app.command("_CcProbeBadSuite")(_probe_bad)
        SUITES.register(
            "_CcProbeBadSuite",
            SuiteEntry(name="_CcProbeBadSuite", sub_app=sub_app, file=__file__, cls=object),
            origin=__name__,
        )
        try:
            _instructions, suites = cc.collect_current_commands()
        finally:
            SUITES.unregister("_CcProbeBadSuite")

        entry = next(e for e in suites if e["name"] == "_CcProbeBadSuite")
        assert entry["options"] == []


def test_clear_cache_removes_existing(tmp_path: Path, monkeypatch) -> None:
    """clear_cache unlinks a present cache file and reports True."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    path = cc._cache_path()
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert cc.clear_cache() is True
    assert not path.exists()


def test_collect_backend_names_includes_builtins():
    from otto.config import completion_cache as cc

    snap = cc.collect_backend_names()
    assert "ssh" in snap["term_backends"]
    assert "telnet" in snap["term_backends"]
    by_name = {e["name"]: e["host_families"] for e in snap["transfer_backends"]}
    assert by_name["scp"] == ["unix"]
    assert by_name["console"] == ["embedded"]


def test_write_read_cache_round_trips_backend_names(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from otto.config import completion_cache as cc

    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

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
# collect_cli_commands — CLI_COMMANDS registry snapshot (third-party only)
# ---------------------------------------------------------------------------


def test_cache_round_trips_third_party_commands(tmp_path: Path, monkeypatch) -> None:
    """collect_cli_commands surfaces third-party specs in the cache shape."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    register_cli_command("e2etool", typer.Typer(name="e2etool"), help="Tool.")
    try:
        from otto.config.completion_cache import collect_cli_commands

        commands = collect_cli_commands()
        assert {"name": "e2etool", "help": "Tool.", "lab_free": False} in commands
    finally:
        CLI_COMMANDS.unregister("e2etool")


def test_collect_cli_commands_skips_otto_builtins() -> None:
    """Builtin commands (origin starting with 'otto.') are never cached."""
    from otto.config.completion_cache import collect_cli_commands

    names = {c["name"] for c in collect_cli_commands()}
    # 'run' is a builtin top-level command registered from otto.* — must be
    # excluded since builtins re-register on every real invocation anyway.
    assert "run" not in names


class TestCollectCliCommandChildren:
    """Third-party GROUP children serialize into the cache (fast-path tab
    completion of `otto <plugin-group> <TAB>` rebuilds stubs from them)."""

    def _collect_entry(self, name: str) -> dict:
        from otto.config.completion_cache import collect_cli_commands

        return next(c for c in collect_cli_commands() if c["name"] == name)

    def test_group_children_serialize_names_help_options(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        grp = typer.Typer(name="grptool")

        @grp.command()
        def ping() -> None:
            """Pong."""

        @grp.command(name="re-set")
        def reset_cmd(
            force: Annotated[bool, typer.Option("--force", help="Force it.")] = False,
        ) -> None:
            """Reset."""

        register_cli_command("grptool", grp, help="Group tool.")
        try:
            children = {c["name"]: c for c in self._collect_entry("grptool")["commands"]}
            assert set(children) == {"ping", "re-set"}
            assert children["ping"]["help"] == "Pong."
            assert ["--force"] in [o["flags"] for o in children["re-set"]["options"]]
        finally:
            CLI_COMMANDS.unregister("grptool")

    def test_nested_group_recurses(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        inner = typer.Typer(name="inner", help="Inner group.")

        @inner.command()
        def alpha() -> None: ...

        @inner.command()
        def beta() -> None: ...

        outer = typer.Typer(name="outer")
        outer.add_typer(inner)

        @outer.command()
        def top() -> None: ...

        register_cli_command("outer", outer, help="Outer.")
        try:
            children = {c["name"]: c for c in self._collect_entry("outer")["commands"]}
            assert set(children) == {"top", "inner"}
            inner_children = {c["name"] for c in children["inner"]["commands"]}
            assert inner_children == {"alpha", "beta"}
        finally:
            CLI_COMMANDS.unregister("outer")

    def test_string_loader_group_imports_at_cache_write(self, monkeypatch) -> None:
        import sys
        import types

        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        app = typer.Typer(name="fptool")

        @app.command()
        def x() -> None: ...

        @app.command()
        def y() -> None: ...

        mod = types.ModuleType("fake_plugin_mod")
        mod.app = app  # ty: ignore[unresolved-attribute]
        monkeypatch.setitem(sys.modules, "fake_plugin_mod", mod)
        register_cli_command("fptool", "fake_plugin_mod:app", help="FP.")
        try:
            names = {c["name"] for c in self._collect_entry("fptool")["commands"]}
            assert names == {"x", "y"}
        finally:
            CLI_COMMANDS.unregister("fptool")

    def test_broken_loader_degrades_to_name_only(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        register_cli_command("brokentool", "nonexistent_module_xyz:app", help="Broken.")
        try:
            entry = self._collect_entry("brokentool")
            assert entry["help"] == "Broken."
            assert "commands" not in entry
            assert "options" not in entry
        finally:
            CLI_COMMANDS.unregister("brokentool")

    def test_flattened_leaf_app_serializes_options(self) -> None:
        from otto.cli.registry import CLI_COMMANDS, register_cli_command

        solo = typer.Typer(name="solo")

        @solo.command()
        def solo_cmd(
            count: Annotated[int, typer.Option("--count", help="How many.")] = 1,
        ) -> None:
            """Solo."""

        register_cli_command("solotool", solo)
        try:
            entry = self._collect_entry("solotool")
            assert "commands" not in entry  # flattens to a leaf, not a group
            assert ["--count"] in [o["flags"] for o in entry["options"]]
        finally:
            CLI_COMMANDS.unregister("solotool")


def test_write_read_cache_round_trips_commands(tmp_path: Path, monkeypatch) -> None:
    """write_cache/read_cache carry the 'commands' key through a round trip."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache(
        [fake_repo],
        instructions=[],
        suites=[],
        hosts=[],
        commands=[{"name": "e2etool", "help": "Tool.", "lab_free": False}],
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["commands"] == [{"name": "e2etool", "help": "Tool.", "lab_free": False}]


def test_read_cache_defaults_commands_to_empty_list(tmp_path: Path, monkeypatch) -> None:
    """A cache entry written without 'commands' reads back as an empty list."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache([fake_repo], instructions=[], suites=[], hosts=[])
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["commands"] == []


def test_write_read_cache_round_trips_hosts_by_lab(tmp_path: Path, monkeypatch) -> None:
    """write_cache/read_cache carry the 'hosts_by_lab' map through a round trip."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache(
        [fake_repo],
        instructions=[],
        suites=[],
        hosts=["test1", "alt2"],
        hosts_by_lab={"unix": ["test1"], "unix_alt": ["alt2"]},
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["hosts_by_lab"] == {"unix": ["test1"], "unix_alt": ["alt2"]}


def test_read_cache_defaults_hosts_by_lab_to_empty_dict(tmp_path: Path, monkeypatch) -> None:
    """A cache entry written without 'hosts_by_lab' reads back as an empty dict."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path))
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir()
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}

    cc.write_cache([fake_repo], instructions=[], suites=[], hosts=[])
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["hosts_by_lab"] == {}


# ---------------------------------------------------------------------------
# collect_docker_capable_host_ids — lab.json reading + docker_capable filter
# ---------------------------------------------------------------------------

_DOCKER_HOST = {
    "ip": "10.0.0.1",
    "element": "b",
    "board": "seed",
    "os_type": "unix",
    "docker_capable": True,
    "creds": [{"login": "user", "password": "pass"}],
    "resources": ["b"],
    "labs": ["lab"],
}
_NON_DOCKER_HOST = {
    "ip": "10.0.0.2",
    "element": "a",
    "board": "seed",
    "os_type": "unix",
    "docker_capable": False,
    "creds": [{"login": "user", "password": "pass"}],
    "resources": ["a"],
    "labs": ["lab"],
}


def _make_fake_repo(tmp_path: Path) -> MagicMock:
    """Build a minimal fake Repo whose lab path is tmp_path."""
    fake_repo = MagicMock()
    fake_repo.sut_dir = tmp_path / "sut"
    fake_repo.sut_dir.mkdir(parents=True, exist_ok=True)
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = []
    fake_repo.libs = []
    fake_repo.tests = []
    # Pinned rather than left to the auto-attribute: `build_inventory` reads
    # it, and a MagicMock's is TRUTHY and converts to `{}`, which validates as
    # a broken [inventory] table — that takes out both the host enumeration
    # and (since a broken declaration is ephemeral) every cache WRITE.
    fake_repo.inventory_settings = {}
    # One json source over tmp_path/lab — the built-in backend, which is what
    # these tests exercise.
    fake_repo.lab_sources = json_lab_sources(fake_repo.sut_dir, [tmp_path / "lab"])
    return fake_repo


def test_collect_returns_only_capable_sorted(tmp_path: Path) -> None:
    """Only docker_capable hosts are returned, sorted, and bad entries are skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # docker_capable host "b_seed", non-docker host "a_seed", and a docker_capable
    # entry whose identity cannot resolve. v2 keeps that skip per RECORD; a
    # malformed ELEMENT takes its whole file out of enumeration instead (see
    # tests/unit/labs/test_json_repository.py), which is why the junk entry
    # here is a bad host field rather than a non-dict.
    write_lab_json(
        lab_path / LAB_FILENAME,
        [
            _DOCKER_HOST,
            _NON_DOCKER_HOST,
            {**_DOCKER_HOST, "element": "junk", "slot": "not-an-int"},
        ],
    )
    repo = _make_fake_repo(tmp_path)

    result = cc.collect_docker_capable_host_ids([repo])

    assert result == ["b_seed"]


def test_collect_skips_missing_file(tmp_path: Path) -> None:
    """A repo whose lab path has no lab.json yields an empty list."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # Deliberately do NOT write lab.json
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


def test_collect_skips_non_list_elements_section(tmp_path: Path) -> None:
    """A lab.json whose ``elements`` section is not a JSON array is skipped.

    ``elements``, not the v1 ``hosts``: a document carrying ``hosts`` at all
    is now the migration error, so this test would go on passing on the WRONG
    branch — never reaching the section-type check it exists to cover.
    """
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    (lab_path / LAB_FILENAME).write_text(json.dumps({"elements": {"not": "a list"}}))
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
    touch_settings(fake_repo.sut_dir)
    fake_repo.init = init
    fake_repo.libs = libs
    fake_repo.tests = []
    # A MagicMock auto-attribute is TRUTHY and `dict()`s to `{}`, which reads
    # as a present-but-empty [inventory] — a shape no real Repo produces, and
    # one `build_inventory` rejects, taking the cache write down with it.
    fake_repo.inventory_settings = {}
    fake_repo.lab_sources = json_lab_sources(fake_repo.sut_dir, labs) if labs else []
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
    """A lab.json with invalid JSON (JSONDecodeError branch) is silently skipped."""
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    (lab_path / LAB_FILENAME).write_text("not valid json }{")
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


def test_collect_skips_invalid_host_dict(tmp_path: Path) -> None:
    """A docker_capable host dict that fails validation is silently skipped.

    The bad entry sits INSIDE an otherwise valid element, so the document
    parses and the skip under test is the per-host one in
    ``list_host_summaries`` — not a whole-file rejection, which would make the
    empty result prove nothing about host-level resilience.
    """
    lab_path = tmp_path / "lab"
    lab_path.mkdir(parents=True)
    # docker_capable=True but missing required fields (no 'ip', invalid os_type, etc.)
    bad_host = {"docker_capable": True, "os_type": "nonexistent_profile"}
    (lab_path / LAB_FILENAME).write_text(
        json.dumps(
            {"labs": {"x": {}}, "elements": [{"name": "x", "labs": ["x"], "hosts": [bad_host]}]}
        )
    )
    repo = _make_fake_repo(tmp_path)

    assert cc.collect_docker_capable_host_ids([repo]) == []


# ── pytest's python_files override ───────────────────────────────────────────


def _write(sut_dir: Path, filename: str, body: str) -> None:
    sut_dir.mkdir(parents=True, exist_ok=True)
    (sut_dir / filename).write_text(body)


#: (id, {filename: body}, expected patterns). Each case is ALSO checked against
#: the real pytest in `test_python_files_matches_what_pytest_itself_collects`,
#: so "expected" is not just my reading of the docs.
_CONFIG_CASES: list[tuple[str, dict[str, str], list[str]]] = [
    ("none", {}, ["test_*.py", "*_test.py"]),
    ("pytest_ini", {"pytest.ini": "[pytest]\npython_files = check_*.py\n"}, ["check_*.py"]),
    (
        "dot_pytest_ini",
        {".pytest.ini": "[pytest]\npython_files = check_*.py\n"},
        ["check_*.py"],
    ),
    (
        "pytest_toml",
        {"pytest.toml": '[pytest]\npython_files = ["check_*.py"]\n'},
        ["check_*.py"],
    ),
    (
        "pyproject_ini_options",
        {"pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n'},
        ["check_*.py"],
    ),
    (
        "pyproject_native_toml",
        {"pyproject.toml": '[tool.pytest]\npython_files = ["check_*.py"]\n'},
        ["check_*.py"],
    ),
    ("tox_ini", {"tox.ini": "[pytest]\npython_files = check_*.py\n"}, ["check_*.py"]),
    (
        "setup_cfg",
        {"setup.cfg": "[tool:pytest]\npython_files = check_*.py\n"},
        ["check_*.py"],
    ),
    # The regression this matrix exists for: pytest stops at the FIRST file
    # that counts as its config and never falls through on a missing key, so a
    # leftover pyproject table next to a pytest.ini is ignored entirely.
    # Reading it instead blinds both readers to every real test.
    (
        "pytest_ini_wins_over_pyproject",
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n',
        },
        ["test_*.py", "*_test.py"],
    ),
    (
        "empty_pytest_ini_still_counts",
        {
            "pytest.ini": "",
            "tox.ini": "[pytest]\npython_files = check_*.py\n",
        },
        ["test_*.py", "*_test.py"],
    ),
    # A tox.ini with no [pytest] section is NOT pytest's config, so the search
    # continues past it — the mirror image of the case above.
    (
        "tox_without_pytest_section_is_skipped",
        {
            "tox.ini": "[tox]\nenvlist = py310\n",
            "setup.cfg": "[tool:pytest]\npython_files = check_*.py\n",
        },
        ["check_*.py"],
    ),
    # Quoted pattern with a space: pytest splits ini "args" with shlex.
    (
        "shlex_quoted_pattern",
        {"pytest.ini": '[pytest]\npython_files = "my check*.py" other*.py\n'},
        ["my check*.py", "other*.py"],
    ),
]


@pytest.mark.parametrize(
    ("files", "expected"),
    [(files, expected) for _id, files, expected in _CONFIG_CASES],
    ids=[case_id for case_id, _f, _e in _CONFIG_CASES],
)
def test_python_files_is_read_the_way_pytest_reads_it(
    tmp_path: Path, files: dict[str, str], expected: list[str]
) -> None:
    for name, body in files.items():
        _write(tmp_path, name, body)
    assert configured_python_files(tmp_path) == expected


@pytest.mark.parametrize(
    ("files", "expected"),
    [(files, expected) for _id, files, expected in _CONFIG_CASES],
    ids=[case_id for case_id, _f, _e in _CONFIG_CASES],
)
def test_python_files_matches_what_pytest_itself_collects(
    tmp_path: Path, files: dict[str, str], expected: list[str]
) -> None:
    """Differential: otto's answer must agree with the REAL pytest, per case.

    The precedence rules here are subtle enough that a table of expectations
    written from the docs is worth exactly nothing — the first version of this
    reader fell through on a missing key, which no amount of re-reading the
    docs revealed. So every case above is also run through a real pytest
    collection, and the two must select the same files.
    """
    import subprocess
    import sys

    for name, body in files.items():
        _write(tmp_path, name, body)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_default.py").write_text("def test_default(): pass" + chr(10))
    (tests / "check_alt.py").write_text("def test_alt(): pass" + chr(10))

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    collected = {
        line.split("::")[0].split("/")[-1] for line in proc.stdout.splitlines() if "::" in line
    }
    otto_selects = {
        name
        for name in ("test_default.py", "check_alt.py")
        if any(fnmatchcase(name, pat) for pat in configured_python_files(tmp_path))
    }
    assert otto_selects == collected, proc.stdout


def test_explicitly_empty_python_files_is_not_the_default(tmp_path: Path) -> None:
    """`python_files = []` tells pytest to collect nothing; absent means defaults."""
    _write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options]\npython_files = []\n")
    assert configured_python_files(tmp_path) == []


def test_a_percent_in_python_files_does_not_explode(tmp_path: Path) -> None:
    """pytest parses ini files with iniconfig, which does no interpolation.

    configparser's default BasicInterpolation would raise
    InterpolationSyntaxError straight out of the completion fast path — and
    `cli/main.py` suppresses only OSError around the cache write, so it would
    traceback on every command, not just on TAB.
    """
    _write(tmp_path, "pytest.ini", "[pytest]\npython_files = test_%d_*.py\n")
    assert configured_python_files(tmp_path) == ["test_%d_*.py"]


def test_overridden_python_files_reach_both_readers(tmp_path: Path) -> None:
    """The whole point: the scan NAMES it and the digest STATS it.

    Verified live against pytest before this landed — with
    ``python_files = check_*.py test_*.py``, pytest collects
    ``check_alt.py::test_alt`` while otto's completer offered nothing and its
    digest never moved, so the stale name-set survived its full 24h TTL.
    """
    _write(tmp_path, "pytest.ini", "[pytest]\npython_files = check_*.py test_*.py\n")
    repo = _tests_repo(tmp_path)
    (tmp_path / "tests" / "unit").mkdir(parents=True)

    before = cc.compute_fingerprint([repo])
    (tmp_path / "tests" / "unit" / "check_alt.py").write_text("def test_alt(): pass\n")

    assert "test_alt" in cc.collect_test_names([repo]), "the scan must honour python_files"
    assert cc.compute_fingerprint([repo]) != before, "the digest must honour it too"


def test_editing_the_pytest_config_itself_moves_the_digest(tmp_path: Path) -> None:
    """`python_files` decides which files count, so it is a source in its own right.

    Without this, adding ``python_files = check_*.py`` to a pyproject.toml
    changes what the completer should offer while the digest sits still.
    """
    repo = _tests_repo(tmp_path)
    before = cc.compute_fingerprint([repo])
    _write(tmp_path, "pytest.ini", "[pytest]\npython_files = check_*.py test_*.py\n")
    assert cc.compute_fingerprint([repo]) != before


def test_the_walk_prunes_what_pytest_never_collects(tmp_path: Path) -> None:
    """`.venv` / `.tox` / `build` under a tests dir are pytest's norecursedirs.

    rglob descended into them: a venv tree measured 83 ms warm, on a path that
    runs twice per TAB, for files pytest would never collect — and whose
    mtimes would then invalidate completion for no reason.
    """
    repo = _tests_repo(tmp_path)
    tests = tmp_path / "tests"
    before = cc.compute_fingerprint([repo])

    for skipped in (".venv/lib", ".tox/py310", "build", "node_modules", "sub.egg"):
        d = tests / skipped
        d.mkdir(parents=True)
        (d / "test_vendored.py").write_text("def test_vendored(): pass\n")

    assert cc.compute_fingerprint([repo]) == before, "pruned dirs must not move the digest"
    assert "test_vendored" not in cc.collect_test_names([repo])


def test_a_directory_named_like_a_test_file_is_not_a_test_source(tmp_path: Path) -> None:
    """`rglob` matched directories too, and `_hash_file` folded their mtime in,
    so writing an unrelated file INSIDE one moved the digest."""
    repo = _tests_repo(tmp_path)
    (tmp_path / "tests" / "test_dirname.py").mkdir(parents=True)
    before = cc.compute_fingerprint([repo])
    (tmp_path / "tests" / "test_dirname.py" / "payload.txt").write_text("x")
    assert cc.compute_fingerprint([repo]) == before


@pytest.mark.parametrize("name", sorted(PYTEST_CONFIG_NAMES))
def test_every_pytest_config_file_is_in_the_digest(tmp_path: Path, name: str) -> None:
    """Each of the seven, not just the one the other tests happen to write.

    `python_files` decides which files below even count, so every file pytest
    might read it from is a fingerprint source. Dropping any one of them from
    `pytest_config_paths` leaves a repo configured that way permanently stale.
    """
    repo = _tests_repo(tmp_path)
    before = cc.compute_fingerprint([repo])
    (tmp_path / name).write_text("# pytest config" + chr(10))
    assert cc.compute_fingerprint([repo]) != before, f"{name} is not a digest source"


@pytest.mark.parametrize(
    ("dirname", "pruned"),
    [
        (".venv", True),
        (".tox", True),
        (".git", True),
        ("sub.egg", True),
        ("_darcs", True),
        ("build", True),
        ("CVS", True),
        ("dist", True),
        ("node_modules", True),
        ("venv", True),
        ("{arch}", True),
        ("__pycache__", True),
        # Near-misses that pytest DOES collect from — pruning these would
        # blind both readers, which is the same failure in the other direction.
        ("builds", False),
        ("mybuild", False),
        ("egg", False),
        ("arch", False),
        ("unit", False),
    ],
)
def test_norecursedirs_table_matches_pytest(dirname: str, pruned: bool) -> None:
    """The full default list, table-checked, including the near-misses.

    pytest matches `norecursedirs` as fnmatch PATTERNS against the basename;
    this reproduces `*.egg` and `.*` as suffix/prefix checks and the rest as
    literals, so the table is the only thing that would catch a future edit
    dropping one — or widening a literal into a prefix.
    """
    assert cc._is_norecurse_dir(dirname) is pruned


# ── A host source that STALLS must not wedge the shell ───────────────────────


@pytest.mark.serial_timing
def test_a_hanging_host_source_is_bounded_not_waited_on(monkeypatch, caplog) -> None:
    """Failing was already contained; stalling was not.

    A custom `[lab]` backend is allowed to be a networked CMDB — that is the
    documented reason this cache exists — but on a cold cache the enumeration
    really runs, and an unreachable service would otherwise hang the user's
    TAB with no feedback until they interrupt it.
    """
    import logging
    import threading
    import time

    monkeypatch.setattr(cc, "HOST_SUMMARY_DEADLINE_SECONDS", 0.05)
    entered = threading.Event()

    def _never_returns(_repo, _abandoned=None):
        entered.set()
        time.sleep(30)  # pragma: no cover — the point is that we do not wait

    monkeypatch.setattr(cc, "_enumerate_host_summaries", _never_returns)
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/nowhere-hang")

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="otto.config.completion_cache"):
        result = cc.repo_host_summaries(repo)
    elapsed = time.monotonic() - started

    assert result == []
    assert entered.is_set(), "positive control: the enumeration must actually have started"
    # Tight against the 0.05s patched deadline: a 5s bound would pass even
    # if the deadline were 4.9s, leaving the caplog line to carry the test.
    assert elapsed < 1, f"waited {elapsed:.1f}s on a hanging backend"
    assert any("did not answer within" in r.message for r in caplog.records), caplog.text


def test_a_working_host_source_is_untouched_by_the_deadline(monkeypatch) -> None:
    """The bound must not cost the normal path its answer."""
    from otto.labs import HostSummary

    expected = [HostSummary(id="test1", labs=["unix"])]
    monkeypatch.setattr(cc, "_enumerate_host_summaries", lambda _repo, _abandoned=None: expected)
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/nowhere-ok")
    assert cc.repo_host_summaries(repo) == expected


def test_one_enumeration_per_repo_however_many_collectors_ask(monkeypatch) -> None:
    """Three collectors enumerate the same repo on one cache-write pass.

    Un-memoized, a stalled backend cost three deadlines — and worse, could
    time out for one collector and not another, writing a cache where
    `otto host <TAB>` is full and `otto docker --on <TAB>` is empty, served
    for the whole TTL.
    """
    from otto.labs import HostSummary

    calls = 0

    def _count(_repo, _abandoned=None):
        nonlocal calls
        calls += 1
        return [HostSummary(id="test1", labs=["unix"])]

    monkeypatch.setattr(cc, "_enumerate_host_summaries", _count)
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/memo")

    for _ in range(3):
        assert [s.id for s in cc.repo_host_summaries(repo)] == ["test1"]
    assert calls == 1, f"enumerated {calls} times for one repo"


def test_the_deadline_is_overridable_for_a_merely_slow_backend(monkeypatch) -> None:
    """Giving up costs the user ALL host completion until the backend speeds up.

    A module constant would leave an affected team no recourse, so the bound
    is an env var — otherwise the fix for "my CMDB takes 3s" is "patch otto".
    """
    monkeypatch.delenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, raising=False)
    assert cc._host_summary_deadline() == cc.HOST_SUMMARY_DEADLINE_SECONDS

    monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, "7.5")
    assert cc._host_summary_deadline() == 7.5

    # Garbage and non-positive values fall back rather than disabling the bound.
    for bad in ("", "soon", "0", "-1"):
        monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, bad)
        assert cc._host_summary_deadline() == cc.HOST_SUMMARY_DEADLINE_SECONDS, bad


def test_a_backend_that_explodes_does_not_reach_the_terminal(monkeypatch, capsys) -> None:
    """`_bounded` runs `work` on a thread, so an escape hits threading.excepthook
    and prints a full traceback to the user's terminal mid-TAB — which is
    exactly what this function's "never crashes the shell" contract forbids."""

    def _explodes(_repo, _abandoned=None):
        raise KeyboardInterrupt  # a BaseException: `except Exception` misses it

    monkeypatch.setattr(cc, "_enumerate_host_summaries", _explodes)
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    repo = MagicMock()
    repo.sut_dir = Path("/boom")

    assert cc.repo_host_summaries(repo) == []
    assert "Traceback" not in capsys.readouterr().err


def test_declining_loader_module_does_not_escape_collect_cli_commands(monkeypatch, tmp_path):
    """A third-party loader module that DECLINES to load must not brick every command.

    ``collect_cli_commands`` imports each non-otto command's ``"pkg.mod:attr"``
    loader. A module-level ``pytest.importorskip`` there raises ``Skipped`` —
    rooted at ``BaseException``, so an ``except Exception`` seam misses it. It
    escapes ``entry()`` as a call ARGUMENT to ``write_cache``, outside the
    ``suppress(OSError)``, and tracebacks out of EVERY command including
    ``otto --help``, and into the shell mid-TAB.
    """
    import sys

    from otto.cli.registry import CLI_COMMANDS, CommandSpec

    mod = tmp_path / "declining_loader.py"
    mod.write_text("import pytest\npytest.importorskip('otto_no_such_optional_dep')\napp = None\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "declining_loader", raising=False)

    spec = CommandSpec(
        name="probecmd", loader="declining_loader:app", help="probe", origin="thirdparty.pkg"
    )
    # Registered through the registry's own dicts so the entry disappears with the
    # test; `collect_cli_commands` skips anything whose origin starts with "otto.".
    monkeypatch.setitem(CLI_COMMANDS._entries, "probecmd", spec)
    monkeypatch.setitem(CLI_COMMANDS._origins, "probecmd", "thirdparty.pkg")

    try:
        out = cc.collect_cli_commands()
    except BaseException as exc:  # the escape IS the defect under test
        raise AssertionError(
            f"collect_cli_commands() let {type(exc).__name__} escape: {exc!r}"
        ) from exc
    # Contained: the command still appears, name-only, with no child metadata.
    entry = next(e for e in out if e["name"] == "probecmd")
    assert "commands" not in entry
    assert "options" not in entry
