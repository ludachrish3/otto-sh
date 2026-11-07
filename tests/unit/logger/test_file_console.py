"""Log files render at a fixed width, whatever stdout was attached to (#785 D5).

The file console reads nothing from the environment or the descriptors, so a
string or markup record is one line, a renderable renders at
:data:`~otto.logger.formatters.LOG_FILE_WIDTH`, and the same records give the
same bytes under a terminal, a pipe, ``COLUMNS``, ``TERM=dumb`` or
``NO_COLOR``.

The environment cases run in a fresh interpreter: the file console is built
once, so changing ``COLUMNS`` after it exists would test nothing. The
renderable cases call a fresh :class:`~otto.logger.formatters.RichFormatter`
directly, because installed logging stringifies a record's message in
``_InProcessQueueHandler.prepare`` before any file formatter sees it.
"""

import copy
import logging
import os
import re
import select
import subprocess
import sys
from pathlib import Path

import pytest
from rich.table import Table

from otto.logger import formatters
from tests._fixtures.controlling_pty import set_winsize
from tests.e2e._otto_subprocess import coverage_subprocess_env

_PROBE = """\
import logging
import sys
from pathlib import Path

from otto.logger import management

out, mode = Path(sys.argv[1]), sys.argv[2]
management.install(log_level="INFO", output_dir=out)
if mode == "rich":
    management._state.console_log_handler.formatter.rich = True
    management._state.verbose_handler.formatter.rich = True
log = logging.getLogger("acme.product")
log.info("s" * 300)
log.info("[bold red]" + "m" * 300 + "[/bold red]", extra={"markup": True})
management.reset()
"""

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RED_SGR = re.compile(r"\x1b\[(?:[0-9]+;)*31(?:;[0-9]+)*m")
_ASCTIME = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}", re.MULTILINE)

_ENVIRONMENTS = {
    "default": {},
    "columns-40": {"COLUMNS": "40"},
    "pty-40x10": {},
    "dumb-forced": {"TERM": "dumb", "FORCE_COLOR": "1"},
    "unknown-forced": {"TERM": "unknown", "FORCE_COLOR": "1"},
    "no-color": {"NO_COLOR": "1"},
}


def _drain(fd: int, proc: subprocess.Popen) -> None:
    """Read a pty master until its child exits, so the child never blocks on a full pty."""
    while True:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                if not os.read(fd, 65536):
                    break
            except OSError:  # EIO: the slave side closed
                break
        elif proc.poll() is not None:
            break


def _run_probe(tmp_path: Path, mode: str, env_name: str) -> dict[str, str]:
    """Log the two records in a fresh interpreter under *env_name*; return both files, masked."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE)
    out = tmp_path / f"{mode}-{env_name}"
    env = coverage_subprocess_env(extra_env=_ENVIRONMENTS[env_name])
    argv = [sys.executable, str(probe), str(out), mode]
    if env_name == "pty-40x10":
        master, slave = os.openpty()
        set_winsize(slave, 40, 10)
        try:
            proc = subprocess.Popen(
                argv, env=env, stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE
            )
        finally:
            os.close(slave)
        try:
            _drain(master, proc)
            _, err = proc.communicate(timeout=60)
        finally:
            os.close(master)
        assert proc.returncode == 0, err
    else:
        done = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
    return {
        name: _ASCTIME.sub("<time>", (out / name).read_text())
        for name in ("console.log", "verbose.log")
    }


@pytest.fixture(scope="module")
def baselines(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, str]]:
    """Each mode's files in the default environment, the bytes every other one must match."""
    return {
        mode: _run_probe(tmp_path_factory.mktemp(f"baseline-{mode}"), mode, "default")
        for mode in ("plain", "rich")
    }


@pytest.mark.parametrize("env_name", list(_ENVIRONMENTS))
@pytest.mark.parametrize("mode", ["plain", "rich"])
def test_a_log_file_does_not_depend_on_how_stdout_was_attached(
    tmp_path: Path, baselines: dict[str, dict[str, str]], mode: str, env_name: str
) -> None:
    files = _run_probe(tmp_path, mode, env_name)
    for name, text in files.items():
        plain = _ANSI.sub("", text)
        assert len([ln for ln in plain.splitlines() if "s" * 300 in ln]) == 1, f"{name}:\n{plain}"
        assert len([ln for ln in plain.splitlines() if "m" * 300 in ln]) == 1, f"{name}:\n{plain}"
        if mode == "rich":
            assert _RED_SGR.search(baselines[mode][name]), f"{name} lost its red:\n{text!r}"
        assert text == baselines[mode][name], f"{name} under {env_name} differs from default"


# ── the formatter, called directly ──────────────────────────────────────────


def _table_record() -> logging.LogRecord:
    table = Table()
    table.add_column("t")
    table.add_row("t" * 300)
    record = logging.LogRecord("acme", logging.INFO, __file__, 1, table, None, None)
    record.markup = True
    return record


@pytest.mark.parametrize("rich", [False, True], ids=["plain", "rich"])
def test_a_renderable_record_renders_at_the_file_width(rich: bool) -> None:
    fmt = formatters.RichFormatter()
    fmt.rich = rich
    rendered = _ANSI.sub("", fmt._stylize(copy.copy(_table_record())).msg)
    assert max(len(line) for line in rendered.splitlines()) == formatters.LOG_FILE_WIDTH

    fresh = _table_record()
    table = fresh.msg
    fmt.format(fresh)
    assert fresh.msg is table, "format() must hand the record back as it found it"


@pytest.mark.parametrize("rich", [False, True], ids=["plain", "rich"])
def test_a_long_markup_record_is_one_line(rich: bool) -> None:
    fmt = formatters.RichFormatter()
    fmt.rich = rich
    record = logging.LogRecord(
        "acme", logging.INFO, __file__, 1, "[bold]" + "m" * 300 + "[/bold]", None, None
    )
    record.markup = True
    rendered = _ANSI.sub("", fmt._stylize(copy.copy(record)).msg)
    assert rendered.rstrip("\n").splitlines() == ["m" * 300]
