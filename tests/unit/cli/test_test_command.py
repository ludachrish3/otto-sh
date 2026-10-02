"""otto test: one command, positional names (spec §3, §4.3, §4.6, §5.4)."""

from pathlib import Path
from typing import Annotated

import pydantic
import pytest
import typer

from otto import options
from otto.params import OptionsCollisionError, register_options

_TREE_GLYPHS = "│├└─ "


def _ok():
    """A passing ``SuiteRunResult``, as a faked ``run_tests`` returns it."""
    from otto.suite.run import SuiteRunResult

    return SuiteRunResult(
        exit_code=0,
        junit_paths=[],
        stability_report=None,
        stability_unstable=False,
        output_dir=Path(),
    )


def _lines(output: str) -> list[str]:
    return [line.strip(_TREE_GLYPHS) for line in output.splitlines()]


@pytest.fixture
def captured_run(monkeypatch):
    """Replace ``run_tests`` with a recorder; return what each call received.

    Stands in for ``run_tests`` from its first act, exactly as
    ``tests/unit/cli/test_test.py``'s ``capture_cov`` does: the real preflight
    (:func:`otto.suite.run.prepare_run`) lives inside ``run_tests``, so a test
    asserting on its side effect (e.g. ``--overwrite-cov-dir`` actually
    clearing a directory) needs it to run even with ``run_tests`` faked.
    """
    calls: list[dict] = []

    def fake(names, **kw):
        from otto.suite.run import prepare_run

        prepare_run(kw["run_options"])
        calls.append({"names": list(names), **kw})
        return _ok()

    monkeypatch.setattr("otto.suite.run.run_tests", fake)
    return calls


# ── Names, flags and the usage rule ─────────────────────────────────────────


def test_names_run_and_flags_mix_freely(otto_test_cli, sut_repo):
    sut_repo(files={"tests/test_a.py": "def test_one(): pass\ndef test_two(): pass\n"})
    result = otto_test_cli(["test", "test_one", "--no-random", "test_two"])
    assert result.exit_code == 0, result.output


def test_names_and_run_flags_reach_run_tests(otto_test_cli, captured_run):
    result = otto_test_cli(["test", "test_one", "--iterations", "3", "TestB::test_x", "-m", "slow"])
    assert result.exit_code == 0, result.output
    (call,) = captured_run
    assert call["names"] == ["test_one", "TestB::test_x"]
    assert call["run_options"].iterations == 3
    assert call["run_options"].markers == "slow"


def test_markers_alone_select(otto_test_cli, captured_run):
    assert otto_test_cli(["test", "-m", "slow"]).exit_code == 0
    assert captured_run[0]["names"] == []


def test_no_names_and_no_marker_is_a_usage_error(otto_test_cli, captured_run):
    result = otto_test_cli(["test"])
    assert result.exit_code == 2
    assert "at least one test name or -m" in result.output
    assert captured_run == []


@pytest.fixture
def preamble_calls(monkeypatch):
    """Record every ``command_preamble`` / ``ensure_lab_session`` call, calling through."""
    from otto.cli import invoke

    calls: list[str] = []
    for name in ("command_preamble", "ensure_lab_session"):
        real = getattr(invoke, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(invoke, name, spy)
    return calls


def test_the_preamble_spy_sees_a_real_run(otto_test_cli, sut_repo, captured_run, preamble_calls):
    """The control for the two parse-time tests below: a run DOES reach the preamble.

    (The harness's spec is lab-free, so the preamble itself skips
    ``ensure_lab_session`` here; the preamble call is the one that matters.)
    """
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    assert otto_test_cli(["test", "test_d"]).exit_code == 0
    assert "command_preamble" in preamble_calls


def test_the_usage_error_needs_no_lab(otto_test_cli, preamble_calls):
    """Checked at parse time, before the preamble could load a lab or make a run dir."""
    result = otto_test_cli(["test"], lab=None)
    assert result.exit_code == 2
    assert "at least one test name or -m" in result.output
    assert preamble_calls == []


def test_list_tests_never_reaches_the_preamble(otto_test_cli, sut_repo, preamble_calls):
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["test", "--list-tests"], lab=None)
    assert result.exit_code == 0, result.output
    assert preamble_calls == []


def test_seed_with_no_random_is_a_usage_error(otto_test_cli, captured_run):
    result = otto_test_cli(["test", "--no-random", "--seed", "7", "test_x"])
    assert result.exit_code == 2
    assert "--seed" in result.output
    assert "--no-random" in result.output
    assert captured_run == []


def test_no_cov_with_a_coverage_destination_is_a_usage_error(otto_test_cli, captured_run):
    result = otto_test_cli(["test", "--no-cov", "--cov-report", "test_x"])
    assert result.exit_code == 2
    assert "--no-cov" in result.output
    assert captured_run == []


def test_unknown_name_suggests(otto_test_cli, sut_repo):
    sut_repo(files={"tests/test_u.py": "def test_reboot(): pass\n"})
    result = otto_test_cli(["test", "test_rebot"])
    assert result.exit_code == 2
    rendered = " ".join(line.strip("│ ") for line in result.output.splitlines())
    assert "did you mean: test_reboot" in rendered
    assert "Invalid value for NAMES" in rendered


def test_no_match_is_a_red_line_and_exit_1(otto_test_cli, monkeypatch):
    from otto.suite.run import NoTestsMatchedError

    def fake(names, **kw):
        raise NoTestsMatchedError("No tests matched the selection.")

    monkeypatch.setattr("otto.suite.run.run_tests", fake)
    result = otto_test_cli(["test", "-m", "nothing"])
    assert result.exit_code == 1
    assert "No tests matched the selection." in result.output


def test_a_failing_run_exits_with_its_code(otto_test_cli, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        "otto.suite.run.run_tests", lambda names, **kw: dataclasses.replace(_ok(), exit_code=1)
    )
    assert otto_test_cli(["test", "test_x"]).exit_code == 1


def test_a_monitor_refusal_at_run_time_exits_4(otto_test_cli, sut_repo, monkeypatch):
    """``--monitor`` over a lab with nothing to sample stops the run with pytest's usage code.

    The whole chain is real: the plugin's session fixture refuses through
    ``pytest.exit``, the in-process session returns its code, ``run_tests``
    folds it into the run's exit code and the command hands that to typer.
    Only the fleet walk is stubbed, to an empty lab.
    """
    sut_repo(files={"tests/test_m.py": "def test_m(): pass\n"})
    monkeypatch.setattr("otto.config.fleet.all_hosts", lambda *a, **kw: iter([]))
    result = otto_test_cli(["test", "test_m", "--monitor"])
    assert result.exit_code == pytest.ExitCode.USAGE_ERROR == 4, result.output
    assert "--monitor: No hosts available in the active lab" in result.output


def test_overwrite_cov_dir_reaches_run_options(otto_test_cli, captured_run, tmp_path):
    cov = tmp_path / "cov"
    cov.mkdir()
    (cov / "old").write_text("x")
    result = otto_test_cli(["test", "--cov-dir", str(cov), "--overwrite-cov-dir", "test_c"])
    assert result.exit_code == 0, result.output
    assert captured_run[0]["run_options"].overwrite_cov_dir is True
    assert list(cov.iterdir()) == []


def test_the_run_goes_to_the_contexts_output_dir(otto_test_cli, captured_run, tmp_path):
    assert otto_test_cli(["test", "test_x"]).exit_code == 0
    assert captured_run[0]["output_dir"] == tmp_path / "otto-out"


# ── Test-verb options ────────────────────────────────────────────────────────


def test_a_verb_option_is_a_flag_and_reaches_ctx_options(otto_test_cli, sut_repo):
    @options
    class FirmwareOpts:
        firmware: str = "latest"

    register_options(FirmwareOpts, verbs=["test"])
    sut_repo(
        files={
            "tests/test_f.py": (
                "from otto.params import verb_option_classes\n"
                "def test_f(ctx):\n"
                "    (o,) = verb_option_classes('test')\n"
                "    assert ctx.options(o.cls).firmware == '2.1'\n"
            )
        }
    )
    assert "--firmware" in otto_test_cli(["test", "--help"]).output
    result = otto_test_cli(["test", "--firmware", "2.1", "test_f"])
    assert result.exit_code == 0, result.output


def test_verb_option_instances_are_passed_to_run_tests(otto_test_cli, captured_run):
    @options
    class FirmwareOpts:
        firmware: str = "latest"

    register_options(FirmwareOpts, verbs=["test"])
    assert otto_test_cli(["test", "test_x", "--firmware", "2.1"]).exit_code == 0
    (instance,) = captured_run[0]["options"]
    assert isinstance(instance, FirmwareOpts)
    assert instance.firmware == "2.1"


def test_a_bad_verb_option_value_is_a_usage_error(otto_test_cli, captured_run):
    """A value click accepts but the options class's constraint refuses: exit 2, nothing run."""

    @options
    class CountOpts:
        count: int = pydantic.Field(default=1, gt=0)

    register_options(CountOpts, verbs=["test"])
    result = otto_test_cli(["test", "test_x", "--count", "0"])
    assert result.exit_code == 2
    assert "greater than 0" in " ".join(result.output.replace("│", " ").split())
    assert captured_run == []


def test_a_verb_option_named_like_a_run_flag_fails_before_any_test(otto_test_cli):
    @options
    class Clash:
        seed: int = 0

    register_options(Clash, verbs=["test"])
    with pytest.raises(OptionsCollisionError, match="otto test's --seed"):
        otto_test_cli(["test", "--help"])


def test_a_collision_names_the_flag_actually_typed(otto_test_cli):
    """``random_order`` is the ``--random/--no-random`` flag, not a ``--random-order``."""

    @options
    class Clash:
        random_order: bool = False

    register_options(Clash, verbs=["test"])
    with pytest.raises(OptionsCollisionError, match="otto test's --random/--no-random"):
        otto_test_cli(["test", "--help"])


def test_a_verb_option_named_names_collides(otto_test_cli):
    @options
    class Clash:
        names: str = ""

    register_options(Clash, verbs=["test"])
    with pytest.raises(OptionsCollisionError, match="otto test's NAMES"):
        otto_test_cli(["test", "--help"])


# ── --list-tests ─────────────────────────────────────────────────────────────


def test_list_tests_groups_and_needs_no_lab(otto_test_cli, sut_repo, tmp_path):
    sut_repo(
        files={
            "tests/test_g.py": "class TestG:\n    def test_1(self): pass\ndef test_plain(): pass\n"
        }
    )
    result = otto_test_cli(["test", "--list-tests"], lab=None)
    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert lines.index("test_g.py") < lines.index("TestG") < lines.index("test_1")
    assert "test_plain" in lines
    assert not list(tmp_path.glob("**/test/*"))


def test_list_tests_keeps_nested_directories_and_classes(otto_test_cli, sut_repo):
    sut_repo(
        files={
            "tests/router/test_basic.py": (
                "class TestOuter:\n    class TestInner:\n        def test_deep(self): pass\n"
            )
        }
    )
    result = otto_test_cli(["test", "--list-tests"], lab=None)
    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    order = [
        lines.index(x) for x in ("router/test_basic.py", "TestOuter", "TestInner", "test_deep")
    ]
    assert order == sorted(order)


def test_list_tests_with_an_unknown_name_suggests_like_a_run(otto_test_cli, sut_repo):
    """A typo'd name is refused with the run's did-you-mean, not answered with an empty tree."""
    sut_repo(files={"tests/test_h.py": "def test_alpha(): pass\ndef test_beta(): pass\n"})
    result = otto_test_cli(["test", "--list-tests", "test_alhpa", "test_beta"], lab=None)
    assert result.exit_code == 2
    flat = " ".join(result.output.replace("│", " ").split())
    assert "'test_alhpa' (did you mean: test_alpha" in flat
    assert "test_h.py" not in _lines(result.output)


def test_list_tests_filters_by_names_and_markers(otto_test_cli, sut_repo):
    sut_repo(
        files={
            "tests/test_h.py": (
                "import pytest\n"
                "class TestH:\n"
                "    @pytest.mark.slow\n"
                "    def test_a(self): pass\n"
                "    def test_b(self): pass\n"
                "def test_c(): pass\n"
            ),
            "pyproject.toml": "[tool.pytest.ini_options]\nmarkers = ['slow']\n",
        }
    )
    named = _lines(otto_test_cli(["test", "--list-tests", "TestH"], lab=None).output)
    assert "test_a" in named
    assert "test_b" in named
    assert "test_c" not in named
    marked = _lines(otto_test_cli(["test", "--list-tests", "-m", "slow"], lab=None).output)
    assert "test_a" in marked
    assert "test_b" not in marked
    assert "test_c" not in marked


def test_list_tests_never_reaches_run_tests(otto_test_cli, sut_repo, captured_run):
    sut_repo(files={"tests/test_l.py": "def test_l(): pass\n"})
    assert otto_test_cli(["test", "--list-tests", "test_l"]).exit_code == 0
    assert captured_run == []


# ── The dry run ──────────────────────────────────────────────────────────────


def test_dry_run_never_reaches_run_tests(otto_test_cli, sut_repo, captured_run):
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["-n", "test", "test_d"])
    assert result.exit_code == 0, result.output
    assert captured_run == []
    assert "would run: otto test test_d" in result.output


@pytest.mark.parametrize("flag", ["--cov-dir", "--cov-report-dir"])
def test_a_dry_run_neither_clears_nor_creates_a_destination(
    otto_test_cli, sut_repo, tmp_path, flag
):
    """`-n` validates a destination as a run would, and never touches it."""
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("keep")
    overwrite = f"--overwrite-{flag.lstrip('-')}"
    result = otto_test_cli(["-n", "test", "test_d", flag, str(full), overwrite])
    assert result.exit_code == 0, result.output
    assert (full / "keep.txt").read_text() == "keep"

    missing = tmp_path / "missing" / "deeper"
    result = otto_test_cli(["-n", "test", "test_d", flag, str(missing)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("flag", ["--cov-dir", "--cov-report-dir"])
def test_a_dry_run_refuses_a_full_destination_like_a_run(otto_test_cli, sut_repo, tmp_path, flag):
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("keep")
    result = otto_test_cli(["-n", "test", "test_d", flag, str(full)])
    assert result.exit_code == 2
    assert f"--overwrite-{flag.lstrip('-')}" in " ".join(result.output.replace("│", " ").split())
    assert (full / "keep.txt").exists()


def test_a_dry_run_writes_no_results_or_monitor_file(otto_test_cli, sut_repo, tmp_path):
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    results = tmp_path / "out" / "junit.xml"
    monitor = tmp_path / "mon" / "m.json"
    result = otto_test_cli(
        ["-n", "test", "test_d", "--results", str(results), "--monitor-output", str(monitor)]
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "mon").exists()


def test_dry_run_echoes_an_off_switch(otto_test_cli, sut_repo):
    """`--no-random` turns a default-on flag off; the echo must say so, not drop it."""
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["-n", "test", "test_d", "--no-random"])
    assert result.exit_code == 0, result.output
    assert "would run: otto test test_d --no-random" in result.output


def test_dry_run_shows_every_resolved_verb_option(otto_test_cli, sut_repo):
    @options
    class FirmwareOpts:
        firmware: str = "latest"
        retries: int = 3

    register_options(FirmwareOpts, verbs=["test"])
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["-n", "test", "test_d", "--firmware", "2.1"])
    assert result.exit_code == 0, result.output
    assert "options:" in result.output
    assert "FirmwareOpts: firmware='2.1', retries=3" in result.output


def test_dry_run_validates_verb_options_as_a_run_would(otto_test_cli, sut_repo):
    """The options class's own constraint, not click's type check, refuses under -n too."""

    @options
    class CountOpts:
        count: int = pydantic.Field(default=1, gt=0)

    register_options(CountOpts, verbs=["test"])
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["-n", "test", "test_d", "--count", "0"])
    assert result.exit_code == 2
    assert "greater than 0" in " ".join(result.output.replace("│", " ").split())
    assert "pytest collected these tests" not in result.output


def test_dry_run_masks_secret_and_repr_false_fields(otto_test_cli, sut_repo):
    import click

    @options
    class Creds:
        token: Annotated[pydantic.SecretStr, typer.Option(click_type=click.STRING)] = (
            pydantic.SecretStr("")
        )
        note: Annotated[str, typer.Option()] = pydantic.Field(default="", repr=False)

    register_options(Creds, verbs=["test"])
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["-n", "test", "test_d", "--token", "hunter2", "--note", "sekrit"])
    assert result.exit_code == 0, result.output
    assert "hunter2" not in result.output
    assert "sekrit" not in result.output
    assert "token=<hidden>" in result.output
    assert "note=<hidden>" in result.output
    assert "--token <hidden>" in result.output
    assert "--note <hidden>" in result.output


def _secret_default_class():
    import click

    @options
    class Vault:
        token: Annotated[pydantic.SecretStr, typer.Option(click_type=click.STRING)] = (
            pydantic.SecretStr("dflt")
        )

    return Vault


def test_a_secret_default_reaches_the_test_as_its_value(otto_test_cli, sut_repo, captured_run):
    """Typer used to render the default through str(): the test got '**********'."""
    vault = _secret_default_class()
    register_options(vault, verbs=["test"])
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["test", "test_d"])
    assert result.exit_code == 0, result.output
    [call] = captured_run
    [inst] = [o for o in call["options"] if isinstance(o, vault)]
    assert inst.token.get_secret_value() == "dflt"

    captured_run.clear()
    result = otto_test_cli(["test", "test_d", "--token", "typed"])
    assert result.exit_code == 0, result.output
    [inst] = [o for o in captured_run[0]["options"] if isinstance(o, vault)]
    assert inst.token.get_secret_value() == "typed"


def test_an_untyped_secret_flag_is_not_echoed(otto_test_cli, sut_repo):
    register_options(_secret_default_class(), verbs=["test"])
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["-n", "test", "test_d"])
    assert result.exit_code == 0, result.output
    assert "would run: otto test test_d\n" in result.output
    assert "dflt" not in result.output
    assert "token=<hidden>" in result.output
    help_text = otto_test_cli(["test", "--help"], lab=None).output
    assert "dflt" not in help_text
    assert "*****" not in help_text


def test_dry_run_probe_announces_even_with_no_hosts(otto_test_cli, sut_repo, monkeypatch):
    """A previewing leaf never drops ``--probe``: its announcement prints, no hosts included."""
    from otto.cli.probe import PROBE_NO_HOSTS

    monkeypatch.setattr("otto.cli.invoke.probe_requested", lambda ctx: True)
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(["-n", "test", "test_d"])
    assert result.exit_code == 0, result.output
    assert PROBE_NO_HOSTS in result.output
    assert result.output.index(PROBE_NO_HOSTS) < result.output.index("would run:")


def test_the_leaf_previews_and_finishes_its_own_dry_run():
    from otto.cli import test as cli_test
    from otto.cli.invoke import DRY_RUN_PREVIEW_ATTR, DRY_RUN_SELF_FINISHING_ATTR

    leaf = cli_test.test_app.registered_commands[0].callback
    assert getattr(leaf, DRY_RUN_PREVIEW_ATTR) is True
    assert getattr(leaf, DRY_RUN_SELF_FINISHING_ATTR) is True


# ── The command's shape ──────────────────────────────────────────────────────


def test_the_command_is_one_leaf_with_the_documented_help():
    import typer.main

    from otto.cli import test as cli_test
    from otto.cli.builtin_commands import register_builtin_commands
    from otto.cli.registry import CLI_COMMANDS

    register_builtin_commands()
    spec = CLI_COMMANDS.get("test")
    assert spec.loader == "otto.cli.test:test_app"
    assert spec.help == "Run tests by name or marker expression."
    assert not spec.async_leaves
    command = typer.main.get_command(cli_test.test_app)
    assert isinstance(command, cli_test._OttoTestCommand)
    assert not hasattr(command, "commands")


def test_test_app_is_built_on_each_access():
    from otto.cli import test as cli_test

    assert cli_test.test_app is not cli_test.test_app


def test_help_lists_names_and_run_flags(otto_test_cli):
    result = otto_test_cli(["test", "--help"], lab=None)
    assert result.exit_code == 0
    for flag in ("NAMES", "--markers", "--iterations", "--list-tests", "--cov", "--monitor"):
        assert flag in result.output
    for gone in ("--tests", "--list-suites"):
        assert gone not in result.output


def test_help_states_the_interval_floor_as_the_errors_do(otto_test_cli, monkeypatch):
    """``1.0s``, as ``otto monitor --help`` and the refusal both spell it — never ``1s``."""
    from tests.unit.cli.conftest import _flat

    monkeypatch.setenv("COLUMNS", "300")
    result = otto_test_cli(["test", "--help"], lab=None)
    assert result.exit_code == 0
    assert "Sampling interval for --monitor (at least 1.0s)." in _flat(result.output)


def test_the_cache_serialises_the_test_verbs_flags_and_the_fast_path_rebuilds_them():
    """`names.test_options` round-trips: serialised at rebuild, a flag again on the fast path."""
    from otto.cli import test as cli_test
    from otto.config.completion_cache import collect_test_verb_options

    @options
    class Firmware:
        firmware: str = "latest"
        retries: Annotated[int, typer.Option("--retries", help="How often.")] = 3

    register_options(Firmware, verbs=["test"])
    cached = collect_test_verb_options()
    assert cached == [
        {"name": "firmware", "flags": [], "kind": "str", "default": "latest", "help": ""},
        {
            "name": "retries",
            "flags": ["--retries"],
            "kind": "int",
            "default": 3,
            "help": "How often.",
        },
    ]
    command = typer.main.get_command(cli_test.build_test_app(cached))
    flags = {opt for p in command.params for opt in p.opts}
    assert {"--firmware", "--retries", "--random"} <= flags


def test_a_colliding_test_verb_caches_no_flags():
    from otto.config.completion_cache import collect_test_verb_options

    @options
    class Clash:
        seed: int = 1

    register_options(Clash, verbs=["test"])
    assert collect_test_verb_options() == []


@pytest.mark.parametrize(
    "argv",
    [
        ["test"],
        ["test", "--no-random", "--seed", "7", "test_x"],
        ["test", "--no-cov", "--cov-report", "test_x"],
        ["test", "--list-tests", "test_nope"],
    ],
    ids=["no-name", "seed-no-random", "no-cov-contradiction", "list-unknown"],
)
def test_a_parse_time_usage_error_prints_usage_and_try(otto_test_cli, sut_repo, argv):
    """Like click's own usage errors: the Usage line and the Try-help hint, not the panel alone."""
    sut_repo(files={"tests/test_d.py": "def test_d(): pass\n"})
    result = otto_test_cli(argv, lab=None)
    assert result.exit_code == 2
    flat = " ".join(result.output.replace("│", " ").split())
    assert "Usage: otto test" in flat
    assert "Try 'otto test -h' for help" in flat


def _probe_options():
    """The reviewer's probe shape: a plain flag, a SecretStr, and two ``repr=False`` defaults."""
    import click

    @options
    class Probe:
        firmware: Annotated[str, typer.Option(help="Firmware.")] = "latest"
        token: Annotated[pydantic.SecretStr, typer.Option(click_type=click.STRING)] = (
            pydantic.SecretStr("S3CR3T-TOKEN")
        )
        note: Annotated[str, typer.Option(help="Hidden note.")] = pydantic.Field(
            default_factory=lambda: "S3CR3T-FROM-FACTORY", repr=False
        )
        plain: str = pydantic.Field(default="S3CR3T-PLAIN", repr=False)

    return Probe


def test_no_sensitive_default_reaches_the_cache_or_the_help(otto_test_cli):
    """A repr=False or secret default is never written to the cache file, nor shown by --help."""
    import json

    from otto.config.completion_cache import collect_test_verb_options

    register_options(_probe_options(), verbs=["test"])
    cached = collect_test_verb_options()
    assert "S3CR3T" not in json.dumps(cached)
    help_text = otto_test_cli(["test", "--help"], lab=None).output
    assert "S3CR3T" not in help_text
    assert "latest" in help_text, "a plain default still shows"


def test_a_secret_flag_keeps_every_other_flag_cached():
    """One SecretStr field (an explicit click_type) used to drop the verb's whole flag list."""
    from otto.config.completion_cache import collect_test_verb_options

    register_options(_probe_options(), verbs=["test"])
    cached = {opt["name"]: opt for opt in collect_test_verb_options()}
    assert set(cached) == {"firmware", "token", "note", "plain"}
    assert cached["token"]["kind"] == "str"
    assert cached["firmware"]["default"] == "latest"
