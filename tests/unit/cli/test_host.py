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

import pytest
from typer.testing import CliRunner

from otto.cli import host as host_module
from otto.cli.host import _host_id_completer, _resolve_host, host_app
from otto.host.remoteHost import RemoteHost
from otto.host.session import SessionManager, ShellSession
from otto.utils import Status

runner = CliRunner()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_host(name: str = 'router1') -> RemoteHost:
    """Return a real RemoteHost (no connection is made on construction)."""
    return RemoteHost(ip='10.0.0.1', ne=name, creds={'admin': 'secret'}, log=True)


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
    name: str = 'router1',
) -> RemoteHost:
    """Build a RemoteHost whose SessionManager uses a FakeSession.

    The full chain ``run -> _run_one -> SessionManager.run_cmd ->
    ShellSession.run_cmd`` runs for real; only the transport is faked.
    Logging callbacks are suppressed to avoid interfering with CliRunner's
    stdout capture.
    """
    host = RemoteHost(ip='10.0.0.1', ne=name, creds={'admin': 'secret'}, log=True)
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
        assert 'Usage' in result.output or 'usage' in result.output.lower()

    def test_help_flag(self):
        result = runner.invoke(host_app, ['--help'])
        assert result.exit_code == 0

    def test_help_short_flag(self):
        result = runner.invoke(host_app, ['-h'])
        assert result.exit_code == 0

    def test_run_listed_in_help(self):
        result = runner.invoke(host_app, ['--help'])
        assert 'run' in result.output

    def test_put_listed_in_help(self):
        result = runner.invoke(host_app, ['--help'])
        assert 'put' in result.output

    def test_get_listed_in_help(self):
        result = runner.invoke(host_app, ['--help'])
        assert 'get' in result.output

    def test_host_id_only_no_subcommand_shows_help(self):
        """otto host router1 (no verb) should show help."""
        result = runner.invoke(host_app, ['router1'])
        assert result.exit_code == 0
        assert 'Usage' in result.output or 'usage' in result.output.lower()


# ── Callback behaviour ───────────────────────────────────────────────────────

class TestHostCallback:
    def test_log_dir_set_for_subcommand(self):
        mock_logger = MagicMock()
        mock_host = _make_host_with_session([('', 0)])

        with (
            patch.object(host_module, 'logger', mock_logger),
            patch.object(host_module, 'get_host', return_value=mock_host),
        ):
            runner.invoke(host_app, ['router1', 'run', 'ls'])

        mock_logger.create_output_dir.assert_called_once_with('host', 'run')


# ── Host resolution ──────────────────────────────────────────────────────────

class TestResolveHost:
    def test_valid_host_returns_host(self):
        mock_host = _make_host()
        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = _resolve_host('router1')
        assert result is mock_host

    def test_invalid_host_exits(self):
        with (
            patch.object(host_module, 'get_host', side_effect=KeyError('nope')),
            patch.object(host_module, 'all_hosts', return_value=iter([_make_host()])),
        ):
            result = runner.invoke(host_app, ['nonexistent', 'run', 'ls'])

        assert result.exit_code == 1
        assert 'No host with ID' in result.output


# ── run command ───────────────────────────────────────────────────────────────

class TestHostRun:
    def test_run_success(self):
        mock_host = _make_host_with_session([('', 0), ('', 0)])

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['router1', 'run', 'ls', 'pwd'])

        assert result.exit_code == 0

    def test_run_failure_exits_nonzero(self):
        mock_host = _make_host_with_session([('command not found', 127)])

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['router1', 'run', 'bad_cmd'])

        assert result.exit_code == 1

    def test_run_closes_host_on_exception(self):
        mock_host = _make_host()
        mock_host.run = AsyncMock(side_effect=RuntimeError("boom"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['router1', 'run', 'ls'])

        assert result.exit_code != 0
        mock_host.close.assert_awaited_once()


# ── put command ───────────────────────────────────────────────────────────────

class TestHostPut:
    def test_put_success(self, tmp_path):
        src_file = tmp_path / "file.txt"
        src_file.write_text("hello")

        mock_host = _make_host()
        mock_host.put = AsyncMock(return_value=(Status.Success, "ok"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['router1', 'put', str(src_file), '/tmp/dest'])

        assert result.exit_code == 0
        assert 'Transfer complete' in result.output
        mock_host.put.assert_awaited_once()
        mock_host.close.assert_awaited_once()

    def test_put_failure(self, tmp_path):
        src_file = tmp_path / "file.txt"
        src_file.write_text("hello")

        mock_host = _make_host()
        mock_host.put = AsyncMock(return_value=(Status.Failed, "permission denied"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['router1', 'put', str(src_file), '/tmp/dest'])

        assert result.exit_code == 1
        assert 'Transfer failed' in result.output
        mock_host.close.assert_awaited_once()


# ── --term and --transfer options ────────────────────────────────────────────

class TestHostTermAndTransfer:
    def test_valid_term_calls_set_term_type(self):
        """Contract test: verify CLI dispatches to set_term_type.
        See test_valid_term_applies_to_host for end-to-end coverage."""
        mock_host = _make_host_with_session([('', 0)])

        with (
            patch.object(host_module, 'get_host', return_value=mock_host),
            patch.object(mock_host, 'set_term_type') as mock_set_term,
        ):
            result = runner.invoke(host_app, ['--term', 'telnet', 'router1', 'run', 'ls'])

        assert result.exit_code == 0
        mock_set_term.assert_called_once_with('telnet')

    def test_valid_transfer_calls_set_transfer_type(self):
        """Contract test: verify CLI dispatches to set_transfer_type.
        See test_valid_transfer_applies_to_host for end-to-end coverage."""
        mock_host = _make_host_with_session([('', 0)])

        with (
            patch.object(host_module, 'get_host', return_value=mock_host),
            patch.object(mock_host, 'set_transfer_type') as mock_set_transfer,
        ):
            result = runner.invoke(host_app, ['--transfer', 'ftp', 'router1', 'run', 'ls'])

        assert result.exit_code == 0
        mock_set_transfer.assert_called_once_with('ftp')

    def test_invalid_term_exits(self):
        mock_host = _make_host()
        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['--term', 'bogus', 'router1', 'run', 'ls'])

        assert result.exit_code != 0

    def test_invalid_transfer_exits(self):
        mock_host = _make_host()
        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['--transfer', 'bogus', 'router1', 'run', 'ls'])

        assert result.exit_code != 0

    def test_no_term_or_transfer_skips_setters(self):
        mock_host = _make_host_with_session([('', 0)])

        with (
            patch.object(host_module, 'get_host', return_value=mock_host),
            patch.object(mock_host, 'set_term_type') as mock_set_term,
            patch.object(mock_host, 'set_transfer_type') as mock_set_transfer,
        ):
            result = runner.invoke(host_app, ['router1', 'run', 'ls'])

        assert result.exit_code == 0
        mock_set_term.assert_not_called()
        mock_set_transfer.assert_not_called()

    def test_valid_term_applies_to_host(self):
        """set_term_type must actually run (not be patched out) to catch
        the _get_literal_values match-on-special-form bug."""
        mock_host = _make_host_with_session([('', 0)])

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['--term', 'telnet', 'router1', 'run', 'ls'])

        assert result.exit_code == 0, result.output
        assert mock_host.term == 'telnet'

    def test_valid_transfer_applies_to_host(self):
        """set_transfer_type must actually run to catch the same bug."""
        mock_host = _make_host_with_session([('', 0)])

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['--transfer', 'sftp', 'router1', 'run', 'ls'])

        assert result.exit_code == 0, result.output
        assert mock_host.transfer == 'sftp'

    def test_term_and_transfer_together(self):
        mock_host = _make_host_with_session([('', 0)])

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['--term', 'ssh', '--transfer', 'sftp', 'router1', 'run', 'ls'])

        assert result.exit_code == 0
        assert mock_host.term == 'ssh'
        assert mock_host.transfer == 'sftp'


# ── get command ───────────────────────────────────────────────────────────────

class TestHostGet:
    def test_get_success(self, tmp_path):
        mock_host = _make_host()
        mock_host.get = AsyncMock(return_value=(Status.Success, "ok"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['router1', 'get', '/remote/file.txt', str(tmp_path)])

        assert result.exit_code == 0
        assert 'Download complete' in result.output
        mock_host.get.assert_awaited_once()
        mock_host.close.assert_awaited_once()

    def test_get_failure(self, tmp_path):
        mock_host = _make_host()
        mock_host.get = AsyncMock(return_value=(Status.Failed, "not found"))
        mock_host.close = AsyncMock()

        with patch.object(host_module, 'get_host', return_value=mock_host):
            result = runner.invoke(host_app, ['router1', 'get', '/remote/file.txt', str(tmp_path)])

        assert result.exit_code == 1
        assert 'Transfer failed' in result.output
        mock_host.close.assert_awaited_once()


# ── host_id shell-completion ─────────────────────────────────────────────────

def _write_hosts_json(path: Path, hosts: list[dict]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    hosts_file = path / 'hosts.json'
    hosts_file.write_text(json.dumps(hosts))
    return hosts_file


def _fake_repo(*lab_paths: Path) -> SimpleNamespace:
    """Stand-in for :class:`Repo` that only exposes the attribute the
    completer actually reads (``labs``)."""
    return SimpleNamespace(labs=list(lab_paths))


class TestHostIdCompleter:
    """``_host_id_completer`` runs during tab completion, before
    ``applyRepoSettings()`` populates the ConfigModule.  It must therefore
    derive host IDs straight from the ``hosts.json`` files referenced by
    each repo's ``labs`` search paths."""

    def test_returns_all_host_ids(self, tmp_path):
        lab = tmp_path / 'labA'
        _write_hosts_json(lab, [
            {'ip': '1.1.1.1', 'ne': 'carrot', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']},
            {'ip': '1.1.1.2', 'ne': 'tomato', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']},
        ])
        # _host_id_completer lazy-imports getRepos from otto.configmodule.
        with patch('otto.configmodule.getRepos', return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete='')
        assert result == ['carrot_seed', 'tomato_seed']

    def test_filters_by_incomplete_prefix(self, tmp_path):
        lab = tmp_path / 'labA'
        _write_hosts_json(lab, [
            {'ip': '1.1.1.1', 'ne': 'carrot', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']},
            {'ip': '1.1.1.2', 'ne': 'tomato', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']},
        ])
        with patch('otto.configmodule.getRepos', return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete='tom')
        assert result == ['tomato_seed']

    def test_merges_ids_across_multiple_paths(self, tmp_path):
        lab1 = tmp_path / 'lab1'
        lab2 = tmp_path / 'lab2'
        _write_hosts_json(lab1, [
            {'ip': '1.1.1.1', 'ne': 'carrot', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']},
        ])
        _write_hosts_json(lab2, [
            {'ip': '2.2.2.2', 'ne': 'beet', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['roots']},
        ])
        with patch('otto.configmodule.getRepos', return_value=[_fake_repo(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete='')
        assert result == ['beet_seed', 'carrot_seed']

    def test_deduplicates_ids(self, tmp_path):
        """Same host id present in two hosts.json files must collapse to one."""
        lab1 = tmp_path / 'lab1'
        lab2 = tmp_path / 'lab2'
        dup = {'ip': '1.1.1.1', 'ne': 'carrot', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']}
        _write_hosts_json(lab1, [dup])
        _write_hosts_json(lab2, [dup])
        with patch('otto.configmodule.getRepos', return_value=[_fake_repo(lab1, lab2)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete='')
        assert result == ['carrot_seed']

    def test_skips_missing_path(self, tmp_path):
        """Non-existent search path must not raise; completer is best-effort."""
        with patch('otto.configmodule.getRepos', return_value=[_fake_repo(tmp_path / 'nope')]):
            result = _host_id_completer(ctx=MagicMock(), incomplete='')
        assert result == []

    def test_skips_malformed_json(self, tmp_path):
        lab = tmp_path / 'bad'
        lab.mkdir()
        (lab / 'hosts.json').write_text('{not json')
        with patch('otto.configmodule.getRepos', return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete='')
        assert result == []

    def test_skips_invalid_host_entries(self, tmp_path):
        """A host dict missing required fields must be skipped, not abort."""
        lab = tmp_path / 'labA'
        _write_hosts_json(lab, [
            {'ne': 'incomplete'},  # missing ip, creds — validate_host_dict rejects
            {'ip': '1.1.1.1', 'ne': 'carrot', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']},
        ])
        with patch('otto.configmodule.getRepos', return_value=[_fake_repo(lab)]):
            result = _host_id_completer(ctx=MagicMock(), incomplete='')
        assert result == ['carrot_seed']

    def test_prefers_cached_host_ids(self, tmp_path):
        """When the completion cache is populated (fast path), the completer
        must serve from it and not re-parse every ``hosts.json``.

        Uses a nonexistent search path to prove live parsing didn't run:
        without the cache, ``collect_host_ids`` would return ``[]`` and the
        assertion on ``router1``/``router2`` would fail.
        """
        fake_cache = {
            'instructions': [],
            'suites': [],
            'hosts': ['router1', 'router2', 'switch7'],
        }
        with (
            patch('otto.configmodule.getCompletionNames', return_value=fake_cache),
            patch('otto.configmodule.getRepos',
                  return_value=[_fake_repo(tmp_path / 'does-not-exist')]),
        ):
            result = _host_id_completer(ctx=MagicMock(), incomplete='r')
        assert result == ['router1', 'router2']

    def test_falls_through_on_cache_miss(self, tmp_path):
        """``getCompletionNames`` returns None off the fast path — completer
        must still find host IDs by scanning ``hosts.json`` live."""
        lab = tmp_path / 'labA'
        _write_hosts_json(lab, [
            {'ip': '1.1.1.1', 'ne': 'carrot', 'board': 'seed', 'creds': {'u': 'p'}, 'labs': ['veggies']},
        ])
        with (
            patch('otto.configmodule.getCompletionNames', return_value=None),
            patch('otto.configmodule.getRepos', return_value=[_fake_repo(lab)]),
        ):
            result = _host_id_completer(ctx=MagicMock(), incomplete='')
        assert result == ['carrot_seed']

    def test_argument_advertises_completer(self):
        """Regression guard: the ``host_id`` parameter must carry the
        completer so Click hands it to the shell during tab completion."""
        import inspect
        from typing import get_args
        sig = inspect.signature(host_module.main)
        metadata = get_args(sig.parameters['host_id'].annotation)
        argument = next(m for m in metadata if hasattr(m, 'autocompletion'))
        assert argument.autocompletion is _host_id_completer
