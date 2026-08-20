"""``otto host <id> <verb> -n`` survives the CLI preamble — driven as a subprocess.

**This file exists because the unit suite provably cannot hold this guard.**
``tests/_fixtures/dispatch.DispatchRunner`` registers every ``CommandSpec``
with ``lab_free=True``, and the leaf preamble calls ``ensure_lab_session`` only
``if not spec.lab_free`` (``otto/cli/invoke.py``) — so
:func:`otto.cli.invoke.ensure_cli_session` never executes in any of the 91
``--dry-run`` seam tests, and neither does the provenance log line inside it.
``tests/unit/cli/test_host.py`` compounds it by patching ``ensure_cli_session``
out by name. The defect this guards was invisible to all of them and took out
the whole ``otto host`` surface under ``-n``:

    CommandNotRunError: 'git -C <sut_dir> log -1 --format=%H' was not run on
    host 'localhost': this is a dry run, which contacts no device.

``HostGroup.get_command`` installs the dry-run ``OttoContext`` at PARSE time
(the soft lab probe that scopes the verb menu to the host's class), so by the
time the preamble reads each repo's HEAD to stamp provenance, the decline is
already armed. Only a real invocation of the real binary, with a lab loaded and
a host id on the command line, reproduces that ordering — which is what
``run_otto`` gives us. See ``todo/test-harness-declares-registration-2026-08-16.md``.

The host is ``local`` throughout: it is the one host in every lab that a
positive control can drive for real without touching lab hardware.
"""

from pathlib import Path

import pytest

from otto.utils import DRY_RUN_HEADLINE
from tests._fixtures.labdata import lab_data_dir
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless

LAB_DATA_DIR = lab_data_dir() / "tech1"

SETTINGS_EXTRA = """\
[[lab.sources]]
backend = "json"
paths = ["{lab_data_dir}"]
"""


def _make_repo(root: Path) -> Path:
    """A throwaway SUT repo wired to otto's own ``veggies`` lab fixture."""
    return make_sut_repo(
        root,
        name="dryrunrepo",
        version="0.1.0",
        tests=["tests"],
        extra=SETTINGS_EXTRA.format(lab_data_dir=LAB_DATA_DIR),
        files={"tests/test_placeholder.py": "def test_placeholder() -> None:\n    assert True\n"},
    )


def _flat(text: str) -> str:
    """Collapse rich's wrapping so a line can be matched as one string."""
    return " ".join(text.split())


def test_a_host_verb_under_dry_run_exits_0_and_runs_no_body(tmp_path: Path) -> None:
    """``otto host local run -n`` prints the seam block; the same command runs without ``-n``.

    Both halves in one test, because either alone is worthless. "The body did
    not run" passes just as happily against a command that is broken end to
    end — which is exactly the state this guard was written against, where the
    invocation aborted before the body for the wrong reason entirely.

    Scored on a FILESYSTEM FACT, not on stdout. The would-run echo line repeats
    the command back verbatim, so any marker string the command carries appears
    in the dry run's own output; only the ``touch``'s effect distinguishes
    "announced" from "executed".
    """
    repo = _make_repo(tmp_path / "dryrunrepo")
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    marker = tmp_path / "body-ran.marker"

    dry = run_otto(
        ["-n", "host", "local", "run", f"touch {marker}"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    combined = dry.stdout + dry.stderr

    assert "CommandNotRunError" not in combined, (
        f"the dry run tracebacked out of the CLI preamble instead of reaching the seam:\n{combined}"
    )
    assert dry.returncode == 0, f"`otto host local run -n` exited {dry.returncode}:\n{combined}"
    assert DRY_RUN_HEADLINE in _flat(dry.stdout), f"the dry-run block never printed:\n{combined}"
    assert not marker.exists(), "the dry run RAN THE COMMAND BODY: the marker file exists"

    # POSITIVE CONTROL — the same command, the same host, the same seam,
    # without `-n`. Without this the assertions above are satisfied by an otto
    # that cannot run `host local run` at all.
    real = run_otto(
        ["host", "local", "run", f"touch {marker}"],
        xdir=xdir,
        sut_dirs=repo,
        lab="veggies",
    )
    assert real.returncode == 0, (
        f"the positive control failed, so the dry-run assertions above prove "
        f"nothing:\n{real.stdout + real.stderr}"
    )
    assert marker.exists(), (
        "the positive control did not run the command body either — this test "
        "is no longer measuring the dry run"
    )
