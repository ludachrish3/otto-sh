"""A planted password reaches none of the files and streams a suite run writes.

One real ``otto test`` run, at DEBUG, of a suite that elevates with a planted
password three ways: an elevated run that succeeds and then fails an
``expect()`` naming ``ctx``, ``cred`` and ``password``; an elevated run whose
password the host refuses; and a run with the caller's own ``SecretStr``
expect pair. The password must be absent from the console, ``console.log``,
``verbose.log``, the JUnit XML and the expect report in it, while the run
itself must show that each path really ran (the answer reached the shell, the
refusal is reported, the expect report is there).

The shell on the other end is :mod:`tests._fixtures.elevation_shell`, attached
as each host's default session, so no lab VM is involved. The password comes
from the environment, never from a source line: the expect report prints the
call's source, and Rich prints the source around every traceback frame.
"""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from otto.config.env import is_otto_env_var
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.project_scaffold import make_project

PLANTED = "PLANTED-SECRET-7f3a"

_ENTRY = [sys.executable, "-c", "from otto.cli.main import entry; entry()"]

_RUN_TIMEOUT = 120
"""Seconds the ``otto test`` subprocess may take: a wedged exit fails the test, never the lane."""

_SUITE = """\
import os

from pydantic import SecretStr

from otto.host.element import Element
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from tests._fixtures.elevation_shell import ElevationSession, ElevationShell, attach


class _Dut(UnixHost):
    def _userland(self):
        return None  # no resolver: sudo, as on a fresh host


def _dut(ctx, accepts_var, outputs=None):
    # The shell's password is read where it is used, never bound in a frame a
    # traceback renders: *accepts_var* names the variable holding it.
    host = _Dut(
        ip="127.0.0.1",
        element=Element("dut"),
        creds=ctx.lab.hosts["host1"].creds,
        log=LogMode.NORMAL,
    )
    shell = ElevationShell(password=os.environ[accepts_var], outputs=outputs)
    attach(host, ElevationSession(shell))
    return host


async def test_elevated_run_then_a_failing_expect(ctx, expect):
    host = _dut(ctx, "ACME_PLANTED_PASSWORD", {"id -u": "0\\n"})
    result = (await host.run("id -u", sudo=True)).only
    cred = host.creds[0]
    password = cred.password
    expect(result.retcode == 7 and ctx is None and cred is None and password is None, "elevated")


async def test_a_refused_elevation(ctx):
    host = _dut(ctx, "ACME_OTHER_PASSWORD")
    await host.run("id -u", sudo=True)


async def test_a_callers_secret_expect(ctx):
    host = _dut(ctx, "ACME_OTHER_PASSWORD", {"ask": "Password:\\nok\\n"})
    answer = SecretStr(os.environ["ACME_PLANTED_PASSWORD"])
    result = (await host.run("ask", expects=[("Password:", answer)])).only
    assert result.retcode == 0
"""


_TESTS = [
    "test_elevated_run_then_a_failing_expect",
    "test_a_refused_elevation",
    "test_a_callers_secret_expect",
]


@pytest.fixture
def run_dir(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    lab_data = tmp_path / "lab_data"
    lab_data.mkdir()
    write_lab_json(
        lab_data / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "host1",
                "labs": ["test_lab"],
                "creds": [{"login": "admin", "password": PLANTED}],
            }
        ],
    )
    project = make_project(
        tmp_path / "sut",
        name="acme",
        tests=["suite"],
        extra='[[lab.sources]]\nbackend = "json"\npaths = ["../lab_data"]',
        files={"suite/test_secret_paths.py": _SUITE},
    )
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    env["OTTOPATH"] = str(project)
    env["OTTO_HOME"] = str(tmp_path / "home")
    env["OTTO_XDIR"] = str(tmp_path / "xdir")
    env["ACME_PLANTED_PASSWORD"] = PLANTED
    env["ACME_OTHER_PASSWORD"] = "the-right-one"
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(CHECKOUT_ROOT), env.get("PYTHONPATH")) if p)
    proc = subprocess.run(
        [*_ENTRY, "--lab", "test_lab", "--log-level", "DEBUG", "test", *_TESTS],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=_RUN_TIMEOUT,
    )
    logs = sorted((tmp_path / "xdir").glob("*/*/verbose.log"))
    assert len(logs) == 1, f"expected one run directory\n{proc.stdout}\n{proc.stderr}"
    return proc, logs[0].parent


def test_no_artifact_of_an_elevating_suite_holds_the_password(run_dir):
    proc, out = run_dir
    junits = sorted(out.rglob("junit*.xml"))
    assert junits, f"no JUnit file under {out}: {sorted(p.name for p in out.rglob('*'))}"
    texts = {
        "console": proc.stdout + proc.stderr,
        "console.log": (out / "console.log").read_text(),
        "verbose.log": (out / "verbose.log").read_text(),
        **{p.name: p.read_text() for p in junits},
    }
    junit = "\n".join(p.read_text() for p in junits)

    # The paths ran: the elevation was answered, the refusal and the expect
    # report were written, and the DEBUG lines are there to be scanned.
    failures: dict[str, str] = {}
    for p in junits:
        for case in ET.parse(p).iter("testcase"):  # noqa: S314 — the JUnit file this run wrote
            failure = case.find("failure")
            if failure is not None:
                failures[case.get("name", "")] = (failure.get("message") or "") + (
                    failure.text or ""
                )
    assert sorted(failures) == [
        "test_a_refused_elevation",
        "test_elevated_run_then_a_failing_expect",
    ], junit
    assert "password refused for admin on dut" in failures["test_a_refused_elevation"], junit
    report = failures["test_elevated_run_then_a_failing_expect"]
    assert "Values:" in report, f"no expect report in the JUnit file:\n{junit}"
    assert "password = '***'" in report, report
    assert "cred = " in report, report
    assert "ctx = " in report, report
    # One answer per path: the elevation, the refused elevation's first try,
    # the caller's pair.
    assert texts["verbose.log"].count("expect matched") == 3, "a DEBUG expect line never ran"

    leaked = [name for name, text in texts.items() if PLANTED in text]
    assert leaked == [], f"the planted password is in {leaked}"
