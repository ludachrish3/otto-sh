"""Root-group lazy resolution against the CLI command registry."""

import re
import sys
from typing import Annotated

import typer
from typer.testing import CliRunner

from otto.cli.main import app

runner = CliRunner()


def test_root_help_lists_all_builtins_without_importing_them(monkeypatch):
    for mod in ("otto.cli.cov", "otto.cli.monitor"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "run",
        "test",
        "monitor",
        "cov",
        "host",
        "docker",
        "reservation",
        "inventory",
        "schema",
    ):
        assert name in result.output
    assert "otto.cli.cov" not in sys.modules
    assert "otto.cli.monitor" not in sys.modules


def test_dispatch_resolves_only_the_target(monkeypatch):
    monkeypatch.delitem(sys.modules, "otto.cli.cov", raising=False)
    result = runner.invoke(app, ["schema", "--help"])
    assert result.exit_code == 0
    assert "otto.cli.cov" not in sys.modules


# ── The hand-maintained "how many groups are there" counters ─────────────────

_NUMBER_WORDS = {
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
}
"""Enough of the range to outlive a few more groups; a count outside it fails loudly."""


def _builtin_command_names() -> list[str]:
    """The groups ``register_builtin_commands`` itself registered, by ORIGIN.

    Not every name in the registry: a third-party plugin — or a test that
    registered one and is running in the same process — is not a first-party
    group, and counting those would make this assertion drift with whatever
    else the worker had done.
    """
    from otto.cli.builtin_commands import register_builtin_commands
    from otto.cli.registry import CLI_COMMANDS

    register_builtin_commands()  # idempotent
    return [
        name
        for name in CLI_COMMANDS.names()
        if CLI_COMMANDS.get(name).origin == "otto.cli.builtin_commands"
    ]


def _one_match(pattern: str, text: str, what: str) -> str:
    """The single capture group of *pattern* in *text*, failing loudly when absent.

    A regex that quietly finds nothing turns every assertion below green — the
    exact vacuity these counters already suffered from, since "twelve" sat in
    two files while the real number was thirteen and nothing noticed.
    """
    found = re.findall(pattern, text)
    assert len(found) == 1, f"expected exactly one {what} (pattern {pattern!r}), found {found}"
    return found[0]


def test_the_hand_maintained_group_counts_track_the_registry():
    """Three counters spell the number of first-party groups in prose. Pin all three.

    ``docs/guide/cli/index.md``'s opening sentence, the ``builtin_commands``
    module docstring, and ``scripts/capture_docs_termynal.py``'s ``COMMANDS``
    list (both its length and the comment above it) each restate a number no
    test could previously read. Two of them were already stale — the docs said
    "twelve" and the capture script "eleven" — for as long as it took someone
    to notice by eye. This is what makes the next one fail a test instead.
    """
    import importlib.util

    from tests._fixtures.paths import PROJECT_ROOT

    names = _builtin_command_names()
    count = len(names)
    assert count in _NUMBER_WORDS, f"extend _NUMBER_WORDS: {count} groups registered ({names})"
    word = _NUMBER_WORDS[count]

    guide = (PROJECT_ROOT / "docs" / "guide" / "cli" / "index.md").read_text()
    assert (
        _one_match(r"`otto` is one command with (\w+) subcommand groups", guide, "count sentence")
        == word
    )

    from otto.cli import builtin_commands

    assert (
        _one_match(r"otto's (\w+) subcommand groups", builtin_commands.__doc__ or "", "docstring")
        == word
    )

    script = PROJECT_ROOT / "scripts" / "capture_docs_termynal.py"
    assert _one_match(r"# The (\w+) first-party commands", script.read_text(), "comment") == word
    spec = importlib.util.spec_from_file_location("capture_docs_termynal", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sorted(module.COMMANDS) == sorted(names)


def test_the_cli_guide_lists_every_group_in_its_table_and_toctree():
    """A group with a page nobody links to is a page nobody reads."""
    from tests._fixtures.paths import PROJECT_ROOT

    names = _builtin_command_names()
    guide = (PROJECT_ROOT / "docs" / "guide" / "cli" / "index.md").read_text()

    rows = re.findall(r"^\| \[`otto (\S+)`\]", guide, re.MULTILINE)
    assert sorted(rows) == sorted(names), "the Commands table and the registry disagree"

    block = _one_match(
        r":caption: Commands\n:hidden:\n\n((?:[\w./-]+\n)+)", guide, "Commands toctree"
    )
    entries = [line.split("/")[0] for line in block.split()]
    assert sorted(entries) == sorted(names), "the Commands toctree and the registry disagree"


# ── Issue #140: banner shows on help screens only ────────────────────────────


def _banner_fragment() -> str:
    """A distinctive line from the otto banner, safe to grep for in CLI output."""
    from otto.cli.banner import banner

    return banner.plain.splitlines()[1].strip()


class TestBannerOnHelpOnly:
    """The banner shows on every help screen and nowhere else (issue #140)."""

    def test_root_help_shows_banner(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert _banner_fragment() in result.output

    def test_root_bare_invocation_shows_banner(self):
        """Bare ``otto`` triggers Typer's ``no_args_is_help`` — same banner hook."""
        result = runner.invoke(app, [])
        assert _banner_fragment() in result.output

    def test_subcommand_help_shows_banner(self):
        result = runner.invoke(app, ["host", "--help"])
        assert result.exit_code == 0
        assert _banner_fragment() in result.output

    def test_nested_group_help_shows_banner(self):
        result = runner.invoke(app, ["test", "--help"], env={"OTTO_LAB": ""})
        assert result.exit_code == 0
        assert _banner_fragment() in result.output

    def test_bare_host_menu_shows_banner(self):
        """``otto host`` (no host id) manually echoes ``ctx.get_help()`` — must show it too."""
        result = runner.invoke(app, ["host"], env={"OTTO_LAB": ""})
        assert _banner_fragment() in result.output

    def test_banner_shown_exactly_once_on_a_help_screen(self):
        result = runner.invoke(app, ["--help"])
        assert result.output.count(_banner_fragment()) == 1

    def test_real_command_execution_shows_no_banner(self, tmp_path):
        """The banner is help-only: a real (non-help) leaf must never print it."""
        result = runner.invoke(
            app,
            ["schema", "export", "--out", str(tmp_path), "--builtins-only"],
            env={"OTTO_LAB": ""},
        )
        assert result.exit_code == 0, result.output
        assert _banner_fragment() not in result.output

    def test_bare_monitor_usage_message_shows_banner(self):
        """``otto monitor`` (neither ``--live`` nor a source) manually echoes
        ``ctx.get_help()`` before exiting 2 — must show the banner too."""
        result = runner.invoke(app, ["monitor"], env={"OTTO_LAB": ""})
        assert result.exit_code == 2
        assert _banner_fragment() in result.output

    def test_ensure_help_banner_is_idempotent(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from otto.cli.invoke import ensure_help_banner

        ctx = SimpleNamespace(meta={})
        with patch("otto.cli.banner.print_banner") as mock_print:
            ensure_help_banner(ctx)
            ensure_help_banner(ctx)
        mock_print.assert_called_once()


def test_monitor_help_through_root_surfaces_flat_options():
    """``otto monitor --help`` (root dispatch) must show the leaf's own flags.

    Regression: ``monitor_app`` is a single-command, callback-free Typer app;
    group-ifying it hid ``--live`` behind a spurious nested ``monitor``
    subcommand (``otto monitor --live`` then failed with exit 2 'No such
    option'). The flattening rule keeps monitor's documented flat CLI.
    """
    result = runner.invoke(app, ["monitor", "--help"])
    assert result.exit_code == 0, result.output
    assert "--live" in result.output
    assert "--hosts" in result.output
    # No nested same-named subcommand row (the group-ified regression symptom).
    assert "COMMAND [ARGS]" not in result.output


def test_builtin_specs_match_native_typer_conversion(monkeypatch):
    """Every builtin's root-resolved surface matches direct Typer conversion.

    Structural guard: for each registered ``CommandSpec`` with a live
    ``typer.Typer`` loader, the root group's resolved command must expose the
    SAME shape (group vs leaf, and the same command/parameter names) as calling
    ``typer.main.get_command(sub_app)`` directly. This catches any future
    accidental group-ification of a single-command app (or vice versa).
    """
    import typer

    from otto.cli.registry import CLI_COMMANDS, resolve_spec_command

    def _surface(cmd):
        if hasattr(cmd, "commands"):
            return ("group", frozenset(cmd.commands))
        return ("leaf", frozenset(p.name for p in cmd.params))

    checked = 0
    for name, spec in CLI_COMMANDS.items():
        loader = spec.loader
        if isinstance(loader, str):
            import importlib

            mod_name, _, attr = loader.partition(":")
            loader = getattr(importlib.import_module(mod_name), attr)
        if not isinstance(loader, typer.Typer):
            continue
        # Native reference conversion, with completion suppressed the same way
        # resolve_spec_command does for the flat case (root owns completion).
        prev = loader._add_completion
        loader._add_completion = False
        try:
            native = typer.main.get_command(loader)
        finally:
            loader._add_completion = prev
        resolved = resolve_spec_command(spec)
        assert _surface(resolved) == _surface(native), name
        checked += 1
    assert checked, "expected at least one builtin Typer-app spec to check"


def test_unknown_command_errors_cleanly():
    result = runner.invoke(app, ["not-a-command"])
    assert result.exit_code != 0
    assert "No such command" in result.output


# ── Task 9: completion cache carries third-party top-level commands ────────


def test_list_commands_includes_cached_third_party_names(monkeypatch):
    """A name present only in the cached ``commands`` list is still listed."""
    from otto.cli.main import _OttoGroup

    monkeypatch.setattr(
        "otto.cli.main.get_completion_names",
        lambda: {"commands": [{"name": "cached-tool", "help": "Cached.", "lab_free": False}]},
    )
    group = _OttoGroup(name="otto")
    names = group.list_commands(None)
    assert "cached-tool" in names


def test_get_command_serves_cached_third_party_as_stub(monkeypatch):
    """A cache-only command name resolves to a help-only stub, not a crash."""
    from otto.cli.main import _OttoGroup

    monkeypatch.setattr(
        "otto.cli.main.get_completion_names",
        lambda: {"commands": [{"name": "cached-tool", "help": "Cached.", "lab_free": False}]},
    )
    group = _OttoGroup(name="otto")
    cmd = group.get_command(None, "cached-tool")
    assert cmd is not None
    assert cmd.name == "cached-tool"


def test_get_command_prefers_real_registry_over_cache(monkeypatch):
    """A name in BOTH the live registry and the cache must resolve to the REAL command.

    The cached stub must never shadow real dispatch: when bootstrap has run
    (registry populated) the registry check in get_command short-circuits
    before the cache fallback is even consulted.
    """
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    async def _real_cmd() -> None:
        return None

    register_cli_command("dupe-tool", _real_cmd, help="Real one.")
    try:
        from otto.cli.main import _OttoGroup

        monkeypatch.setattr(
            "otto.cli.main.get_completion_names",
            lambda: {
                "commands": [{"name": "dupe-tool", "help": "Stale cached.", "lab_free": False}]
            },
        )
        group = _OttoGroup(name="otto")

        class _FakeCtx:
            def __init__(self) -> None:
                self.meta: dict = {"_pending_subcmd_args": ["dupe-tool"]}

        cmd = group.get_command(_FakeCtx(), "dupe-tool")
        assert cmd is not None
        # The real command resolves via _real(); its help comes from the
        # live CommandSpec ("Real one."), never the stale cached help text.
        assert cmd.help != "Stale cached."
    finally:
        CLI_COMMANDS.unregister("dupe-tool")


def test_list_commands_dedupes_registry_over_cache(monkeypatch):
    """A name in both the registry and the cache appears exactly once; registry wins."""
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    register_cli_command("both-tool", typer.Typer(name="both-tool"), help="Real.")
    try:
        from otto.cli.main import _OttoGroup

        monkeypatch.setattr(
            "otto.cli.main.get_completion_names",
            lambda: {"commands": [{"name": "both-tool", "help": "Stale.", "lab_free": False}]},
        )
        group = _OttoGroup(name="otto")
        names = group.list_commands(None)
        assert names.count("both-tool") == 1
    finally:
        CLI_COMMANDS.unregister("both-tool")


def test_cached_stub_group_serves_children(monkeypatch):
    """A cache-only GROUP rebuilds its children as stubs on the fast path,
    so `otto <plugin-group> <TAB>` completes subcommand names without
    bootstrap having run."""
    from otto.cli.main import _OttoGroup

    monkeypatch.setattr(
        "otto.cli.main.get_completion_names",
        lambda: {
            "commands": [
                {
                    "name": "cached-grp",
                    "help": "G.",
                    "lab_free": False,
                    "commands": [
                        {"name": "ping", "help": "Pong.", "options": []},
                        {
                            "name": "sub",
                            "help": "Nested.",
                            "commands": [{"name": "deep", "help": "", "options": []}],
                        },
                    ],
                }
            ]
        },
    )
    group = _OttoGroup(name="otto")
    cmd = group.get_command(None, "cached-grp")
    assert cmd is not None
    assert {"ping", "sub"} <= set(cmd.commands)
    assert "deep" in cmd.commands["sub"].commands


def test_cached_stub_leaf_serves_option_flags(monkeypatch):
    """A cache-only LEAF with cached options rebuilds them for --<TAB>."""
    from otto.cli.main import _OttoGroup

    monkeypatch.setattr(
        "otto.cli.main.get_completion_names",
        lambda: {
            "commands": [
                {
                    "name": "cached-leaf",
                    "help": "L.",
                    "lab_free": False,
                    "options": [
                        {
                            "name": "count",
                            "flags": ["--count"],
                            "kind": "int",
                            "default": 1,
                            "help": "How many.",
                        }
                    ],
                }
            ]
        },
    )
    group = _OttoGroup(name="otto")
    cmd = group.get_command(None, "cached-leaf")
    assert cmd is not None
    assert not hasattr(cmd, "commands")  # a leaf, not a group
    flag_decls = [opt for p in cmd.params for opt in getattr(p, "opts", [])]
    assert "--count" in flag_decls


def test_completion_descent_target_resolves_real(monkeypatch):
    """During completion, the pending dispatch target must resolve to the real
    command (importing its module) — completion of `otto run <TAB>` needs the
    real run group to list instructions."""
    from otto.cli.main import _OttoGroup

    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto run ")
    monkeypatch.setenv("COMP_CWORD", "2")
    monkeypatch.delitem(sys.modules, "otto.cli.run", raising=False)
    group = _OttoGroup(name="otto")

    class _FakeCtx:
        def __init__(self) -> None:
            self.meta: dict = {"_pending_subcmd_args": ["run"]}

    group.get_command(_FakeCtx(), "run")
    assert "otto.cli.run" in sys.modules


def test_completion_enumeration_stubs_command_named_as_option_value(monkeypatch):
    """A command name typed as an option VALUE must stay a stub during
    completion enumeration — it is not the descent target, and resolving it
    real imports an unrelated module chain on the fast path."""
    from otto.cli.main import _OttoGroup

    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto run --host monitor ")
    monkeypatch.setenv("COMP_CWORD", "4")
    monkeypatch.delitem(sys.modules, "otto.cli.monitor", raising=False)
    group = _OttoGroup(name="otto")

    class _FakeCtx:
        def __init__(self) -> None:
            self.meta: dict = {"_pending_subcmd_args": ["run", "--host", "monitor"]}

    cmd = group.get_command(_FakeCtx(), "monitor")
    assert cmd is not None
    assert "otto.cli.monitor" not in sys.modules, "option value must not trigger a real import"


# ── Task 7: lazy lab + leaf-invoke preamble ──────────────────────────────────


def test_subcommand_help_needs_no_lab_and_makes_no_output_dir(tmp_path, monkeypatch):
    """A subcommand ``--help`` must load no lab and create no per-invocation dir.

    ``--help`` exits during the leaf's own parse (click's eager help option), so
    the leaf-invoke preamble — which loads the lab, inits logging, creates the
    output dir, and gates reservations — never runs. This mirrors the e2e
    ``assert_no_output_dir`` contract but in-process.
    """
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0

    from otto.logger import management

    made = [p for p in tmp_path.rglob("*") if management._LOG_DIR_NAME_RE.match(p.name)]
    assert made == [], f"expected no output dir under {tmp_path}, found: {made}"


def test_option_value_equal_to_help_flag_is_not_sniffed(monkeypatch):
    """An option VALUE of the literal string ``--help`` must NOT short-circuit to help.

    Regression for the review's token-sniffing false-positive suspicion: the old
    pending-token scan for a lab-free ``--help`` invocation could mistake an
    option's *value* for a help request. With the sniffing deleted, a real
    command whose option value happens to be ``--help`` must still EXECUTE its
    body.

    Wired in-process against a scratch ``lab_free`` command (so the preamble runs
    its body without needing a real lab); the authoritative host-touching version
    lands as e2e in Task 13.
    """
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    seen: dict[str, str] = {}

    async def _scratch_echo(msg: Annotated[str, typer.Option("--msg")] = "") -> None:
        seen["msg"] = msg

    register_cli_command("scratch-help-value-echo", _scratch_echo, help="scratch", lab_free=True)
    try:
        result = runner.invoke(
            app,
            ["scratch-help-value-echo", "--msg", "--help"],
            env={"OTTO_LAB": ""},
        )
        assert result.exit_code == 0, result.output
        # The body ran and received the literal string, NOT a help page.
        assert seen == {"msg": "--help"}
        assert "Usage" not in result.output
    finally:
        CLI_COMMANDS.unregister("scratch-help-value-echo")
