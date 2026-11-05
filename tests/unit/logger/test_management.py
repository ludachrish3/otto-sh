import logging
import logging.handlers
import os
import re
import time
from pathlib import Path

import pytest

from otto import layout
from otto.host.host import HostFilter
from otto.logger import management
from otto.logger.mode import LogMode


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _otto_handlers(logger: logging.Logger) -> list[logging.Handler]:
    """The handlers otto installed on *logger* (marked with OTTO_HANDLER_ATTR)."""
    return [h for h in logger.handlers if getattr(h, management.OTTO_HANDLER_ATTR, False)]


def test_init_cli_logging_puts_a_marked_console_handler_on_root(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    root = logging.getLogger()
    assert root.level == logging.INFO  # the verbose floor
    assert len(_otto_handlers(root)) == 1
    # The otto logger is a plain library logger again: no otto handlers, propagating.
    otto = logging.getLogger("otto")
    assert _otto_handlers(otto) == []
    assert otto.propagate is True


def test_create_output_dir_returns_and_creates_dir(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test", "mysuite")
    assert out.exists()
    assert out.is_dir()
    assert out.parent == layout.command_output_dir(tmp_path, "test")
    assert (out / "console.log").exists()  # file handler created the console transcript
    # otto's import-time NullHandler stays a LIBRARY concern: it is never
    # marked, so it can never be adopted onto root or fanned into the listener.
    # (The old form here asserted "no NullHandler in listener.handlers", which
    # became unfalsifiable once install_sinks stopped touching the otto logger:
    # the fan-out is built from _state.console_handler + the two file handlers,
    # so no NullHandler can reach it by any route.)
    nulls = [h for h in logging.getLogger("otto").handlers if isinstance(h, logging.NullHandler)]
    assert nulls, "otto keeps its import-time NullHandler through a CLI install"
    assert not any(getattr(h, management.OTTO_HANDLER_ATTR, False) for h in nulls)
    assert not any(h in logging.getLogger().handlers for h in nulls)


def test_create_output_dir_writes_console_and_verbose(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test", "mysuite")
    assert (out / "console.log").exists()
    assert (out / "verbose.log").exists()
    assert not (out / "otto.log").exists()


def test_verbose_log_keeps_quiet_console_log_drops_it(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.attach_console_suppress_filter(HostFilter())
    out = management.create_output_dir("test")
    log = logging.getLogger("otto")
    host = type("H", (), {"name": "h1"})()
    log.info("@h1 > | quiet line", extra={"host": host, "log_mode": LogMode.QUIET})
    management._state.listener.stop()  # flush the queue
    assert "quiet line" in (out / "verbose.log").read_text()
    assert "quiet line" not in (out / "console.log").read_text()


def test_verbose_floor():
    assert management.verbose_floor("INFO") == logging.INFO
    assert management.verbose_floor("WARNING") == logging.INFO
    assert management.verbose_floor("DEBUG") == logging.DEBUG


def test_remove_old_logs_respects_time_budget(tmp_path, monkeypatch):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    cmd_dir = layout.command_output_dir(tmp_path, "test")
    olds = [_old_run(cmd_dir, f"20200101_0000{i:02d}_000") for i in range(6)]
    ticks = iter([float(n) for n in range(1000)])
    monkeypatch.setattr(management.time, "monotonic", lambda: next(ticks))
    management.remove_old_logs(seconds=60, time_budget=2.5)
    survivors = [d for d in olds if d.exists()]
    assert survivors, "budget should stop before removing all"
    assert len(survivors) < len(olds), "a prune that removes nothing passes no budget test"


def test_library_import_attaches_nullhandler():
    import otto.logger

    otto = logging.getLogger("otto")
    assert any(isinstance(h, logging.NullHandler) for h in otto.handlers)


def test_unregistered_library_logger_lands_in_sinks(tmp_path):
    """Spec §7.1: zero-registration capture. No prefix list exists any more."""
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    logging.getLogger("fake_vendor.sub").info("vendor line")
    logging.getLogger("myproduct.install").info("product line")
    management._state.listener.stop()  # flush the queue
    verbose = (out / "verbose.log").read_text()
    assert "vendor line" in verbose
    assert "product line" in verbose


def test_default_library_levels_quiet_known_noisy_libraries(tmp_path):
    """Spec §7.2: a defaults-table logger's INFO does not enter the funnel even
    at --log-level DEBUG; WARNING still does."""
    management.init_cli_logging(xdir=tmp_path, log_level="DEBUG", keep_days=7)
    out = management.create_output_dir("test")
    name = next(iter(management.DEFAULT_LIBRARY_LEVELS))
    logging.getLogger(name).info("noisy info")
    logging.getLogger(name).warning("real warning")
    management._state.listener.stop()
    verbose = (out / "verbose.log").read_text()
    assert "noisy info" not in verbose
    assert "real warning" in verbose


def test_levels_overrides_unmute_and_extend(tmp_path):
    """Spec §4.2: a project entry overrides a default per name, and extends the table."""
    management.init_cli_logging(xdir=tmp_path, log_level="DEBUG", keep_days=7)
    name = next(iter(management.DEFAULT_LIBRARY_LEVELS))
    management.apply_library_levels({name: "DEBUG", "chatty_sdk": "ERROR"})
    assert logging.getLogger(name).level == logging.DEBUG
    assert logging.getLogger("chatty_sdk").level == logging.ERROR


def test_reset_restores_floored_loggers_to_notset(tmp_path):
    """Spec §3.2's restore discipline, extended to the levels the floor set."""
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.apply_library_levels({"chatty_sdk": "ERROR"})
    management.reset()
    assert logging.getLogger("chatty_sdk").level == logging.NOTSET
    name = next(iter(management.DEFAULT_LIBRARY_LEVELS))
    assert logging.getLogger(name).level == logging.NOTSET


def test_a_warn_alias_from_settings_drives_a_real_setlevel(tmp_path):
    """otto's own WARN/CRIT aliases must survive the whole path, not just parsing.

    A project writes ``vendor = "warn"``; the settings model normalizes it and
    ``apply_library_levels`` hands the NAME straight to ``Logger.setLevel``.
    The parse-only test in ``test_project.py`` stops before that hand-off, which
    is precisely where an alias that stopped being registered would surface —
    as ``ValueError: Unknown level``, at CLI startup, on a config that
    validated.
    """
    from otto.config.project import Project
    from tests._fixtures.project_scaffold import make_project

    sut = make_project(
        tmp_path / "aliasrepo",
        name="aliasrepo",
        extra='[logging.levels]\nvendor = "warn"\nloud_vendor = "CRIT"\n',
    )
    project = Project(root=sut)

    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.apply_library_levels(project.logging_levels)

    assert logging.getLogger("vendor").level == logging.WARNING
    assert logging.getLogger("loud_vendor").level == logging.CRITICAL


def test_a_second_install_console_keeps_the_project_overrides(tmp_path):
    """Re-installing the console must not silently undo ``[logging.levels]``.

    ``install_console`` re-applies the table on every install. Applying the
    bare defaults there would put an operator's one deliberate override back to
    otto's value with no signal — so the overrides are remembered on the state
    and re-applied with them.
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    default_name = next(iter(management.DEFAULT_LIBRARY_LEVELS))
    management.apply_library_levels({default_name: "DEBUG", "vendor_sdk": "ERROR"})

    management.install_console("INFO")

    assert logging.getLogger(default_name).level == logging.DEBUG, (
        "a re-install reverted an override of an otto default back to the default"
    )
    assert logging.getLogger("vendor_sdk").level == logging.ERROR


def test_reset_forgets_the_overrides_so_a_later_install_is_defaults_only(tmp_path):
    """The other half of the memory: it is per-invocation, not per-process.

    Without this, an embedder's ``reset()`` would leave the previous run's
    ``[logging.levels]`` silently in force across the next ``install()``.
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    default_name = next(iter(management.DEFAULT_LIBRARY_LEVELS))
    management.apply_library_levels({default_name: "DEBUG", "vendor_sdk": "ERROR"})

    management.reset()
    management.install_console("INFO")

    assert logging.getLogger(default_name).level == logging.getLevelName(
        management.DEFAULT_LIBRARY_LEVELS[default_name]
    )
    assert logging.getLogger("vendor_sdk").level == logging.NOTSET


def test_double_install_adds_no_duplicate_handlers(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    assert len(_otto_handlers(logging.getLogger())) == 1


def test_foreign_root_handler_survives_install_and_reset(tmp_path):
    """Spec §3.2: pytest-caplog stand-in — receives records, never detached."""

    class _Foreign(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    foreign = _Foreign()
    root = logging.getLogger()
    root.addHandler(foreign)
    try:
        management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
        management.create_output_dir("test")
        logging.getLogger("otto.something").info("shared record")
        assert any(r.getMessage() == "shared record" for r in foreign.records)
        management.reset()
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)


def test_early_console_records_before_output_dir_reach_the_console(tmp_path, capsys):
    """Spec §7.4: a warning emitted between install_console and create_output_dir
    must be VISIBLE — the window the lab_free swallowed-warning class lived in.

    What this pins is the console handler's PRESENCE in that window (Mutation 1
    reds it). It is not a before/after discriminator for THIS logger: a record
    was only swallowed on paths where ``init_cli_logging`` never ran at all —
    ``otto.cli.gates`` is a child of ``otto``, which carried the console handler
    before the cutover too, so this line printed then as well. What changed is
    that the window now covers EVERY logger, not just otto's own subtree.
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    logging.getLogger("otto.cli.gates").warning("demoted project xyz")
    assert "demoted project xyz" in capsys.readouterr().out


def test_reinstalling_the_console_leaves_the_sinks_wired(tmp_path):
    """install_console after install_sinks must not sever the fan-out.

    A marked QueueHandler is the sinks' only attachment to root; the console
    handler is the only thing install_console owns there. Removing marked
    handlers indiscriminately would detach the QueueHandler while its listener
    kept running, and every sink would go silent with nothing to say so.
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    management.install_console("INFO")
    logging.getLogger("fake_vendor.after_reinstall").info("still captured")
    management._state.listener.stop()  # flush the queue
    assert "still captured" in (out / "verbose.log").read_text()


def test_second_install_sinks_retires_the_first_fan_out(tmp_path):
    """Spec §3.1 ("both are idempotent") for install_sinks.

    A second output dir must leave exactly ONE marked QueueHandler on root and
    must not strand the first listener's thread (or its two open files).
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    first_dir = tmp_path / "run1"
    second_dir = tmp_path / "run2"
    first_dir.mkdir()
    second_dir.mkdir()

    management.install_sinks(first_dir, proxy_streams=False)
    first = management._state.listener
    management.install_sinks(second_dir, proxy_streams=False)

    assert management._state.listener is not first, "the listener must be replaced"
    assert getattr(first, "_thread", None) is None, "the first listener must have been stopped"
    root = logging.getLogger()
    queue_handlers = [
        h for h in _otto_handlers(root) if isinstance(h, logging.handlers.QueueHandler)
    ]
    assert len(queue_handlers) == 1, queue_handlers
    # The retired run's files are closed; the shared console handler is not.
    logging.getLogger("fake_vendor.second_run").info("second run line")
    management._state.listener.stop()
    assert "second run line" in (second_dir / "verbose.log").read_text()
    assert "second run line" not in (first_dir / "verbose.log").read_text()


def test_reset_restores_root_exactly(tmp_path):
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    management.init_cli_logging(xdir=tmp_path, log_level="DEBUG", keep_days=7)
    management.create_output_dir("test")
    management.reset()
    assert root.handlers == before_handlers
    assert root.level == before_level


# ---------------------------------------------------------------------------
# The public embedder entry (spec 2026-08-30 §3.4)
# ---------------------------------------------------------------------------


def test_install_reset_round_trip_for_embedders(tmp_path):
    """Spec §3.4: one public call gives an embedder otto's sinks; reset undoes it."""
    from otto.logger import install

    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    install(log_level="INFO", output_dir=tmp_path / "out", overrides={"chatty_sdk": "ERROR"})
    logging.getLogger("embedder_lib").info("hello from the embedder's dependency")
    assert logging.getLogger("chatty_sdk").level == logging.ERROR
    management._state.listener.stop()
    assert "hello from the embedder's dependency" in (tmp_path / "out" / "verbose.log").read_text()
    management.reset()
    assert (list(root.handlers), root.level) == before


def test_repeated_install_swaps_the_console_instead_of_doubling_it(tmp_path, capsys):
    """Spec §3.4: the ordering contract this entry point picks is SWAP, not refuse.

    Re-installing over live sinks is the reachable ordering an embedder owns (a
    level change, a framework that calls setup twice). The console handler then
    hangs inside the listener's fan-out, NOT on root, so a naive re-install
    leaves the old one fanned out and puts a new one on root — every record
    printed twice — while re-opening the same ``mode="x"`` files would blow up
    and leave the process with no sinks at all.
    """
    from otto.logger import install

    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    first_listener = management._state.listener
    install(log_level="INFO", output_dir=out)

    root = logging.getLogger()
    marked = _otto_handlers(root)
    assert len(marked) == 1, marked
    assert isinstance(marked[0], logging.handlers.QueueHandler)
    listener = management._state.listener
    assert listener is first_listener, "the same directory must keep its open files"
    assert getattr(listener, "_thread", None) is not None, "the listener must still be running"
    consoles = [h for h in listener.handlers if not isinstance(h, logging.FileHandler)]
    assert consoles == [management._state.console_handler], consoles

    logging.getLogger("embedder_lib").warning("said once")
    listener.stop()
    assert capsys.readouterr().out.count("said once") == 1
    assert (out / "verbose.log").read_text().count("said once") == 1


def test_repeated_install_with_a_new_directory_retires_the_old_fan_out(tmp_path, capsys):
    """A second install naming a DIFFERENT directory moves the sinks, once."""
    from otto.logger import install

    first, second = tmp_path / "one", tmp_path / "two"
    install(log_level="INFO", output_dir=first)
    old_listener = management._state.listener
    install(log_level="INFO", output_dir=second)

    assert management._state.listener is not old_listener
    assert getattr(old_listener, "_thread", None) is None, "the retired listener must be stopped"
    root = logging.getLogger()
    assert len(_otto_handlers(root)) == 1, _otto_handlers(root)

    logging.getLogger("embedder_lib").warning("second run only")
    management._state.listener.stop()
    assert capsys.readouterr().out.count("second run only") == 1
    assert "second run only" in (second / "verbose.log").read_text()
    assert "second run only" not in (first / "verbose.log").read_text()


def test_repeated_install_normalizes_the_directory_spelling(tmp_path, monkeypatch):
    """Two spellings of ONE directory are one directory.

    The stored directory is an IDENTITY for "the sinks are already here", and a
    caller's spelling is not one. Comparing them verbatim made ``Path("out")``
    and an absolute path to the same place unequal, re-entering
    ``install_sinks`` — which stops the listener and closes the files BEFORE
    reopening ``console.log`` at ``mode="x"``, so the second install raised
    ``FileExistsError`` and left the process with no handlers and no listener:
    exactly the failure the same-directory branch exists to prevent.
    """
    from otto.logger import install

    monkeypatch.chdir(tmp_path)
    install(log_level="INFO", output_dir=Path("out"))
    first_listener = management._state.listener
    install(log_level="INFO", output_dir=tmp_path / "out")

    assert management._state.listener is first_listener, "the fan-out was rebuilt"
    assert getattr(management._state.listener, "_thread", None) is not None
    root = logging.getLogger()
    marked = _otto_handlers(root)
    assert len(marked) == 1, marked
    assert isinstance(marked[0], logging.handlers.QueueHandler)

    logging.getLogger("embedder_lib").warning("still alive")
    management._state.listener.stop()
    assert "still alive" in (tmp_path / "out" / "verbose.log").read_text()


def test_repeated_install_raises_the_level_in_the_files_too(tmp_path):
    """A changed log_level must reach the SINKS, not only the console.

    The two ``FileHandler``s take their level at construction, so keeping the
    fan-out for an unchanged directory froze ``console.log`` and
    ``verbose.log`` at the FIRST call's level: a DEBUG record entered the
    funnel (root is at the new floor), printed on screen, and was dropped by
    both files under ``respect_handler_level=True``. Re-levelling in place
    rather than rebuilding is what keeps the earlier transcript.
    """
    from otto.logger import install

    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    logging.getLogger("embedder_lib").info("phase one")
    install(log_level="DEBUG", output_dir=out)
    logging.getLogger("embedder_lib").debug("phase two, a debug line")
    management._state.listener.stop()

    verbose = (out / "verbose.log").read_text()
    assert "phase one" in verbose, "the earlier transcript must survive the re-level"
    assert "phase two, a debug line" in verbose
    # console.log is the console transcript, so it moves to --log-level too.
    assert "phase two, a debug line" in (out / "console.log").read_text()
    assert management._state.verbose_handler.level == logging.DEBUG
    assert management._state.console_log_handler.level == logging.DEBUG


def test_a_level_change_reaches_the_files_without_repeating_the_directory(tmp_path):
    """``install(log_level=...)`` with no *output_dir* must re-level the live sinks.

    The sibling shape of the frozen-file-levels bug: omitting *output_dir* says
    "leave WHERE the sinks write alone", not "leave what they admit alone". If
    only the repeated-directory call re-levelled, the one knob would mean two
    different things depending on whether the caller named the directory again
    — and the DEBUG just asked for would print on the console while both files
    dropped it.
    """
    from otto.logger import install

    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    logging.getLogger("embedder_lib").info("phase one")
    install(log_level="DEBUG")
    logging.getLogger("embedder_lib").debug("phase two, a debug line")
    management._state.listener.stop()

    verbose = (out / "verbose.log").read_text()
    assert "phase one" in verbose, "the earlier transcript must survive the re-level"
    assert "phase two, a debug line" in verbose
    assert "phase two, a debug line" in (out / "console.log").read_text()
    assert management._state.verbose_handler.level == logging.DEBUG
    assert management._state.console_log_handler.level == logging.DEBUG


def test_install_revives_the_sinks_after_shutdown_listener(tmp_path):
    """``shutdown_listener()`` then ``install(same dir)`` must deliver to the files again.

    ``shutdown_listener`` claims and stops the listener for an exit path and
    leaves every other sink field standing — so a same-directory guard that
    trusts ``sinks_dir`` alone saw "the sinks are already here" and did
    nothing: the console kept printing while both files were silently dead and
    the detached QueueHandler on root grew a queue nobody drained.
    """
    from otto.logger import install

    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    logging.getLogger("embedder_lib").info("phase one")
    management.shutdown_listener()
    assert management._state.listener is None

    install(log_level="INFO", output_dir=out)

    assert management._state.listener is not None
    assert getattr(management._state.listener, "_thread", None) is not None
    root = logging.getLogger()
    marked = _otto_handlers(root)
    assert len(marked) == 1, marked
    assert isinstance(marked[0], logging.handlers.QueueHandler)

    logging.getLogger("embedder_lib").info("phase two, after the revival")
    management._state.listener.stop()
    verbose = (out / "verbose.log").read_text()
    assert "phase one" in verbose, "the revival must not restart the transcript"
    assert "phase two, after the revival" in verbose


def test_install_refuses_to_adopt_closed_handlers(tmp_path):
    """A revival must never rebuild the fan-out around CLOSED file handlers.

    The open-failure window can close the current files while ``sinks_dir``
    still names their directory. Adopting them is a delayed process-wide
    kill: a closed ``FileHandler`` re-opens on its next emit, ``mode="x"``
    refuses the existing file, and the raise lands inside
    ``QueueListener._monitor`` — which catches only ``queue.Empty`` — taking
    the console down with the files. The honest behaviour is to fall through
    to the rebuild and fail loudly at the ``install()`` call itself.
    """
    from otto.logger import install

    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    management.shutdown_listener()
    management._state.console_log_handler.close()
    management._state.verbose_handler.close()
    assert management._state.console_log_handler.stream is None

    with pytest.raises(FileExistsError):
        install(log_level="INFO", output_dir=out)


def test_reset_closes_the_log_files_after_shutdown_listener(tmp_path):
    """``reset()`` must close the two files even when no listener holds them.

    The close loop hung off the listener, which ``shutdown_listener`` has
    already nulled — so ``reset()`` cleared the handler fields and the open
    files survived to whenever the garbage collector reached them.
    """
    from otto.logger import install

    install(log_level="INFO", output_dir=tmp_path / "out")
    console_log = management._state.console_log_handler
    verbose_log = management._state.verbose_handler
    management.shutdown_listener()
    assert console_log.stream is not None, "shutdown_listener must leave them open"

    management.reset()

    assert console_log.stream is None, "console.log was left open"
    assert verbose_log.stream is None, "verbose.log was left open"


def test_attaching_the_same_filter_class_twice_leaves_one(tmp_path):
    """One suppress filter per class per handler (repeated ensure_cli_session).

    Every session builds a fresh ``HostFilter``, and ``install_console`` copies
    the filters it finds onto each replacement — so N in-process invocations
    left N interchangeable HostFilters re-deciding the same verdict.
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.attach_console_suppress_filter(HostFilter())
    management.attach_console_suppress_filter(HostFilter())

    console = management._state.console_handler
    assert [type(f) for f in console.filters] == [HostFilter], console.filters


def test_the_console_swap_carries_the_suppress_filters(tmp_path, capsys):
    """A re-install must not silently UN-suppress.

    ``attach_console_suppress_filter`` hangs HostFilter/LogMode on the console
    handler; the replacement a re-install builds carries none of them. Not
    reachable from the CLI (it attaches the filter after its last install), but
    ``install`` makes "attach a suppress filter, then re-install to raise the
    level" an ordinary embedder sequence.
    """
    from otto.logger import install

    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    filt = HostFilter()
    management.attach_console_suppress_filter(filt)
    install(log_level="DEBUG", output_dir=out)

    assert filt in management._state.console_handler.filters
    host = type("H", (), {"name": "h1"})()
    logging.getLogger("embedder_lib").warning(
        "@h1 > | quiet line", extra={"host": host, "log_mode": LogMode.QUIET}
    )
    management._state.listener.stop()
    assert "quiet line" not in capsys.readouterr().out, "the filter stopped suppressing"
    assert "quiet line" in (out / "verbose.log").read_text()


def test_reinstalling_the_console_over_live_sinks_does_not_double_the_output(tmp_path, capsys):
    """The same swap, reached through the bare installers the CLI uses."""
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    management.install_console("INFO")

    logging.getLogger("fake_vendor.after_reinstall").warning("printed once")
    management._state.listener.stop()
    assert capsys.readouterr().out.count("printed once") == 1
    assert (out / "verbose.log").read_text().count("printed once") == 1


# ---------------------------------------------------------------------------
# shutdown_listener's bounded flush (the sync_phase force path's only caller)
#
# The force-path call site wraps this in contextlib.suppress(Exception), so a
# regression here (probe inverted, _thread rename, enqueue failure) would be
# swallowed silently forever — these tests are the only loud surface it has.
# ---------------------------------------------------------------------------


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("otto.flush", logging.INFO, __file__, 1, msg, None, None)


def _listener_with_queued_record(msg: str):
    import queue

    capture = _CaptureHandler()
    q: "queue.Queue[logging.LogRecord]" = queue.Queue(-1)
    listener = logging.handlers.QueueListener(q, capture)
    listener.start()
    q.put(_record(msg))
    management._state.listener = listener
    return listener, q, capture


def test_shutdown_listener_bounded_flush_drains_and_claims():
    listener, _q, capture = _listener_with_queued_record("flush-me")
    management.shutdown_listener(join_timeout=5.0)
    assert capture.messages == ["flush-me"], "bounded flush must drain queued records"
    assert management._state.listener is None, "the listener must be claimed out of module state"
    thread = listener._thread
    assert thread is None or not thread.is_alive(), "the listener thread must have stopped"
    # Idempotent: a second call (force watchdog racing atexit) no-ops.
    management.shutdown_listener(join_timeout=5.0)


def test_shutdown_listener_bounded_flush_skips_when_queue_mutex_held():
    listener, q, capture = _listener_with_queued_record("never-flushed")
    # A wedged producer holds the queue mutex: the force path must abandon the
    # flush (never the exit) — and must not enqueue the stop sentinel.
    assert q.mutex.acquire(blocking=False)
    try:
        management.shutdown_listener(join_timeout=5.0)
    finally:
        q.mutex.release()
    assert management._state.listener is None, "state is claimed even when the flush is skipped"
    assert listener._thread is not None
    assert listener._thread.is_alive(), (
        "the listener must NOT have been stopped through a held mutex"
    )
    # Cleanup: drain and stop for real now that the mutex is free.
    listener.stop()
    assert capture.messages == ["never-flushed"]


def test_stop_listener_unbounded_branch_still_stops():
    listener, _q, capture = _listener_with_queued_record("atexit-path")
    management._stop_listener()
    assert capture.messages == ["atexit-path"]
    assert management._state.listener is None
    assert listener._thread is None, "the unbounded branch uses QueueListener.stop()"


# ── Where a run's directory goes ─────────────────────────────────────────────
_TS = r"\d{8}_\d{6}_\d{3}"


@pytest.mark.parametrize(
    ("command", "sub", "rel", "leaf"),
    [
        ("docker", None, "docker", rf"{_TS}"),
        ("run", "write-file", "run", rf"{_TS}_write_file"),
    ],
    ids=["command", "command-and-subcommand"],
)
def test_an_explicit_xdir_puts_the_run_straight_under_its_command(
    tmp_path, command, sub, rel, leaf
):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir(command, sub)
    assert out.parent == tmp_path / rel
    assert re.fullmatch(leaf, out.name), out.name
    # No `otto-output/` level: the command directory is the xdir's only child.
    assert [p.name for p in tmp_path.iterdir()] == [rel]
    assert layout.is_run_dir(out)


def test_a_missing_explicit_xdir_is_created(tmp_path):
    xdir = tmp_path / "new" / "d"
    management.init_cli_logging(xdir=xdir, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    assert out.parent == xdir / "test"
    assert layout.is_run_dir(out)


def test_two_runs_never_share_a_run_dir(tmp_path, monkeypatch):
    # The CLI refuses a collision (make_run_dir without exist_ok), as it always did.
    from datetime import datetime, timezone

    fixed = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    real = layout.run_output_dir
    monkeypatch.setattr(
        "otto.layout.run_output_dir", lambda xdir, entry, **_kw: real(xdir, entry, started=fixed)
    )
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("test")
    with pytest.raises(FileExistsError):
        management.create_output_dir("test")


def _aged(path: Path) -> Path:
    past = time.time() - 3600
    os.utime(path, (past, past))
    return path


def _old_run(parent: Path, name: str = "20200101_000000_000") -> Path:
    """An old run dir otto made: the run name AND the marker (layout.make_run_dir)."""
    return _aged(layout.make_run_dir(parent / name))


def _old_lookalike(parent: Path, name: str = "20200101_000000_000") -> Path:
    """An old directory with otto's run name but no marker: a project's own."""
    d = parent / name
    d.mkdir(parents=True)
    (d / "keep.txt").write_text("mine\n")
    return _aged(d)


def test_log_days_prunes_an_old_marked_run_dir(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    old = _old_run(layout.command_output_dir(tmp_path, "test"))
    management.remove_old_logs(seconds=60)
    assert not old.exists()


def test_log_days_keeps_an_old_pattern_matching_dir_without_the_marker(tmp_path, monkeypatch):
    # `--xdir .`: the walk meets the project's own top-level directories.
    monkeypatch.chdir(tmp_path)
    management.init_cli_logging(xdir=Path(), log_level="INFO", keep_days=7)
    project_own = _old_lookalike(tmp_path / "test")
    management.remove_old_logs(seconds=60)
    assert (project_own / "keep.txt").read_text() == "mine\n"


def test_log_days_keeps_young_runs_and_non_run_dirs(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    cmd = layout.command_output_dir(tmp_path, "test")
    young = layout.make_run_dir(cmd / "20200101_000000_000")
    # Marked, old, but not named like a run: the name check is half the fence.
    notes = cmd / "notes"
    notes.mkdir()
    (notes / ".otto-run").touch()
    _aged(notes)
    management.remove_old_logs(seconds=60)
    assert young.is_dir()
    assert notes.is_dir()


def test_prune_removes_old_marked_runs_and_never_unmarked_lookalikes(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    old_run = _old_run(layout.command_output_dir(tmp_path, "test"), "20200101_000000_001")
    # A user's own ./docker and ./test, each holding an old child named like
    # one of otto's runs but carrying no marker: neither is otto's.
    users_docker = _old_lookalike(tmp_path / "docker")
    users_test = _old_lookalike(tmp_path / "test", "20200101_000000_000_suite")
    management.remove_old_logs(seconds=60)
    assert not old_run.exists()
    assert users_docker.is_dir()
    assert users_test.is_dir()


def test_log_days_never_follows_a_symlinked_command_dir(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    old = _old_run(outside)
    project = tmp_path / "project"
    project.mkdir()
    (project / "archive").symlink_to(outside)
    monkeypatch.chdir(project)
    management.init_cli_logging(xdir=Path(), log_level="INFO", keep_days=7)
    management.remove_old_logs(seconds=60)
    assert layout.is_run_dir(old)


def test_log_days_never_deletes_a_symlinked_run_dir(tmp_path):
    target = _old_run(tmp_path / "elsewhere")
    management.init_cli_logging(xdir=tmp_path / "xdir", log_level="INFO", keep_days=7)
    cmd = layout.command_output_dir(tmp_path / "xdir", "test")
    cmd.mkdir(parents=True)
    link = cmd / "20200101_000000_000"
    link.symlink_to(target)
    management.remove_old_logs(seconds=60)
    assert link.is_symlink()
    assert layout.is_run_dir(target)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
@pytest.mark.parametrize("where", ["project-root", "home-root"])
def test_log_days_survives_an_unreadable_child(tmp_path, monkeypatch, where):
    if where == "home-root":
        monkeypatch.setenv("HOME", str(tmp_path / "user"))
        xdir = Path.home()
    else:
        xdir = tmp_path / "project"
    xdir.mkdir(parents=True)
    locked = xdir / "locked"
    locked.mkdir()
    old = _old_run(layout.command_output_dir(xdir, "test"))
    locked.chmod(0)
    try:
        management.init_cli_logging(xdir=xdir, log_level="INFO", keep_days=7)
        management.remove_old_logs(seconds=60)
    finally:
        locked.chmod(0o755)
    assert not old.exists()


def test_log_days_survives_a_command_dir_whose_stat_fails(tmp_path, monkeypatch):
    """`--xdir .` meets a stale network mount: its stat raises, the walk goes on."""
    import errno

    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    stale = tmp_path / "mnt"
    stale.mkdir()
    old = _old_run(layout.command_output_dir(tmp_path, "test"))
    real_stat = Path.stat

    def stat_fails_on_the_mount(self, *args, **kwargs):
        if self == stale:
            raise OSError(errno.ESTALE, "Stale file handle", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_fails_on_the_mount)
    management.remove_old_logs(seconds=60)
    assert not old.exists()


def test_log_days_survives_an_entry_that_vanishes_mid_walk(tmp_path, monkeypatch):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    cmd = layout.command_output_dir(tmp_path, "test")
    gone = _old_run(cmd, "20200101_000000_000")
    kept_old = _old_run(cmd, "20200101_000000_001")
    real_iterdir = Path.iterdir

    def iterdir_then_vanish(self):
        entries = list(real_iterdir(self))
        if self == cmd:
            import shutil

            shutil.rmtree(gone)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", iterdir_then_vanish)
    management.remove_old_logs(seconds=60)
    assert not gone.exists()
    assert not kept_old.exists()


def test_log_days_spends_no_stat_on_an_entry_without_a_run_name(tmp_path, monkeypatch):
    """Perf is file operations: the free name check comes before any stat.

    Under ``--xdir .`` or ``--xdir ~`` the walk meets every entry of every
    top-level directory (``node_modules/``, ``build/``); an entry not named like
    a run must cost the listing and nothing else.
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    for name in ("node_modules", "src"):
        for i in range(50):
            (tmp_path / name / f"entry{i}").mkdir(parents=True)
    old = _old_run(layout.command_output_dir(tmp_path, "test"))
    command_dirs = 3

    calls = []
    real_stat, real_lstat = Path.stat, Path.lstat

    def counting_stat(self, *args, **kwargs):
        calls.append(("stat", self))
        return real_stat(self, *args, **kwargs)

    def counting_lstat(self, *args, **kwargs):
        calls.append(("lstat", self))
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)
    monkeypatch.setattr(Path, "lstat", counting_lstat)
    management.remove_old_logs(seconds=60)
    monkeypatch.undo()

    assert not old.exists()
    # is_real_dir per command dir: is_dir's stat and is_symlink's lstat (which
    # Python 3.10's pathlib spells as a second stat call, so up to 3 counted).
    # Every other call is on the one run-named entry: its marker, the real-dir
    # test, the age, then rmtree's walk of it.
    walked = [c for c in calls if old not in (c[1], *c[1].parents)]
    assert len(walked) <= 3 * command_dirs, walked
    assert len(calls) - len(walked) >= 1
    assert not [c for c in calls if c[1].name.startswith("entry")], "a non-run entry was statted"


def test_the_time_budget_is_checked_between_command_dirs(tmp_path, monkeypatch):
    """A long run of empty or unlistable xdir children still consults the budget."""
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    for i in range(10):
        (tmp_path / f"empty{i}").mkdir()
    listed = []
    real_children = management._children

    def recording(directory):
        listed.append(directory)
        return real_children(directory)

    ticks = iter([float(n) for n in range(1000)])
    monkeypatch.setattr(management.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(management, "_children", recording)
    management.remove_old_logs(seconds=60, time_budget=2.5)
    # The xdir itself, then only the command dirs listed before the budget ran out.
    assert 1 < len(listed) < 11, listed
