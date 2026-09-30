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

import dataclasses
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.repo import Repo
from otto.result import CommandResult, Results
from otto.utils import Status
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sut_repos import (  # noqa: F401 — fixtures, shared with tests/unit/suite
    _generated_modules_evicted,
    sut_repo,
    two_sut_repos,
)
from tests._fixtures.sutrepo import make_sut_repo


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

    We also strip otto-MARKED handlers off the ROOT logger for the duration.
    Since the root-capture cutover (spec 2026-08-30) otto installs its console
    handler and QueueHandler on root, not on the ``'otto'`` logger, so the old
    ``otto.propagate = False`` posture no longer describes anything: a CLI unit
    test that really runs ``init_cli_logging`` would otherwise leave otto's Rich
    console handler on root, writing into whatever stream the NEXT CliRunner
    invoke is isolating. Foreign root handlers (pytest's own capture) are left
    alone — this is the same ownership rule production uses.

    Note (issue #110, unchanged): with ``log_cli = true`` a record reaching
    pytest's live-log handler mid-invoke temporarily suspends stdout capture
    inside the CliRunner's isolation context, dropping the runner's
    ``_NamedTextIOWrapper`` whose ``TextIOWrapper.__del__`` closes the
    underlying ``BytesIOCopy`` — the subsequent ``outstreams[0].getvalue()``
    then raises ``ValueError``. That hazard is held off by the ROOT conftest's
    ``_clirunner_live_log_capture_guard``, not by this fixture.
    """
    from logging import getLogger

    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context, try_get_context
    from otto.logger.management import OTTO_HANDLER_ATTR

    token = None
    if try_get_context() is None:
        token = set_context(OttoContext(lab=Lab(name="_test_stub")))
    root = getLogger()
    saved_root_handlers = list(root.handlers)
    for handler in saved_root_handlers:
        if getattr(handler, OTTO_HANDLER_ATTR, False):
            root.removeHandler(handler)
    try:
        # The Mock is yielded so a test that needs create_output_dir to DO
        # something (record its calls, return a path) sets `side_effect` /
        # `return_value` on THIS object rather than re-patching the attribute
        # with `monkeypatch.setattr`. The two mechanisms unwind in fixture
        # order, and the shared `monkeypatch` fixture's position is set by
        # whichever fixture asks for it first — so a monkeypatch over a
        # patched attribute restores the Mock AFTER this patch has already
        # put the real function back, and the Mock outlives the test (seen
        # 2026-09-11 in the logger and dry-run tests on three gate runs).
        # Pinned by tests/unit/test_autouse_fixture_ordering.py.
        with patch("otto.logger.management.create_output_dir") as mock_create:
            yield mock_create
    finally:
        # Put back exactly what was there (membership AND order), which also
        # drops any handler the test itself installed on root.
        root.handlers[:] = list(saved_root_handlers)
        if token is not None:
            reset_context(token)


@pytest.fixture(autouse=True)
def _resync_cli_submodule_identity():
    """Keep ``otto.cli.<sub>`` (attribute) identical to ``sys.modules["otto.cli.<sub>"]``.

    Several tests here ``monkeypatch.delitem`` an ``otto.cli.*`` submodule from
    ``sys.modules`` to exercise lazy loading, then trigger a re-import (e.g.
    ``_OttoGroup.get_command(ctx, "run")``). The re-import builds a *new* module
    object and repoints the parent-package attribute (``otto.cli.run``) at it,
    but ``monkeypatch`` restores only the ``sys.modules`` entry on teardown — not
    the attribute. That leaves two live ``otto.cli.run`` objects. ``mock.patch``
    resolves ``"otto.cli.run.X"`` by getattr traversal (the attribute), while
    code that does ``from ..cli.run import X`` reads ``sys.modules`` — so under
    the nightly ``--repeat-scope=session`` repeat a later test's patch landed on
    one object while the code under test read the other, silently defeating the
    patch (it desynced ``test_listing``'s ``INSTRUCTIONS`` patch → issue #108).
    A single CI pass never re-runs the victim, so it only surfaced under repeat.

    Resyncing each parent-package submodule attribute to whatever ``sys.modules``
    holds (the entry ``monkeypatch`` restores) after every test keeps the two in
    lockstep, so ``mock.patch`` and ``from``-imports always see one object.
    """
    import sys

    import otto.cli as cli_pkg

    yield

    for name in list(vars(cli_pkg)):
        cached = sys.modules.get(f"otto.cli.{name}")
        if cached is not None and getattr(cli_pkg, name, None) is not cached:
            setattr(cli_pkg, name, cached)


# ── Helpers for real filesystem fixtures ─────────────────────────────────────

HOSTS_DATA = [
    {
        "ip": "10.0.0.1",
        "element": "host1",
        "labs": ["test_lab"],
        "creds": [{"login": "admin", "password": "pass"}],
    },
    {
        "ip": "10.0.0.2",
        "element": "host2",
        "labs": ["test_lab", "lab2"],
        "creds": [{"login": "admin", "password": "pass"}],
    },
    {
        "ip": "10.0.0.3",
        "element": "host3",
        "labs": ["lab2"],
        "creds": [{"login": "admin", "password": "pass"}],
    },
]


def _make_lab_fs(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal SUT repo and lab data directory in *tmp_path*.

    Returns ``(sut_dir, lab_data_dir)`` so callers can reference both.
    """
    lab_data_dir = tmp_path / "lab_data"
    lab_data_dir.mkdir()
    write_lab_json(lab_data_dir / "lab.json", HOSTS_DATA)

    sut_dir = make_sut_repo(
        tmp_path / "sut",
        name="test_repo",
        extra='[[lab.sources]]\nbackend = "json"\npaths = ["../lab_data"]',
    )

    return sut_dir, lab_data_dir


@pytest.fixture
def real_main_mocks(tmp_path):
    """Fixture that lets business logic run for real, mocking only I/O.

    What runs for real:
      - ``management.init_cli_logging`` (level, handler setup)
      - ``load_lab`` (reads lab.json from tmp_path)
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
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    clean_env["OTTO_XDIR"] = str(tmp_path)

    # Snapshot ROOT, not the ``'otto'`` logger: since the root-capture cutover
    # the real ``init_cli_logging`` this fixture lets run puts otto's console
    # handler on root and sets ROOT's level to the verbose floor, and touches
    # the ``'otto'`` logger nowhere at all. Restoring ``'otto'`` here would put
    # back a snapshot nothing ever dirtied while leaving the real spill in place.
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    original_root_handlers = list(root_logger.handlers)

    from otto import bootstrap as bs

    bs._reset()
    # Lab load + session setup are lazy (Task 7): otto.cli.invoke imports
    # get_repos from otto.config at call time, so patch the source.
    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch("otto.logger.management.remove_old_logs") as p_remove,
        # A REAL (inert) handler, not a bare MagicMock instance. Since the
        # root-capture cutover otto's console handler goes on the ROOT logger,
        # where the stdlib compares ``record.levelno >= handler.level`` for
        # EVERY record in the process; a MagicMock's ``.level`` is a Mock and
        # that comparison raises TypeError inside whatever code happened to
        # log — e.g. asyncio's own DEBUG line emitted from inside
        # ``new_event_loop()``, which leaks the loop it is halfway through
        # building. The mock still records the constructor call, so the
        # RichHandler call-args assertions are unaffected. ``*args`` too: the
        # only call site is all-keyword today, and a future positional would
        # otherwise raise TypeError from inside mock, far from its cause —
        # exactly the failure shape this double exists to eliminate.
        patch(
            "otto.logger.management.RichHandler",
            side_effect=lambda *args, **kwargs: logging.NullHandler(),
        ) as p_rich,
        patch("otto.config.get_repos", return_value=[repo]),
        patch(
            "otto.host.local_host.LocalHost.run",
            new_callable=AsyncMock,
            return_value=Results.collect(
                [
                    CommandResult(
                        Status.Success,
                        value="abc123",
                        command="git log",
                        retcode=0,
                    )
                ]
            ),
        ),
    ):
        yield {
            "tmp_path": tmp_path,
            "sut_dir": sut_dir,
            "lab_data_dir": lab_data_dir,
            "repo": repo,
            "remove_old_logs": p_remove,
            "RichHandler": p_rich,
        }

    # Teardown: restore the root logger to its pre-test state (membership AND
    # order, then level). The per-logger levels the real ``init_cli_logging``
    # pins (the spec §4 noise floor: asyncssh/asyncio/aiosqlite, plus anything
    # a test's repo table names) are NOT handed back here: that state is
    # module-global on ``management._state.floored_loggers``, so the guard for
    # it lives with the state, in ``tests/conftest.py``'s autouse
    # ``_restore_otto_logger_state`` — which covers every venue, not just this
    # fixture's.
    bs._reset()
    root_logger.handlers[:] = list(original_root_handlers)
    root_logger.setLevel(original_root_level)


@pytest.fixture
def run_cli():
    """Invoke ``otto run ...`` through the production dispatch seam, lab-free.

    Takes the whole argv as a user types it after ``otto`` (``["run", ...]``)
    and drives ``run_app`` through :class:`~tests._fixtures.dispatch.DispatchRunner`
    with the ``run`` lane's async-leaf rule. A bare context is installed for
    the invocation, standing in for the one the lab slice installs on the
    real path, so a leaf can inject ``OttoContext`` and bind verb options.

    *dry_run* installs that same context with ``dry_run=True`` — the run
    lane's ``lab_free`` spec never seeds ``ctx.meta['_otto_root_options']``,
    so :func:`~otto.cli.invoke.dry_run_requested` falls back to reading it
    straight off this context, exactly as it would off a library caller's.
    """
    from otto.cli.run import run_app
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context
    from tests._fixtures.dispatch import DispatchRunner

    runner = DispatchRunner()

    def _invoke(args: list[str], *, dry_run: bool = False):
        verb, *rest = args
        assert verb == "run", f"run_cli drives `otto run` only, not {verb!r}"
        token = set_context(OttoContext(lab=Lab(name="run-cli"), dry_run=dry_run))
        try:
            return runner.invoke(run_app, rest, async_leaves=True)
        finally:
            reset_context(token)

    return _invoke


@pytest.fixture
def otto_test_cli(tmp_path):
    """Invoke ``otto test ...`` through the production dispatch seam, lab-free.

    Takes the argv as a user types it after ``otto`` (``["test", ...]``, or
    ``["-n", "test", ...]`` for a dry run) and drives ``otto.cli.test``'s
    ``test_app`` through :class:`~tests._fixtures.dispatch.DispatchRunner`,
    under a bare ``otto`` root group as on the real CLI.
    The app is read off the module at call time, so it carries whatever the
    test registered for the ``test`` verb.

    *lab* names the bare context installed for the invocation, standing in
    for the one the lab slice installs on the real path (its ``output_dir``
    is a fresh directory under ``tmp_path``). ``lab=None`` installs no
    context at all: the command must not need one. A leading ``-n`` installs
    the context with ``dry_run=True``, which
    :func:`~otto.cli.invoke.dry_run_requested` reads when no root options
    were parsed.
    """
    from otto.cli import test as cli_test
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context
    from tests._fixtures.dispatch import DispatchRunner

    runner = DispatchRunner()

    def _invoke(args: list[str], *, lab: "str | None" = "unix"):
        args = list(args)
        dry_run = bool(args) and args[0] in ("-n", "--dry-run")
        if dry_run:
            args.pop(0)
        assert args[:1] == ["test"], f"otto_test_cli drives `otto test` only, not {args!r}"
        token = None
        if lab is not None:
            out = tmp_path / "otto-out"
            out.mkdir(exist_ok=True)
            token = set_context(OttoContext(lab=Lab(name=lab), output_dir=out, dry_run=dry_run))
        try:
            return runner.invoke(cli_test.test_app, args, spec_name="test", under_root=True)
        finally:
            if token is not None:
                reset_context(token)

    return _invoke


# ── otto test's run-flag fixtures ────────────────────────────────────────────
# Shared by tests/unit/cli/test_test.py and tests/unit/cli/test_test_differential.py:
# both exercise `otto test`'s run flags against the SAME faked `run_tests`, so the
# fixture and its helpers live here rather than being duplicated or imported
# cross-test-module.


def _flat(output: str) -> str:
    """Collapse a CLI result's output to single-spaced text, panel borders stripped.

    Rich's error panel wraps a long message across lines at its fixed width,
    bordering each with ``│`` — a literal ``in output`` substring check can
    land across that wrap (or a border character) and false-negative on a
    message that is otherwise present. Where the panel wraps depends on the
    absolute path length (``tmp_path``/``--basetemp``), so this normalization
    is not optional cosmetics: without it, a substring check that passes here
    can fail under a longer basetemp.
    """
    return " ".join(output.replace("│", " ").split())


def _lib_ok_result():
    """A zero-exit ``SuiteRunResult`` for a faked ``otto.suite.run.run_tests``."""
    from otto.suite.run import SuiteRunResult

    return SuiteRunResult(
        exit_code=0,
        junit_paths=[],
        stability_report=None,
        stability_unstable=False,
        output_dir=Path(),
    )


def _repo_with_tickets_configured():
    """A repo whose settings satisfy the --cov-tickets-json preflight gate."""
    repo = MagicMock()
    repo.settings = {
        "coverage": {
            "tiers": {"system": {"kind": "e2e", "precedence": 1}},
            "tickets": {"pattern": r"[A-Z]{2,10}-[0-9]+"},
        }
    }
    return repo


@pytest.fixture
def capture_cov(otto_test_cli, monkeypatch):
    """Invoke ``otto test <cli_args> test_x`` with ``run_tests`` faked.

    Returns ``(exit_code, run_options, output)``; ``run_options`` is the
    ``RunOptions`` the command handed ``run_tests`` as a dict, or ``{}`` when
    the command aborted before the run (e.g. during option validation).
    """

    def _capture(cli_args: list[str]) -> tuple[int, dict, str]:
        captured: dict = {}

        def fake_run_tests(names, **kw):
            # Stand in for run_tests from its first act: the preflight is
            # real, so the CLI's translation of its errors is exercised.
            from otto.suite.run import prepare_run

            prepare_run(kw["run_options"])
            captured.update(dataclasses.asdict(kw["run_options"]))
            return _lib_ok_result()

        monkeypatch.setattr("otto.suite.run.run_tests", fake_run_tests)
        result = otto_test_cli(["test", *cli_args, "test_x"])
        return result.exit_code, captured, result.output

    return _capture
