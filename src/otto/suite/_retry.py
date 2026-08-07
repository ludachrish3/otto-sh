"""The shared ``@pytest.mark.retry(n)`` implementation — otto's only retry.

Two registration sites delegate here, both as hookwrappers around
``pytest_runtest_call``: :class:`otto.suite.plugin.OttoPlugin` (active under
``otto test``) and otto's own ``tests/conftest.py`` (active under bare
``pytest``). No current path registers both on one item — ``otto test``
builds a fresh plugin manager and its confcutdir keeps otto's root conftest
out even for the in-tree example repos — but ``pytest_runtest_call`` is not
``firstresult``, so if both ever were live the cost would be silently
multiplied attempts. Defensively, the outermost wrapper claims the item and
any inner one passes through. The previous generation had one copy per site
with divergent bugs: the plugin copy was a plain hookimpl, so pytest's
default runner executed the body once more after a successful retry, and
that extra run decided the outcome.

Semantics, each pinned by ``tests/unit/suite/test_retry_semantics.py``:

- **Every attempt is bounded, on the main thread.** The retry loop runs in
  the post-``yield`` half of the hookwrapper, and whether pytest-timeout's
  alarm is still live there depends on which timer it armed: the
  protocol-scoped default (``func_only`` off) always encloses this hook and
  is still ticking; the call-scoped timer (``func_only = true``, the dev
  ini) shares this hook, where registration order decides — the dev conftest
  registers after the entry-point plugins and wraps outside them (alarm
  already cancelled here; retries ran *unbounded* in the previous
  generation, the verified defect), while ``OttoPlugin`` registers before
  them via ``pytest.main(plugins=...)`` and wraps inside (alarm still
  live). Each retried attempt therefore re-arms its own alarm from the
  item's effective ``timeout`` (positional or keyword marker form, else
  ini), saving any live outer timer first and restoring it after — the
  outer clock pauses for the attempt; the conservative direction. A
  pytest-timeout expiry on an attempt counts as a failed attempt and is
  retried (a hang can be the flake), so a retried item's total budget
  approaches n-times the timeout. Off the main thread (``run_suite`` as a library
  call from a worker thread — a supported mode) SIGALRM is unavailable;
  attempts still retry, unbounded, and never convert an arming error into a
  fake test failure.
- **Reruns leave evidence.** ``item.user_properties`` gains
  ``("retry_attempts", k)`` — serialized through xdist and rendered into
  JUnit XML — every failed attempt logs at WARNING, and single-process runs
  print a terminal summary via :func:`report_retries`. A pass-after-retries
  (or a skip-after-retries) is a recorded event, never an erased one. (The
  JUnit property is the xdist-safe channel; the terminal summary reflects
  only the controller process.)
- **``pytest.fail()`` retries like any failure.** ``Failed`` derives from
  ``BaseException``, which the previous implementation's ``except
  Exception`` let escape mid-loop. Outcome decisions stop the loop instead:
  ``Skipped`` and ``XFailed`` force that outcome, ``Exit`` and
  ``KeyboardInterrupt`` stay session-fatal.
- **The test body alone is re-run.** Fixtures keep the failed attempt's
  state (``item.runtest()`` re-enters only the call phase), so retry is
  sound only for tests whose body is idempotent — the user-facing guidance
  lives on ``tests/repo1``'s retry example.
"""

import logging
import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager, suppress
from typing import Any

import pytest
from _pytest.outcomes import Exit, Failed, Skipped, XFailed

from otto.errors import OttoError

_logger = logging.getLogger(__name__)

#: Claimed by the outermost wrapper for the duration of ONE hook invocation;
#: an inner wrapper sees the claim and passes through.
_RETRY_OWNED = pytest.StashKey[bool]()

#: Per-session record of retried items, rendered by :func:`report_retries`.
_RETRY_EVENTS = pytest.StashKey[list[str]]()


class RetryAttemptTimeoutError(OttoError, TimeoutError):
    """A retried attempt exceeded the item's per-test timeout budget."""


class _AttemptTimeout(BaseException):
    """In-flight alarm signal for a retried attempt's timeout.

    ``BaseException``-derived so a test body's ``except Exception:`` (the
    standard flaky-network shape retry exists for) cannot swallow its own
    timeout; pytest-timeout's ``Failed`` earns its unswallowability the
    same way. Converted to the public :class:`RetryAttemptTimeoutError` in
    the retry loop.
    """


def _effective_timeout(item: pytest.Item) -> float | None:
    """Resolve the item's per-test timeout: closest ``timeout`` marker, else ini.

    pytest-timeout accepts both marker forms — ``timeout(5)`` and
    ``timeout(timeout=5)`` (the keyword shape is how ``func_only``/``method``
    are combined with a budget) — so both are honored here; reading only
    ``args`` silently un-bounded every keyword-marked retry (interim review,
    finding 1).
    """
    marker = item.get_closest_marker("timeout")
    candidate: Any = None
    if marker is not None:
        candidate = marker.args[0] if marker.args else marker.kwargs.get("timeout")
    if candidate is None:
        try:
            candidate = item.config.getini("timeout")
        except (KeyError, ValueError):
            return None
    try:
        timeout = float(candidate) if candidate is not None else None
    except (TypeError, ValueError):
        return None
    return timeout if timeout and timeout > 0 else None


@contextmanager
def _attempt_alarm(item: pytest.Item) -> Generator[None, None, None]:
    """Arm SIGALRM for one retried attempt, preserving any live outer timer.

    pytest-timeout's protocol-scoped timer (its default) is still ticking
    here, and under ``otto test`` even its call-scoped timer is (see the
    module docstring for why registration order decides) — so the outer
    timer is quiesced FIRST, then the handler swapped, then our budget
    armed. Restore runs in the opposite order: disarm ours, restore the
    handler, then re-arm the outer remainder — a pending alarm must never
    reach a half-restored handler (``SIG_DFL`` on SIGALRM kills the
    process). Off the main thread ``signal.signal`` raises ``ValueError``,
    so arming is skipped entirely — a retried attempt must never fail on
    the harness's own arming call (interim review, finding 2).
    """
    timeout = _effective_timeout(item)
    if (
        timeout is None
        or not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def _on_alarm(signum: int, frame: object) -> None:  # noqa: ARG001 — signal handler signature
        raise _AttemptTimeout(f"retried attempt exceeded the {timeout}s timeout")

    outer_remaining, outer_interval = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, 0)
    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    # A budget the itimer cannot represent (timeout=1e300, the plausible
    # "disable it" idiom) must not become the attempt's failure — run the
    # attempt unarmed; the finally below still restores everything.
    with suppress(OverflowError, OSError, ValueError):
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if outer_remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, outer_remaining, outer_interval)


def retry_hookwrapper(item: pytest.Item) -> Generator[None, Any, None]:
    """Post-yield body for a ``pytest_runtest_call`` hookwrapper.

    Both registration sites use exactly::

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(item):
            yield from retry_hookwrapper(item)

    The last-registered (outermost) wrapper claims the item pre-``yield``;
    the earlier-registered inner one sees the claim and passes through, so
    a double registration cannot multiply attempts. The claim is released
    on the way out because ownership is per *invocation*, not per item —
    ``OttoPlugin``'s stability loop re-enters this hook once per iteration,
    and every iteration must retry.
    """
    if item.stash.get(_RETRY_OWNED, False):
        yield
        return
    item.stash[_RETRY_OWNED] = True
    try:
        outcome = yield
        marker = item.get_closest_marker("retry")
        if marker is None or outcome.excinfo is None:
            return
        # Both marker forms, symmetric with _effective_timeout: retry(3)
        # and retry(n=3). An unparseable n must not replace the real test
        # failure with the wrapper's own ValueError.
        try:
            n = int(marker.args[0]) if marker.args else int(marker.kwargs.get("n", 1))
        except (TypeError, ValueError):
            _logger.warning(f"retry: {item.nodeid} has an unusable retry marker; not retrying")
            return
        if n <= 1:
            return  # a degenerate marker is not a retry; leave the outcome alone
        first_exc = outcome.excinfo[1]
        if isinstance(first_exc, (Skipped, XFailed, Exit)) or not isinstance(
            first_exc, (Exception, Failed)
        ):
            # Outcome decisions and session-fatal signals are never retried.
            # Exit derives from Exception, so it needs naming explicitly;
            # KeyboardInterrupt fails the isinstance and falls out naturally.
            return
        _logger.warning(f"retry: {item.nodeid} attempt 1/{n} failed: {first_exc}")
        extra_attempts = 0
        for attempt in range(1, n):
            extra_attempts = attempt
            try:
                with _attempt_alarm(item):
                    item.runtest()
            except _AttemptTimeout as alarm:
                timeout_exc = RetryAttemptTimeoutError(str(alarm))
                _logger.warning(
                    f"retry: {item.nodeid} attempt {attempt + 1}/{n} failed: {timeout_exc}"
                )
                outcome.force_exception(timeout_exc)
            except (Skipped, XFailed, Exit) as decision_exc:
                # Skip/xfail force that outcome; Exit stays session-fatal.
                outcome.force_exception(decision_exc)
                break
            except (Exception, Failed) as retry_exc:  # noqa: BLE001 — a retry loop must absorb whatever the test body raises
                _logger.warning(
                    f"retry: {item.nodeid} attempt {attempt + 1}/{n} failed: {retry_exc}"
                )
                outcome.force_exception(retry_exc)
            else:
                _logger.warning(
                    f"retry: {item.nodeid} passed on attempt {attempt + 1}/{n} — "
                    f"{attempt} failed attempt(s) recorded"
                )
                outcome.force_result(None)
                break
        if extra_attempts:
            item.user_properties.append(("retry_attempts", extra_attempts))
            events = item.config.stash.setdefault(_RETRY_EVENTS, [])
            events.append(f"{item.nodeid}: {extra_attempts + 1} attempts")
    finally:
        item.stash[_RETRY_OWNED] = False


def report_retries(terminalreporter: Any) -> None:
    """Terminal-summary companion: name every retried test of this process.

    Under xdist this reflects only items the controller ran (workers stash
    into their own config); the JUnit ``retry_attempts`` property is the
    aggregation-safe record.
    """
    events = terminalreporter.config.stash.get(_RETRY_EVENTS, [])
    if events:
        terminalreporter.write_sep("=", f"retried tests ({len(events)})")
        for line in events:
            terminalreporter.write_line(line)
