"""``otto run`` merges verb-wide options into every instruction's flags.

A class registered for ``run`` adds its fields to every instruction command,
standalone ``@instruction`` and project instruction alike. The parsed values
are bound on the context (``ctx.options(Cls)``), and a parameter annotated with
a registered class is injected. An instruction's own ``options=`` class that
inherits a registered base shares that base's flag rather than doubling it.
"""

import sys
from typing import Annotated

import pytest
import typer

from otto import options
from otto.cli.run import instruction
from otto.context import OttoContext
from otto.params import OptionsRegistrationError, register_options
from tests._fixtures.paths import TESTS_ROOT
from tests.unit.cli.test_project_instruction_commands import _publish_the_six


@options
class RepoOptions:
    lab_env: Annotated[str, typer.Option(help="Lab environment.")] = "staging"


@options
class InstallOptions(RepoOptions):
    debug: bool = False


@options
class RivalOptions:
    debug: str = "x"


@pytest.fixture
def project_install_fixture():
    """Otto's project instructions, ``install`` among them, published."""
    _publish_the_six()


@pytest.fixture
def repo1_cli(monkeypatch, run_cli):
    """``run_cli`` over the real ``tests/repo1``, bootstrapped as ``otto`` would.

    ``OTTO_SUT_DIRS`` points at repo1 and ``bootstrap()`` imports its init
    modules, so its instructions and its ``register_options`` calls are the
    ones the command sees. repo1's init package is evicted first (and put back
    after), so a copy another test cached cannot turn the import into a no-op.
    """
    from otto import bootstrap as bs

    evicted = {
        m: sys.modules.pop(m) for m in list(sys.modules) if m.startswith("repo1_instructions")
    }
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("OTTO_SUT_DIRS", str(TESTS_ROOT / "repo1"))
    bs._reset()
    try:
        assert bs.bootstrap().errors == []
        yield run_cli
    finally:
        bs._reset()
        for name in [m for m in sys.modules if m.startswith("repo1_instructions")]:
            sys.modules.pop(name)
        sys.modules.update(evicted)


def test_repo1_install_has_one_lab_env_flag(repo1_cli):
    help_text = repo1_cli(["run", "install", "--help"]).output
    assert help_text.count("--lab-env") == 1


@pytest.mark.parametrize("command", ["test-instruction", "nc-smoke"])
def test_repo1_own_options_inheriting_repo_options_share_its_flags(repo1_cli, command):
    """repo1's own ``_Options(RepoOptions)`` classes meet the registered ``RepoOptions``.

    The inherited fields are one flag each, not a collision and not doubled.
    """
    result = repo1_cli(["run", command, "--help"])
    assert result.exit_code == 0, result.output
    assert result.output.count("--lab-env") == 1
    assert result.output.count("--device-type") == 1


def test_every_instruction_gains_the_run_flags(run_cli):
    register_options(RepoOptions, verbs=["run"])
    seen = {}

    @instruction()
    async def smoke(opts: RepoOptions) -> None:
        seen["lab_env"] = opts.lab_env

    result = run_cli(["run", "smoke", "--lab-env", "prod"])
    assert result.exit_code == 0, result.output
    assert seen == {"lab_env": "prod"}


def test_own_options_inheriting_a_registered_base_share_one_flag(run_cli):
    register_options(RepoOptions, verbs=["run"])
    seen = {}

    @instruction(options=InstallOptions)
    async def install(opts: InstallOptions, repo: RepoOptions) -> None:
        seen.update(own=(opts.lab_env, opts.debug), repo=repo.lab_env)

    help_text = run_cli(["run", "install", "--help"]).output
    assert help_text.count("--lab-env") == 1
    result = run_cli(["run", "install", "--lab-env", "prod", "--debug"])
    assert result.exit_code == 0, result.output
    assert seen == {"own": ("prod", True), "repo": "prod"}


def test_ctx_options_works_inside_an_instruction(run_cli):
    register_options(RepoOptions, verbs=["run"])
    seen = {}

    @instruction()
    async def peek(ctx: OttoContext) -> None:
        seen["lab_env"] = ctx.options(RepoOptions).lab_env

    assert run_cli(["run", "peek", "--lab-env", "qa"]).exit_code == 0
    assert seen == {"lab_env": "qa"}


def test_an_unhashable_annotation_is_an_ordinary_parameter(run_cli):
    """A registered class is matched by identity: metadata that cannot be hashed is fine."""
    register_options(RepoOptions, verbs=["run"])
    seen = {}

    @instruction()
    async def tagged(note: Annotated[str, typer.Option(), {"doc": "a tag"}] = "n") -> None:
        seen["note"] = note

    result = run_cli(["run", "tagged", "--note", "x"])
    assert result.exit_code == 0, result.output
    assert seen == {"note": "x"}


def test_an_own_field_colliding_with_a_run_flag_fails_before_the_body(run_cli):
    register_options(InstallOptions, verbs=["run"])
    ran = []

    @instruction(options=RivalOptions)
    async def clash(opts: RivalOptions) -> None:
        ran.append(True)

    result = run_cli(["run", "clash"])
    assert result.exit_code != 0
    assert "debug" in result.output
    assert "RivalOptions" in result.output
    assert ran == []


def test_a_collision_also_renders_on_the_commands_help(run_cli):
    """``otto run clash --help`` resolves the command too, and says why it can't."""
    register_options(InstallOptions, verbs=["run"])

    @instruction(options=RivalOptions)
    async def clash(opts: RivalOptions) -> None: ...

    result = run_cli(["run", "clash", "--help"])
    assert result.exit_code == 1
    assert "debug" in result.output
    assert "RivalOptions" in result.output
    assert "Traceback" not in result.output


def test_validation_exits_2_before_the_body(run_cli):
    @options
    class Positive:
        n: int = 1

        def __post_init__(self):
            if self.n < 1:
                raise ValueError("n must be >= 1")

    register_options(Positive, verbs=["run"])
    ran = []

    @instruction()
    async def body() -> None:
        ran.append(True)

    result = run_cli(["run", "body", "--n", "0"])
    assert result.exit_code == 2
    assert ran == []
    assert "n must be >= 1" in result.output, "exit 2 for the VALIDATION, not an unknown flag"


def test_project_instructions_gain_run_flags_too(run_cli, project_install_fixture):
    register_options(RepoOptions, verbs=["run"])
    assert "--lab-env" in run_cli(["run", "install", "--help"]).output


def test_a_project_instruction_binds_the_run_flags(run_cli, project_install_fixture, monkeypatch):
    """The published leaf binds the verb before the orchestrator runs any body."""
    from otto.context import get_context
    from otto.project import orchestrator

    register_options(RepoOptions, verbs=["run"])
    seen = {}

    async def _install(source):
        seen["lab_env"] = get_context().options(RepoOptions).lab_env

    monkeypatch.setattr(orchestrator, "install", _install)
    result = run_cli(["run", "install", "--lab-env", "prod"])
    assert result.exit_code == 0, result.output
    assert seen == {"lab_env": "prod"}


def test_registering_and_publishing_resolve_no_run_class():
    """The ``run`` classes resolve when a command is BUILT, never at registration.

    A class registered by string may import a heavy module, which only
    ``otto run`` should pay for; every other verb's startup decorates and
    publishes instructions too. The unimportable target makes a resolution
    visible: decorating and publishing survive it, building the app does not.
    """
    from otto.cli.run import build_instruction_app
    from otto.instructions import INSTRUCTIONS

    register_options("tests_unit_cli_absent_verb_options:Missing", verbs=["run"])

    @instruction()
    async def later() -> None: ...

    _publish_the_six()

    for name in ["later", "install"]:
        with pytest.raises(OptionsRegistrationError) as excinfo:
            build_instruction_app(INSTRUCTIONS.get(name))
        assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)


def test_the_completion_payload_lists_run_flags():
    register_options(RepoOptions, verbs=["run"])

    @instruction()
    async def smoke() -> None: ...

    from otto.config.completion_cache import collect_current_commands

    instructions = collect_current_commands()
    (entry,) = [e for e in instructions if e["name"] == "smoke"]
    assert "lab_env" in [o["name"] for o in entry["options"]]


def test_an_own_class_registered_for_the_verb_is_built_once(run_cli):
    """The own instance IS the bound one: one construction, one ``__post_init__``."""
    built = []

    @options
    class Counted:
        level: int = 1

        def __post_init__(self):
            built.append(self.level)

    register_options(Counted, verbs=["run"])
    seen = {}

    @instruction(options=Counted)
    async def once(opts: Counted, ctx: OttoContext) -> None:
        seen["same"] = opts is ctx.options(Counted)
        seen["level"] = opts.level

    result = run_cli(["run", "once", "--level", "3"])
    assert result.exit_code == 0, result.output
    assert seen == {"same": True, "level": 3}
    assert built == [3]


def test_an_own_class_registered_for_the_verb_is_built_once_under_dry_run(run_cli):
    """The ``-n`` twin: one options line, one construction, no body call.

    Item D.11 -- a leaf's own class is BOUND (``ctx.options(Cls)``) exactly
    once whether or not the invocation is a dry run: :func:`_wrap_with_options`
    builds the instance before branching on ``dry_run_requested``, so the same
    build feeds both the real body and ``finish_dry_run``. This proves that
    sharing holds under ``-n`` too, not just on the real path above.
    """
    built = []

    @options
    class Counted:
        level: int = 1

        def __post_init__(self):
            built.append(self.level)

    register_options(Counted, verbs=["run"])

    @instruction(options=Counted)
    async def once(opts: Counted, ctx: OttoContext) -> None:  # pragma: no cover -- must not run
        raise AssertionError("the dry run reached the body")

    dry = run_cli(["run", "once", "--level", "3"], dry_run=True)
    assert dry.exit_code == 0, dry.output
    out = flat(dry.output)
    assert out.count("Counted:") == 1, "one options line, not one per bound class"
    assert "Counted: level=3" in out
    assert built == [3], "the own class must be built exactly once under -n too"


def test_a_secret_default_reaches_the_instruction_as_its_value(run_cli):
    """Typer used to render the default through str(): the body got '**********'."""
    import click
    import pydantic

    @options
    class Vault:
        token: Annotated[pydantic.SecretStr, typer.Option(click_type=click.STRING)] = (
            pydantic.SecretStr("dflt")
        )

    register_options(Vault, verbs=["run"])
    seen = []

    @instruction()
    async def peek_secret(vault: Vault) -> None:
        seen.append(vault.token.get_secret_value())

    assert run_cli(["run", "peek-secret"]).exit_code == 0
    assert run_cli(["run", "peek-secret", "--token", "typed"]).exit_code == 0
    assert seen == ["dflt", "typed"]
    dry = run_cli(["run", "peek-secret"], dry_run=True)
    assert dry.exit_code == 0, dry.output
    assert "--token" not in dry.output
    assert "dflt" not in dry.output


def test_no_sensitive_default_reaches_the_run_cache_or_help(run_cli):
    """``otto run X --help`` and the cached instruction flags never carry a hidden default."""
    import json

    import pydantic

    from otto.config.completion_cache import collect_current_commands

    @options
    class Hidden:
        note: str = pydantic.Field(default_factory=lambda: "S3CR3T-FROM-FACTORY", repr=False)
        level: int = 2

    register_options(Hidden, verbs=["run"])

    @instruction()
    async def hush(h: Hidden) -> None:  # pragma: no cover -- only its flags are read
        pass

    help_text = run_cli(["run", "hush", "--help"]).output
    assert "S3CR3T" not in help_text
    assert "--note" in help_text
    [entry] = [c for c in collect_current_commands() if c["name"] == "hush"]
    assert {o["name"] for o in entry["options"]} >= {"note", "level"}
    assert "S3CR3T" not in json.dumps(entry)


def _serialized_cli() -> dict:
    from otto.cli.main import app
    from otto.config.completion_tree import serialize_tree

    return serialize_tree(typer.main.get_command(app)).tree


def test_the_completion_tree_survives_a_colliding_instruction():
    """One bad instruction costs its own flags, never root help or another verb's TAB."""
    register_options(InstallOptions, verbs=["run"])

    @instruction(options=RivalOptions)
    async def clash(opts: RivalOptions) -> None: ...

    @instruction()
    async def fine() -> None: ...

    tree = _serialized_cli()

    assert {"host", "run", "test"} <= set(tree["commands"])
    run = tree["commands"]["run"]["commands"]
    assert run["clash"] == {"name": "clash", "params": [], "commands": {}, "group": False}
    assert "--lab-env" in [f for p in run["fine"]["params"] for f in p["flags"]]


@pytest.mark.parametrize("broken", ["missing", "syntax error"])
def test_a_broken_run_ref_leaves_the_rebuild_standing(broken, tmp_path, monkeypatch):
    """An unimportable ``run`` class is contained by both rebuild readers, however it breaks."""
    from otto.config.completion_cache import collect_current_commands

    module = "tests_unit_cli_absent_verb_options"
    if broken == "syntax error":
        module = "tests_unit_cli_broken_verb_options"
        (tmp_path / f"{module}.py").write_text("def broken(:\n")
        monkeypatch.syspath_prepend(str(tmp_path))
    register_options(f"{module}:Missing", verbs=["run"])

    @instruction()
    async def smoke() -> None: ...

    instructions = collect_current_commands()
    (entry,) = [e for e in instructions if e["name"] == "smoke"]
    assert entry["options"] == []

    tree = _serialized_cli()
    assert {"host", "run", "test"} <= set(tree["commands"])
    assert tree["commands"]["run"]["commands"]["smoke"]["params"] == []


def test_otto_run_names_a_broken_run_ref_in_one_line(run_cli):
    register_options("tests_unit_cli_absent_verb_options:Missing", verbs=["run"])
    ran = []

    @instruction()
    async def smoke() -> None:
        ran.append(True)

    result = run_cli(["run", "smoke"])
    assert result.exit_code == 1
    assert "tests_unit_cli_absent_verb_options:Missing" in result.output
    assert "Traceback" not in result.output
    assert ran == []


# ---------------------------------------------------------------------------
# Task 4b: `otto run NAME -n` builds, validates and shows the options
# (spec §4.3, amended 2026-09-27) — same rules as a real run, before printing.
# ---------------------------------------------------------------------------


def flat(text: str) -> str:
    """Collapse rich's wrapping/padding so a line can be matched as one string."""
    return " ".join(text.split())


class TestDryRunBuildsAndShowsOptions:
    """``otto run NAME -n`` binds and builds options exactly as a real run does."""

    def test_an_invalid_value_fails_the_dry_run_exactly_like_a_real_run(self, run_cli):
        @options
        class Positive:
            n: int = 1

            def __post_init__(self) -> None:
                if self.n < 1:
                    raise ValueError("n must be >= 1")

        register_options(Positive, verbs=["run"])
        ran = []

        @instruction()
        async def body() -> None:
            ran.append(True)

        dry = run_cli(["run", "body", "--n", "0"], dry_run=True)
        assert dry.exit_code == 2, dry.output
        assert ran == [], "the dry run let the body run on an invalid value"
        assert "n must be >= 1" in dry.output, "exit 2 for the VALIDATION, not an unknown flag"
        assert "would run" not in dry.output, (
            "a bad value must never be reported as something that would run"
        )

        # POSITIVE CONTROL, same command, same validation: without -n the
        # identical bad value fails the identical way.
        real = run_cli(["run", "body", "--n", "0"], dry_run=False)
        assert real.exit_code == dry.exit_code
        assert "n must be >= 1" in real.output
        assert ran == []

    def test_an_override_and_a_default_both_resolve_under_options(self, run_cli):
        @options
        class Two:
            a: Annotated[str, typer.Option(help="a")] = "default-a"
            b: Annotated[str, typer.Option(help="b")] = "default-b"

        register_options(Two, verbs=["run"])
        ran = []

        @instruction()
        async def smoke() -> None:
            ran.append(True)

        dry = run_cli(["run", "smoke", "--a", "override"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        assert ran == [], "the dry run ran the body"
        out = flat(dry.output)
        assert "options:" in out
        assert "Two: a='override', b='default-b'" in out

    def test_own_class_and_a_verb_class_both_show_own_first(self, run_cli):
        register_options(RepoOptions, verbs=["run"])

        @instruction(options=InstallOptions)
        async def install(opts: InstallOptions, repo: RepoOptions) -> None: ...

        dry = run_cli(["run", "install", "--lab-env", "prod", "--debug"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        assert "InstallOptions: lab_env='prod', debug=True" in out
        assert "RepoOptions: lab_env='prod'" in out
        assert out.index("InstallOptions:") < out.index("RepoOptions:"), (
            "the own class must be listed before the verb's own registered class"
        )

    def test_a_secretstr_field_is_masked_never_the_raw_value(self, run_cli) -> None:
        import click
        import pydantic

        @options
        class CredOptions:
            token: Annotated[
                pydantic.SecretStr, typer.Option(click_type=click.STRING, help="A secret.")
            ] = pydantic.SecretStr("x")

        register_options(CredOptions, verbs=["run"])

        @instruction()
        async def smoke() -> None: ...

        dry = run_cli(["run", "smoke", "--token", "hunter2"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        # `<hidden>`, not `SecretStr('**********')`: `_options_line` masks
        # through the SAME `sensitive_field_names` set the `would run:` line
        # reads (fix round 2, item 2) -- one masking symbol everywhere,
        # rather than a pydantic-repr special case that a plain (non-pydantic)
        # dataclass's SecretStr-typed field could never have earned anyway
        # (its runtime value is a raw, uncoerced string).
        assert "CredOptions: token=<hidden>" in out
        # The WHOLE output, not just the options: line: the `would run:` echo
        # also masks a sensitive flag's value (item B.7), so no line
        # anywhere in this dry run may still carry the raw secret.
        assert "hunter2" not in out, "some line in the dry run leaked the raw secret"
        assert "--token <hidden>" in out, "the would run: line must still show the FLAG was given"

    def test_a_leaf_without_options_prints_no_options_section(self, run_cli):
        @instruction()
        async def bare() -> None: ...

        dry = run_cli(["run", "bare"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        assert "options:" not in flat(dry.output)

    def test_a_previewing_leaf_with_options_still_validates_under_n(self, run_cli):
        """``dry_run_preview=True`` runs the real body — no change (item 4) —
        so an invalid value still fails BEFORE the body, and a valid one
        reaches it."""

        @options
        class Positive:
            n: int = 1

            def __post_init__(self) -> None:
                if self.n < 1:
                    raise ValueError("n must be >= 1")

        register_options(Positive, verbs=["run"])
        ran = []

        @instruction()
        async def body() -> None:
            ran.append(True)

        body.__cli_dry_run_preview__ = True

        ok = run_cli(["run", "body", "--n", "3"], dry_run=True)
        assert ok.exit_code == 0, ok.output
        assert ran == [True], "a previewing leaf's body did not run under -n"

        ran.clear()
        bad = run_cli(["run", "body", "--n", "0"], dry_run=True)
        assert bad.exit_code == 2, bad.output
        assert ran == [], "an invalid value reached a previewing leaf's body"
        assert "n must be >= 1" in bad.output

    def test_a_self_finishing_leaf_still_never_runs_its_body_under_dry_run(self, run_cli):
        """POSITIVE CONTROL for the whole class: validating its own options never
        buys a self-finishing leaf a body call.

        Item D.12 -- renamed from ``test_a_dry_run_still_contacts_no_device``:
        that name (and its "no device contacted" framing) belonged to the old
        seam-only guard; every other test in this class already asserts
        ``ran == []`` for its own case, so this one's only remaining job is to
        pin the claim for the restructure's own leaf-side path (options bound
        AND built here, not skipped by the seam) rather than dropping coverage
        entirely.
        """
        register_options(RepoOptions, verbs=["run"])
        ran = []

        @instruction()
        async def smoke() -> None:
            ran.append(True)

        dry = run_cli(["run", "smoke", "--lab-env", "prod"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        assert ran == [], "a self-finishing leaf let its body run under --dry-run"

    def test_a_plain_dataclass_shows_the_real_typer_converted_values(self, run_cli) -> None:
        """Item A.5 -- a plain stdlib dataclass (no pydantic) built from
        typer's CONVERTED kwargs, never ``ctx.params``.

        ``Enum``, ``Path`` and ``list`` fields print the same objects a real
        run would build -- ``PosixPath(...)``, the enum MEMBER, a real list --
        and a ``__post_init__`` that reads ``self.where.name`` /
        ``self.mode.value`` (both of which only exist on the CONVERTED types,
        never on the raw strings ``ctx.params`` would have held) must not
        raise under ``-n``.
        """
        import dataclasses
        import enum
        from pathlib import Path

        class Mode(enum.Enum):
            FAST = "fast"
            SLOW = "slow"

        @dataclasses.dataclass
        class PlainOpts:
            mode: Annotated[Mode, typer.Option(help="mode")] = Mode.FAST
            where: Annotated[Path, typer.Option(help="path")] = Path("/tmp")
            tags: Annotated[list[str], typer.Option(help="tags")] = dataclasses.field(
                default_factory=list
            )

            def __post_init__(self) -> None:
                # A real-run-legal check that relies on typer's conversion --
                # AttributeError here means the leaf was built from raw,
                # unconverted strings instead.
                _ = self.where.name
                _ = self.mode.value

        ran = []

        @instruction(options=PlainOpts)
        async def plain(opts: PlainOpts) -> None:
            ran.append(True)

        dry = run_cli(
            [
                "run",
                "plain",
                "--mode",
                "slow",
                "--where",
                "/etc/otto",
                "--tags",
                "a",
                "--tags",
                "b",
            ],
            dry_run=True,
        )
        assert dry.exit_code == 0, dry.output
        assert ran == [], "the dry run ran the body"
        out = flat(dry.output)
        assert "options:" in out
        assert (
            "PlainOpts: mode=<Mode.SLOW: 'slow'>, where=PosixPath('/etc/otto'), "
            "tags=['a', 'b']" in out
        )

    def test_a_repr_false_field_is_masked_for_both_plain_and_options_classes(self, run_cli) -> None:
        """Item B.6 -- ``repr=False`` masks a field in the ``options:`` block,
        on a bare stdlib dataclass AND on an ``@options`` (pydantic) class.
        """
        import dataclasses

        @dataclasses.dataclass
        class PlainSecret:
            token: Annotated[str, typer.Option(help="t")] = dataclasses.field(
                default="hunter2", repr=False
            )

        ran = []

        @instruction(options=PlainSecret)
        async def plainsecret(opts: PlainSecret) -> None:
            ran.append(True)

        dry = run_cli(["run", "plainsecret", "--token", "hunter2"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        assert "PlainSecret: token=<hidden>" in out
        assert "hunter2" not in out

        @options
        class PydSecret:
            token: Annotated[str, typer.Option(help="t")] = dataclasses.field(
                default="hunter2", repr=False
            )

        @instruction(options=PydSecret)
        async def pydsecret(opts: PydSecret) -> None: ...

        dry2 = run_cli(["run", "pydsecret", "--token", "hunter2"], dry_run=True)
        assert dry2.exit_code == 0, dry2.output
        out2 = flat(dry2.output)
        assert "PydSecret: token=<hidden>" in out2
        assert "hunter2" not in out2

    def test_would_run_masks_a_repr_false_flags_value_too(self, run_cli) -> None:
        """Item B.7 -- the ``would run:`` echo masks the VALUE of a flag whose
        options field is ``repr=False``, keeping the flag itself visible.
        """
        import dataclasses

        @dataclasses.dataclass
        class PlainSecret:
            token: Annotated[str, typer.Option(help="t")] = dataclasses.field(
                default="default-token", repr=False
            )

        @instruction(options=PlainSecret)
        async def plainsecret2(opts: PlainSecret) -> None: ...

        dry = run_cli(["run", "plainsecret2", "--token", "hunter2"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        assert "would run: run plainsecret2 --token <hidden>" in out
        assert "hunter2" not in out, "some line still leaked the raw value"

    def test_dry_run_output_soft_wraps_a_long_value_at_a_narrow_terminal(
        self, run_cli, monkeypatch
    ) -> None:
        """Item B.8 -- the dry-run block prints with ``soft_wrap=True`` (like
        ``fail()``), so a long resolved value stays on ONE line when the
        console is narrow, instead of being hard-wrapped or cropped.

        Same self-proving control as ``test_instruction_ownership.py``'s
        narrow-terminal cases: assert the fixture value WOULD fold at 80
        columns under plain hard-wrapping before asserting the fix keeps it
        whole, so this case cannot pass by being too short to matter.
        """
        import io

        from rich.console import Console

        long_value = "x" * 200

        @options
        class LongOpts:
            path: Annotated[str, typer.Option(help="p")] = "default"

        register_options(LongOpts, verbs=["run"])

        @instruction()
        async def longone() -> None: ...

        width = 80
        buf = io.StringIO()
        Console(file=buf, width=width, no_color=True).print(f"    LongOpts: path={long_value!r}")
        hard_wrapped_lines = len(buf.getvalue().rstrip("\n").splitlines())
        assert hard_wrapped_lines > 1, (
            "the fixture value never reaches the wrap column, so this case cannot fail"
        )

        monkeypatch.setenv("COLUMNS", str(width))
        dry = run_cli(["run", "longone", "--path", long_value], dry_run=True)
        assert dry.exit_code == 0, dry.output
        assert f"path={long_value!r}" in dry.output, (
            "the long value was folded or cropped instead of staying on one line"
        )

    # ── Task 4b fix round 2, item 2: masking must see THROUGH a union/container ──

    def test_an_optional_secretstr_field_is_masked_in_would_run_too(self, run_cli) -> None:
        """``SecretStr | None`` -- a bare ``isinstance(bare, type)`` check misses this:
        the bare type hint is a ``UnionType``, never a ``type`` itself, so the old
        check silently treated the field as non-sensitive.
        """
        import dataclasses

        import click
        import pydantic

        @dataclasses.dataclass
        class OptSecret:
            token: Annotated[
                pydantic.SecretStr | None, typer.Option(click_type=click.STRING, help="t")
            ] = None

        @instruction(options=OptSecret)
        async def optsecret(opts: OptSecret) -> None: ...

        dry = run_cli(["run", "optsecret", "--token", "hunter2"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        assert "would run: run optsecret --token <hidden>" in out
        assert "hunter2" not in out, "some line still leaked the raw optional secret"

    def test_a_secretstr_list_field_is_masked_in_would_run_too(self, run_cli) -> None:
        """``list[SecretStr]`` -- same gap, through a container instead of a union."""
        import click
        import pydantic

        @options
        class ListSecret:
            tokens: Annotated[
                list[pydantic.SecretStr], typer.Option(click_type=click.STRING, help="t")
            ] = pydantic.Field(default_factory=list)

        @instruction(options=ListSecret)
        async def listsecret(opts: ListSecret) -> None: ...

        dry = run_cli(["run", "listsecret", "--tokens", "hunter2"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        assert "would run: run listsecret --tokens <hidden>" in out
        assert "hunter2" not in out, "some line still leaked a secret inside a list field"

    def test_a_pydantic_field_repr_false_is_masked_in_options(self, run_cli) -> None:
        """Item 5 -- the idiomatic ``pydantic.Field(repr=False)`` spelling (not just
        ``dataclasses.field(repr=False)``) also masks in the ``options:`` block.
        """
        import pydantic

        @options
        class PydSecret:
            token: Annotated[str, typer.Option(help="t")] = pydantic.Field(
                default="hunter2", repr=False
            )

        @instruction(options=PydSecret)
        async def pydsecret2(opts: PydSecret) -> None: ...

        dry = run_cli(["run", "pydsecret2", "--token", "hunter2"], dry_run=True)
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        assert "PydSecret: token=<hidden>" in out
        assert "hunter2" not in out


def _built_callback(name: str):
    """Return the callback ``otto run <name>`` is built with, from its registry entry.

    The decorator hands the handler back unchanged; the wrapper these tests
    read is the one ``build_instruction_app`` prepares.
    """
    from otto.cli.run import build_instruction_app
    from otto.instructions import INSTRUCTIONS

    (command,) = build_instruction_app(INSTRUCTIONS.get(name)).registered_commands
    return command.callback


class TestNoHiddenClickContextParameter:
    """Task 4b fix round 2, item 3: no ``__otto_click_ctx__`` on the public signature."""

    def test_an_instructions_own_signature_has_no_extra_parameter(self) -> None:
        """The wrapper's public ``inspect.signature`` is exactly the own + verb fields."""
        import inspect

        @options
        class P:
            n: int = 1

        @instruction(options=P)
        async def posi(opts: P) -> None: ...

        sig = inspect.signature(_built_callback("posi"))
        assert list(sig.parameters) == ["n"], sig

    def test_a_direct_call_bypassing_the_cli_still_works(self) -> None:
        """The built wrapper called as ``(n=3)`` -- no click Context, no CLI dispatch -- runs.

        The old hidden ``__otto_click_ctx__`` keyword-only parameter made a
        direct call like this raise ``TypeError: missing 1 required keyword-only
        argument``. Reading the CURRENT click context (``None`` when there is
        none) instead means a direct call needs nothing extra, and is never
        treated as a dry run.
        """
        import asyncio

        @options
        class P2:
            n: int = 1

        ran = []

        @instruction(options=P2)
        async def posi2(opts: P2) -> None:
            ran.append(opts.n)

        asyncio.run(_built_callback("posi2")(n=3))
        assert ran == [3]
