"""
Unit tests for the ``otto host`` subcommand.

Covers:
  - Help / no-args behaviour
  - Callback sets the logger output directory and resolves host to ctx.obj
  - Host resolution (success and failure)
  - The run, put, and get commands invoke the correct host methods
"""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from otto.cli import host as host_module
from otto.cli.host import _host_id_completer, _resolve_host, host_app
from otto.host.session import SessionManager, ShellSession
from otto.host.unix_host import UnixHost
from otto.utils import Status

runner = CliRunner()


# ── Shared helpers ────────────────────────────────────────────────────────────


def _make_host(name: str = "router1") -> UnixHost:
    """Return a real UnixHost (no connection is made on construction)."""
    return UnixHost(ip="10.0.0.1", element=name, creds={"admin": "secret"}, log=True)


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
    host = UnixHost(ip="10.0.0.1", element=name, creds={"admin": "secret"}, log=True)
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
        mock_host = _make_host_with_session([("", 0)])

        with (
            patch("otto.logger.management.create_output_dir") as p_create,
            patch.object(host_module, "get_host", return_value=mock_host),
        ):
            runner.invoke(host_app, ["router1", "run", "ls"])

        p_create.assert_called_once_with("host", "run")


# ── Host resolution ──────────────────────────────────────────────────────────


class TestResolveHost:
    def test_valid_host_returns_host(self):
        mock_host = _make_host()
        with patch.object(host_module, "get_host", return_value=mock_host):
            result = _resolve_host("router1")
        assert result is mock_host

    def test_invalid_host_exits(self):
        with (
            patch.object(host_module, "get_host", side_effect=KeyError("nope")),
            patch.object(host_module, "all_hosts", return_value=iter([_make_host()])),
        ):
            result = runner.invoke(host_app, ["nonexistent", "run", "ls"])

        assert result.exit_code == 1
        assert "No host with ID" in result.output


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

        assert result.exit_code == 1

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
        # Return empty msg so _render_result falls through to the @cli_exposed
        # success= string ("Transfer complete.").  The dynamic path reads the
        # success string from __cli_success__ on the bound method, so we must
        # preserve that marker on the AsyncMock.
        mock_host.put = AsyncMock(return_value=(Status.Success, ""))
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
        mock_host.put = AsyncMock(return_value=(Status.Failed, "permission denied"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "put", str(src_file), "/tmp/dest"])

        assert result.exit_code == 1
        # Dynamic path prints the error message from the tuple, not a "Transfer failed:" prefix.
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
        # Return empty msg so _render_result falls through to the @cli_exposed
        # success= string ("Download complete.").  The dynamic path reads the
        # success string from __cli_success__ on the bound method, so we must
        # preserve that marker on the AsyncMock.
        mock_host.get = AsyncMock(return_value=(Status.Success, ""))
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
        mock_host.get = AsyncMock(return_value=(Status.Failed, "not found"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, "get_host", return_value=mock_host):
            result = runner.invoke(host_app, ["router1", "get", "/remote/file.txt", str(tmp_path)])

        assert result.exit_code == 1
        # Dynamic path prints the error message from the tuple, not a "Transfer failed:" prefix.
        assert "not found" in result.output
        mock_host.close.assert_awaited_once()


# ── host_id shell-completion ─────────────────────────────────────────────────


def _write_hosts_json(path: Path, hosts: list[dict]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    hosts_file = path / "hosts.json"
    hosts_file.write_text(json.dumps(hosts))
    return hosts_file


def _fake_repo(*lab_paths: Path) -> SimpleNamespace:
    """Stand-in for :class:`Repo` that only exposes the attribute the
    completer actually reads (``labs``)."""
    return SimpleNamespace(labs=list(lab_paths))


class TestHostIdCompleter:
    """``_host_id_completer`` runs during tab completion, before
    ``apply_repo_settings()`` populates the ConfigModule.  It must therefore
    derive host IDs straight from the ``hosts.json`` files referenced by
    each repo's ``labs`` search paths."""

    def test_returns_all_host_ids(self, tmp_path):
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "carrot",
                    "board": "seed",
                    "creds": {"u": "p"},
                    "labs": ["veggies"],
                },
                {
                    "ip": "1.1.1.2",
                    "element": "tomato",
                    "board": "seed",
                    "creds": {"u": "p"},
                    "labs": ["veggies"],
                },
            ],
        )
        # _host_id_completer lazy-imports get_repos from otto.configmodule.
        with patch("otto.configmodule.get_repos", return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["carrot_seed", "tomato_seed"]

    def test_filters_by_incomplete_prefix(self, tmp_path):
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "carrot",
                    "board": "seed",
                    "creds": {"u": "p"},
                    "labs": ["veggies"],
                },
                {
                    "ip": "1.1.1.2",
                    "element": "tomato",
                    "board": "seed",
                    "creds": {"u": "p"},
                    "labs": ["veggies"],
                },
            ],
        )
        with patch("otto.configmodule.get_repos", return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="tom")
        assert result == ["tomato_seed"]

    def test_merges_ids_across_multiple_paths(self, tmp_path):
        lab1 = tmp_path / "lab1"
        lab2 = tmp_path / "lab2"
        _write_hosts_json(
            lab1,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "carrot",
                    "board": "seed",
                    "creds": {"u": "p"},
                    "labs": ["veggies"],
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
                    "creds": {"u": "p"},
                    "labs": ["roots"],
                },
            ],
        )
        with patch("otto.configmodule.get_repos", return_value=[_fake_repo(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["beet_seed", "carrot_seed"]

    def test_deduplicates_ids(self, tmp_path):
        """Same host id present in two hosts.json files must collapse to one."""
        lab1 = tmp_path / "lab1"
        lab2 = tmp_path / "lab2"
        dup = {
            "ip": "1.1.1.1",
            "element": "carrot",
            "board": "seed",
            "creds": {"u": "p"},
            "labs": ["veggies"],
        }
        _write_hosts_json(lab1, [dup])
        _write_hosts_json(lab2, [dup])
        with patch("otto.configmodule.get_repos", return_value=[_fake_repo(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["carrot_seed"]

    def test_skips_missing_path(self, tmp_path):
        """Non-existent search path must not raise; completer is best-effort."""
        with patch("otto.configmodule.get_repos", return_value=[_fake_repo(tmp_path / "nope")]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == []

    def test_skips_malformed_json(self, tmp_path):
        lab = tmp_path / "bad"
        lab.mkdir()
        (lab / "hosts.json").write_text("{not json")
        with patch("otto.configmodule.get_repos", return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == []

    def test_skips_invalid_host_entries(self, tmp_path):
        """A host dict missing required fields must be skipped, not abort."""
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {"element": "incomplete"},  # missing ip, creds — validate_host_dict rejects
                {
                    "ip": "1.1.1.1",
                    "element": "carrot",
                    "board": "seed",
                    "creds": {"u": "p"},
                    "labs": ["veggies"],
                },
            ],
        )
        with patch("otto.configmodule.get_repos", return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["carrot_seed"]

    def test_prefers_cached_host_ids(self, tmp_path):
        """When the completion cache is populated (fast path), the completer
        must serve from it and not re-parse every ``hosts.json``.

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
            patch("otto.configmodule.get_completion_names", return_value=fake_cache),
            patch(
                "otto.configmodule.get_repos",
                return_value=[_fake_repo(tmp_path / "does-not-exist")],
            ),
        ):
            result = _host_id_completer(ctx=MagicMock(), incomplete="r")
        assert result == ["router1", "router2"]

    def test_falls_through_on_cache_miss(self, tmp_path):
        """``get_completion_names`` returns None off the fast path — completer
        must still find host IDs by scanning ``hosts.json`` live."""
        lab = tmp_path / "labA"
        _write_hosts_json(
            lab,
            [
                {
                    "ip": "1.1.1.1",
                    "element": "carrot",
                    "board": "seed",
                    "creds": {"u": "p"},
                    "labs": ["veggies"],
                },
            ],
        )
        with (
            patch("otto.configmodule.get_completion_names", return_value=None),
            patch("otto.configmodule.get_repos", return_value=[_fake_repo(lab)]),
        ):
            result = _host_id_completer(ctx=MagicMock(), incomplete="")
        assert result == ["carrot_seed"]

    def test_argument_advertises_completer(self):
        """Regression guard: the ``host_id`` parameter must carry the
        completer so Click hands it to the shell during tab completion."""
        import inspect
        from typing import get_args

        sig = inspect.signature(host_module.main)
        metadata = get_args(sig.parameters["host_id"].annotation)
        argument = next(m for m in metadata if hasattr(m, "autocompletion"))
        assert argument.autocompletion is _host_id_completer
