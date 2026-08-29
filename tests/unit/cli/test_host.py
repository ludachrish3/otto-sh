"""
Unit tests for the ``otto host`` subcommand.

Covers:
  - Help / no-args behaviour
  - Callback sets the logger output directory and resolves host to ctx.obj
  - Host resolution (success and failure)
  - The run, put, and get commands invoke the correct host methods
"""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto.cli import host as host_module
from otto.cli.host import _host_id_completer, _resolve_host, host_app
from otto.host.login_proxy import Cred
from otto.host.session import SessionManager, ShellSession
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import Result
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.labdata import json_lab_sources, write_lab_json

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
        element=name,
        creds=[Cred(login="admin", password="secret")],
        log=LogMode.NORMAL,
    )


class FakeSession(ShellSession):
    """ShellSession with pre-loaded responses for synchronous CLI tests.

    Each entry in *responses* is an ``(output, retcode)`` pair consumed in
    order by successive ``run_cmd`` calls.  When the base class writes the
    sentinel-wrapped command, the fake immediately enqueues the matching
    begin marker, output lines, and end sentinel so that ``_read_until_pattern``
    can return them without real I/O.
    """

    def __init__(self, responses: list[tuple[str, int]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._read_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _open(self) -> None:
        pass  # no transport to open

    async def _write(self, data: str) -> None:
        if self._ready_marker in data:
            # Initialization handshake — echo the ready marker back
            self._read_queue.put_nowait(f"{self._ready_marker}\n")
        elif self._begin_marker in data and self._responses:
            # Sentinel-wrapped command — enqueue the canned response
            output, retcode = self._responses.pop(0)
            self._read_queue.put_nowait(f"{self._begin_marker}\n")
            if output:
                for line in output.splitlines():
                    self._read_queue.put_nowait(f"{line}\n")
            self._read_queue.put_nowait(f"{self._end_marker_prefix}{retcode}__\n")

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
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
) -> UnixHost:
    """Build a UnixHost whose SessionManager uses a FakeSession.

    The full chain ``run -> _run_one -> SessionManager.run_cmd ->
    ShellSession.run_cmd`` runs for real; only the transport is faked.
    Logging callbacks are suppressed to avoid interfering with CliRunner's
    stdout capture.
    """
    host = UnixHost(
        ip="10.0.0.1",
        element=name,
        creds=[Cred(login="admin", password="secret")],
        log=LogMode.NORMAL,
    )
    fake = FakeSession(responses)
    host._session_mgr = SessionManager(
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

    def test_run_listed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "run" in result.output

    def test_login_and_run_exposed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "login" in result.output
        assert "run" in result.output

    def test_put_listed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "put" in result.output

    def test_get_listed_in_help(self):
        result = runner.invoke(host_app, ["--help"])
        assert "get" in result.output

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
            patch.object(host_module, "get_host", return_value=mock_host),
        ):
            root_runner.invoke(app, ["--lab", "x", "host", "router1", "run", "ls"])

        p_create.assert_called_once_with("host", "run")


# ── Host resolution ──────────────────────────────────────────────────────────


class TestResolveHost:
    def test_valid_host_returns_host(self):
        mock_host = _make_host()
        with patch.object(host_module, "get_host", return_value=mock_host):
            result = _resolve_host("router1")
        assert result is mock_host

    def test_invalid_host_exits(self):
        # The "Available hosts" listing reads the lab's mapping directly rather
        # than the fleet generator (explicit targeting is unscoped), so there is
        # no all_hosts to stand in for — an empty lab is enough here, and
        # tests/unit/config/test_fleet_scoping.py owns the unscoped-listing
        # guard itself.
        with patch.object(host_module, "get_host", side_effect=KeyError("nope")):
            result = runner.invoke(host_app, ["nonexistent", "run", "ls"])

        assert result.exit_code == 1
        assert "No host with ID" in result.output


class TestResolveCliHostHop:
    def test_hop_handle_resolves_to_canonical_id(self):
        """A positional-handle ``--hop`` (e.g. "dut1", the N-th "dut" host by
        logical index) must be canonicalized before being stored on
        ``host.hop`` — downstream hop lookups (e.g.
        ``RemoteHost._build_hop_transport``'s ``lab.hosts[hop_id]``) are
        canonical-id-only and would KeyError on a raw handle.

        ``get_host`` is the lab-data-lookup I/O boundary (see conftest mock
        policy), so it's faked here to mimic ``Lab.resolve_handle``: "dut1"
        (the positional handle) resolves to the host whose canonical id is
        "dut47" (a repeated "dut" element whose lowest element_id sorts to
        logical index 1).
        """
        target_host = _make_host("router1")
        canonical_hop_host = _make_host("dut47")

        def _fake_get_host(host_id: str, **_overrides: object) -> UnixHost:
            if host_id == "router1":
                return target_host
            if host_id == "dut1":
                return canonical_hop_host
            raise KeyError(host_id)

        ctx = SimpleNamespace(
            obj=None,
            meta={
                "_otto_host_request": {
                    "host_id": "router1",
                    "hop": "dut1",
                    "term": None,
                    "transfer": None,
                }
            },
        )

        with patch.object(host_module, "get_host", side_effect=_fake_get_host):
            host = host_module.resolve_cli_host(ctx)

        assert host.hop == "dut47"


# ── An explicitly named host is reserved too ──────────────────────────────────
#  `otto host <id> --hop <id>` reaches any host in the lab (explicit targeting
#  beats scoping), while the preamble's gate requires only the fleet of interest
#  (spec 2026-08-28 three-level-reservations §5). A host the fleet does not hold
#  — target OR hop — therefore needs its OWN slot checked here, or the run
#  touches hardware nobody reserved.


class _SlotBackend:
    """Reservation backend holding a fixed set; every other resource is dana's."""

    def __init__(self, held: set[str]) -> None:
        self._held = set(held)

    def backend_name(self) -> str:
        return "fake"

    def get_reserved_resources(self, username: str) -> set[str]:
        return set(self._held)

    def who_reserved(self, resource: str) -> list[str]:
        return ["dana"]


def _slot_fleet(monkeypatch, tmp_path):
    """A two-host lab whose declared fleet is ``slot1`` alone; each host owns a slot."""
    from tests._fixtures.fleet import _lab as fleet_lab
    from tests._fixtures.fleet import _repo, install_scoped_context

    lab = fleet_lab(("slot1", "rig"), ("slot2", "rig"))
    lab.hosts["slot1"].resources = frozenset({"slot-1"})
    lab.hosts["slot2"].resources = frozenset({"slot-2"})
    install_scoped_context(monkeypatch, lab, [_repo(tmp_path, "r1", labs=["rig"], hosts=["slot1"])])
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


def _gate(held, *, skip_check=False):
    from otto.reservations.check import ReservationGate
    from otto.reservations.identity import ResolvedIdentity

    return ReservationGate(
        backend=_SlotBackend(held),
        identity=ResolvedIdentity(username="chris", source="$USER"),
        skip_check=skip_check,
    )


def test_targeting_a_host_outside_the_fleet_demands_its_own_slot(monkeypatch, tmp_path, capsys):
    """Holding the fleet's slot is not permission to touch a host outside it.

    The project declares ``slot1``, so the preamble's gate asked for
    ``slot-1`` and got it. ``otto host slot2 …`` then resolves a host the fleet
    never covered — and before this check it simply ran.

    Red at HEAD: ``resolve_cli_host`` returned the ``slot2`` host and no
    ``typer.Exit`` was raised.
    """
    _slot_fleet(monkeypatch, tmp_path)
    ctx = _host_ctx(_gate({"slot-1"}), "slot2")

    with pytest.raises(typer.Exit) as exc:
        host_module.resolve_cli_host(ctx)

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "slot-2" in out
    assert "host slot2" in out  # the origin: level and owner, not just the id
    assert "dana" in out  # and who holds it


def test_targeting_a_host_outside_the_fleet_proceeds_when_its_slot_is_held(monkeypatch, tmp_path):
    """The check is a check, not a refusal: hold ``slot-2`` and ``slot2`` is reachable."""
    _slot_fleet(monkeypatch, tmp_path)
    ctx = _host_ctx(_gate({"slot-1", "slot-2"}), "slot2")

    assert host_module.resolve_cli_host(ctx).id == "slot2"


def test_a_target_inside_the_fleet_is_not_checked_twice(monkeypatch, tmp_path):
    """The preamble already asked the backend for exactly the fleet's requirement.

    A second ``check_reservations`` for a host already in play would be a
    second backend query per command for an answer otto has just had. The spy
    goes red on a check that drops the ``admissible_ids`` guard.
    """
    from otto.reservations import check as check_mod

    _slot_fleet(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        check_mod, "check_reservations", lambda *a, **kw: calls.append(kw.get("host_ids"))
    )
    ctx = _host_ctx(_gate({"slot-1"}), "slot1")

    assert host_module.resolve_cli_host(ctx).id == "slot1"
    assert calls == []


def test_dash_r_skips_the_targeted_host_check_too(monkeypatch, tmp_path):
    """``-R`` already printed one loud warning; this path adds no second verdict.

    The gate's ``skip_check`` is the whole break-glass contract — a check that
    ignored it would make ``-R`` stop working for exactly the command whose
    target is out of scope.
    """
    from otto.reservations import check as check_mod

    _slot_fleet(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        check_mod, "check_reservations", lambda *a, **kw: calls.append(kw.get("host_ids"))
    )
    ctx = _host_ctx(_gate(set(), skip_check=True), "slot2")

    assert host_module.resolve_cli_host(ctx).id == "slot2"
    assert calls == []


def test_no_reservation_gate_on_the_context_is_a_no_op(monkeypatch, tmp_path):
    """A lab-free or ungated invocation has no gate to read; targeting still works."""
    _slot_fleet(monkeypatch, tmp_path)
    ctx = _host_ctx(None, "slot2")

    assert host_module.resolve_cli_host(ctx).id == "slot2"


def test_a_hop_outside_the_fleet_demands_its_own_slot(monkeypatch, tmp_path, capsys):
    """Reaching a host you hold THROUGH a jump box you do not hold is still using it.

    The target is ``slot1``, squarely inside the declared fleet and already
    covered by the preamble's gate; only the ``--hop`` is outside it. Otto then
    builds a jump transport through ``slot2`` and opens a session on it, so a
    check that looked at the target alone left the whole hole open on the host
    that actually gets logged into first.

    Red at HEAD: the hop resolved after the check, `slot-2` was never demanded,
    and ``resolve_cli_host`` returned with ``host.hop == "slot2"``.
    """
    _slot_fleet(monkeypatch, tmp_path)
    ctx = _host_ctx(_gate({"slot-1"}), "slot1", hop="slot2")

    with pytest.raises(typer.Exit) as exc:
        host_module.resolve_cli_host(ctx)

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "slot-2" in out
    assert "host slot2" in out
    assert "slot-1" not in out  # the target is in the fleet: not re-demanded


def test_a_hop_outside_the_fleet_proceeds_when_its_slot_is_held(monkeypatch, tmp_path):
    """Hold the hop's slot and the jump is wired as before."""
    _slot_fleet(monkeypatch, tmp_path)
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

    Red at HEAD: the spy recorded one call with ``{'gw'}``.
    """
    from otto.reservations import check as check_mod

    lab = _slot_fleet(monkeypatch, tmp_path)
    lab.resources = {"rig-pdu"}  # a lab-level id, so the requirement is non-empty
    from tests._fixtures.fleet import _host

    lab.add_host(_host("gw", "rig", 9))  # no resources, no element_resources
    assert not lab.hosts["gw"].resources  # the premise, stated
    assert not lab.hosts["gw"].element_resources

    calls = []
    monkeypatch.setattr(
        check_mod, "check_reservations", lambda *a, **kw: calls.append(kw.get("host_ids"))
    )
    ctx = _host_ctx(_gate({"rig-pdu", "slot-1"}), "gw")

    assert host_module.resolve_cli_host(ctx).id == "gw"
    assert calls == []


# ── run command ───────────────────────────────────────────────────────────────


class TestHostRun:
    def test_run_success(self):
        mock_host = _make_host_with_session([("", 0), ("", 0)])

        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "run", "ls", "pwd"])

        assert result.exit_code == 0

    def test_run_failure_exits_nonzero(self):
        mock_host = _make_host_with_session([("command not found", 127)])

        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "run", "bad_cmd"])

        # Results.exit_code is ssh-like: it propagates the failing command's
        # own return code (127) rather than a flat 1.
        assert result.exit_code == 127

    def test_run_closes_host_on_exception(self):
        mock_host = _make_host()
        mock_host.run = AsyncMock(side_effect=RuntimeError("boom"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "run", "ls"])

        assert result.exit_code != 0
        mock_host.close.assert_awaited_once()


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
        mock_host.put = AsyncMock(return_value=Result(Status.Success, value={}))
        mock_host.put.__cli_success__ = "Transfer complete."
        mock_host.close = AsyncMock()

        with patch.object(host_module, "get_host", return_value=mock_host):
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
            return_value=Result(Status.Failed, value={}, msg="permission denied")
        )
        mock_host.close = AsyncMock()

        with patch.object(host_module, "get_host", return_value=mock_host):
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
            patch.object(host_module, "get_host", return_value=mock_host),
            patch.object(
                host_module, "_apply_option_overrides", return_value=mock_host
            ) as mock_override,
        ):
            result = runner.invoke(host_app, ["--term", "telnet", "router1", "run", "ls"])

        assert result.exit_code == 0, result.output
        mock_override.assert_any_call(mock_host, term="telnet")

    def test_valid_transfer_dispatches_to_override(self):
        """Contract: --transfer applies an override-copy via _apply_option_overrides."""
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch.object(host_module, "get_host", return_value=mock_host),
            patch.object(
                host_module, "_apply_option_overrides", return_value=mock_host
            ) as mock_override,
        ):
            result = runner.invoke(host_app, ["--transfer", "ftp", "router1", "run", "ls"])

        assert result.exit_code == 0, result.output
        mock_override.assert_any_call(mock_host, transfer="ftp")

    def test_invalid_term_exits(self):
        mock_host = _make_host()
        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["--term", "bogus", "router1", "run", "ls"])

        assert result.exit_code != 0

    def test_invalid_transfer_exits(self):
        mock_host = _make_host()
        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["--transfer", "bogus", "router1", "run", "ls"])

        assert result.exit_code != 0

    def test_no_term_or_transfer_skips_override(self):
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch.object(host_module, "get_host", return_value=mock_host),
            patch.object(host_module, "_apply_option_overrides") as mock_override,
        ):
            result = runner.invoke(host_app, ["router1", "run", "ls"])

        assert result.exit_code == 0
        mock_override.assert_not_called()

    def test_valid_term_applies_to_host(self):
        """End-to-end: --term resolves an override copy whose active term is set.
        Uses a real UnixHost; telnet is in the default unix menu."""
        base = _make_host_with_session([("", 0)])
        switched = _make_host_with_session([("", 0)])
        switched.term = "telnet"

        with (
            patch.object(host_module, "get_host", return_value=base),
            patch.object(host_module, "_apply_option_overrides", return_value=switched),
        ):
            result = runner.invoke(host_app, ["--term", "telnet", "router1", "run", "ls"])

        assert result.exit_code == 0, result.output

    def test_valid_transfer_applies_to_host(self):
        base = _make_host_with_session([("", 0)])
        switched = _make_host_with_session([("", 0)])
        switched.transfer = "sftp"

        with (
            patch.object(host_module, "get_host", return_value=base),
            patch.object(host_module, "_apply_option_overrides", return_value=switched),
        ):
            result = runner.invoke(host_app, ["--transfer", "sftp", "router1", "run", "ls"])

        assert result.exit_code == 0, result.output

    def test_term_and_transfer_together(self):
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch.object(host_module, "get_host", return_value=mock_host),
            patch.object(
                host_module, "_apply_option_overrides", return_value=mock_host
            ) as mock_override,
        ):
            result = runner.invoke(
                host_app, ["--term", "ssh", "--transfer", "sftp", "router1", "run", "ls"]
            )

        assert result.exit_code == 0
        # Both options dispatch through the seam: one call per option, each with
        # its own per-param kwarg. The patch returns mock_host every time, so the
        # second call's host arg is the (same) override copy the first returned.
        assert mock_override.call_count == 2
        mock_override.assert_any_call(mock_host, term="ssh")
        mock_override.assert_any_call(mock_host, transfer="sftp")


# ── get command ───────────────────────────────────────────────────────────────


class TestHostGet:
    def test_get_success(self, tmp_path):
        mock_host = _make_host()
        # An ok Result with a success= string falls through to the @cli_exposed
        # success message ("Download complete.").  The dynamic path reads the
        # success string from __cli_success__ on the bound method, so we must
        # preserve that marker on the AsyncMock.
        mock_host.get = AsyncMock(return_value=Result(Status.Success, value={}))
        mock_host.get.__cli_success__ = "Download complete."
        mock_host.close = AsyncMock()

        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "get", "/remote/file.txt", str(tmp_path)])

        assert result.exit_code == 0
        assert "Download complete" in result.output
        mock_host.get.assert_awaited_once()
        mock_host.close.assert_awaited_once()

    def test_get_failure(self, tmp_path):
        mock_host = _make_host()
        mock_host.get = AsyncMock(return_value=Result(Status.Failed, value={}, msg="not found"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "get", "/remote/file.txt", str(tmp_path)])

        assert result.exit_code == 1
        # Dynamic path prints the error message from the Result, not a "Transfer failed:" prefix.
        assert "not found" in result.output
        mock_host.close.assert_awaited_once()


# ── host_id shell-completion ─────────────────────────────────────────────────


def _write_hosts_json(path: Path, hosts: list[dict]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return write_lab_json(path / "lab.json", hosts)


def _fake_repo(*lab_paths: Path) -> SimpleNamespace:
    """Stand-in for :class:`Repo` exposing what the completer reads.

    ``lab_sources`` is the compiled ``[[lab.sources]]`` list the host source is
    built from; ``inventory_settings`` is what ``build_inventory`` reads on the
    same path (an omitted one makes the enumeration return no hosts at all)."""
    sut_dir = lab_paths[0].parent if lab_paths else Path()
    return SimpleNamespace(
        lab_sources=json_lab_sources(sut_dir, list(lab_paths)),
        sut_dir=sut_dir,
        inventory_settings={},
    )


class TestHostIdCompleter:
    """``_host_id_completer`` runs during tab completion, before
    :func:`otto.bootstrap.bootstrap` registers repo init modules.  It must
    therefore derive host IDs straight from the lab files each repo's
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
        # _host_id_completer lazy-imports get_repos from otto.config.
        with patch("otto.config.get_repos", return_value=[_fake_repo(lab)]):
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
        with patch("otto.config.get_repos", return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="test2")
        assert result == ["test2"]
        # A STRICT prefix must narrow too. Since the rename, `test1` and `test2`
        # share every prefix that is not the whole id, so the case above is
        # satisfied by an `==` filter as well as a `startswith` one. `loc` is a
        # genuine strict prefix of exactly one offered id, and an `==` filter
        # returns [] for it.
        with patch("otto.config.get_repos", return_value=[_fake_repo(lab)]):
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
        with patch("otto.config.get_repos", return_value=[_fake_repo(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["beet_seed", "local", "test1"]  # + built-in local

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
        with patch("otto.config.get_repos", return_value=[_fake_repo(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["local", "test1"]  # + built-in local

    def test_skips_missing_path(self, tmp_path):
        """Non-existent search path must not raise; completer is best-effort."""
        with patch("otto.config.get_repos", return_value=[_fake_repo(tmp_path / "nope")]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["local"]  # only the built-in local (no lab.json to scan)

    def test_skips_malformed_json(self, tmp_path):
        lab = tmp_path / "bad"
        lab.mkdir()
        (lab / "lab.json").write_text("{not json")
        with patch("otto.config.get_repos", return_value=[_fake_repo(lab)]):
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
        with patch("otto.config.get_repos", return_value=[_fake_repo(lab)]):
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
            "suites": [],
            "hosts": ["router1", "router2", "switch7"],
        }
        with (
            patch("otto.config.get_completion_names", return_value=fake_cache),
            patch(
                "otto.config.get_repos",
                return_value=[_fake_repo(tmp_path / "does-not-exist")],
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
            patch("otto.config.get_completion_names", return_value=None),
            patch("otto.config.get_repos", return_value=[_fake_repo(lab)]),
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


def _ctx_with_labs(lab_names, *, on_child=False) -> SimpleNamespace:
    """Build a Click-like context chain for completion.

    Mirrors the real layout: ``-l/--lab`` lives on the root ``otto`` callback,
    so ``labs`` sits on the *parent* context, while the ``otto host`` child ctx
    (the one handed to the completer) carries only its own params. Set
    ``on_child`` only to prove the completer reads the nearest context first.
    """
    child_params = {"host_id": "", "hop": "", "term": None, "transfer": None}
    root_params = {"labs": lab_names}
    if on_child:
        child_params["labs"] = lab_names
        root_params = {"labs": None}
    root = SimpleNamespace(info_name="otto", params=root_params, parent=None)
    return SimpleNamespace(info_name="host", params=child_params, parent=root)


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
            patch("otto.config.get_completion_names", return_value=None),
            patch("otto.config.get_repos", return_value=[_fake_repo(lab)]),
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
            patch("otto.config.get_completion_names", return_value=fake_cache),
            patch(
                "otto.config.get_repos",
                return_value=[_fake_repo(tmp_path / "does-not-exist")],
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
        with patch("otto.config.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(["unix"]), incomplete="")
        assert result == ["local", "test1"]

    def test_unknown_lab_offers_only_builtin(self, tmp_path):
        """A lab absent from the cache map still resolves the built-in host."""
        fake_cache = {
            "hosts": ["test1"],
            "hosts_by_lab": {"unix": ["test1"]},
        }
        with patch("otto.config.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(["ghosts"]), incomplete="")
        assert result == ["local"]

    def test_no_lab_selected_returns_all_hosts(self, tmp_path):
        """No lab (labs=None on the root ctx) keeps the whole-fleet behaviour."""
        fake_cache = {
            "hosts": ["test1", "alt2", "alt3"],
            "hosts_by_lab": {"unix": ["test1"]},
        }
        with patch("otto.config.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(None), incomplete="")
        assert result == ["alt2", "alt3", "test1"]

    def test_prefix_filter_still_applies_within_lab(self, tmp_path):
        fake_cache = {
            "hosts": ["test1", "cabbage_seed", "alt2"],
            "hosts_by_lab": {"unix": ["test1", "cabbage_seed"]},
        }
        with patch("otto.config.get_completion_names", return_value=fake_cache):
            result = _host_id_completer(ctx=_ctx_with_labs(["unix"]), incomplete="test")
        assert result == ["test1"]
