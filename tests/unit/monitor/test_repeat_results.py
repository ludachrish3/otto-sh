"""
Unit tests for RepeatRunner result storage.

Tests the new _repeat_results, _store_result(), get_results(),
and the on_result callback in start().
"""

import asyncio
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import RunResult
from otto.host.remoteHost import RemoteHost
from otto.utils import CommandStatus, Status


@pytest.fixture
def host():
    h = RemoteHost(ip='10.0.0.1', ne='box', creds={'user': 'pass'}, log=False)
    yield h


class TestRepeatResultsInit:

    def test_repeat_results_starts_empty(self, host):
        assert host._repeater._repeat_results == {}

    def test_get_results_missing_name_returns_empty(self, host):
        assert host.repeat_results('nonexistent') == []


class TestStoreRepeatResult:
    """Tests for the RepeatRunner._store_result() done-callback helper."""

    def _make_task(self, result=None, cancelled=False, exception=None):
        """Build a mock asyncio.Task with a controlled outcome."""
        task = MagicMock()
        task.cancelled.return_value = cancelled
        if exception:
            task.exception.return_value = exception
        else:
            task.exception.return_value = None
        if result is not None:
            task.result.return_value = result
        return task

    def test_stores_result_on_success(self, host):
        results = [
            CommandStatus(command='echo hi', output='hi', status=Status.Success, retcode=0),
        ]
        host._repeater._repeat_results['myjob'] = deque(maxlen=10)
        task = self._make_task(
            result=RunResult(status=Status.Success, statuses=results)
        )

        host._repeater._store_result('myjob', task, None)

        stored = host.repeat_results('myjob')
        assert len(stored) == 1
        ts, cmds = stored[0]
        assert isinstance(ts, datetime)
        assert cmds == results

    def test_ignores_cancelled_task(self, host):
        host._repeater._repeat_results['myjob'] = deque(maxlen=10)
        task = self._make_task(cancelled=True)

        host._repeater._store_result('myjob', task, None)

        assert host.repeat_results('myjob') == []

    def test_ignores_task_with_exception(self, host):
        host._repeater._repeat_results['myjob'] = deque(maxlen=10)
        task = self._make_task(exception=RuntimeError('boom'))

        host._repeater._store_result('myjob', task, None)

        assert host.repeat_results('myjob') == []

    def test_fires_on_result_callback(self, host):
        results = [
            CommandStatus(command='date', output='Mon Jan  1 00:00:00 UTC 2024',
                          status=Status.Success, retcode=0),
        ]
        host._repeater._repeat_results['myjob'] = deque(maxlen=10)
        task = self._make_task(
            result=RunResult(status=Status.Success, statuses=results)
        )

        received: list[tuple] = []
        def callback(name, ts, cmds):
            received.append((name, ts, cmds))

        host._repeater._store_result('myjob', task, callback)

        assert len(received) == 1
        name, ts, cmds = received[0]
        assert name == 'myjob'
        assert isinstance(ts, datetime)
        assert cmds == results

    def test_respects_maxlen(self, host):
        host._repeater._repeat_results['myjob'] = deque(maxlen=3)
        single = [CommandStatus(command='x', output='', status=Status.Success, retcode=0)]

        for _ in range(5):
            task = self._make_task(
                result=RunResult(status=Status.Success, statuses=single)
            )
            host._repeater._store_result('myjob', task, None)

        stored = host.repeat_results('myjob')
        assert len(stored) == 3  # capped by maxlen


class TestGetRepeatResults:

    def test_returns_snapshot_not_live_reference(self, host):
        host._repeater._repeat_results['myjob'] = deque(maxlen=10)
        single = [CommandStatus(command='x', output='', status=Status.Success, retcode=0)]
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None
        task.result.return_value = RunResult(status=Status.Success, statuses=single)

        host._repeater._store_result('myjob', task, None)
        snapshot = host.repeat_results('myjob')

        # Mutating the deque after getting snapshot should not affect snapshot
        host._repeater._repeat_results['myjob'].append((datetime.now(), single))
        assert len(snapshot) == 1
