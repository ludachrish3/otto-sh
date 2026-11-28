"""
Unit tests for the ``otto host`` subcommand.

Covers:
  - Help / no-args behaviour
  - Callback sets the logger output directory and resolves host to ctx.obj
  - Host resolution (success and failure)
  - The exec, put, and get commands invoke the correct host methods
"""

import asyncio
import itertools
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto.cli import host as host_module
from otto.cli.host import _host_id_completer, host_app
from otto.config.project import Project
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.session import ShellSession
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.reservations import Reservation, ReservationBackendBase
from otto.result import CommandResult, Result
from otto.utils import Status
from tests._fixtures.clickctx import chain
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.fake_project import fake_project
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests.unit.host._session_feed import identity_handshake_reply, writes_handshake
from tests.unit.host._sessions import session_manager

# The dynamic verbs are plain ``async def`` leaves bridged by the leaf-invoke
# wrapper, so host_app invocations go through the production dispatch seam;
# root-``app`` invocations use the plain CliRunner (the root dispatch already
# wraps its own leaves).
runner = DispatchRunner()
root_runner = CliRunner()


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_host(name: str = "router1") -> UnixHost:
    """Return a real UnixHost (no connection is made on construction)."""
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name=name),
        creds=[Cred(login="admin", password="secret")],
        log=LogMode.NORMAL,
    )


class FakeSession(ShellSession):
    """ShellSession with pre-loaded responses for synchronous CLI tests.

    Each entry in *responses* is an ``(output, retcode)`` pair consumed in
    order by successive ``run_cmd`` calls.  When the base class writes the
    sentinel-wrapped command, the fake immediately enqueues the matching
    begin marker, output lines, and end sentinel so that ``read_transport_until``
    can return them without real I/O.
    """

    def __init__(self, responses: list[tuple[str, int]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._read_queue: asyncio.Queue[str] = asyncio.Queue()

    async def open_transport(self) -> None:
        pass  # no transport to open

    async def write_transport(self, data: str) -> None:
        if writes_handshake(data, self._ready_marker):
            # Initialization handshake — echo the ready marker back
            self._read_queue.put_nowait(identity_handshake_reply(self._ready_marker))
        elif self._begin_marker in data and self._responses:
            # Sentinel-wrapped command — enqueue the canned response
            output, retcode = self._responses.pop(0)
            self._read_queue.put_nowait(f"{self._begin_marker}\n")
            if output:
                for line in output.splitlines():
                    self._read_queue.put_nowait(f"{line}\n")
            self._read_queue.put_nowait(f"{self._end_marker_prefix}{retcode}__\n")

    async def _take_unread(self) -> str:
        return ""

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        buf = ""
        while True:
            chunk = await self._read_queue.get()
            buf += chunk
            if pattern.search(buf):
                return buf

    async def close(self) -> None:
        self._alive = False
        self._initialized = False


def _make_host_with_session(
    responses: list[tuple[str, int]],
    name: str = "router1",
    term: str = "telnet",
) -> UnixHost:
    """Build a UnixHost whose SessionManager uses a FakeSession.

    ``term="telnet"`` by default: ``exec`` runs on a pooled session, on the same
    ``FakeSession``. The full chain ``exec -> _exec_one ->
    SessionManager.exec_pooled -> ShellSession.run_cmd`` runs for
    real; only the transport is faked. Logging callbacks are suppressed to
    avoid interfering with CliRunner's stdout capture.
    """
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name=name),
        creds=[Cred(login="admin", password="secret")],
        log=LogMode.NORMAL,
        term=term,
    )
    fake = FakeSession(responses)
    host._session_mgr = session_manager(
        connections=host._connections,
        session_factory=lambda: fake,
        name=host.name,
    )
    return host


# ── Help / no-args behaviour ─────────────────────────────────────────────────


class TestHostHelp:
    def test_no_args_shows_help(self):
        result = runner.invoke(host_app, [])
        assert result.exit_code == 0
        assert "Usage" in result.output or "usage" in result.output.lower()

    def test_help_flag(self):
        result = runner.invoke(host_app, ["--help"])
        assert result.exit_code == 0

    def test_help_short_flag(self):
        result = runner.invoke(host_app, ["-h"])
        assert result.exit_code == 0

    def test_exec_listed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "exec" in result.output

    def test_login_and_exec_exposed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "login" in result.output
        assert "exec" in result.output

    def test_run_is_not_a_verb(self):
        with patch("otto.fleet.get_host", return_value=_make_host()):
            result = runner.invoke(host_app, ["router1", "run", "ls"])
        assert result.exit_code == 2
        assert "No such command" in result.output

    def test_exec_offers_timeout_sudo_and_user(self):
        with patch("otto.fleet.get_host", return_value=_make_host()):
            result = runner.invoke(host_app, ["router1", "exec", "--help"])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "COMMAND" in flat
        assert "--timeout" in flat
        assert "--sudo --no-sudo" in flat
        assert "--user" in flat
        assert "join steps with &&" in flat

    def test_put_listed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "put" in result.output

    def test_get_listed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "get" in result.output

    @pytest.mark.parametrize("verb", ["put", "get"])
    def test_the_real_transfer_verbs_offer_the_concurrency_flag(self, verb: str):
        """``otto host <id> put|get --help`` shows ``--concurrent/--no-concurrent``.

        The synthesis mechanism is pinned in ``test_param_synth``; this is the
        claim a reader of the docs actually checks -- that the flag reaches the
        REAL verb, on both halves of the transfer pair, with its off switch
        beside it.
        """
        with patch("otto.fleet.get_host", return_value=_make_host()):
            result = runner.invoke(host_app, ["router1", verb, "--help"])

        assert result.exit_code == 0, result.output
        # Rich wraps the options table, so compare on collapsed whitespace
        # rather than the rendered column positions.
        flat = " ".join(result.output.split())
        assert "--concurrent --no-concurrent" in flat
        assert "[default: concurrent]" in flat

    def test_host_id_only_no_subcommand_shows_help(self):
        """otto host router1 (no verb) should show help."""
        result = runner.invoke(host_app, ["router1"])
        assert result.exit_code == 0
        assert "Usage" in result.output or "usage" in result.output.lower()


# ── Callback behaviour ───────────────────────────────────────────────────────


class TestHostCallback:
    def test_log_dir_set_for_subcommand(self):
        """The leaf-invoke preamble creates the host output dir named after the verb.

        Since Task 7 the output dir is created by the shared leaf-invoke preamble
        (``otto.cli.invoke.command_preamble``), not the ``host_app`` callback — so
        dispatch goes through the root ``app`` (which wraps the dynamic verb
        commands with the preamble). ``ensure_cli_session`` / ``ensure_lab_context``
        are stubbed to isolate the output-dir naming (``create_output_dir('host',
        <verb>)``).
        """
        from otto.cli.main import app

        mock_host = _make_host_with_session([("", 0)])

        with (
            patch("otto.cli.invoke.ensure_cli_session"),
            patch("otto.cli.invoke.ensure_lab_context"),
            patch("otto.logger.management.create_output_dir") as p_create,
            patch("otto.fleet.get_host", return_value=mock_host),
        ):
            root_runner.invoke(app, ["--lab", "x", "host", "router1", "exec", "ls"])

        p_create.assert_called_once_with("host", "exec")


# ── Host resolution ──────────────────────────────────────────────────────────


class TestResolveHost:
    def test_valid_host_returns_host(self):
        mock_host = _make_host()
        with patch("otto.fleet.get_host", return_value=mock_host):
            result = host_module._resolve_target("router1")
        assert result is mock_host

    def test_invalid_host_exits(self):
        # The "Available hosts" listing is the ids the lookup's own miss
        # carries, so the stand-in raises the lookup's error, as get_host does.
        from otto.host.targeting import unknown_host_error

        missed = unknown_host_error("nonexistent", lab_name="rig", available=["router1"])
        with patch("otto.fleet.get_host", side_effect=missed):
            result = runner.invoke(host_app, ["nonexistent", "exec", "ls"])

        assert result.exit_code == 1
        assert "No host with ID 'nonexistent'" in result.output
        assert "  - router1" in result.output


class TestResolveCliHostHop:
    def test_hop_is_resolved_through_the_hop_lookup_and_stored_by_id(self):
        """``--hop`` is resolved through the hop lookup (``otto.cli.host_lookup.hop_host``),
        the target through ``get_host``, and the RESOLVED hop's ``.id`` is what
        gets stored on ``host.hop``, not the raw request string.

        The hop request (``"hop-input"``) is deliberately a different string
        from the resolved host's id (``"dut47"``): if ``resolve_cli_host``
        skipped resolution and stored the raw request, or stored something
        other than what the hop lookup returned, ``host.hop`` would come back
        ``"hop-input"``, not ``"dut47"`` — and the call log would be missing
        the hop lookup entirely.
        """
        target_host = _make_host("router1")
        hop_host = _make_host("dut47")
        calls: list[str] = []

        def _fake_get_host(host_id: str, **_overrides: object) -> UnixHost:
            calls.append(host_id)
            if host_id == "router1":
                return target_host
            raise KeyError(host_id)

        def _fake_hop_host(host_id: str) -> UnixHost:
            calls.append(f"hop:{host_id}")
            if host_id == "hop-input":
                return hop_host
            raise KeyError(host_id)

        ctx = SimpleNamespace(
            obj=None,
            meta={
                "_otto_host_request": {
                    "host_id": "router1",
                    "hop": "hop-input",
                    "term": None,
                    "transfer": None,
                }
            },
        )

        with (
            patch("otto.fleet.get_host", side_effect=_fake_get_host),
            patch("otto.cli.host_lookup.hop_host", side_effect=_fake_hop_host),
        ):
            host = host_module.resolve_cli_host(ctx)

        assert calls == ["router1", "hop:hop-input"]
        assert host.hop == "dut47"

    def test_unknown_hop_fails_loud_through_the_same_path_as_an_unknown_target(
        self, tmp_path, capsys
    ):
        """A hop naming no host fails exactly like an unknown target does:
        the same "No host with ID" message, the same ``typer.Exit(1)`` — not a
        silently-``None`` hop and not a different error shape.
        """
        _slot_fleet(tmp_path)
        ctx = _host_ctx(None, "slot1", hop="ghost-hop")

        with pytest.raises(typer.Exit) as exc:
            host_module.resolve_cli_host(ctx)

        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "No host with ID" in out
        assert "ghost-hop" in out


def _hop_labs():
    """The run's lab narrowed to the target, as a scoped view shows it, and the whole lab.

    Only the whole lab holds the hop, ``jump1``. Fresh hosts on every call:
    resolving a hop wires it onto the target.
    """
    from otto.labs.loader import Lab

    target, jump = _make_host("router1"), _make_host("jump1")
    narrow = Lab(name="rig", hosts={"router1": target})
    whole = Lab(name="rig", hosts={"router1": target, "jump1": jump})
    return narrow, whole


def _unscoped_stand_in(monkeypatch, labs: dict) -> None:
    """Patch ``OttoContext.unscoped`` to a stand-in over ``labs["whole"]``, read at each access."""
    from otto.context import OttoContext

    monkeypatch.setattr(
        OttoContext,
        "unscoped",
        property(lambda _ctx: SimpleNamespace(lab=labs["whole"], projects=[])),
        raising=False,
    )


def test_hop_and_remote_completion_share_hop_lookup(monkeypatch, capsys):
    """``otto host --hop`` and its remote path completion resolve a hop through one lookup.

    The lookup reads the run's whole lab, because a hop needs only lab
    membership. ``ctx.lab`` holds only the target, as a scoped view shows it,
    and ``OttoContext.unscoped`` is a stand-in over the whole lab, so a lookup
    that read ``ctx.lab`` misses ``jump1``. Both entry points get the same
    input, and a miss names the whole lab from either.
    """
    from otto.cli import host_lookup
    from otto.cli import remote_completion as rc
    from otto.context import OttoContext
    from otto.run_context import reset_context, set_context

    looked_up: list[str] = []
    real_hop_host = host_lookup.hop_host

    def _spy(host_id: str):
        looked_up.append(host_id)
        return real_hop_host(host_id)

    labs: dict = {}
    _unscoped_stand_in(monkeypatch, labs)
    monkeypatch.setattr(host_lookup, "hop_host", _spy)
    monkeypatch.setattr("otto.declared.get_projects", list)
    # The completer builds its own lab: the narrowed one, as the command's context holds.
    monkeypatch.setattr("otto.session.lab.build_lab", lambda _projects, **_kw: labs["narrow"])

    def _chain(hop: str):
        return rc._ChainParams(host_id="router1", hop=hop, term=None, labs=["rig"], holder=None)

    # `otto host router1 --hop jump1 ...`
    labs["narrow"], labs["whole"] = _hop_labs()
    binding = set_context(OttoContext(lab=labs["narrow"]))
    try:
        assert host_module.resolve_cli_host(_host_ctx(None, "router1", hop="jump1")).hop == "jump1"
    finally:
        reset_context(binding)

    # Its remote path completion, from the same chain.
    labs["narrow"], labs["whole"] = _hop_labs()
    host, token = rc._load_host(_chain("jump1"))
    rc._release_context(token)
    assert host.hop == "jump1"
    assert looked_up == ["jump1", "jump1"]

    # A hop no lab host has: the completion's lookup names the whole lab ...
    with pytest.raises(KeyError) as missed:
        rc._load_host(_chain("ghost"))
    assert missed.value.args[0] == "No host 'ghost' in lab 'rig'. Available: ['jump1', 'router1']"

    # ... and so does the command's refusal.
    binding = set_context(OttoContext(lab=labs["narrow"]))
    try:
        with pytest.raises(typer.Exit) as exc:
            host_module.resolve_cli_host(_host_ctx(None, "router1", hop="ghost"))
    finally:
        reset_context(binding)
    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "No host with ID 'ghost'" in out
    assert "  - jump1" in out
    assert looked_up == ["jump1", "jump1", "ghost", "ghost"]

    # One hop lab: with a stand-in for it, the lookup searches it from both
    # entry points and the command's miss lists it, not the context's lab.
    from otto.labs.loader import Lab

    elsewhere = Lab(name="elsewhere", hosts={"jump9": _make_host("jump9")})
    monkeypatch.setattr(host_lookup, "hop_lab", lambda: elsewhere)
    labs["narrow"], labs["whole"] = _hop_labs()
    with pytest.raises(KeyError) as missed:
        rc._load_host(_chain("ghost"))
    assert missed.value.args[0] == "No host 'ghost' in lab 'elsewhere'. Available: ['jump9']"
    binding = set_context(OttoContext(lab=labs["narrow"]))
    try:
        with pytest.raises(typer.Exit) as refused:
            host_module.resolve_cli_host(_host_ctx(None, "router1", hop="ghost"))
        assert refused.value.exit_code == 1
        assert host_module.resolve_cli_host(_host_ctx(None, "router1", hop="jump9")).hop == "jump9"
    finally:
        reset_context(binding)
    out = capsys.readouterr().out
    assert "  - jump9" in out
    assert "  - jump1" not in out


def test_named_reservation_check_reads_unscoped_lab(monkeypatch):
    """The named-host reservation check judges a named host against the run's whole lab.

    A named host, or its hop, may be one no project targets; its slot comes
    from the lab that holds it. ``ctx.lab`` is narrowed to the target and
    ``OttoContext.unscoped`` is a stand-in over the whole lab, so the lab the
    gate is handed shows which one the check read.
    """
    from otto.context import OttoContext
    from otto.run_context import reset_context, set_context

    narrow, whole = _hop_labs()
    _unscoped_stand_in(monkeypatch, {"whole": whole})
    seen = []

    def _check_hosts(*, lab, hosts):
        seen.append((lab, [h.id for h in hosts]))

    gate = SimpleNamespace(check_hosts=_check_hosts)
    binding = set_context(OttoContext(lab=narrow))
    try:
        host_module._check_named_host_reservations(
            SimpleNamespace(meta={"otto_reservation": gate}), [whole.hosts["jump1"]]
        )
    finally:
        reset_context(binding)
    assert len(seen) == 1
    assert seen[0][0] is whole
    assert seen[0][1] == ["jump1"]


# ── An explicitly named host is reserved too ──────────────────────────────────
#  A `--hop` reaches any host in the lab, and so does the target of a command
#  that opted out of scoping, while the preamble's gate requires only the fleet
#  of interest (spec 2026-08-28 three-level-reservations §5). A host the fleet
#  does not hold, target or hop, therefore needs its OWN slot checked here, or
#  the run touches hardware nobody reserved.


class _SlotBackend(ReservationBackendBase):
    """Reservation backend holding a fixed set; every other resource is dana's.

    ``ends`` optionally gives a resource an end time, which is what the
    expiry warning reads. Rows are built per call rather than stored, because
    ``ReservationBackendBase.reservations`` is a ``cached_property``: a fake
    reused across two constructions would freeze the first answer.
    """

    def __init__(self, held: "set[str]", *, ends: "dict[str, object] | None" = None) -> None:
        super().__init__(username="chris")
        self._held = set(held)
        self._ends = dict(ends or {})

    def backend_name(self) -> str:
        return "fake"

    def fetch_reservations(self, username, *, start=None, end=None):
        return [
            Reservation(user=username, resource=r, end=self._ends.get(r))
            for r in sorted(self._held)
        ]

    def holders(self, resource: str) -> "list[Reservation]":
        return [Reservation(user="dana", resource=resource)]


def _slot_fleet(tmp_path, *, scoped=True):
    """A two-host lab whose declared fleet is ``slot1`` alone; each host owns a slot.

    A scoped command refuses ``slot2`` as a target at the lookup, before any
    reservation is read, so a test that TARGETS it passes ``scoped=False``:
    the context of a command that opted out of scoping, which resolves any
    lab host while its hosts in play stay the declared fleet. A hop needs
    only lab membership, so the hop tests stay scoped.
    """
    from tests._fixtures.fleet import _lab as fleet_lab
    from tests._fixtures.fleet import _project, install_scoped_context

    lab = fleet_lab(("slot1", "rig"), ("slot2", "rig"))
    lab.hosts["slot1"].resources = frozenset({"slot-1"})
    lab.hosts["slot2"].resources = frozenset({"slot-2"})
    projects = [_project(tmp_path, "r1", labs=["rig"], hosts=["slot1"])]
    install_scoped_context(lab, projects, scoped=scoped)
    return lab


def _host_ctx(gate, host_id, hop=""):
    return SimpleNamespace(
        obj=None,
        meta={
            "otto_reservation": gate,
            "_otto_host_request": {
                "host_id": host_id,
                "hop": hop,
                "term": None,
                "transfer": None,
            },
        },
    )


def _gate(held, *, skip_check=False, ends=None):
    from otto.reservations.check import ReservationGate
    from otto.reservations.identity import ResolvedIdentity

    return ReservationGate(
        backend=_SlotBackend(held, ends=ends),
        identity=ResolvedIdentity(username="chris", source="$USER"),
        skip_check=skip_check,
    )


def _spy_build_report(monkeypatch):
    """Record the ``host_ids`` of every report the named-host check builds.

    ``build_report`` is the one place a backend is queried, so an empty record
    is the proof that no query was made. It is patched where it is defined and
    delegates to the real function, so the verdict is unchanged.
    """
    from otto.reservations import report as report_mod

    calls = []
    real = report_mod.build_report

    def spy(lab, username, backend, *, host_ids=None, now=None):
        calls.append(None if host_ids is None else sorted(host_ids))
        return real(lab, username, backend, host_ids=host_ids, now=now)

    monkeypatch.setattr(report_mod, "build_report", spy)
    return calls


def test_targeting_a_host_outside_the_fleet_demands_its_own_slot(tmp_path, capsys):
    """Holding the fleet's slot is not permission to touch a host outside it.

    The project declares ``slot1``, so the preamble's gate asked for
    ``slot-1`` and got it. ``otto host slot2 …`` then resolves a host the fleet
    never covered — and before this check it simply ran.

    Red at HEAD: ``resolve_cli_host`` returned the ``slot2`` host and no
    ``typer.Exit`` was raised.
    """
    _slot_fleet(tmp_path, scoped=False)
    ctx = _host_ctx(_gate({"slot-1"}), "slot2")

    with pytest.raises(typer.Exit) as exc:
        host_module.resolve_cli_host(ctx)

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "slot-2" in out
    assert "host slot2" in out  # the origin: level and owner, not just the id
    assert "dana" in out  # and who holds it


def test_a_scoped_out_of_fleet_target_refuses_before_any_reservation_query(monkeypatch, tmp_path):
    """``otto host slot2 …`` in a scoped command refuses at the lookup and queries nothing.

    ``slot2`` is in the lab but in no declaration, so the scoped lookup
    refuses it before the named-host reservation check runs.
    """
    _slot_fleet(tmp_path)
    calls = _spy_build_report(monkeypatch)
    ctx = _host_ctx(_gate({"slot-1"}), "slot2")

    with pytest.raises(typer.Exit) as exc:
        host_module.resolve_cli_host(ctx)

    assert exc.value.exit_code == 1
    assert calls == []


def test_targeting_a_host_outside_the_fleet_proceeds_when_its_slot_is_held(tmp_path):
    """The check is a check, not a refusal: hold ``slot-2`` and ``slot2`` is reachable."""
    _slot_fleet(tmp_path, scoped=False)
    ctx = _host_ctx(_gate({"slot-1", "slot-2"}), "slot2")

    assert host_module.resolve_cli_host(ctx).id == "slot2"


def test_a_target_inside_the_fleet_is_not_checked_twice(monkeypatch, tmp_path):
    """The preamble already asked the backend for exactly the fleet's requirement.

    A second report for a host already in play would be a second backend query
    per command for an answer otto has just had. The spy goes red on a check
    that drops the fleet filter in ``ReservationGate.check_hosts``.
    """
    _slot_fleet(tmp_path)
    calls = _spy_build_report(monkeypatch)
    ctx = _host_ctx(_gate({"slot-1"}), "slot1")

    assert host_module.resolve_cli_host(ctx).id == "slot1"
    assert calls == []


def test_dash_r_skips_the_targeted_host_check_too(monkeypatch, tmp_path):
    """``-R`` already printed one loud warning; this path adds no second verdict.

    The gate's ``skip_check`` is the whole break-glass contract — a check that
    ignored it would make ``-R`` stop working for exactly the command whose
    target is out of scope.
    """
    _slot_fleet(tmp_path, scoped=False)
    calls = _spy_build_report(monkeypatch)
    ctx = _host_ctx(_gate(set(), skip_check=True), "slot2")

    assert host_module.resolve_cli_host(ctx).id == "slot2"
    assert calls == []


def test_no_reservation_gate_on_the_context_is_a_no_op(tmp_path):
    """A lab-free or ungated invocation has no gate to read; targeting still works."""
    _slot_fleet(tmp_path, scoped=False)
    ctx = _host_ctx(None, "slot2")

    assert host_module.resolve_cli_host(ctx).id == "slot2"


def test_a_hop_outside_the_fleet_demands_its_own_slot(tmp_path, capsys):
    """Reaching a host you hold THROUGH a jump box you do not hold is still using it.

    The target is ``slot1``, squarely inside the declared fleet and already
    covered by the preamble's gate; only the ``--hop`` is outside it. Otto then
    builds a jump transport through ``slot2`` and opens a session on it, so a
    check that looked at the target alone left the whole hole open on the host
    that actually gets logged into first.

    Red at HEAD: the hop resolved after the check, `slot-2` was never demanded,
    and ``resolve_cli_host`` returned with ``host.hop == "slot2"``.
    """
    _slot_fleet(tmp_path)
    ctx = _host_ctx(_gate({"slot-1"}), "slot1", hop="slot2")

    with pytest.raises(typer.Exit) as exc:
        host_module.resolve_cli_host(ctx)

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "slot-2" in out
    assert "host slot2" in out
    assert "slot-1" not in out  # the target is in the fleet: not re-demanded


def test_a_hop_outside_the_fleet_proceeds_when_its_slot_is_held(tmp_path):
    """Hold the hop's slot and the jump is wired as before."""
    _slot_fleet(tmp_path)
    ctx = _host_ctx(_gate({"slot-1", "slot-2"}), "slot1", hop="slot2")

    host = host_module.resolve_cli_host(ctx)

    assert host.id == "slot1"
    assert host.hop == "slot2"


def test_an_out_of_fleet_host_with_no_slot_of_its_own_is_never_queried(monkeypatch, tmp_path):
    """A host that declares nothing can only re-ask for the lab level. Don't.

    ``required_resource_origins`` seeds the lab-level set unconditionally, so
    checking a resource-less out-of-fleet host asks the backend for exactly
    what the preamble's gate already required and got. ``otto host local …``
    is the everyday shape of this — ``local`` is excluded from every declared
    fleet, so without the guard every such run pays a second round trip for a
    verdict otto has just had.

    Red at HEAD: the spy recorded one call with ``['gw']``.
    """
    lab = _slot_fleet(tmp_path, scoped=False)
    lab.resources = {"rig-pdu"}  # a lab-level id, so the requirement is non-empty
    from tests._fixtures.fleet import _host

    lab.add_host(_host("gw", "rig", 9))  # no resources, no element_resources
    assert not lab.hosts["gw"].resources  # the premise, stated
    assert not lab.hosts["gw"].element.resources

    calls = _spy_build_report(monkeypatch)
    ctx = _host_ctx(_gate({"rig-pdu", "slot-1"}), "gw")

    assert host_module.resolve_cli_host(ctx).id == "gw"
    assert calls == []


# ── exec command ──────────────────────────────────────────────────────────────


class TestHostExec:
    def test_exec_success(self):
        mock_host = _make_host_with_session([("", 0)])
        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "exec", "ls"])
        assert result.exit_code == 0, result.output

    def test_exec_failure_exits_with_the_commands_retcode(self):
        mock_host = _make_host_with_session([("command not found", 127)])
        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "exec", "bad_cmd"])
        assert result.exit_code == 127

    def test_two_commands_are_a_usage_error(self):
        """One command, as one argument — click rejects the second positional
        before the verb runs, and the argument's help says how to join steps."""
        with patch("otto.fleet.get_host", return_value=_make_host()):
            result = runner.invoke(host_app, ["router1", "exec", "cd /tmp", "ls"])
        assert result.exit_code == 2
        assert "unexpected extra argument" in result.output

    def test_a_split_command_is_told_to_quote_it(self):
        """`exec -- pfctl show` leaves a second positional; the error says how to join it."""
        with patch("otto.fleet.get_host", return_value=_make_host()):
            result = runner.invoke(host_app, ["router1", "exec", "--", "pfctl", "show"])
        assert result.exit_code == 2
        flat = " ".join(result.output.replace("│", " ").split())
        assert "exec takes its command as one quoted argument" in flat
        assert "otto host <host_id> exec '<command>'" in flat

    @pytest.mark.parametrize(
        ("verb", "args", "noun"),
        [("glob", ["*.c", "*.h"], "pattern"), ("ls", ["/a", "/b"], "path")],
    )
    def test_the_hint_names_the_verbs_own_positional(self, verb, args, noun):
        """The sentence is true of the verb: `glob` takes a pattern, not a command."""
        with patch("otto.fleet.get_host", return_value=_make_host()):
            result = runner.invoke(host_app, ["router1", verb, *args])
        assert result.exit_code == 2
        flat = " ".join(result.output.replace("│", " ").split())
        assert f"{verb} takes its {noun} as one quoted argument" in flat
        assert f"otto host <host_id> {verb} '<{noun}>'" in flat
        assert "pfctl" not in flat

    def test_a_two_positional_command_gets_no_quoting_hint(self):
        """Only a verb whose one positional is the command is told to quote it."""
        from otto.cli.expose import _synthesize_command
        from otto.config._typer import UsageError

        def two(a: str, b: str) -> None:  # pragma: no cover - never reached
            pass

        cmd = _synthesize_command("two", "two", "", two)
        with pytest.raises(UsageError) as exc:
            cmd.main(["x", "y", "z"], standalone_mode=False)
        assert "unexpected extra argument" in exc.value.message
        assert "one quoted argument" not in exc.value.message

    def test_exec_closes_host_on_exception(self):
        mock_host = _make_host()
        mock_host.exec = AsyncMock(side_effect=RuntimeError("boom"))
        mock_host.close = AsyncMock()
        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "exec", "ls"])
        assert result.exit_code != 0
        mock_host.close.assert_awaited_once()

    def test_exec_user_and_sudo_reach_the_method(self):
        mock_host = _make_host()
        mock_host.exec = AsyncMock(
            return_value=CommandResult(status=Status.Success, value="", command="id", retcode=0)
        )
        mock_host.close = AsyncMock()
        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "exec", "id", "--user", "root", "--sudo"])
        assert result.exit_code == 0, result.output
        kw = mock_host.exec.await_args.kwargs
        assert kw["user"] == "root"
        assert kw["sudo"] is True
        assert kw["cmd"] == "id"


# ── put command ───────────────────────────────────────────────────────────────


class TestHostPut:
    def test_put_success(self, tmp_path):
        src_file = tmp_path / "file.txt"
        src_file.write_text("hello")

        mock_host = _make_host()
        # An ok Result with a success= string falls through to the @cli_exposed
        # success message ("Transfer complete.").  The dynamic path reads the
        # success string from __cli_success__ on the bound method, so we must
        # preserve that marker on the AsyncMock.
        mock_host.put = AsyncMock(return_value=Result(status=Status.Success, value={}))
        mock_host.put.__cli_success__ = "Transfer complete."
        mock_host.close = AsyncMock()

        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "put", str(src_file), "/tmp/dest"])

        assert result.exit_code == 0
        assert "Transfer complete" in result.output
        mock_host.put.assert_awaited_once()
        mock_host.close.assert_awaited_once()

    def test_put_failure(self, tmp_path):
        src_file = tmp_path / "file.txt"
        src_file.write_text("hello")

        mock_host = _make_host()
        mock_host.put = AsyncMock(
            return_value=Result(status=Status.Failed, value={}, msg="permission denied")
        )
        mock_host.close = AsyncMock()

        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "put", str(src_file), "/tmp/dest"])

        assert result.exit_code == 1
        # Dynamic path prints the error message from the Result, not a "Transfer failed:" prefix.
        assert "permission denied" in result.output
        mock_host.close.assert_awaited_once()


# ── --term and --transfer options ────────────────────────────────────────────


class TestHostTermAndTransfer:
    def test_valid_term_dispatches_to_override(self):
        """Contract: --term applies an override-copy via _apply_option_overrides."""
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch("otto.fleet.get_host", return_value=mock_host),
            patch(
                "otto.run_context._apply_option_overrides", return_value=mock_host
            ) as mock_override,
        ):
            result = runner.invoke(host_app, ["--term", "telnet", "router1", "exec", "ls"])

        assert result.exit_code == 0, result.output
        mock_override.assert_any_call(mock_host, term="telnet")

    def test_valid_transfer_dispatches_to_override(self):
        """Contract: --transfer applies an override-copy via _apply_option_overrides."""
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch("otto.fleet.get_host", return_value=mock_host),
            patch(
                "otto.run_context._apply_option_overrides", return_value=mock_host
            ) as mock_override,
        ):
            result = runner.invoke(host_app, ["--transfer", "ftp", "router1", "exec", "ls"])

        assert result.exit_code == 0, result.output
        mock_override.assert_any_call(mock_host, transfer="ftp")

    def test_invalid_term_exits(self):
        mock_host = _make_host()
        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["--term", "bogus", "router1", "exec", "ls"])

        assert result.exit_code != 0

    def test_invalid_transfer_exits(self):
        mock_host = _make_host()
        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["--transfer", "bogus", "router1", "exec", "ls"])

        assert result.exit_code != 0

    def test_no_term_or_transfer_skips_override(self):
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch("otto.fleet.get_host", return_value=mock_host),
            patch("otto.run_context._apply_option_overrides") as mock_override,
        ):
            result = runner.invoke(host_app, ["router1", "exec", "ls"])

        assert result.exit_code == 0
        mock_override.assert_not_called()

    def test_valid_term_applies_to_host(self):
        """End-to-end: --term resolves an override copy whose active term is set.
        Uses a real UnixHost; telnet is in the default unix menu."""
        base = _make_host_with_session([("", 0)])
        switched = _make_host_with_session([("", 0)])
        switched.term = "telnet"

        with (
            patch("otto.fleet.get_host", return_value=base),
            patch("otto.run_context._apply_option_overrides", return_value=switched),
            patch.object(
                base._session_mgr, "exec_pooled", wraps=base._session_mgr.exec_pooled
            ) as base_exec,
            patch.object(
                switched._session_mgr, "exec_pooled", wraps=switched._session_mgr.exec_pooled
            ) as switched_exec,
        ):
            result = runner.invoke(host_app, ["--term", "telnet", "router1", "exec", "ls"])

        assert result.exit_code == 0, result.output
        # The command must run on the override copy, never the base host.
        switched_exec.assert_called_once()
        base_exec.assert_not_called()

    def test_valid_transfer_applies_to_host(self):
        base = _make_host_with_session([("", 0)])
        switched = _make_host_with_session([("", 0)])
        switched.transfer = "sftp"

        with (
            patch("otto.fleet.get_host", return_value=base),
            patch("otto.run_context._apply_option_overrides", return_value=switched),
            patch.object(
                base._session_mgr, "exec_pooled", wraps=base._session_mgr.exec_pooled
            ) as base_exec,
            patch.object(
                switched._session_mgr, "exec_pooled", wraps=switched._session_mgr.exec_pooled
            ) as switched_exec,
        ):
            result = runner.invoke(host_app, ["--transfer", "sftp", "router1", "exec", "ls"])

        assert result.exit_code == 0, result.output
        # The command must run on the override copy, never the base host.
        switched_exec.assert_called_once()
        base_exec.assert_not_called()

    def test_term_and_transfer_together(self):
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch("otto.fleet.get_host", return_value=mock_host),
            patch(
                "otto.run_context._apply_option_overrides", return_value=mock_host
            ) as mock_override,
        ):
            result = runner.invoke(
                host_app, ["--term", "ssh", "--transfer", "sftp", "router1", "exec", "ls"]
            )

        assert result.exit_code == 0
        # Both options dispatch through the seam: one call per option, each with
        # its own per-param kwarg. The patch returns mock_host every time, so the
        # second call's host arg is the (same) override copy the first returned.
        assert mock_override.call_count == 2
        mock_override.assert_any_call(mock_host, term="ssh")
        mock_override.assert_any_call(mock_host, transfer="sftp")

    def test_transfer_nc_on_a_console_host_is_a_parameter_error(self):
        """An nc pin on a console-term host is refused as a ``--transfer``
        parameter error (exit 2, the option named), not as a bare
        ConsoleError sentence from building the copy's transfer backend."""
        from otto.host.options import ConsoleOptions

        console_host = UnixHost(
            ip="10.0.0.2",
            element=Element(name="test2"),
            creds=[Cred(login="test", password="pw")],
            term="console",
            valid_terms=["ssh", "console"],
            transfer="shell",
            valid_transfers=["shell", "nc"],
            console_options=ConsoleOptions(server="test1", port=4001),
            log=LogMode.QUIET,
        )
        with patch("otto.fleet.get_host", return_value=console_host):
            result = runner.invoke(host_app, ["--transfer", "nc", "test2", "exec", "ls"])

        assert result.exit_code == 2, result.output
        flat = " ".join(re.sub(r"[│╭╮╰╯─]", " ", result.output).split())
        assert "Invalid value for --transfer: test2: transfer 'nc' cannot run" in flat


# ── get command ───────────────────────────────────────────────────────────────


class TestHostGet:
    def test_get_success(self, tmp_path):
        mock_host = _make_host()
        # An ok Result with a success= string falls through to the @cli_exposed
        # success message ("Download complete.").  The dynamic path reads the
        # success string from __cli_success__ on the bound method, so we must
        # preserve that marker on the AsyncMock.
        mock_host.get = AsyncMock(return_value=Result(status=Status.Success, value={}))
        mock_host.get.__cli_success__ = "Download complete."
        mock_host.close = AsyncMock()

        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "get", "/remote/file.txt", str(tmp_path)])

        assert result.exit_code == 0
        assert "Download complete" in result.output
        mock_host.get.assert_awaited_once()
        mock_host.close.assert_awaited_once()

    def test_get_failure(self, tmp_path):
        mock_host = _make_host()
        mock_host.get = AsyncMock(
            return_value=Result(status=Status.Failed, value={}, msg="not found")
        )
        mock_host.close = AsyncMock()

        with patch("otto.fleet.get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "get", "/remote/file.txt", str(tmp_path)])

        assert result.exit_code == 1
        # Dynamic path prints the error message from the Result, not a "Transfer failed:" prefix.
        assert "not found" in result.output
        mock_host.close.assert_awaited_once()


# ── host_id shell-completion ─────────────────────────────────────────────────


def _write_hosts_json(path: Path, hosts: list[dict]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return write_lab_json(path / "lab.json", hosts)


def _fake_project(*lab_paths: Path) -> Project:
    """Stand-in for :class:`Project` whose ``[[lab.sources]]`` are *lab_paths*.

    ``lab_sources`` is the compiled ``[[lab.sources]]`` list the host source is
    built from."""
    root = lab_paths[0].parent if lab_paths else Path()
    return fake_project(lab_sources=json_lab_sources(root, list(lab_paths)), root=root)


class TestHostIdCompleter:
    """``_host_id_completer`` runs during tab completion, before
    :func:`otto.bootstrap.bootstrap` registers project init modules.  It must
    therefore derive host IDs straight from the lab files each project's
    ``[[lab.sources]]`` json entries name."""

    def test_returns_all_host_ids(self, tmp_path):
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "test1",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
                {
                    "ip": "1.1.1.2",
                    "element": "test2",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
            ],
        )
        # _host_id_completer lazy-imports get_projects from otto.config.
        with patch("otto.declared.get_projects", return_value=[_fake_project(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        # collect_host_ids also surfaces the built-in `local` host (sorted).
        assert result == ["local", "test1", "test2"]

    def test_filters_by_incomplete_prefix(self, tmp_path):
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "test1",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
                {
                    "ip": "1.1.1.2",
                    "element": "test2",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
            ],
        )
        with patch("otto.declared.get_projects", return_value=[_fake_project(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="test2")
        assert result == ["test2"]
        # A STRICT prefix must narrow too. Since the rename, `test1` and `test2`
        # share every prefix that is not the whole id, so the case above is
        # satisfied by an `==` filter as well as a `startswith` one. `loc` is a
        # genuine strict prefix of exactly one offered id, and an `==` filter
        # returns [] for it.
        with patch("otto.declared.get_projects", return_value=[_fake_project(lab)]):
            strict = _host_id_completer(ctx=MagicMock(), incomplete="loc")
        assert strict == ["local"]

    def test_merges_ids_across_multiple_paths(self, tmp_path):
        lab1 = tmp_path / "lab1"
        lab2 = tmp_path / "lab2"
        _write_hosts_json(
            lab1,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "test1",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
            ],
        )
        _write_hosts_json(
            lab2,
            [
                {
                    "ip": "2.2.2.2",
                    "element": "beet",
                    "board": "seed",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["roots"],
                },
            ],
        )
        with patch("otto.declared.get_projects", return_value=[_fake_project(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["beet-seed", "local", "test1"]  # + built-in local

    def test_deduplicates_ids(self, tmp_path):
        """Same host id present in two lab.json files must collapse to one."""
        lab1 = tmp_path / "lab1"
        lab2 = tmp_path / "lab2"
        dup = {
            "ip": "1.1.1.1",
            "element": "test1",
            "creds": [{"login": "u", "password": "p"}],
            "labs": ["unix"],
        }
        _write_hosts_json(lab1, [dup])
        _write_hosts_json(lab2, [dup])
        with patch("otto.declared.get_projects", return_value=[_fake_project(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["local", "test1"]  # + built-in local

    def test_skips_missing_path(self, tmp_path):
        """Non-existent search path must not raise; completer is best-effort."""
        with patch("otto.declared.get_projects", return_value=[_fake_project(tmp_path / "nope")]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["local"]  # only the built-in local (no lab.json to scan)

    def test_skips_malformed_json(self, tmp_path):
        lab = tmp_path / "bad"
        lab.mkdir()
        (lab / "lab.json").write_text("{not json")
        with patch("otto.declared.get_projects", return_value=[_fake_project(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["local"]  # malformed json skipped; built-in local remains

    def test_skips_invalid_host_entries(self, tmp_path):
        """A host dict missing required fields must be skipped, not abort."""
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {"element": "incomplete"},  # missing ip, creds — validate_host_dict rejects
                {
                    "ip": "1.1.1.1",
                    "element": "test1",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
            ],
        )
        with patch("otto.declared.get_projects", return_value=[_fake_project(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["local", "test1"]  # invalid entry skipped; built-in local remains

    def test_prefers_cached_host_ids(self, tmp_path):
        """When the completion cache is populated (fast path), the completer
        must serve from it and not re-parse every ``lab.json``.

        Uses a nonexistent search path to prove live parsing didn't run:
        without the cache, ``collect_host_ids`` would return ``[]`` and the
        assertion on ``router1``/``router2`` would fail.
        """
        fake_cache = {
            "instructions": [],
            "hosts": ["router1", "router2", "switch7"],
        }
        with (
            patch("otto.registry.get_completion_names", return_value=fake_cache),
            patch(
                "otto.declared.get_projects",
                return_value=[_fake_project(tmp_path / "does-not-exist")],
            ),
        ):
            result = _host_id_completer(ctx=MagicMock(), incomplete="r")
        assert result == ["router1", "router2"]

    def test_falls_through_on_cache_miss(self, tmp_path):
        """``get_completion_names`` returns None off the fast path — completer
        must still find host IDs by scanning ``lab.json`` live."""
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "test1",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
            ],
        )
        with (
            patch("otto.registry.get_completion_names", return_value=None),
            patch("otto.declared.get_projects", return_value=[_fake_project(lab)]),
        ):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["local", "test1"]  # live scan + built-in local

    def test_argument_advertises_completer(self):
        """Regression guard: the ``host_id`` parameter must carry the
        completer so Click hands it to the shell during tab completion."""
        import inspect
        from typing import get_args

        sig = inspect.signature(host_module.main)
        metadata = get_args(sig.parameters["host_id"].annotation)
        argument = next(m for m in metadata if hasattr(m, "autocompletion"))
        assert argument.autocompletion is _host_id_completer


def _ctx_with_labs(lab_names):
    """Build the context chain completion builds.

    Mirrors the real layout: ``-l/--lab`` lives on the root ``otto`` callback,
    so ``labs`` sits on otto's root context, while the ``otto host`` child ctx
    (the one handed to the completer) carries only its own params.
    """
    host = chain("otto", "host", root_params={"labs": lab_names})
    host.params = {"host_id": "", "hop": "", "term": None, "transfer": None}
    return host


class TestHostIdCompleterLabFilter:
    """When a lab is selected (``-l``/``--lab`` or ``OTTO_LAB``), completion
    must offer only hosts in that lab, not the whole fleet."""

    def test_live_scan_filters_by_selected_lab(self, tmp_path):
        """Cache miss: a live lab.json scan is restricted to the lab."""
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "test1",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix"],
                },
                {
                    "ip": "1.1.1.2",
                    "element": "alt2",
                    "creds": [{"login": "u", "password": "p"}],
                    "labs": ["unix_alt"],
                },
            ],
        )
        with (
            patch("otto.registry.get_completion_names", return_value=None),
            patch("otto.declared.get_projects", return_value=[_fake_project(lab)]),
        ):
            result = _host_id_completer(ctx=_ctx_with_labs(["unix"]), incomplete="")
        # test1 (unix) + built-in local; alt2 (unix_alt) excluded.
        assert result == ["local", "test1"]

    def test_cached_hosts_filtered_by_selected_lab(self, tmp_path):
        """Fast path: the completer reads the per-lab cache map, not flat hosts."""
        fake_cache = {
            "hosts": ["test1", "alt2", "alt3"],
            "hosts_by_lab": {
                "unix": ["test1"],
                "unix_alt": ["alt2", "alt3"],
            },
        }
        with (
            patch("otto.registry.get_completion_names", return_value=fake_cache),
            patch(
                "otto.declared.get_projects",
                return_value=[_fake_project(tmp_path / "does-not-exist")],
            ),
        ):
            result = _host_id_completer(ctx=_ctx_with_labs(["unix_alt"]), incomplete="")
        # unix_alt members + built-in local; test1 (unix) excluded.
        assert result == ["alt2", "alt3", "local"]

    def test_reads_lab_from_parent_context(self, tmp_path):
        """``-l`` sits on the root ctx, not the host child ctx — walk up to it."""
        fake_cache = {
            "hosts": ["test1", "alt2"],
            "hosts_by_lab": {"unix": ["test1"], "unix_alt": ["alt2"]},
        }
        with patch("otto.registry.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(["unix"]), incomplete="")
        assert result == ["local", "test1"]

    def test_unknown_lab_offers_only_builtin(self, tmp_path):
        """A lab absent from the cache map still resolves the built-in host."""
        fake_cache = {
            "hosts": ["test1"],
            "hosts_by_lab": {"unix": ["test1"]},
        }
        with patch("otto.registry.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(["ghosts"]), incomplete="")
        assert result == ["local"]

    def test_no_lab_selected_returns_all_hosts(self, tmp_path):
        """No lab (labs=None on the root ctx) keeps the whole-fleet behaviour."""
        fake_cache = {
            "hosts": ["test1", "alt2", "alt3"],
            "hosts_by_lab": {"unix": ["test1"]},
        }
        with patch("otto.registry.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(None), incomplete="")
        assert result == ["alt2", "alt3", "test1"]

    def test_prefix_filter_still_applies_within_lab(self, tmp_path):
        fake_cache = {
            "hosts": ["test1", "cabbage-seed", "alt2"],
            "hosts_by_lab": {"unix": ["test1", "cabbage-seed"]},
        }
        with patch("otto.registry.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(["unix"]), incomplete="test")
        assert result == ["test1"]


# ── The expiry warning on the out-of-fleet path ───────────────────────────────
#  The preamble's gate warned about the resources the FLEET needs. An
#  explicitly named out-of-fleet host brings its own element- and host-level
#  slots, which were never in that set and are exactly the ones this command
#  is about to use — so this site warns too. The lab-level requirement is
#  common to both sites, and a suppression set keyed on ``(resource, end)`` is
#  what keeps it from being announced twice in one run.


def _soon_utc(minutes):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_naming_an_out_of_fleet_host_warns_that_its_own_slot_is_lapsing(tmp_path, caplog):
    """``slot-2`` is held, so no refusal — but it ends in a minute, so say so.

    The preamble's gate never saw ``slot-2``: the declared fleet is ``slot1``
    alone. Without a warning at this site the user gets no notice at all for
    the host they explicitly targeted.
    """
    import logging

    _slot_fleet(tmp_path, scoped=False)
    ctx = _host_ctx(_gate({"slot-1", "slot-2"}, ends={"slot-2": _soon_utc(1)}), "slot2")

    with caplog.at_level(logging.WARNING, logger="otto"):
        assert host_module.resolve_cli_host(ctx).id == "slot2"

    assert "slot-2" in caplog.text
    assert "expires" in caplog.text.lower()


def test_a_healthy_out_of_fleet_slot_says_nothing(tmp_path, caplog):
    """The negative half: a booking well outside the window is not news."""
    import logging

    _slot_fleet(tmp_path, scoped=False)
    ctx = _host_ctx(_gate({"slot-1", "slot-2"}, ends={"slot-2": _soon_utc(240)}), "slot2")

    with caplog.at_level(logging.WARNING, logger="otto"):
        assert host_module.resolve_cli_host(ctx).id == "slot2"

    assert "expires" not in caplog.text.lower()


def test_warning_fires_only_once_for_an_out_of_fleet_host(tmp_path, caplog):
    """A lab-level booking is required by BOTH sites and announced by one.

    The gate requires the lab's own ``rig-pdu`` plus the fleet's ``slot-1``;
    the out-of-fleet check for ``slot2`` requires ``rig-pdu`` again — it is
    seeded unconditionally — plus ``slot-2``. Announce from every report
    rather than once per ``(resource, end)`` and this goes red with a count
    of 2.
    """
    import logging

    lab = _slot_fleet(tmp_path, scoped=False)
    lab.resources = {"rig-pdu"}
    gate = _gate({"rig-pdu", "slot-1", "slot-2"}, ends={"rig-pdu": _soon_utc(2)})
    ctx = _host_ctx(gate, "slot2")

    with caplog.at_level(logging.WARNING, logger="otto"):
        gate.evaluate()  # the preamble
        assert host_module.resolve_cli_host(ctx).id == "slot2"

    assert caplog.text.count("rig-pdu") == 1


def test_dash_r_suppresses_the_out_of_fleet_expiry_warning(tmp_path, caplog):
    """``-R`` returns above the check at this site, so above the warning too.

    Mutation: move the ``skip_check`` return in
    ``ReservationGate.check_hosts`` below the report and this goes red. The
    asymmetry with ``otto reservation check`` — which still warns under
    ``-R`` — is deliberate and pinned in ``tests/unit/cli/test_reservation.py``.
    """
    import logging

    _slot_fleet(tmp_path, scoped=False)
    ctx = _host_ctx(
        _gate({"slot-1", "slot-2"}, skip_check=True, ends={"slot-2": _soon_utc(1)}), "slot2"
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        assert host_module.resolve_cli_host(ctx).id == "slot2"

    assert "expires" not in caplog.text.lower()


def test_a_null_backend_never_reaches_the_out_of_fleet_expiry_helper(tmp_path, caplog):
    """The guard that keeps the warning from becoming the thing that queries.

    ``build_report`` short-circuits on the null backend before it asks
    anything, and the expiry nudge reads that report, so a lab configured
    with no scheduler stays a no-op.

    Mutation: drop the ``null or`` short-circuit from ``build_report`` and the
    fetch below raises. A plain
    ``NullReservationBackend`` could not show that — it answers ``[]``, so an
    unguarded call would stay silently green.
    """
    import logging

    from otto.reservations import NullReservationBackend
    from otto.reservations.check import ReservationGate
    from otto.reservations.identity import ResolvedIdentity

    class _ExplodingNullBackend(NullReservationBackend):
        """Null-shaped to ``is_null_backend``'s ``isinstance``, fatal to query."""

        def fetch_reservations(self, username, *, start=None, end=None):
            raise AssertionError(
                "the null backend was queried; the is_null_backend guard is missing"
            )

    _slot_fleet(tmp_path, scoped=False)
    gate = ReservationGate(
        # WITH the username the check is for: without it the fetch this test
        # is trying to prove unreachable would fail on the base class's "no
        # username was resolved" error instead of the AssertionError the
        # exploding double exists to raise, and the mutation would go red for
        # the wrong reason. `is_null_backend` short-circuits before the
        # username is ever read, so the guard stays green either way.
        backend=_ExplodingNullBackend(username="chris"),
        identity=ResolvedIdentity(username="chris", source="$USER"),
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        assert host_module.resolve_cli_host(_host_ctx(gate, "slot2")).id == "slot2"

    assert "expires" not in caplog.text.lower()


# ── The target is scoped; a --hop is not ──────────────────────────────────────
#  Lab ``alpha`` holds ``a-dut``, ``a-jump`` and the built-in runner; the one
#  project declares ``a-dut`` alone, so ``a-jump`` is in the lab and out of
#  scope. Dispatch goes through the real root app over real parsed projects.


def _alpha_lab(*extra_hosts):
    """Lab ``alpha``: ``a-dut``, ``a-jump``, any *extra_hosts*, then the built-in runner."""
    from tests._fixtures.fleet import _lab, add_builtin_local

    lab = _lab(("a-dut", "alpha"), ("a-jump", "alpha"), component_names=["alpha"])
    for host in extra_hosts:
        lab.add_host(host)
    return add_builtin_local(lab)


def _inscope(tmp_path):
    from tests._fixtures.fleet import _project

    return _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut"])


def _refusal(project, host_id="a-jump"):
    """The out-of-scope refusal for *host_id* in lab ``alpha``, *project* the one declarer."""
    return (
        f"host {host_id!r} is in lab 'alpha' but outside this run's scope: no declared "
        "project targets it. To reach it, add its id to [project] host_patterns (or its "
        f"lab to lab_patterns) in {project.root}/.otto/settings.toml."
    )


def _dispatch_alpha(monkeypatch, tmp_path, argv, *, lab=None):
    from tests._fixtures.fleet import dispatch_root

    project = _inscope(tmp_path)
    result = dispatch_root(
        monkeypatch,
        argv,
        lab=lab if lab is not None else _alpha_lab(),
        projects=[project],
        env={"COLUMNS": "400"},
    )
    return result, project


def _menu_verbs(output):
    """Return the verb names the help's Commands panel lists, in order."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if "─ Commands ─" in line)
    verbs = []
    for line in lines[start + 1 :]:
        if line.startswith("╰"):
            break
        name = re.match(r"│ (\S+)\s", line)
        if name:
            verbs.append(name.group(1))
    return verbs


def _never_runs(monkeypatch):
    """Make the unix ``exec`` verb fail the test if it is reached; return the call log."""
    import functools

    calls: list[object] = []

    # Wrapped, so the stand-in keeps the verb's markers and signature.
    @functools.wraps(UnixHost.exec)
    async def _exec(self, *args, **kwargs):
        calls.append(self.id)
        raise AssertionError(f"exec reached host {self.id!r}")

    monkeypatch.setattr(UnixHost, "exec", _exec)
    return calls


def test_host_out_of_scope_refuses_exit_1(monkeypatch, tmp_path):
    """Fail-first: ``otto host <out-of-scope> exec`` prints the refusal and connects nowhere."""
    calls = _never_runs(monkeypatch)

    result, project = _dispatch_alpha(monkeypatch, tmp_path, ["host", "a-jump", "exec", "true"])

    assert result.exit_code == 1, result.output
    assert _refusal(project) in result.output.splitlines()
    assert "No host with ID" not in result.output
    assert calls == []


@pytest.mark.parametrize("argv", [["host", "a-jump", "--help"], ["host", "a-jump"]])
def test_host_out_of_scope_help_refuses_no_menu(monkeypatch, tmp_path, argv):
    """Fail-first: the group's menu for an out-of-scope target is its refusal, not a menu."""
    result, project = _dispatch_alpha(monkeypatch, tmp_path, argv)

    assert result.exit_code == 1, result.output
    assert _refusal(project) in result.output.splitlines()
    assert "Usage" not in result.output
    assert "Commands" not in result.output


def test_host_out_of_scope_verb_help_refuses(monkeypatch, tmp_path):
    """Fail-first: a scoped verb's ``--help`` on an out-of-scope target refuses."""
    result, project = _dispatch_alpha(monkeypatch, tmp_path, ["host", "a-jump", "exec", "--help"])

    assert result.exit_code == 1, result.output
    assert _refusal(project) in result.output.splitlines()
    assert "Usage" not in result.output


@pytest.fixture
def scope_free_jump():
    """``a-jump`` is a host whose ``scope-probe`` verb opts out of scoping.

    Yields ``(lab_factory, seen)``: each call of ``lab_factory`` builds lab
    ``alpha`` with ``a-dut``, that ``a-jump`` and the built-in runner;
    ``seen`` records ``(host id, ctx.scoped)`` for each run of the verb.
    """
    import otto.host.os_profile as op
    from otto.cli.invoke import SCOPED_ATTR
    from otto.host.factory import create_host_from_dict
    from otto.utils import cli_exposed
    from tests._fixtures.fleet import _lab, add_builtin_local

    seen: list[object] = []

    class _ScopeFreeHost(UnixHost):
        @cli_exposed(output_dir=False)
        async def scope_probe(self) -> str:
            """Report the scope verdict this verb ran under."""
            from otto.context import get_context

            seen.append((self.id, get_context().scoped))
            return "ran"

        setattr(scope_probe, SCOPED_ATTR, False)

    def lab_with_jump():
        lab = _lab(("a-dut", "alpha"), component_names=["alpha"])
        jump = create_host_from_dict(
            {
                "os_type": "t13-scope-free",
                "ip": "192.0.2.9",
                "creds": [{"login": "admin", "password": "admin"}],
            },
            lab_name="alpha",
            element=Element(name="a-jump"),
        )
        assert type(jump) is _ScopeFreeHost
        lab.add_host(jump)
        return add_builtin_local(lab)

    op.register_host_class("t13-scope-free", _ScopeFreeHost)
    try:
        yield lab_with_jump, seen
    finally:
        op.HOST_CLASSES.unregister(name="t13-scope-free")


def test_opted_out_host_verb_runs_on_out_of_scope_target(monkeypatch, tmp_path, scope_free_jump):
    """Fail-first: a host verb that opts out of scoping runs on a target outside the scope.

    The verb's ``__cli_scoped__ = False`` reaches its command, so the leaf's
    verdict replaces the probe's scoped context with an unscoped one and the
    lookup resolves ``a-jump``. Its ``--help`` prints; the target's menu lists
    only it; an ordinary verb on the same target still refuses.
    """
    lab_with_jump, seen = scope_free_jump
    runs = itertools.count()

    def _run(argv):
        root = tmp_path / str(next(runs))
        return _dispatch_alpha(monkeypatch, root, argv, lab=lab_with_jump())

    ran, _ = _run(["host", "a-jump", "scope-probe"])
    assert ran.exit_code == 0, ran.output
    assert seen == [("a-jump", False)]

    verb_help, _ = _run(["host", "a-jump", "scope-probe", "--help"])
    assert verb_help.exit_code == 0, verb_help.output
    assert "Report the scope verdict" in verb_help.output

    menu, _ = _run(["host", "a-jump", "--help"])
    assert menu.exit_code == 0, menu.output
    assert _menu_verbs(menu.output) == ["scope-probe"], menu.output

    ordinary, project = _run(["host", "a-jump", "exec", "true"])
    assert ordinary.exit_code == 1, ordinary.output
    assert _refusal(project) in ordinary.output.splitlines()
    assert seen == [("a-jump", False)]


@pytest.mark.parametrize("held", [False, True])
def test_opted_out_verb_checks_its_out_of_scope_targets_slot(
    monkeypatch, tmp_path, scope_free_jump, held
):
    """Regression: an opted-out verb's target outside the fleet needs its own slot held.

    The preamble's gate covers the hosts in play (``a-dut``'s ``slot-a``);
    ``a-jump`` is outside them, so the named-host check asks for ``slot-j``
    before the verb runs, through the whole dispatch.
    """
    lab_with_jump, seen = scope_free_jump
    lab = lab_with_jump()
    lab.hosts["a-dut"].resources = frozenset({"slot-a"})
    lab.hosts["a-jump"].resources = frozenset({"slot-j"})
    slots = {"slot-a", "slot-j"} if held else {"slot-a"}
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate", lambda **_kw: _gate(slots)
    )

    ran, _ = _dispatch_alpha(monkeypatch, tmp_path, ["host", "a-jump", "scope-probe"], lab=lab)

    if held:
        assert ran.exit_code == 0, ran.output
        assert seen == [("a-jump", False)]
    else:
        assert ran.exit_code == 1, ran.output
        assert "slot-j" in ran.output
        assert "dana" in ran.output
        assert seen == []


def test_class_discovery_reads_unscoped_lab(tmp_path):
    """Fail-first: a target's class is found whether or not the run may target it."""
    from otto.cli.expose import host_class_for_id
    from otto.cli.host_lookup import host_class
    from tests._fixtures.fleet import scope_workspace

    ws = scope_workspace(tmp_path)
    assert "a-jump" not in ws.ctx.targetable_ids()

    assert host_class("a-jump") is type(ws.lab.hosts["a-jump"])
    assert host_class_for_id("a-jump") is type(ws.lab.hosts["a-jump"])
    assert host_class("ghost") is None
    assert host_class_for_id("ghost") is None


def test_host_unknown_id_message_and_exit_unchanged(monkeypatch, tmp_path):
    """Regression: an unknown target prints today's lines, listing the targetable hosts."""
    result, _ = _dispatch_alpha(monkeypatch, tmp_path, ["host", "nope", "exec", "true"])

    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    assert "No host with ID 'nope'." in lines
    assert "Available hosts:" in lines
    listed = [line for line in lines if line.startswith("  - ")]
    assert listed == ["  - a-dut", "  - local"]


def test_host_no_lab_error_unchanged(monkeypatch, tmp_path):
    """Regression: with no ``--lab`` and no ``OTTO_LAB``, a target gets the missing-lab error."""
    from typer.testing import CliRunner

    from otto.cli.main import app
    from tests._fixtures.bootstrap_seam import install_composed, patch_bootstrap

    monkeypatch.delenv("OTTO_LAB", raising=False)
    install_composed(monkeypatch, patch_bootstrap(monkeypatch, [_inscope(tmp_path)]))

    result = CliRunner().invoke(app, ["host", "a-dut", "exec", "true"])

    assert result.exit_code == 2, result.output
    assert "Missing option '--lab' / '-l' (env var: 'OTTO_LAB')" in result.output


_HELP_PATHS = (["host", "nope", "--help"], ["host", "nope", "exec", "--help"])


@pytest.mark.parametrize("argv", _HELP_PATHS, ids=["menu", "verb"])
def test_host_help_unknown_id_and_no_lab_full_menu(monkeypatch, tmp_path, argv):
    """Regression: an unknown id, or no lab at all, gets today's full help on both paths."""
    from typer.testing import CliRunner

    from otto.cli.main import app
    from tests._fixtures.bootstrap_seam import install_composed, patch_bootstrap

    unknown, _ = _dispatch_alpha(monkeypatch, tmp_path / "unknown", argv)

    monkeypatch.delenv("OTTO_LAB", raising=False)
    install_composed(monkeypatch, patch_bootstrap(monkeypatch, [_inscope(tmp_path / "nolab")]))
    no_lab = CliRunner().invoke(app, argv)

    for result in (unknown, no_lab):
        assert result.exit_code == 0, result.output
        assert "Usage" in result.output
        if argv[-2] == "nope":
            # The group's menu: every verb, since no class was found.
            assert re.search(r"│\s+exec\s", result.output), result.output
            assert re.search(r"│\s+reboot\s", result.output), result.output


def test_host_help_project_load_error_falls_through(monkeypatch, tmp_path):
    """Regression: a lookup that raises ``ProjectLoadError`` leaves help as it was."""
    from otto.context import OttoContext
    from otto.session.errors import ProjectLoadError

    def _refuses(self, host_id, **overrides):
        raise ProjectLoadError([])

    monkeypatch.setattr(OttoContext, "get_host", _refuses)

    menu, _ = _dispatch_alpha(monkeypatch, tmp_path / "menu", ["host", "a-jump", "--help"])
    verb, _ = _dispatch_alpha(monkeypatch, tmp_path / "verb", ["host", "a-jump", "exec", "--help"])

    assert menu.exit_code == 0, menu.output
    assert re.search(r"│\s+exec\s", menu.output), menu.output
    assert verb.exit_code == 0, verb.output
    assert "Usage" in verb.output


def test_exclude_typo_still_exits_2_before_scope_refusal(monkeypatch, tmp_path):
    """Regression: a typo'd ``-E`` is reported as a typo, ahead of the target's refusal."""
    result, _ = _dispatch_alpha(
        monkeypatch, tmp_path, ["-E", "typo", "host", "a-jump", "exec", "true"]
    )

    assert result.exit_code == 2, result.output
    assert "typo" in result.output
    assert "outside this run's scope" not in result.output


def test_host_unknown_lists_targetable_only(tmp_path, capsys):
    """Regression: the listing under an unknown target is the ids the run may target."""
    from tests._fixtures.fleet import scope_workspace

    ws = scope_workspace(tmp_path)

    with pytest.raises(typer.Exit) as exc:
        host_module._resolve_target("nope")

    assert exc.value.exit_code == 1
    listed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  - ")]
    assert listed == [f"  - {host_id}" for host_id in ws.ctx.targetable_ids()]
    assert "  - a-jump" not in listed


def test_hop_out_of_scope_accepted(tmp_path):
    """Regression: a hop needs only lab membership; an out-of-scope one is wired."""
    from tests._fixtures.fleet import scope_workspace

    scope_workspace(tmp_path)

    host = host_module.resolve_cli_host(_host_ctx(None, "a-dut", hop="a-jump"))

    assert host.id == "a-dut"
    assert host.hop == "a-jump"


def test_hop_unknown_lists_whole_lab(tmp_path, capsys):
    """Regression: an unknown hop lists every lab host, the out-of-scope ones included."""
    from tests._fixtures.fleet import scope_workspace

    ws = scope_workspace(tmp_path)

    with pytest.raises(typer.Exit) as exc:
        host_module.resolve_cli_host(_host_ctx(None, "a-dut", hop="ghost"))

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "No host with ID 'ghost'." in out
    listed = [line for line in out.splitlines() if line.startswith("  - ")]
    assert listed == [f"  - {host_id}" for host_id in ws.lab.hosts]
    assert "  - a-jump" in listed


def test_host_local_resolves_under_declaration(tmp_path):
    """Regression: ``otto host local`` resolves under a declaration: the runner is targetable."""
    from otto.host.builtin_hosts import is_runner_side
    from tests._fixtures.fleet import scope_workspace

    scope_workspace(tmp_path)

    assert is_runner_side(host_module._resolve_target("local"))


def test_dry_run_probe_rides_scoped_lookup(monkeypatch, tmp_path):
    """Regression: ``-n --probe`` resolves its target through the scoped lookup.

    An out-of-scope target is refused at that lookup, before the probe runs;
    an in-scope target's dry run resolves.
    """
    probed: list[object] = []
    monkeypatch.setattr("otto.cli.probe.run_probe", lambda refs: probed.append(refs) or [])

    refused, project = _dispatch_alpha(
        monkeypatch, tmp_path / "out", ["-n", "--probe", "host", "a-jump", "exec", "true"]
    )
    assert refused.exit_code == 1, refused.output
    assert _refusal(project) in refused.output.splitlines()
    assert probed == []

    resolved, _ = _dispatch_alpha(
        monkeypatch, tmp_path / "in", ["-n", "host", "a-dut", "exec", "true"]
    )
    assert resolved.exit_code == 0, resolved.output
    assert "references resolve: host 'a-dut'" in " ".join(resolved.output.split())


def test_typo_verb_on_refused_target_is_no_such_command(monkeypatch, tmp_path):
    """Regression: a verb no class has is a usage error, exit 2, even on a refused target."""
    result, _ = _dispatch_alpha(monkeypatch, tmp_path, ["host", "a-jump", "exce", "true"])

    assert result.exit_code == 2, result.output
    assert "No such command 'exce'" in result.output
    assert "outside this run's scope" not in result.output


def test_host_plain_keyerror_lists_targetable_ids(monkeypatch, tmp_path, capsys):
    """A lookup that misses with a plain ``KeyError`` still prints the unknown-id lines.

    ``fleet.get_host`` documents a plain ``KeyError`` for an unknown id, so a
    substituted lookup that raises one lists the hosts the run may target.
    """
    from tests._fixtures.fleet import scope_workspace

    ws = scope_workspace(tmp_path)

    def _plain_miss(host_id, **_overrides):
        raise KeyError(host_id)

    monkeypatch.setattr("otto.fleet.get_host", _plain_miss)

    with pytest.raises(typer.Exit) as exc:
        host_module._resolve_target("nope")

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "No host with ID 'nope'." in out
    listed = [line for line in out.splitlines() if line.startswith("  - ")]
    assert listed == [f"  - {host_id}" for host_id in ws.ctx.targetable_ids()]
