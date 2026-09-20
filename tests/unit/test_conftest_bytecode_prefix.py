"""The root conftest's bytecode-cache prefix must resolve on any machine.

``tests/conftest.py`` computes ``PYTHONPYCACHEPREFIX`` at MODULE SCOPE, before
a single fixture or hook runs, so an exception there is not a test failure —
it aborts the conftest import and takes the whole session with it, on every
tree, with a traceback that names ``expanduser`` rather than anything a reader
would connect to bytecode caching.

``Path.expanduser`` raises ``RuntimeError`` when ``$HOME`` is unset AND the
running uid has no ``pwd`` entry: a scratch container, a ``nobody``-style uid
in a CI image, a ``sudo -u`` with a pruned passwd file. That is not
reproducible on this dev VM, so the condition is INJECTED here rather than
waited for — a guard whose red depends on how the runner happens to be
configured is not a guard.

The session guard those two lines exist for — ``pytest_sessionfinish``, which
fails the run when a ``__pycache__`` directory appears under ``src/otto``
during it — is pinned here too, and for the same reason: its red depends on a
test process losing the prefix, which is exactly what the rest of this branch
makes impossible. So the escape is injected (a patched ``_SRC_OTTO`` tree, an
empty snapshot, a directory created between them) rather than waited for, with
the pre-snapshotted control beside it.
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

import tests.conftest as root_conftest


def test_the_bytecode_prefix_survives_an_unresolvable_home(monkeypatch):
    """With no HOME and no pwd entry, the prefix lands under the temp dir."""
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    def _no_home(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _no_home)

    resolved = Path(root_conftest._resolve_pycache_prefix())

    assert resolved == Path(tempfile.gettempdir()) / "otto" / "pytest-pycache", (
        f"an unresolvable home must not abort conftest import; got {resolved}"
    )


def test_an_inherited_prefix_is_honoured_before_any_home_lookup(monkeypatch):
    """The inherited value wins — and wins WITHOUT consulting the home at all.

    The positive control for the test above: without it, "falls back to the
    temp dir" is also satisfied by a resolver that ignores its input, and a CI
    job redirecting the cache would silently lose the redirect.
    """
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/somewhere/a-job-chose")

    def _never(self):
        raise AssertionError("an inherited prefix must short-circuit the home lookup")

    monkeypatch.setattr(Path, "expanduser", _never)

    assert root_conftest._resolve_pycache_prefix() == "/somewhere/a-job-chose"


class _RecordingReporter:
    """The terminal reporter's two write calls, captured as text."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_sep(self, sep, title, **kwargs):  # type: ignore[no-untyped-def]
        self.lines.append(title)

    def write_line(self, line, **kwargs):  # type: ignore[no-untyped-def]
        self.lines.append(line)


def _session_with(reporter):  # type: ignore[no-untyped-def]
    """The smallest object ``pytest_sessionfinish`` actually touches."""
    return SimpleNamespace(
        exitstatus=0,
        config=SimpleNamespace(
            pluginmanager=SimpleNamespace(getplugin=lambda name: reporter),
        ),
    )


def _src_otto_tree(tmp_path: Path) -> Path:
    """A stand-in ``src/otto`` with one package in it, no bytecode yet."""
    package = tmp_path / "src" / "otto" / "examples"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    return tmp_path / "src" / "otto"


def test_a_pycache_dir_created_during_the_session_fails_it(monkeypatch, tmp_path):
    """A NEW ``__pycache__`` under src/otto flips exit 0 to 1 and is named.

    The escape this guard exists to catch is a subprocess spawned with an env
    that dropped ``PYTHONPYCACHEPREFIX``. Injected here instead: the directory
    appears under a patched ``_SRC_OTTO`` after an empty snapshot, which is the
    same state the walk sees.
    """
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    src_otto = _src_otto_tree(tmp_path)
    monkeypatch.setattr(root_conftest, "_SRC_OTTO", src_otto)
    monkeypatch.setattr(root_conftest, "_PYCACHE_AT_SESSION_START", frozenset())
    escaped = src_otto / "examples" / "__pycache__"
    escaped.mkdir()

    reporter = _RecordingReporter()
    session = _session_with(reporter)
    root_conftest.pytest_sessionfinish(session, 0)

    assert session.exitstatus == 1, (
        "bytecode written into the editable source tree must red the session; "
        f"exitstatus stayed {session.exitstatus}"
    )
    report = "\n".join(reporter.lines)
    assert str(escaped) in report, (
        f"the report must NAME the directory that appeared; got:\n{report}"
    )


def test_a_pycache_dir_present_before_the_session_does_not_fail_it(monkeypatch, tmp_path):
    """The control: a directory already in the snapshot leaves exit status alone.

    Without this, "reds on a ``__pycache__`` under src/otto" is also satisfied
    by a guard that ignores its snapshot — and every developer tree carrying
    dirs from a plain ``python -c 'import otto'`` would red on its first run.
    """
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    src_otto = _src_otto_tree(tmp_path)
    pre_existing = src_otto / "examples" / "__pycache__"
    pre_existing.mkdir()
    monkeypatch.setattr(root_conftest, "_SRC_OTTO", src_otto)
    monkeypatch.setattr(root_conftest, "_PYCACHE_AT_SESSION_START", frozenset({pre_existing}))

    reporter = _RecordingReporter()
    session = _session_with(reporter)
    root_conftest.pytest_sessionfinish(session, 0)

    assert session.exitstatus == 0, "a pre-existing __pycache__ must not red the session"
    assert reporter.lines == [], f"nothing should be reported; got {reporter.lines}"
