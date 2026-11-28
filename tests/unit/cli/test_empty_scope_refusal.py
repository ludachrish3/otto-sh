"""An empty scope refuses up front; the recovery commands still run (R-EMPTY, Q26, Q28).

When some project declares a ``[project]`` scope and no host is in play, a
scoped, reservation-gated command refuses with exit 1 before its body runs,
with the fleet-shaped text that names the settings files to widen or the
``-E`` to drop. The recovery commands opt out: ``otto run cleanup``,
``otto link repair``, ``otto tunnel list|remove`` and the host verbs, whose
explicit target is judged at its own lookup.

Every CLI case goes through the real root dispatch (``otto.cli.main.app``):
the composition root hands back real projects parsed from real
``settings.toml`` files, and the lab build hands back a lab built by the host
factory. An impairment and a tunnel on a host no project targets stand in for
the device state at the library seams that read it (``repair_all``,
``discover_tunnels``, ``remove_all_tunnels``): they are found only by a sweep
handed a lab that holds that host, so a recovery command that swept the
scoped view would find nothing.
"""

import asyncio
import dataclasses
from datetime import datetime, timezone
from typing import Any

import pytest
import typer

from otto.bootstrap import ProjectScopeError
from otto.cli.registry import CLI_COMMANDS, register_cli_command
from tests._fixtures.fleet import (
    _lab,
    _project,
    add_builtin_local,
    dispatch_root,
    install_scoped_context,
    scope_workspace,
)

_OUTSIDE = "a-dut"
"""The host the out-of-scope impairment and tunnel sit on: in the lab, targeted by no one."""

_LINK = "a-dut--a-jump"
_TUNNEL = "tun-out-161"
_DECLINE = f"{_LINK}: root qdisc is not otto's"

_FLEET_SKIPS = (
    "debug-log sweep skipped: no host is in play",
    "toolchain removal skipped: no host is in play",
)
"""The two fleet steps an empty-scope cleanup declines by default, one line each."""


def _shown(*lines: str) -> "list[str]":
    """*lines* as the CLI prints a skipped result: the first one carries the ``skipped:`` label.

    The renderer labels every skip message the same way, whatever its words,
    so a step's own ``<step> skipped: <why>`` leads with a second label.
    """
    return [f"skipped: {lines[0]}", *lines[1:]]


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(CLI_COMMANDS.names())
    yield
    for name in set(CLI_COMMANDS.names()) - before:
        CLI_COMMANDS.unregister(name=name)


def _settings(project: Any) -> str:
    """The settings file a refusal names for *project*."""
    return f"{project.root}/.otto/settings.toml"


def _alpha_lab() -> Any:
    """Lab ``alpha``: ``a-dut``, ``a-jump`` and the built-in runner."""
    return add_builtin_local(
        _lab(("a-dut", "alpha"), ("a-jump", "alpha"), component_names=["alpha"])
    )


def _dispatch(monkeypatch: pytest.MonkeyPatch, argv: "list[str]", *, lab: Any, projects: Any):
    """Run ``otto --lab <lab> <argv>`` through the root app, over *projects* and *lab*."""
    return dispatch_root(monkeypatch, argv, lab=lab, projects=projects)


@dataclasses.dataclass
class _Workspace:
    """An empty declared scope over lab ``alpha``: its projects, its switches, its refusal."""

    projects: "list[Any]"
    switches: "list[str]"
    text: str
    """The refusal's exact text."""
    skip_lines: "list[str]"
    """The lines ``otto run cleanup`` logs for the projects it leaves out."""


def _empty_fleet_text(projects: "list[Any]", files: "list[Any]") -> str:
    declared = ", ".join(f"{p.name} (lab_patterns: {_lab_patterns(p)})" for p in projects)
    settings = f"\n    settings:     {', '.join(_settings(p) for p in files)}" if files else ""
    return (
        "no host in this run belongs to any project's fleet of interest, so every fleet\n"
        "walk would be empty.\n"
        "\n"
        "    loaded labs:  alpha\n"
        f"    declared by:  {declared}{settings}\n"
        "\n"
        "Load a lab these projects apply to (otto -l <lab>), or widen [project]\n"
        "lab_patterns / host_patterns in their .otto/settings.toml."
    )


def _lab_patterns(project: Any) -> str:
    return ", ".join(p.pattern for p in project.project_scope.lab_patterns)


def _excluded_fleet_text(names: str) -> str:
    return (
        "no host is in play: every project whose [project] declaration admits a host in\n"
        "the loaded labs was switched off for this run (--exclude-projects), so every\n"
        "fleet walk would be empty.\n"
        "\n"
        f"    switched off: {names}\n"
        "\n"
        "Drop --exclude-projects for a project whose hosts this run should reach."
    )


def _host_starved(tmp_path: Any) -> _Workspace:
    """One project, its labs loaded, its ``host_patterns`` matching no host there."""
    starved = _project(tmp_path, "starved", labs=["alpha"], hosts=["nothing-here"])
    return _Workspace(
        projects=[starved],
        switches=[],
        text=_empty_fleet_text([starved], [starved]),
        skip_lines=[
            (
                "project 'starved' applies to lab(s) [alpha] but its [project] host_patterns "
                "(nothing-here) match no host there — skipping it for cleanup"
            )
        ],
    )


def _all_excluded(tmp_path: Any) -> _Workspace:
    """Every declaring project switched off with ``-E``; one would have put a host in play."""
    inscope = _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    outscope = _project(tmp_path, "outscope", labs=["beta"], hosts=["b-dut.*"])
    return _Workspace(
        projects=[inscope, outscope],
        switches=["-E", "inscope,outscope"],
        text=_excluded_fleet_text("inscope, outscope"),
        skip_lines=[
            f"project {name!r} switched off for this run (--exclude-projects {name})"
            " — skipping it for cleanup"
            for name in ("inscope", "outscope")
        ],
    )


def _forced_on_empty(tmp_path: Any) -> _Workspace:
    """One project forced on with ``-I`` whose own fleet holds no host of lab ``alpha``."""
    outscope = _project(tmp_path, "outscope", labs=["beta"], hosts=["b-dut.*"])
    return _Workspace(
        projects=[outscope],
        switches=["-I", "outscope"],
        text=_empty_fleet_text([outscope], [outscope]),
        skip_lines=["cleanup skipped for project 'outscope': its fleet has no host in play"],
    )


_EMPTY_SCOPES = {
    "host-starved": _host_starved,
    "every-project-excluded": _all_excluded,
    "forced-on-empty-fleet": _forced_on_empty,
}


def _assert_refused(result: Any, text: str) -> None:
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, ProjectScopeError), repr(result.exception)
    assert str(result.exception) == text


# ── The out-of-scope device state the recovery commands must find ────────────


@dataclasses.dataclass
class _DeviceState:
    """An impairment on ``_LINK`` and a tunnel ``_TUNNEL``, both on ``_OUTSIDE``.

    The fakes stand in for the library sweeps: each finds the state only in
    a lab that holds ``_OUTSIDE``, and records the labs it was handed.
    """

    impaired: bool = True
    tunnel_up: bool = True
    declined: bool = False
    """``repair_all`` declines the link (a foreign qdisc) instead of clearing it."""
    swept: "list[tuple[str, bool]]" = dataclasses.field(default_factory=list)

    def _holds(self, name: str, lab: Any) -> bool:
        holds = _OUTSIDE in lab.hosts
        self.swept.append((name, holds))
        return holds

    async def repair_all(self, *, lab: Any) -> Any:
        from otto.link.manage import RepairAllReport, RepairReport

        if not (self._holds("repair_all", lab) and self.impaired):
            return RepairAllReport()
        if self.declined:
            return RepairAllReport(skipped=[_DECLINE])
        self.impaired = False
        return RepairAllReport(repaired=[RepairReport(link_id=_LINK)])

    async def remove_all_tunnels(self, *, lab: Any) -> Any:
        from otto.tunnel.manage import RemovedReport

        reaped = self._holds("remove_all_tunnels", lab) and self.tunnel_up
        self.tunnel_up = self.tunnel_up and not reaped
        ids = [_TUNNEL] if reaped else []
        return RemovedReport(removed_ids=ids, killed={}, unreachable=[], survivors=[])

    async def discover_tunnels(self, lab: Any) -> Any:
        from otto.tunnel.discovery import DiscoveredTunnel, TunnelDiscovery
        from otto.tunnel.model import Tunnel, TunnelHop

        if not (self._holds("discover_tunnels", lab) and self.tunnel_up):
            return TunnelDiscovery(tunnels=[], unreachable=[])
        tunnel = Tunnel(
            protocol="tcp",
            service_port=161,
            path=(TunnelHop(host=_OUTSIDE, interface="eth0"), TunnelHop(host="a-jump")),
        )
        expected = tunnel.expected_processes()
        found = DiscoveredTunnel(
            tunnel=tunnel, present=expected, missing=set(), age_seconds=5, uncertain=False
        )
        return TunnelDiscovery(tunnels=[found], unreachable=[])


@pytest.fixture
def device_state(monkeypatch: pytest.MonkeyPatch) -> _DeviceState:
    """Wire the out-of-scope impairment and tunnel into every reader of them."""
    state = _DeviceState()
    for module in ("otto.link.manage", "otto.cli.link"):
        monkeypatch.setattr(f"{module}.repair_all", state.repair_all)
    for module in ("otto.tunnel.manage", "otto.cli.tunnel"):
        monkeypatch.setattr(f"{module}.remove_all_tunnels", state.remove_all_tunnels)
    monkeypatch.setattr("otto.cli.tunnel.discover_tunnels", state.discover_tunnels)
    monkeypatch.setattr("otto.cli.tunnel.record_tunnel_ids", lambda *_a, **_kw: None)
    return state


def _ran_leaf(name: str, **registration: Any) -> "list[str]":
    """Register a lab-bound plugin command *name*; the returned list records each run."""
    ran: "list[str]" = []

    async def leaf() -> None:
        ran.append(name)

    register_cli_command(name, leaf, **registration)
    return ran


# ── Refused: a scoped, gated command with nothing in play ────────────────────


@pytest.mark.parametrize("shape", list(_EMPTY_SCOPES))
def test_scoped_gated_command_refuses_empty_scope(monkeypatch, tmp_path, shape):
    """Fail-first: a plugin command (scoped, gated by omission) refuses before its body runs."""
    ws = _EMPTY_SCOPES[shape](tmp_path)
    ran = _ran_leaf("t11-plugin")

    result = _dispatch(
        monkeypatch, [*ws.switches, "t11-plugin"], lab=_alpha_lab(), projects=ws.projects
    )

    _assert_refused(result, ws.text)
    assert ran == []


def test_every_declarer_switched_off_refuses_excluded_text(monkeypatch, tmp_path):
    """Fail-first: ``-E`` that empties the scope names the switch; with no file, no settings line.

    When a switched-off project would have put a host in play, the refusal is
    the switch text. When none would have (every declarer switched off, none
    targeting the loaded lab), the text names the declarations, and its
    ``settings:`` line is left out: no file would widen a scope ``-E``
    switched off.
    """
    ws = _all_excluded(tmp_path)
    ran = _ran_leaf("t11-plugin")

    switched = _dispatch(
        monkeypatch, [*ws.switches, "t11-plugin"], lab=_alpha_lab(), projects=ws.projects
    )

    _assert_refused(switched, ws.text)

    elsewhere = _project(tmp_path, "elsewhere", labs=["beta"], hosts=["b-dut.*"])
    no_file = _dispatch(
        monkeypatch, ["-E", "elsewhere", "t11-plugin"], lab=_alpha_lab(), projects=[elsewhere]
    )

    _assert_refused(no_file, _empty_fleet_text([elsewhere], []))
    assert "settings:" not in str(no_file.exception)
    assert ran == []


@pytest.mark.parametrize("verb", ["install", "status"])
def test_run_install_and_status_refuse_empty_scope(monkeypatch, tmp_path, verb):
    """Fail-first: one text replaces each project's skip line for ``otto run install|status``."""
    ws = _host_starved(tmp_path)

    result = _dispatch(monkeypatch, ["run", verb], lab=_alpha_lab(), projects=ws.projects)

    _assert_refused(result, ws.text)


def test_otto_test_mixed_workspace_refuses(monkeypatch, tmp_path):
    """Fail-first: a project that declares nothing does not make an empty declared scope run."""
    ws = scope_workspace(tmp_path, mixed=True)
    projects = ws.ctx.unscoped.projects
    assert [p.name for p in projects] == ["inscope", "outscope", "plain"]

    result = _dispatch(
        monkeypatch, ["-E", "inscope", "test", "-m", "smoke"], lab=ws.lab, projects=projects
    )

    _assert_refused(result, _excluded_fleet_text("inscope"))


def test_otto_test_dry_run_refuses_list_tests_does_not(monkeypatch, tmp_path):
    """Fail-first: ``-n test`` validates as a run does and refuses; ``--list-tests`` does not."""
    ws = _host_starved(tmp_path)

    dry = _dispatch(
        monkeypatch, ["-n", "test", "-m", "smoke"], lab=_alpha_lab(), projects=ws.projects
    )
    listed = _dispatch(
        monkeypatch, ["test", "--list-tests"], lab=_alpha_lab(), projects=ws.projects
    )

    _assert_refused(dry, ws.text)
    assert listed.exit_code == 0, (listed.output, repr(listed.exception))
    assert "no host in this run belongs" not in listed.output


def test_forced_on_project_with_no_host_refuses(monkeypatch, tmp_path):
    """Fail-first: ``-I`` brings a project in, but it brings no host into play."""
    ws = _forced_on_empty(tmp_path)
    ran = _ran_leaf("t11-plugin")

    result = _dispatch(
        monkeypatch, [*ws.switches, "t11-plugin"], lab=_alpha_lab(), projects=ws.projects
    )

    _assert_refused(result, ws.text)
    assert ran == []


def test_monitor_live_refuses_empty_scope(monkeypatch, tmp_path):
    """Fail-first: ``otto monitor --live`` opens its lab gated, so it refuses the same way."""
    from otto.monitor.live import LiveReport

    ws = _host_starved(tmp_path)
    collected: "list[bool]" = []

    async def _run_live(**_kwargs: Any) -> LiveReport:
        collected.append(True)
        now = datetime.now(tz=timezone.utc)
        return LiveReport(session_id="s", hosts=[], db=None, start=now, end=now)

    monkeypatch.setattr("otto.monitor.live.run_live", _run_live)

    result = _dispatch(monkeypatch, ["monitor", "--live"], lab=_alpha_lab(), projects=ws.projects)

    # monitor renders its own scope refusals, naming no switch it does not take.
    assert result.exit_code == 1, result.output
    assert "".join(ws.text.split()) in "".join(result.output.split())  # wrapped at any width
    assert collected == []


def test_refused_before_reservation_gate(monkeypatch, tmp_path):
    """The refusal comes after the dependency check and before the reservation gate speaks."""
    from otto.cli import invoke

    ws = _host_starved(tmp_path)
    order: "list[str]" = []
    real_deps = invoke.refuse_unsatisfied_dependencies

    def _deps() -> None:
        order.append("dependencies")
        real_deps()

    monkeypatch.setattr(invoke, "refuse_unsatisfied_dependencies", _deps)
    monkeypatch.setattr(invoke, "present_reservation_gate", lambda _ctx: order.append("gate"))
    _ran_leaf("t11-plugin")

    result = _dispatch(monkeypatch, ["t11-plugin"], lab=_alpha_lab(), projects=ws.projects)

    _assert_refused(result, ws.text)
    assert order == ["dependencies"]


# ── Not refused ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("how", ["registration", "leaf"])
def test_gate_false_leaf_not_refused(monkeypatch, tmp_path, how):
    """A command that contacts no host (``gate=False``, or ``__cli_gate__ = False``) runs."""
    from otto.cli.invoke import GATE_ATTR

    ws = _host_starved(tmp_path)
    ran: "list[str]" = []
    if how == "registration":
        ran = _ran_leaf("t11-ungated", gate=False)
        argv = ["t11-ungated"]
    else:
        group = typer.Typer(name="t11-group")

        @group.callback()
        def _group() -> None:
            pass

        @group.command("go")
        async def go() -> None:
            ran.append("go")

        setattr(go, GATE_ATTR, False)
        register_cli_command("t11-group", group)
        argv = ["t11-group", "go"]

    result = _dispatch(monkeypatch, argv, lab=_alpha_lab(), projects=ws.projects)

    assert result.exit_code == 0, result.output
    assert ran


def test_reservation_check_not_refused(monkeypatch, tmp_path):
    """``otto reservation check`` answers an empty in-play set; it does not refuse it."""
    ws = _host_starved(tmp_path)

    result = _dispatch(
        monkeypatch, ["reservation", "check"], lab=_alpha_lab(), projects=ws.projects
    )

    assert not isinstance(result.exception, ProjectScopeError), repr(result.exception)
    assert result.exit_code == 0, result.output


def test_undeclared_workspace_not_refused(monkeypatch, tmp_path):
    """No project declares a scope: the whole lab is in play and nothing is refused."""
    ran = _ran_leaf("t11-plugin")

    result = _dispatch(
        monkeypatch, ["t11-plugin"], lab=_alpha_lab(), projects=[_project(tmp_path, "plain")]
    )

    assert result.exit_code == 0, result.output
    assert ran == ["t11-plugin"]


def test_unscoped_command_not_refused(monkeypatch, tmp_path):
    """A command registered ``scoped=False`` sees every host, so its scope is never empty."""
    ws = _host_starved(tmp_path)
    ran = _ran_leaf("t11-unscoped", scoped=False)

    result = _dispatch(monkeypatch, ["t11-unscoped"], lab=_alpha_lab(), projects=ws.projects)

    assert result.exit_code == 0, result.output
    assert ran == ["t11-unscoped"]


# ── The recovery commands run ────────────────────────────────────────────────


@pytest.mark.parametrize("shape", list(_EMPTY_SCOPES))
def test_run_cleanup_empty_scope(monkeypatch, tmp_path, caplog, device_state, shape):
    """Fail-first: cleanup resets and reaps lab-wide, skips host steps, exits 0 (Q26, Q28)."""
    ws = _EMPTY_SCOPES[shape](tmp_path)

    result = _dispatch(
        monkeypatch, [*ws.switches, "run", "cleanup"], lab=_alpha_lab(), projects=ws.projects
    )

    assert result.exit_code == 0, (result.output, repr(result.exception))
    assert not device_state.impaired
    assert not device_state.tunnel_up
    assert device_state.swept == [("repair_all", True), ("remove_all_tunnels", True)]
    for line in ws.skip_lines:
        assert line in caplog.messages
    assert result.output.splitlines()[-2:] == _shown(*_FLEET_SKIPS), result.output


def test_run_cleanup_empty_scope_reports_a_declined_reset(monkeypatch, tmp_path, device_state):
    """Fail-first: an impairment reset's decline outranks the routine empty-fleet skips.

    The decline names otto state still on the wire, so it is reported first;
    each skipped fleet step is still reported after it, and the reap runs.
    """
    ws = _host_starved(tmp_path)
    device_state.declined = True

    result = _dispatch(monkeypatch, ["run", "cleanup"], lab=_alpha_lab(), projects=ws.projects)

    assert result.exit_code == 0, (result.output, repr(result.exception))
    assert not device_state.tunnel_up
    lines = result.output.splitlines()
    decline = f"skipped: impairment reset declined: {_DECLINE}"
    assert lines[-3:] == [decline, *_FLEET_SKIPS], result.output


def _skip_records(caplog: Any) -> "list[str]":
    """The lines a cleanup logged for the projects it left out, in order."""
    return [m for m in caplog.messages if "skipping it for cleanup" in m or "cleanup skipped" in m]


@pytest.mark.parametrize("shape", list(_EMPTY_SCOPES))
def test_run_cleanup_dry_run_empty_scope(monkeypatch, tmp_path, caplog, device_state, shape):
    """Fail-first: ``-n run cleanup`` makes the decision the real run makes, from the same input.

    Both entry points get the same projects, lab and switches. The dry run
    exits 0, touches nothing, logs the same project skips, and previews the
    two fleet steps the real run declines with the real run's own words.
    """
    ws = _EMPTY_SCOPES[shape](tmp_path)
    argv = [*ws.switches, "run", "cleanup"]

    dry = _dispatch(monkeypatch, ["-n", *argv], lab=_alpha_lab(), projects=ws.projects)
    previewed = _skip_records(caplog)
    caplog.clear()
    assert device_state.swept == []
    real = _dispatch(monkeypatch, argv, lab=_alpha_lab(), projects=ws.projects)

    assert dry.exit_code == 0, (dry.output, repr(dry.exception))
    assert real.exit_code == 0, (real.output, repr(real.exception))
    assert previewed == _skip_records(caplog)
    assert sorted(previewed) == sorted(ws.skip_lines)
    plan = " ".join(dry.output.split())
    assert "debug logs debug-log sweep skipped: no host is in play" in plan
    assert "toolchain toolchain removal skipped: no host is in play" in plan
    assert real.output.splitlines()[-2:] == _shown(*_FLEET_SKIPS), real.output


def test_cleanup_steps_skipped_text(monkeypatch, tmp_path, device_state):
    """With no debug-log sweep asked for, the toolchain removal's skip is the one reported."""
    ws = _host_starved(tmp_path)

    result = _dispatch(
        monkeypatch,
        ["run", "cleanup", "--no-debug-logs"],
        lab=_alpha_lab(),
        projects=ws.projects,
    )

    assert result.exit_code == 0, (result.output, repr(result.exception))
    assert result.output.splitlines()[-1:] == _shown(_FLEET_SKIPS[1]), result.output
    assert _FLEET_SKIPS[0] not in result.output


def test_cleanup_nonempty_scope_unchanged(monkeypatch, tmp_path, device_state):
    """With hosts in play, a driving project whose own declaration fails still aborts (Q40)."""
    starved = _project(tmp_path, "starved", labs=["alpha"], hosts=["nothing-here"])
    inscope = _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])

    result = _dispatch(
        monkeypatch, ["run", "cleanup"], lab=_alpha_lab(), projects=[starved, inscope]
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, ProjectScopeError), repr(result.exception)
    assert str(result.exception).startswith("project 'starved' applies to loaded lab(s) alpha")
    assert device_state.swept == []
    assert device_state.impaired
    assert device_state.tunnel_up


def test_cleanup_preview_and_run_refuse_a_starved_driving_project(
    monkeypatch, tmp_path, device_state
):
    """Fail-first: ``-n run cleanup`` makes the real run's driving-project refusal (Q40, #1397).

    Same input to both entry points: a host is in play (``inscope``'s), and
    the driving project, ``starved``, admits none. The preview refuses with
    the real run's text, and neither touches the device state.
    """
    starved = _project(tmp_path, "starved", labs=["alpha"], hosts=["nothing-here"])
    inscope = _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    projects = [starved, inscope]

    refusals = []
    for argv in (["-n", "run", "cleanup"], ["run", "cleanup"]):
        result = _dispatch(monkeypatch, argv, lab=_alpha_lab(), projects=projects)
        assert result.exit_code == 1, (argv, result.output)
        assert isinstance(result.exception, ProjectScopeError), (argv, repr(result.exception))
        refusals.append(str(result.exception))

    assert refusals[0] == refusals[1]
    assert refusals[0].startswith("project 'starved' applies to loaded lab(s) alpha")
    assert device_state.swept == []


def test_link_repair_runs_empty_scope(monkeypatch, tmp_path, device_state):
    """Regression: ``otto link repair --all`` runs and finds the out-of-scope impairment."""
    ws = _host_starved(tmp_path)

    result = _dispatch(
        monkeypatch, ["link", "repair", "--all"], lab=_alpha_lab(), projects=ws.projects
    )

    assert result.exit_code == 0, (result.output, repr(result.exception))
    assert "repaired 1 link(s)" in result.output
    assert device_state.swept == [("repair_all", True)]
    assert not device_state.impaired


def test_tunnel_list_and_remove_run_empty_scope(monkeypatch, tmp_path, device_state):
    """Regression: ``otto tunnel list`` shows, and ``remove --all`` reaps, the outside tunnel."""
    ws = _host_starved(tmp_path)

    listed = _dispatch(monkeypatch, ["tunnel", "list"], lab=_alpha_lab(), projects=ws.projects)
    removed = _dispatch(
        monkeypatch, ["tunnel", "remove", "--all", "--yes"], lab=_alpha_lab(), projects=ws.projects
    )

    assert listed.exit_code == 0, (listed.output, repr(listed.exception))
    assert f"{_OUTSIDE}@eth0" in listed.output
    assert removed.exit_code == 0, (removed.output, repr(removed.exception))
    assert f"removed {_TUNNEL}" in removed.output
    assert device_state.swept == [("discover_tunnels", True), ("remove_all_tunnels", True)]


def test_library_install_keeps_d3_abort(tmp_path):
    """Regression: a library ``install`` still aborts on the driving project's scope (D3)."""
    from otto.project import orchestrator

    ws = _host_starved(tmp_path)
    install_scoped_context(_alpha_lab(), ws.projects)

    with pytest.raises(ProjectScopeError) as aborted:
        asyncio.run(orchestrator.install())

    assert str(aborted.value).startswith("project 'starved' applies to loaded lab(s) alpha")


def test_host_local_resolves_empty_scope(monkeypatch, tmp_path):
    """Regression: ``otto host local`` resolves under an empty scope: the runner is targetable."""
    ws = _host_starved(tmp_path)

    result = _dispatch(
        monkeypatch, ["-n", "host", "local", "exec", "true"], lab=_alpha_lab(), projects=ws.projects
    )

    assert result.exit_code == 0, (result.output, repr(result.exception))


def test_host_other_id_refuses_at_its_lookup(monkeypatch, tmp_path):
    """Regression: another target passes the preamble and is refused at its own lookup.

    The preamble's refusal would surface as the ``ProjectScopeError`` itself;
    the lookup's is the host command's own exit 1, printing the empty-scope
    text the lookup raised.
    """
    ws = _host_starved(tmp_path)

    result = _dispatch(
        monkeypatch,
        ["-n", "host", _OUTSIDE, "exec", "true"],
        lab=_alpha_lab(),
        projects=ws.projects,
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert " ".join(ws.text.split()) in " ".join(result.output.split())
    assert "No host with ID" not in result.output


# ── The root listings warn and list the runner ───────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [["--list-hosts"], ["host", "--list-hosts"], ["--show-lab"]],
    ids=["root-list-hosts", "host-list-hosts", "show-lab"],
)
def test_show_lab_and_list_hosts_warn_and_list_runner(monkeypatch, tmp_path, argv):
    """Fail-first: the inline listings print the refusal as a warning and list only the runner."""
    ws = _host_starved(tmp_path)

    result = _dispatch(monkeypatch, argv, lab=_alpha_lab(), projects=ws.projects)

    assert result.exit_code == 0, (result.output, repr(result.exception))
    assert result.stderr.startswith(f"warning: {ws.text}\n")
    listed = [line for line in result.stdout.splitlines() if line.startswith(("•", "│"))]
    assert any("local" in line for line in listed), result.stdout
    for host_id in ("a-dut", "a-jump"):
        assert not any(host_id in line for line in listed), result.stdout
