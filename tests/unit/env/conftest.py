"""The one fake for :mod:`otto.env.backends`' subprocess, shared by every env unit test."""

import subprocess
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from otto.env import backends


class _ExecSeam(list):
    """The recorded argvs, plus the outcome the next exec reports (a test sets these)."""

    returncode: int = 0
    stdout: str = ""
    raises: "BaseException | None" = None
    effect: "Callable[[list[str]], None] | None" = None
    """Run on each recorded argv before it returns, for a test that needs the
    exec's side effect (``uv venv`` creating the env directory) to be real."""


@pytest.fixture
def exec_seam(monkeypatch):
    """Record the installer argv at the seam the exec lives at, and seal every other.

    ``backends._run`` is left alone deliberately: the thing worth pinning is
    the argv that reaches ``subprocess.run``, so the fake sits there. Every
    other exec entry point reachable from that module is replaced by a raiser,
    so a probe that ever escapes the fake fails loudly on EVERY machine instead
    of quietly running a real installer on the one that has an index.

    THE WHOLE MODULE REFERENCE IS SWAPPED, not an attribute on it:
    ``backends.subprocess`` IS the stdlib module object, so setting ``run`` on
    it would replace the real ``subprocess.run`` process-wide for the duration
    of the test. Binding a stand-in namespace to the name ``backends`` looks up
    confines the fake to the module under test.
    """
    calls = _ExecSeam()

    def _record(argv, *args, **kwargs):
        calls.append(list(argv))
        if calls.raises is not None:
            raise calls.raises
        if calls.effect is not None:
            calls.effect(list(argv))
        return subprocess.CompletedProcess(list(argv), calls.returncode, calls.stdout, "")

    def _escaped(*args, **kwargs):
        raise AssertionError(f"a real subprocess escaped the fake: {args!r}")

    monkeypatch.setattr(
        backends,
        "subprocess",
        SimpleNamespace(
            run=_record,
            Popen=_escaped,
            CompletedProcess=subprocess.CompletedProcess,
        ),
    )
    return calls
