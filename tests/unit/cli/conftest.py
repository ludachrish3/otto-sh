"""Shared fixtures for CLI unit tests.

Mock boundary rule: mock at I/O, not at business logic.

- **Mock**: network I/O (asyncssh, telnetlib3, aioftp), lab data lookups
  (``get_host``, ``all_hosts``), logger side-effects (``create_output_dir``),
  and ``asyncio.run`` for commands that start event loops.
- **Do NOT mock**: validation functions (``is_literal``), setter methods
  (``set_term_type``, ``set_transfer_type``), data transformation, or anything
  in ``utils.py``.
- **Contract tests** that verify the CLI called the right method may patch
  business logic, but pair them with an integration test that lets real
  code run.  Name pairs clearly (e.g. ``test_*_calls_set_term_type`` +
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
from otto.logger import getOttoLogger
from otto.utils import CommandStatus, Status


@pytest.fixture(autouse=True)
def no_logger_output_dir():
    """Prevent OttoLogger.create_output_dir from being called in CLI tests.

    The CLI commands call logger.create_output_dir() early, which requires
    logger.xdir to be set (done by initOttoLogger() in the main callback).
    Unit tests invoke subcommand apps directly, bypassing that callback, so
    we patch it out globally here rather than repeating the patch per test.
    """
    with patch('otto.cli.monitor.logger.create_output_dir'):
        yield


# ── Helpers for real filesystem fixtures ─────────────────────────────────────

HOSTS_DATA = [
    {"ip": "10.0.0.1", "ne": "host1", "labs": ["test_lab"],
     "creds": {"admin": "pass"}},
    {"ip": "10.0.0.2", "ne": "host2", "labs": ["test_lab", "lab2"],
     "creds": {"admin": "pass"}},
    {"ip": "10.0.0.3", "ne": "host3", "labs": ["lab2"],
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
        'labs = ["${sutDir}/../lab_data"]\n'
    )

    return sut_dir, lab_data_dir


@pytest.fixture
def real_main_mocks(tmp_path):
    """Fixture that lets business logic run for real, mocking only I/O.

    What runs for real:
      - ``initOttoLogger`` (level, handler setup)
      - ``getLab`` (reads hosts.json from tmp_path)
      - ``setConfigModule`` / ``getConfigModule``

    What is mocked (I/O boundaries only):
      - ``OttoLogger.removeOldLogs`` — filesystem listing + deletion
      - ``RichHandler`` — console I/O
      - ``getRepos`` — module-level singleton; returns a real ``Repo``
      - ``LocalHost.run`` — subprocess for git commands
    """
    sut_dir, lab_data_dir = _make_lab_fs(tmp_path)
    repo = Repo(sutDir=sut_dir)

    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith('OTTO_')}

    logger = getOttoLogger()
    original_level = logger.level
    original_handlers = list(logger.handlers)

    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch('otto.logger.logger.OttoLogger.removeOldLogs') as p_remove,
        patch('otto.logger.logger.RichHandler') as p_rich,
        patch('otto.cli.main.getRepos', return_value=[repo]),
        patch(
            'otto.host.localHost.LocalHost.run',
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
            'removeOldLogs': p_remove,
            'RichHandler': p_rich,
        }

    # Teardown: restore logger to pre-test state
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    for handler in original_handlers:
        logger.addHandler(handler)
    logger.setLevel(original_level)
