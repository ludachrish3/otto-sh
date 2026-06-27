"""Shared fixtures for CLI unit tests.

Mock boundary rule: mock at I/O, not at business logic.

- **Mock**: network I/O (asyncssh, telnetlib3, aioftp), lab data lookups
  (``get_host``, ``all_hosts``), logger side-effects (``create_output_dir``),
  and ``asyncio.run`` for commands that start event loops.
- **Do NOT mock**: validation functions (``is_literal``), the override-copy
  seam (``_apply_option_overrides``), data transformation, or anything
  in ``utils.py``.
- **Contract tests** that verify the CLI called the right method may patch
  business logic, but pair them with an integration test that lets real
  code run.  Name pairs clearly (e.g. ``test_*_applies_overrides`` +
  ``test_*_applies_to_host``).

Litmus test: "If the function I am patching had a bug, would my test
catch it?"  If no, move the mock boundary closer to I/O.
"""

import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from otto.configmodule.repo import Repo
from otto.host import RunResult
from otto.logger import get_otto_logger
from otto.utils import CommandStatus, Status


@pytest.fixture(autouse=True)
def no_logger_output_dir():
    """Prevent management.create_output_dir from being called in CLI unit tests.

    The CLI commands call ``get_context().output_dir = management.create_output_dir(...)``
    early, which requires (a) an active OttoContext and (b) management._state.xdir set
    by init_cli_logging(). Unit tests invoke subcommand apps directly, bypassing the
    main callback, so we patch out ``create_output_dir`` AND install a minimal stub
    context (if none is already active) so that ``get_context()`` doesn't raise.

    Tests that use ``real_main_mocks`` install a real context via ``set_context``
    beforehand; the stub is skipped for them.

    We also pre-set ``propagate = False`` on the ``'otto'`` logger to match what
    ``management.init_cli_logging`` would do in a real invocation. Without this,
    tests that mock ``init_cli_logging`` leave ``propagate = True`` (restored by
    ``_reset_otto_logger_retention`` / ``management.reset()`` between tests),
    causing log records to reach pytest's live-log handler, which temporarily
    suspends stdout capture inside the CliRunner's isolation context.  Suspending
    capture drops the CliRunner's ``_NamedTextIOWrapper`` reference, whose
    ``TextIOWrapper.__del__`` closes the underlying ``BytesIOCopy`` — making the
    subsequent ``outstreams[0].getvalue()`` fail with ``ValueError``.
    """
    from logging import getLogger

    from otto.configmodule.lab import Lab
    from otto.context import OttoContext, reset_context, set_context, try_get_context
    token = None
    if try_get_context() is None:
        token = set_context(OttoContext(lab=Lab(name='_test_stub')))
    # Mirror the propagate=False that init_cli_logging sets so that mocked-
    # init_cli_logging tests don't accidentally emit live logs into pytest's
    # capture machinery while a CliRunner invocation is in progress.
    getLogger('otto').propagate = False
    with patch('otto.logger.management.create_output_dir'):
        yield
    if token is not None:
        reset_context(token)


# ── Helpers for real filesystem fixtures ─────────────────────────────────────

HOSTS_DATA = [
    {"ip": "10.0.0.1", "element": "host1", "labs": ["test_lab"],
     "creds": {"admin": "pass"}},
    {"ip": "10.0.0.2", "element": "host2", "labs": ["test_lab", "lab2"],
     "creds": {"admin": "pass"}},
    {"ip": "10.0.0.3", "element": "host3", "labs": ["lab2"],
     "creds": {"admin": "pass"}},
]


def _make_lab_fs(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal SUT repo and lab data directory in *tmp_path*.

    Returns ``(sut_dir, lab_data_dir)`` so callers can reference both.
    """
    lab_data_dir = tmp_path / 'lab_data'
    lab_data_dir.mkdir()
    (lab_data_dir / 'hosts.json').write_text(json.dumps(HOSTS_DATA))

    sut_dir = tmp_path / 'sut'
    sut_dir.mkdir()
    otto_dir = sut_dir / '.otto'
    otto_dir.mkdir()
    (otto_dir / 'settings.toml').write_text(
        'name = "test_repo"\n'
        'version = "1.0.0"\n'
        'labs = ["${sut_dir}/../lab_data"]\n'
    )

    return sut_dir, lab_data_dir


@pytest.fixture
def real_main_mocks(tmp_path):
    """Fixture that lets business logic run for real, mocking only I/O.

    What runs for real:
      - ``management.init_cli_logging`` (level, handler setup)
      - ``load_lab`` (reads hosts.json from tmp_path)
      - OttoContext installation via ``set_context``

    What is mocked (I/O boundaries only):
      - ``management.remove_old_logs`` — filesystem listing + deletion
      - ``RichHandler`` — console I/O
      - ``get_repos`` — module-level singleton; returns a real ``Repo``
      - ``LocalHost.run`` — subprocess for git commands
    """
    sut_dir, lab_data_dir = _make_lab_fs(tmp_path)
    repo = Repo(sut_dir=sut_dir)

    # Strip the user's OTTO_* env so test outcomes don't drift with the shell;
    # point OTTO_XDIR at tmp_path so init_cli_logging never writes to the project
    # root (--xdir is optional and defaults to CWD, which we don't want here).
    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith('OTTO_')}
    clean_env['OTTO_XDIR'] = str(tmp_path)

    logger = get_otto_logger()
    original_level = logger.level
    original_handlers = list(logger.handlers)

    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch('otto.logger.management.remove_old_logs') as p_remove,
        patch('otto.logger.management.RichHandler') as p_rich,
        patch('otto.cli.main.get_repos', return_value=[repo]),
        patch(
            'otto.host.local_host.LocalHost.run',
            new_callable=AsyncMock,
            return_value=RunResult(
                status=Status.Success,
                statuses=[CommandStatus(
                    command='git log',
                    output='abc123',
                    status=Status.Success,
                    retcode=0,
                )],
            ),
        ),
    ):
        yield {
            'tmp_path': tmp_path,
            'sut_dir': sut_dir,
            'lab_data_dir': lab_data_dir,
            'repo': repo,
            'remove_old_logs': p_remove,
            'RichHandler': p_rich,
        }

    # Teardown: restore logger to pre-test state
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    for handler in original_handlers:
        logger.addHandler(handler)
    logger.setLevel(original_level)
