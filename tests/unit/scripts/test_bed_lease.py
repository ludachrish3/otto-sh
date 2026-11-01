"""Pins for scripts/bed_lease.py: one holder per bed cell, dead runs taken over, otto reads it.

The holder in most tests is a real child process in a session of its own (a
run this test process is NOT part of), and the asker is this process: a
child of this process would be a descendant of a holder that is this
process, which is the re-entrant case, tested on its own.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from otto.reservations.identity import resolve_username
from otto.reservations.json_backend import JsonReservationBackend
from otto.utils import wait_for
from scripts import bed_lease
from scripts.process_group import process_state, signal_group
from tests._fixtures.labdata import lab_data_path
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.project_scaffold import make_project

pytestmark = pytest.mark.interpreter_agnostic

LAB = lab_data_path("tech1")
ME = os.getpid()


def _run(root: Path, *argv: str, env: "dict[str, str] | None" = None) -> int:
    """Call the script's ``main`` in-process against *root*; return its exit status."""
    full = {"BED_LEASE_ROOT": str(root), "BED_LEASE_DRAIN": "1", **(env or {})}
    saved = {k: os.environ.get(k) for k in full}
    os.environ.update(full)
    try:
        return bed_lease.main(["--lab", str(LAB), *argv])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _record(root: Path) -> dict:
    return json.loads((root / "tech1.json").read_text())


def _rendered(root: Path) -> dict:
    return json.loads((root / "tech1.reservations.json").read_text())


def _write_record(root: Path, record: dict) -> None:
    (root / "tech1.json").write_text(json.dumps(record))


def _sleeper(seconds: float, *, new_session: bool = True) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        start_new_session=new_session,
    )


def _gone_pid() -> int:
    proc = _sleeper(0, new_session=False)
    proc.wait()
    return proc.pid


def _orphan_sleeper(seconds: float) -> int:
    """Start a sleeper nobody here will reap: a grandchild, its parent gone, in its own session.

    A child this process spawns and never waits for would linger as a zombie, which is
    "gone" to the lease but still a member of its group; a run that really ended has
    its processes reaped by init, which is what the double fork reproduces.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, sys, time\n"
                "pid = os.fork()\n"
                "if pid:\n"
                "    print(pid, flush=True)\n"
                "    sys.exit(0)\n"
                f"time.sleep({seconds})\n"
            ),
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    pid = int(proc.stdout.readline())
    proc.stdout.close()
    proc.wait()
    return pid


SCRIPT = f"{sys.executable} {CHECKOUT_ROOT / 'scripts' / 'bed_lease.py'} --lab {LAB}"


def _run_shell(root: Path, body: str) -> "subprocess.CompletedProcess[str]":
    """Run *body* under ``sh`` in a session of its own, the shell being the holder (``$$``).

    The recipe shape: the lease script runs as a descendant of the holder, so the
    holder's group holds nothing but the script's own ancestry once the lane is done.
    """
    env = {**os.environ, "BED_LEASE_ROOT": str(root), "BED_LEASE_DRAIN": "1"}
    return subprocess.run(
        ["sh", "-c", body],
        start_new_session=True,
        cwd=CHECKOUT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lease"


@pytest.fixture
def holder():
    """A run of its own holding the lease for 30 s (killed at teardown)."""
    proc = _sleeper(30)
    yield proc
    proc.kill()
    proc.wait()


# --- acquire -------------------------------------------------------------


def test_acquire_on_a_free_cell_writes_both_files_with_the_declared_modes(root, capsys):
    assert _run(root, "acquire", "--pid", str(ME), "--purpose", "t") == 0
    assert "taken" in capsys.readouterr().out
    assert root.stat().st_mode & 0o777 == 0o777
    for name in ("tech1.json", "tech1.reservations.json", ".lock"):
        assert (root / name).stat().st_mode & 0o777 == 0o666, name
    rec = _record(root)
    assert rec["pid"] == ME
    assert rec["pid_started"] == bed_lease.process_start(ME)
    assert rec["purpose"] == "t"
    assert rec["user"] == resolve_username(None).username
    assert rec["owner"].startswith(resolve_username(None).username + "@")
    assert rec["checkout"]
    assert rec["since"] < rec["expires"]
    assert rec["groups"] == [{"pgid": os.getpgid(0), "pid": ME, "owner": bed_lease.group_owner(ME)}]
    assert rec["resources"] == sorted(rec["resources"])


def test_the_rendered_file_is_otto_format_with_every_resolved_resource(root):
    _run(root, "acquire", "--pid", str(ME))
    rendered = _rendered(root)
    assert rendered["version"] == 1
    [entry] = rendered["reservations"]
    assert entry["user"] == resolve_username(None).username
    assert "expires" not in entry, "the backend drops an expired entry; the lease must not"
    backend = JsonReservationBackend(path=root / "tech1.reservations.json")
    for resource in ("test1-chassis", "test2-console", "test1", "zephyr37_llext", "bb1350"):
        assert [h.user for h in backend.holders(resource)] == [entry["user"]], resource
    assert entry["resources"] == _record(root)["resources"]


def test_a_second_acquire_from_another_run_is_refused_naming_the_holder(root, holder, capsys):
    _run(root, "acquire", "--pid", str(holder.pid), "--purpose", "first")
    assert _run(root, "acquire", "--pid", str(ME)) == 1
    out = capsys.readouterr().out
    assert "held by" in out
    assert "first" in out
    assert "holder running" in out
    assert _record(root)["pid"] == holder.pid, "the record is untouched"


def test_a_waiting_acquire_takes_a_lease_whose_holder_dies(root, capsys):
    short = _orphan_sleeper(2)
    _run(root, "acquire", "--pid", str(short), "--purpose", "short")
    started = time.monotonic()
    assert _run(root, "acquire", "--pid", str(ME), env={"BED_LEASE_WAIT": "20"}) == 0
    assert time.monotonic() - started < 15
    out = capsys.readouterr().out
    assert "taken over" in out
    assert "short" in out
    assert "dead" in out
    assert _record(root)["pid"] == ME


def test_a_zombie_holder_is_dead_even_with_its_own_group_recorded(root, capsys):
    """An exited holder nobody reaped is not running, and its group (itself alone) is empty.

    The kernel still lists a zombie as a group member; the lease reads "alive" as
    running, for the holder and its groups alike, else a launcher that never waits
    would hold the bed through its dead child.
    """
    zombie = _sleeper(0)
    wait_for(lambda: process_state(zombie.pid) == "Z", 10.0, on_timeout="never a zombie")
    _run(root, "acquire", "--pid", str(ME))
    rec = _record(root)
    rec["pid"], rec["pid_started"] = zombie.pid, bed_lease.process_start(zombie.pid)
    rec["groups"] = [{"pgid": zombie.pid, "pid": zombie.pid, "owner": "holder"}]
    _write_record(root, rec)
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert "dead" in out
    assert f"group {zombie.pid} (holder, registered by pid {zombie.pid}): gone" in out
    zombie.wait()


def test_a_waiting_acquire_gives_up_at_its_budget_and_says_held_once(
    root, holder, capsys, monkeypatch
):
    _run(root, "acquire", "--pid", str(holder.pid))
    capsys.readouterr()
    monkeypatch.setattr(bed_lease, "POLL_S", 0.1)  # ~10 attempts in the budget below
    assert _run(root, "acquire", "--pid", str(ME), env={"BED_LEASE_WAIT": "1"}) == 1
    out = capsys.readouterr().out
    assert out.count("is held by") == 1, out


def test_a_bad_wait_or_ttl_is_a_usage_error(root):
    assert _run(root, "acquire", "--pid", str(ME), env={"BED_LEASE_WAIT": "soon"}) == 2
    assert _run(root, "acquire", "--pid", str(ME), env={"BED_LEASE_TTL": "-1"}) == 2
    assert not root.exists()


def test_a_pid_that_does_not_exist_is_a_usage_error(root):
    assert _run(root, "acquire", "--pid", str(_gone_pid())) == 2


def test_the_rendered_user_is_the_holders_whoever_runs_a_verb(root, holder, capsys):
    """Another user's `bed-status` (or refused acquire) must not rewrite the lease as theirs."""
    _run(root, "acquire", "--pid", str(ME))
    rec = _record(root)
    rec["user"] = "someone-else"
    _write_record(root, rec)
    assert _run(root, "status") == 0
    assert _rendered(root)["reservations"][0]["user"] == "someone-else"
    assert _run(root, "release", "--pid", str(holder.pid)) == 0  # a non-holder's release
    assert _rendered(root)["reservations"][0]["user"] == "someone-else"
    capsys.readouterr()


def test_a_record_with_unknown_keys_is_still_this_lease(root, capsys):
    """A newer branch's record (an extra field) reads on an older one sharing the root."""
    _run(root, "acquire", "--pid", str(ME))
    rec = _record(root)
    rec["from_the_future"] = 1
    _write_record(root, rec)
    assert _run(root, "status") == 0
    assert "held by" in capsys.readouterr().out


def test_force_release_clears_a_record_it_cannot_read(root, capsys):
    """Garbage in the record jams every verb (exit 2); the documented eviction still works."""
    _run(root, "acquire", "--pid", str(ME))
    (root / "tech1.json").write_text("{not json")
    assert _run(root, "status") == 2
    assert _run(root, "release", "--pid", str(ME)) == 2
    assert _run(root, "release", "--pid", str(ME), "--force") == 0
    assert "unreadable record" in capsys.readouterr().out
    assert not (root / "tech1.json").exists()
    assert _rendered(root) == {"version": 1, "reservations": []}


def test_release_and_status_do_not_load_the_labs(root, tmp_path, capsys):
    """A lab that fails to load must not turn a green lane's release into a red exit."""
    cell = tmp_path / "tech1" / "lab.json"
    cell.parent.mkdir()
    cell.write_text('{"labs": {"nope": {}}}')
    argv = ["--lab", str(cell), "--root", str(root)]
    assert bed_lease.main([*argv, "status"]) == 0
    assert "free" in capsys.readouterr().out
    assert bed_lease.main([*argv, "release", "--pid", str(ME)]) == 0
    assert bed_lease.main([*argv, "acquire", "--pid", str(ME)]) == 2


def test_a_symlinked_root_is_refused(root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere)
    assert _run(root, "status") == 2


def test_stray_paths_without_the_classifier_are_a_usage_error(root):
    assert _run(root, "acquire", "--pid", str(ME), "tests/unit") == 2
    assert not (root / "tech1.json").exists()


def test_an_unloadable_lab_is_a_usage_error(root, tmp_path):
    bad = tmp_path / "nolab" / "lab.json"
    bad.parent.mkdir()
    bad.write_text("{}")
    assert bed_lease.main(["--lab", str(bad), "--root", str(root), "status"]) == 2


# --- re-entrancy ---------------------------------------------------------


def test_a_descendant_of_the_holder_re_enters_and_registers_a_new_group(root, capsys):
    _run(root, "acquire", "--pid", str(ME))
    before = _record(root)
    same_group = _sleeper(30, new_session=False)
    own_session = _sleeper(30)
    try:
        assert _run(root, "acquire", "--pid", str(same_group.pid)) == 0
        assert "held by this run" in capsys.readouterr().out
        assert _record(root)["groups"] == before["groups"], "same group: nothing new"
        assert _run(root, "acquire", "--pid", str(own_session.pid)) == 0
        rec = _record(root)
        assert rec["pid"] == ME
        assert rec["groups"][-1] == {
            "pgid": own_session.pid,
            "pid": own_session.pid,
            "owner": "holder",
        }
        assert {k: v for k, v in rec.items() if k != "groups"} == {
            k: v for k, v in before.items() if k != "groups"
        }
    finally:
        for proc in (same_group, own_session):
            proc.kill()
            proc.wait()


def test_a_reused_pid_is_not_the_holder(root, capsys):
    _run(root, "acquire", "--pid", str(ME))
    rec = _record(root)
    rec["pid_started"] -= 1  # the record names "an earlier process with this pid"
    rec["groups"] = []
    _write_record(root, rec)
    child = _sleeper(30, new_session=False)
    try:
        assert _run(root, "acquire", "--pid", str(child.pid)) == 0
        out = capsys.readouterr().out
        assert "taken over" in out
        assert "held by this run" not in out
        assert _record(root)["pid"] == child.pid
    finally:
        child.kill()
        child.wait()


# --- liveness by group ---------------------------------------------------


def _dead_record(root: Path, *, groups: list) -> None:
    """Leave a record whose holder pid is gone, with *groups* as its recorded groups."""
    _run(root, "acquire", "--pid", str(ME), "--purpose", "old")
    rec = _record(root)
    rec["pid"] = _gone_pid()
    rec["pid_started"] = 1
    rec["groups"] = groups
    _write_record(root, rec)


def test_a_dead_holder_with_no_survivor_is_taken_over(root, capsys):
    _dead_record(root, groups=[])
    assert _run(root, "acquire", "--pid", str(ME)) == 0
    out = capsys.readouterr().out
    assert "taken over" in out
    assert "old" in out
    assert "dead" in out


def test_a_dead_holder_with_a_surviving_group_is_held(root, capsys):
    survivor = _sleeper(30)
    _dead_record(root, groups=[{"pgid": survivor.pid, "pid": survivor.pid, "owner": "holder"}])
    assert _run(root, "acquire", "--pid", str(ME)) == 1
    assert "holder pid gone, 1 group(s) still running" in capsys.readouterr().out
    assert _run(root, "status") == 0
    assert "group(s) still running" in capsys.readouterr().out
    signal_group(survivor.pid, signal.SIGKILL)
    survivor.wait()
    assert _run(root, "acquire", "--pid", str(ME)) == 0
    assert "taken over" in capsys.readouterr().out


def test_a_foreign_group_counts_as_live_and_is_reported(root, capsys):
    _dead_record(root, groups=[{"pgid": os.getpgid(0), "pid": ME + 1, "owner": "foreign"}])
    assert _run(root, "acquire", "--pid", str(ME)) == 1
    out = capsys.readouterr().out
    assert "foreign" in out
    assert "launcher" in out


def test_group_owner_classifies_holder_ancestor_and_foreign():
    leader = _sleeper(30)
    grandchild = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "print(p.pid, flush=True); p.wait()"
            ),
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    inherited = _sleeper(30, new_session=False)
    try:
        assert grandchild.stdout is not None
        inner = int(grandchild.stdout.readline())
        assert bed_lease.group_owner(leader.pid) == "holder"
        assert bed_lease.group_owner(inner) == "ancestor"
        # This process's group is led by whatever launched pytest (or by this
        # process itself under a job-control shell): the child's group is a
        # launcher's unless that leader is in its ancestry.
        expected = "ancestor" if os.getpgid(0) in bed_lease.ancestors(inherited.pid) else "foreign"
        assert bed_lease.group_owner(inherited.pid) == expected
    finally:
        signal_group(grandchild.pid, signal.SIGKILL)
        grandchild.wait()
        if grandchild.stdout is not None:
            grandchild.stdout.close()
        for proc in (leader, inherited):
            proc.kill()
            proc.wait()


def test_an_expired_lease_with_a_live_holder_is_held_and_still_read_by_otto(root, holder, capsys):
    _run(root, "acquire", "--pid", str(holder.pid), env={"BED_LEASE_TTL": "0"})
    time.sleep(1.1)
    assert _run(root, "acquire", "--pid", str(ME)) == 1
    assert "overran its budget" in capsys.readouterr().out
    backend = JsonReservationBackend(path=root / "tech1.reservations.json")
    assert backend.holders("test1")


# --- release -------------------------------------------------------------


def test_release_by_a_non_holder_keeps_the_lease(root, holder, capsys):
    _run(root, "acquire", "--pid", str(holder.pid))
    assert _run(root, "release", "--pid", str(ME)) == 0
    assert "kept" in capsys.readouterr().out
    assert (root / "tech1.json").exists()


def test_release_by_the_holder_frees_the_cell_and_renders_the_empty_file(root):
    """The recipe shape: the shell is the holder, the script its descendant."""
    done = _run_shell(
        root, f"{SCRIPT} acquire --pid $$ --purpose t && {SCRIPT} release --pid $$ && echo DONE"
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "released by pid" in done.stdout, done.stdout
    assert done.stdout.rstrip().endswith("DONE")
    assert not (root / "tech1.json").exists()
    assert _rendered(root) == {"version": 1, "reservations": []}


def test_release_of_a_free_cell_is_fine(root, capsys):
    assert _run(root, "release", "--pid", str(ME)) == 0
    assert "free" in capsys.readouterr().out
    assert _run(root, "status") == 0
    assert "free" in capsys.readouterr().out


def test_release_keeps_the_lease_while_the_run_has_survivors(root, capsys):
    """A worker the lane left behind keeps the lease past the drain; `--force` evicts."""
    done = _run_shell(
        root,
        f"{SCRIPT} acquire --pid $$ && {{ sleep 30 >/dev/null 2>&1 & }}; "
        f"{SCRIPT} release --pid $$; echo DONE",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "kept: 1 process(es) of this run still running" in done.stdout, done.stdout
    assert (root / "tech1.json").exists()
    rec = _record(root)
    try:
        assert _run(root, "status") == 0
        assert "group(s) still running" in capsys.readouterr().out
        assert _run(root, "release", "--pid", str(ME), "--force") == 0
        assert "evicted" in capsys.readouterr().out
        assert not (root / "tech1.json").exists()
    finally:
        for group in rec["groups"]:
            signal_group(group["pgid"], signal.SIGKILL)


def test_force_release_evicts_and_names_the_holder(root, holder, capsys):
    _run(root, "acquire", "--pid", str(holder.pid), "--purpose", "theirs")
    assert _run(root, "release", "--pid", str(ME), "--force") == 0
    out = capsys.readouterr().out
    assert "evicted" in out
    assert "theirs" in out
    assert not (root / "tech1.json").exists()
    assert _rendered(root) == {"version": 1, "reservations": []}


def test_any_verb_repairs_a_missing_or_stale_rendered_file(root):
    _run(root, "acquire", "--pid", str(ME))
    (root / "tech1.reservations.json").unlink()
    assert _run(root, "status") == 0
    assert _rendered(root)["reservations"][0]["resources"]
    (root / "tech1.reservations.json").write_text('{"version": 1, "reservations": []}')
    assert _run(root, "acquire", "--pid", str(ME)) == 0
    assert _rendered(root)["reservations"], "re-entrant acquire re-rendered"


# --- quick's classifier --------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "needs"),
    [
        (["tests/unit/x"], False),
        (["./tests/unit/x", "tests/unit/../unit/y"], False),
        (["tests"], True),
        (["."], True),
        (["tests/unit/../e2e"], True),
        ([str(CHECKOUT_ROOT / "tests" / "integration")], True),
        (["tests/unit", "tests/integration/cov"], True),
        ([], True),  # no selection is the whole tree
    ],
)
def test_paths_need_bed_normalises_before_classifying(paths, needs):
    assert bed_lease.paths_need_bed(CHECKOUT_ROOT, "tests/unit", paths) is needs


def test_unless_all_under_skips_the_lease_for_a_unit_only_quick(root, capsys):
    unit_only = ["--unless-all-under", "tests/unit", "--", "tests/unit/scripts", "./tests/unit/x"]
    assert _run(root, "acquire", "--pid", str(ME), *unit_only) == 0
    assert "no bed path" in capsys.readouterr().out
    assert not root.exists()
    mixed = ["--unless-all-under", "tests/unit", "--", "tests/unit/scripts", "tests/e2e/cli"]
    assert _run(root, "acquire", "--pid", str(ME), *mixed) == 0
    assert (root / "tech1.json").exists()


# --- otto reads it -------------------------------------------------------


def _check(project: Path) -> "subprocess.CompletedProcess[str]":
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO")}
    env["OTTOPATH"] = str(project)
    return subprocess.run(
        [sys.executable, "-m", "otto", "--lab", "unix+busybox+embedded", "reservation", "check"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def project(tmp_path: Path, root: Path) -> Path:
    """A scratch project whose settings name the cell's lab directory and the rendered file."""
    lab_dir = LAB.parent
    return make_project(
        tmp_path / "proj",
        name="bedlease",
        version="0.0.0",
        extra=(
            f'libs = ["{CHECKOUT_ROOT / "tests" / "custom_hosts"}"]\n'
            'init = ["custom_hosts"]\n'
            "\n"
            "[[lab.sources]]\n"
            'backend = "json"\n'
            f'paths = ["{lab_dir}"]\n'
            "\n"
            "[reservations]\n"
            'backend = "json"\n'
            "\n"
            "[reservations.json]\n"
            f'path = "{root / "tech1.reservations.json"}"\n'
        ),
    )


@pytest.mark.timeout(300)
def test_otto_reservation_check_reports_the_lease(root, project):
    _run(root, "acquire", "--pid", str(ME))
    done = _check(project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "OK" in done.stdout, done.stdout
    assert done.stdout.count("yes") >= 10, done.stdout

    rendered = _rendered(root)
    rendered["reservations"][0]["user"] = "someone-else"
    (root / "tech1.reservations.json").write_text(json.dumps(rendered))
    done = _check(project)
    assert done.returncode != 0, done.stdout + done.stderr
    assert "someone-else" in done.stdout + done.stderr, done.stdout + done.stderr

    _run(root, "release", "--pid", str(ME), "--force")
    done = _check(project)
    assert done.returncode != 0
    assert "nobody" in done.stdout + done.stderr, done.stdout + done.stderr
