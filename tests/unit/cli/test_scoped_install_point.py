"""The scope verdict: recorded before any context, built through one seam, the same everywhere.

A command is scoped unless its registration or its leaf opts out
(``otto.cli.invoke.leaf_is_scoped``). The root dispatch records the resolved
spec before any group callback or parse-time probe runs, so the first context
an invocation builds already has the command's verdict; the preamble refines
it to the leaf's, and a context a probe built with the command's verdict is
replaced over the same lab, with no second build and no second gate. Every
builder of a run's context goes through ``otto.run_context.new_run_context``.

The names this verdict adds are imported inside each test.
"""

import ast
import asyncio
import contextlib
import dataclasses
import importlib
from collections.abc import Callable, Generator
from datetime import datetime, timezone
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import otto
from otto.cli.main import app
from otto.cli.registry import CLI_COMMANDS, register_cli_command
from otto.labs.loader import Lab
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.paths import CHECKOUT_ROOT

runner = CliRunner()

_SRC = CHECKOUT_ROOT / "src" / "otto"


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(CLI_COMMANDS.names())
    yield
    for name in set(CLI_COMMANDS.names()) - before:
        CLI_COMMANDS.unregister(name=name)


def _counted(monkeypatch: pytest.MonkeyPatch, target: str) -> "list[Any]":
    """Wrap the function at dotted *target* so each call is counted; return the call list."""
    module_name, attr = target.rsplit(".", 1)
    module = importlib.import_module(module_name)
    real = getattr(module, attr)
    calls: "list[Any]" = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs or args)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, attr, _spy)
    return calls


@contextlib.contextmanager
def _no_context() -> "Generator[None, None, None]":
    """Run the block with no context installed (the CLI tests' autouse stub set aside)."""
    from otto import invocation

    token = invocation._ACTIVE.set(None)
    try:
        yield
    finally:
        invocation._ACTIVE.reset(token)


_OPT_OUTS = (None, "registration", "leaf")
"""How a CLI case says it is unscoped: not at all, at its registration, or on its leaf."""


def _registration_keywords(opt_out: "str | None") -> "dict[str, Any]":
    return {"scoped": False} if opt_out == "registration" else {}


def _mark_leaf(leaf: Any, opt_out: "str | None") -> None:
    from otto.cli.invoke import SCOPED_ATTR

    if opt_out == "leaf":
        setattr(leaf, SCOPED_ATTR, False)


def _dispatched_context(name: str, *, opt_out: "str | None" = None) -> Any:
    """Register a lab-bound leaf *name*, dispatch it, and return the context its body saw.

    *opt_out* ``None`` registers it saying nothing about scope: scoped by omission.
    """
    seen: "list[Any]" = []

    async def leaf() -> None:
        from otto.context import get_context

        seen.append(get_context())

    _mark_leaf(leaf, opt_out)
    register_cli_command(name, leaf, gate=False, **_registration_keywords(opt_out))
    result = runner.invoke(app, ["--lab", "test_lab", name])
    assert result.exit_code == 0, result.output
    [ctx] = seen
    return ctx


def _lab_free_opener_context(name: str, *, opens: str, opt_out: "str | None") -> Any:
    """Dispatch a lab-free group's leaf that opens the lab itself; return the context it got.

    *opens* is how it opens it: ``"inline"`` (``ensure_inline_lab``, as
    ``otto host --list-hosts`` does), ``"live"`` (``open_live_lab``, as
    ``otto monitor --live`` does) or ``"context"`` (``ensure_lab_context``, as
    ``otto reservation check`` does). The preamble skips the lab for a
    lab-free leaf, so the only verdict this context can carry is the one the
    preamble recorded before that skip.
    """
    from otto.cli import invoke

    seen: "list[Any]" = []

    def go(ctx: typer.Context) -> None:
        from otto.context import get_context

        if opens == "inline":
            invoke.ensure_inline_lab(ctx)
        elif opens == "live":
            invoke.open_live_lab(ctx, invoke.command_spec(ctx), gate=True)
        else:
            with invoke.lab_context_refusals():
                invoke.ensure_lab_context(ctx)
        seen.append(get_context())

    _mark_leaf(go, opt_out)
    group = typer.Typer(name=name)

    @group.callback()
    def _group() -> None:
        pass

    group.command("go")(go)
    register_cli_command(
        name,
        group,
        lab_free=True,
        output_dir=False,
        gate=False,
        **_registration_keywords(opt_out),
    )
    result = runner.invoke(app, ["--lab", "test_lab", name, "go"])
    assert result.exit_code == 0, result.output
    [ctx] = seen
    return ctx


# ── One install point each: what the context it builds answers ───────────────


@dataclasses.dataclass(frozen=True)
class _Answer:
    """The context one install point built, and the verdict it should carry."""

    ctx: Any
    expected: bool


@dataclasses.dataclass(frozen=True)
class _InstallPoint:
    """One way a run's context is built, and how to drive it."""

    id: str
    drive: "Callable[[pytest.FixtureRequest], _Answer]"


def _lab_bound(opt_out: "str | None") -> _InstallPoint:
    def drive(request: pytest.FixtureRequest) -> _Answer:
        request.getfixturevalue("real_main_mocks")
        ctx = _dispatched_context(f"t6-bound-{opt_out or 'scoped'}", opt_out=opt_out)
        return _Answer(ctx, opt_out is None)

    return _InstallPoint(f"cli-preamble-{opt_out or 'scoped'}", drive)


def _lab_free(opens: str, opt_out: "str | None") -> _InstallPoint:
    def drive(request: pytest.FixtureRequest) -> _Answer:
        request.getfixturevalue("real_main_mocks")
        ctx = _lab_free_opener_context(
            f"t6-free-{opens}-{opt_out or 'scoped'}", opens=opens, opt_out=opt_out
        )
        return _Answer(ctx, opt_out is None)

    return _InstallPoint(f"cli-lab-free-{opens}-{opt_out or 'scoped'}", drive)


def _root_flag(request: pytest.FixtureRequest) -> _Answer:
    """``otto --lab L --list-hosts``: the root flag loads the lab with no command resolved."""
    request.getfixturevalue("real_main_mocks")
    installed = request.getfixturevalue("contexts_at_close")
    result = runner.invoke(app, ["--lab", "test_lab", "--list-hosts"])
    assert result.exit_code == 0, result.output
    return _Answer(installed[-1], True)


def _monitor_live(request: pytest.FixtureRequest) -> _Answer:
    """The shipped ``otto monitor --live``: a scoped, lab-free command that opens its lab."""
    from otto.monitor.live import LiveReport

    request.getfixturevalue("real_main_mocks")
    seen: "list[Any]" = []

    async def _run_live(**_kwargs: Any) -> LiveReport:
        from otto.context import get_context

        seen.append(get_context())
        now = datetime.now(tz=timezone.utc)
        return LiveReport(session_id="s", hosts=[], db=None, start=now, end=now)

    request.getfixturevalue("monkeypatch").setattr("otto.monitor.live.run_live", _run_live)
    result = runner.invoke(app, ["--lab", "test_lab", "monitor", "--live"])
    assert result.exit_code == 0, result.output
    [ctx] = seen
    return _Answer(ctx, True)


def _reservation_check(request: pytest.FixtureRequest) -> _Answer:
    """The shipped ``otto reservation check``: a scoped, lab-free command that loads its lab."""
    request.getfixturevalue("real_main_mocks")
    installed = request.getfixturevalue("contexts_at_close")
    result = runner.invoke(app, ["--lab", "test_lab", "reservation", "check"])
    assert result.exit_code == _RESERVATION_CHECK_EXIT, result.output
    return _Answer(installed[-1], True)


_RESERVATION_CHECK_EXIT = 0
"""What ``otto reservation check`` exits with in the ``real_main_mocks`` world."""


def _open_context(request: pytest.FixtureRequest) -> _Answer:
    """The library ``open_context``: it takes no verdict, so it builds the default."""
    install_composed(request.getfixturevalue("monkeypatch"), fake_bootstrap_result([]))

    async def _opened() -> Any:
        async with otto.open_context(lab=Lab(name="rig")) as ctx:
            return ctx

    return _Answer(asyncio.run(_opened()), True)


def _run_tests_reusing(request: pytest.FixtureRequest) -> _Answer:
    """A library test run reuses the installed context, whose own verdict (opted out) stands."""
    from otto.config.scope import ScopeSelection
    from otto.run_context import new_run_context, reset_context, set_context
    from otto.suite.run import _session_context

    outer = new_run_context(
        lab=Lab(name="rig"),
        selection=ScopeSelection(labs=("rig",), include=(), exclude=()),
        policy=None,
        bootstrap=fake_bootstrap_result([]),
        scoped=False,
    )
    binding = set_context(outer)
    try:
        with _session_context(request.getfixturevalue("tmp_path")) as ctx:
            assert ctx is outer
    finally:
        reset_context(binding)
    return _Answer(ctx, False)


def _run_tests_building(request: pytest.FixtureRequest) -> _Answer:
    """A library test run with no context builds its own; it takes no verdict: the default."""
    from otto.suite.run import _session_context

    with _no_context(), _session_context(request.getfixturevalue("tmp_path")) as ctx:
        pass
    return _Answer(ctx, True)


_INSTALL_POINTS = [
    *(_lab_bound(opt_out) for opt_out in _OPT_OUTS),
    *(
        _lab_free(opens, opt_out)
        for opens in ("inline", "live", "context")
        for opt_out in _OPT_OUTS
    ),
    _InstallPoint("cli-root-flag-default", _root_flag),
    _InstallPoint("shipped-monitor-live-default", _monitor_live),
    _InstallPoint("shipped-reservation-check-default", _reservation_check),
    _InstallPoint("library-open-context-default-only", _open_context),
    _InstallPoint("library-run-tests-reusing-opted-out", _run_tests_reusing),
    _InstallPoint("library-run-tests-building-default-only", _run_tests_building),
]


@pytest.mark.parametrize("point", _INSTALL_POINTS, ids=[point.id for point in _INSTALL_POINTS])
def test_install_point_same_answer(point: _InstallPoint, request: pytest.FixtureRequest):
    """Every place a run's context is built answers ``scoped`` as its command's verdict says.

    The CLI points take the same three inputs: scoped by omission, opted out
    at the registration, and opted out on the leaf alone. They are the
    preamble of a lab-bound leaf and a lab-free leaf that opens its own lab
    each of the three ways a shipped command does (so the leaf's verdict must
    be recorded before the preamble skips the lab). Opted out, every one
    answers ``False``. The root flag and the two shipped lab-free commands
    are scoped. The library points take no verdict: ``open_context`` and a
    test run that builds its own context answer the default (``-default-only``),
    and a test run that reuses a context keeps that context's own value.
    """
    answer = point.drive(request)
    assert answer.ctx.scoped is answer.expected


# ── The verdict is recorded before any group runs ────────────────────────────


def test_verdict_recorded_before_group_parse(real_main_mocks):
    """The spec is on ``ctx.meta`` while the group is still parsing its own options.

    A parse-time probe (``otto host <id>``'s class lookup) builds a lab context
    before the group callback runs, so the verdict has to be there first.
    """
    from otto.cli.invoke import leaf_is_scoped, recorded_spec

    seen: "list[Any]" = []

    def _parse_time(ctx: typer.Context, value: bool) -> bool:
        seen.append(recorded_spec(ctx))
        return value

    group = typer.Typer(name="t6-early")

    @group.callback()
    def _group(early: bool = typer.Option(False, "--early", callback=_parse_time)) -> None:
        seen.append("callback")

    @group.command()
    def leaf() -> None:
        seen.append("leaf")

    register_cli_command(
        "t6-early", group, lab_free=True, output_dir=False, gate=False, scoped=False
    )
    result = runner.invoke(app, ["t6-early", "leaf"])
    assert result.exit_code == 0, result.output
    spec = CLI_COMMANDS.get("t6-early")
    assert seen[1:] == ["callback", "leaf"]
    assert seen[0] is spec
    assert leaf_is_scoped(seen[0], None) is False


def test_preamble_verdict_reads_the_dispatched_spec():
    """Fail-first: the preamble judges the leaf with the spec it dispatched, as the gate is judged.

    A harness that wraps a command without the root dispatch records no spec
    for parse time; a command registered ``scoped=False`` there must still
    dispatch its leaf unscoped.
    """
    from otto.cli.invoke import _scope_verdict_in_force
    from tests._fixtures.dispatch import DispatchRunner

    seen: "list[bool]" = []
    group = typer.Typer(name="t13-harness")

    @group.callback()
    def _group() -> None:
        pass

    @group.command()
    def leaf(ctx: typer.Context) -> None:
        seen.append(_scope_verdict_in_force(ctx))

    unscoped = DispatchRunner().invoke(group, ["leaf"], scoped=False, dry_run_preview=False)
    scoped = DispatchRunner().invoke(group, ["leaf"], dry_run_preview=False)

    assert unscoped.exit_code == 0, unscoped.output
    assert scoped.exit_code == 0, scoped.output
    assert seen == [False, True]


def test_opted_out_leaf_after_parse_time_probe_dispatches_unscoped(
    real_main_mocks, monkeypatch, contexts_at_close
):
    """A leaf that opts out under a probing group runs unscoped: one build, one gate.

    The group's option probes the lab at parse time, as ``otto host <id>``
    does, so the first context has the group's verdict (scoped). The leaf says
    ``__cli_scoped__ = False``: its preamble replaces that context over the
    same lab, without building the lab or the gate again, and the probe's
    context is reset when it is replaced. The leaf's option value reaches its
    body through the replacement.
    """
    from otto.cli.invoke import SCOPED_ATTR, prepare_command_target, try_ensure_lab

    probed: "list[Any]" = []

    def _probe(ctx: typer.Context, value: str) -> str:
        probed.append(try_ensure_lab(ctx))
        return value

    @dataclasses.dataclass
    class Knob:
        level: int = 0

    seen: "list[Any]" = []

    async def opted_out(knob: Knob) -> None:
        from otto.context import get_context

        seen.append((get_context(), otto.get_options(Knob).level, knob.level))

    target = prepare_command_target(opted_out, Knob)
    setattr(target, SCOPED_ATTR, False)

    group = typer.Typer(name="t6-probed")

    @group.callback()
    def _group(target_id: str = typer.Option("", "--target", callback=_probe)) -> None:
        pass

    group.command("opted-out")(target)

    builds = _counted(monkeypatch, "otto.session.lab.build_lab_and_view")
    gates = _counted(monkeypatch, "otto.reservations.factory.build_reservation_gate")
    presented = _counted(monkeypatch, "otto.cli.invoke.present_reservation_gate")
    register_cli_command("t6-probed", group)  # scoped and gated by omission

    result = runner.invoke(app, ["--lab", "test_lab", "t6-probed", "opted-out", "--level", "4"])

    assert result.exit_code == 0, result.output
    [probe] = probed
    [(body_ctx, read, given)] = seen
    assert probe.scoped is True
    assert body_ctx.scoped is False
    assert body_ctx is not probe
    assert body_ctx.unscoped.lab is probe.unscoped.lab
    assert (read, given) == (4, 4)
    assert (len(builds), len(gates), len(presented)) == (1, 1, 1)
    # Reset in order: the probe's at its replacement, the replacement's at close.
    ours = [ctx for ctx in contexts_at_close if ctx is probe or ctx is body_ctx]
    assert len(ours) == 2
    assert ours[0] is probe
    assert ours[1] is body_ctx


# ── One construction seam ─────────────────────────────────────────────────────


def _calls_by_function(predicate: "Callable[[ast.Call], bool]") -> "set[tuple[str, str]]":
    """Every ``(module path, enclosing function)`` under ``src/otto`` holding a matching call."""
    found: "set[tuple[str, str]]" = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(n, ast.Call) and predicate(n) for n in ast.walk(func)):
                found.add((path.relative_to(_SRC).as_posix(), func.name))
    return found


def _called_name(call: ast.Call) -> "str | None":
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _calls_seam(call: ast.Call) -> bool:
    return _called_name(call) == "new_run_context"


def _constructs_context(call: ast.Call) -> bool:
    return _called_name(call) == "OttoContext"


def _passes_scoped(call: ast.Call) -> bool:
    return any(keyword.arg == "_scoped" for keyword in call.keywords)


def _remote_chain() -> Any:
    from otto.cli import remote_completion as rc

    return rc._ChainParams(
        host_id="slot1",
        hop="",
        term=None,
        labs=["rig"],
        holder=None,
        include_projects=["r1"],
        exclude_projects=["r2"],
    )


def _patch_remote_lab(monkeypatch: pytest.MonkeyPatch) -> Lab:
    from tests._fixtures.fleet import _lab as fleet_lab

    lab = fleet_lab(("slot1", "rig"))
    monkeypatch.setattr("otto.declared.get_projects", list)
    monkeypatch.setattr("otto.session.lab.build_lab", lambda projects, labs, **_kw: lab)
    monkeypatch.setattr("otto.fleet.get_host", lambda host_id: lab.hosts[host_id])
    return lab


def test_three_builders_share_the_seam(real_main_mocks, monkeypatch):
    """The CLI's lab context, ``open_context`` and completion's lookup build through one seam.

    Driven, each hands the seam its selection. Read from the source, the seam
    has exactly these three callers and is the only code that passes the
    context's private verdict, and no other code builds a context directly
    but the two library builders that keep the default verdict.
    """
    from otto.cli import remote_completion as rc
    from otto.config.scope import ScopeSelection

    calls = _counted(monkeypatch, "otto.run_context.new_run_context")

    _dispatched_context("t6-seam")

    async def _opened() -> None:
        async with otto.open_context(lab=Lab(name="rig")):
            pass

    asyncio.run(_opened())

    _patch_remote_lab(monkeypatch)
    _host, token = rc._load_host(_remote_chain())
    rc._release_context(token)

    assert [call["selection"] for call in calls] == [
        ScopeSelection(labs=("test_lab",), include=(), exclude=()),
        ScopeSelection(labs=("rig",), include=(), exclude=()),
        ScopeSelection(labs=("rig",), include=("r1",), exclude=("r2",)),
    ]
    assert [call.get("scoped", True) for call in calls] == [True, True, True]

    assert _calls_by_function(_calls_seam) == {
        ("cli/invoke.py", "_ensure_lab_context"),
        ("run_context.py", "open_context"),
        ("cli/remote_completion.py", "_lookup_context"),
    }
    assert _calls_by_function(_passes_scoped) == {("run_context.py", "new_run_context")}
    # A context built directly bypasses the seam: only the seam itself and the
    # two default-verdict library builders do it.
    assert _calls_by_function(_constructs_context) == {
        ("run_context.py", "new_run_context"),
        ("suite/run.py", "lab_less_context"),
        ("testing/conformance_host.py", "_dry_run"),
    }


def test_remote_lookup_context_carries_switches(monkeypatch):
    """Both completion paths build the context the command would: its lab and its ``-I``/``-E``.

    The host lookup used to build a bare context with no switches while the
    requirement read carried them, so the two judged projects differently.
    """
    from otto.cli import remote_completion as rc

    lab = _patch_remote_lab(monkeypatch)
    seen: "list[Any]" = []

    def _get_host(host_id: str) -> Any:
        from otto.context import get_context

        seen.append(get_context())
        return lab.hosts[host_id]

    def _in_play() -> "set[str]":
        from otto.context import get_context

        seen.append(get_context())
        return set()

    monkeypatch.setattr("otto.fleet.get_host", _get_host)
    monkeypatch.setattr("otto.fleet.get_hosts_in_play", _in_play)

    _host, token = rc._load_host(_remote_chain())
    rc._release_context(token)
    rc._required_for(_remote_chain())

    assert [(ctx.include_projects, ctx.exclude_projects) for ctx in seen] == [
        (("r1",), ("r2",)),
        (("r1",), ("r2",)),
    ]
    assert all(ctx.unscoped.lab is lab for ctx in seen)


@pytest.mark.parametrize("opt_out", _OPT_OUTS)
def test_the_dispatch_and_the_leaf_record_one_spec(monkeypatch, tmp_path, opt_out):
    """Regression: the spec the root dispatch records is the one the leaf's preamble reads.

    Help and the early probe read the root dispatch's record; the preamble
    reads the leaf's stamp. On a real dispatch they are one object, so the
    two cannot judge a command's scope differently.
    """
    from otto.cli import invoke
    from otto.cli.invoke import SCOPED_ATTR, command_spec, recorded_spec
    from tests._fixtures.fleet import _lab, dispatch_root

    seen: "list[tuple[Any, Any]]" = []
    preamble = invoke.command_preamble

    def spy(ctx: Any) -> None:
        seen.append((command_spec(ctx), recorded_spec(ctx)))
        preamble(ctx)

    monkeypatch.setattr(invoke, "command_preamble", spy)

    async def leaf() -> None:
        pass

    if opt_out == "leaf":
        setattr(leaf, SCOPED_ATTR, False)
    register_cli_command("t-one-spec", leaf, **_registration_keywords(opt_out))

    result = dispatch_root(monkeypatch, ["t-one-spec"], lab=_lab(("a-dut", "alpha")), projects=[])

    assert result.exit_code == 0, (result.output, repr(result.exception))
    [(stamped, recorded)] = seen
    assert recorded is stamped
