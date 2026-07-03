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


# ---------------------------------------------------------------------------
# collect_current_commands — reads otto.cli.run.INSTRUCTIONS + otto.suite.register.SUITES
# ---------------------------------------------------------------------------


class TestCollectCurrentCommands:
    """collect_current_commands() reads the live INSTRUCTIONS/SUITES registries."""

    def test_no_instructions_module_imported_yields_empty_instructions(self, monkeypatch) -> None:
        """If otto.cli.run was never imported, instructions is empty (not an error)."""
        import sys

        monkeypatch.delitem(sys.modules, "otto.cli.run", raising=False)
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
            SuiteEntry(name="_CcProbeSuite", sub_app=sub_app, file=__file__),
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
            SuiteEntry(name="_CcProbeBadSuite", sub_app=sub_app, file=__file__),
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
# collect_cli_commands — CLI_COMMANDS registry snapshot (third-party only)
# ---------------------------------------------------------------------------


def test_cache_round_trips_third_party_commands(tmp_path: Path, monkeypatch) -> None:
    """collect_cli_commands surfaces third-party specs in the cache shape."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    register_cli_command("e2etool", typer.Typer(name="e2etool"), help="Tool.")
    try:
        from otto.configmodule.completion_cache import collect_cli_commands

        commands = collect_cli_commands()
        assert {"name": "e2etool", "help": "Tool.", "lab_free": False} in commands
    finally:
        CLI_COMMANDS.unregister("e2etool")


def test_collect_cli_commands_skips_otto_builtins() -> None:
    """Builtin commands (origin starting with 'otto.') are never cached."""
    from otto.configmodule.completion_cache import collect_cli_commands

    names = {c["name"] for c in collect_cli_commands()}
    # 'run' is a builtin top-level command registered from otto.* — must be
    # excluded since builtins re-register on every real invocation anyway.
    assert "run" not in names


class TestCollectCliCommandChildren:
    """Third-party GROUP children serialize into the cache (fast-path tab
    completion of `otto <plugin-group> <TAB>` rebuilds stubs from them)."""

    def _collect_entry(self, name: str) -> dict:
        from otto.configmodule.completion_cache import collect_cli_commands

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
        commands=[{"name": "e2etool", "help": "Tool.", "lab_free": False}],
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["commands"] == [{"name": "e2etool", "help": "Tool.", "lab_free": False}]


def test_read_cache_defaults_commands_to_empty_list(tmp_path: Path, monkeypatch) -> None:
    """A cache entry written without 'commands' reads back as an empty list."""
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

    cc.write_cache([fake_repo], instructions=[], suites=[], hosts=[])
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["commands"] == []


def test_write_read_cache_round_trips_hosts_by_lab(tmp_path: Path, monkeypatch) -> None:
    """write_cache/read_cache carry the 'hosts_by_lab' map through a round trip."""
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
        hosts=["carrot_seed", "apple_seed"],
        hosts_by_lab={"veggies": ["carrot_seed"], "fruits": ["apple_seed"]},
    )
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["hosts_by_lab"] == {"veggies": ["carrot_seed"], "fruits": ["apple_seed"]}


def test_read_cache_defaults_hosts_by_lab_to_empty_dict(tmp_path: Path, monkeypatch) -> None:
    """A cache entry written without 'hosts_by_lab' reads back as an empty dict."""
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

    cc.write_cache([fake_repo], instructions=[], suites=[], hosts=[])
    out = cc.read_cache([fake_repo])
    assert out is not None
    assert out["hosts_by_lab"] == {}


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
