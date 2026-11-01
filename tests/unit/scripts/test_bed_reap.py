"""Pins for scripts/bed_reap.py: what the reap removes, refuses, reports, and in what order.

Everything the reap would do to a host is a module function the tests replace
and record (``remove_tunnels``, ``cancel_timers``, ``clear_qdisc``, ...), and the
hygiene oracle's snapshots are scripted; the lease underneath is the real one,
in a root of this test's own, with the drain stubbed (an xdist sibling is a
"survivor" of this process's group and would hold the drain otherwise).
"""

import asyncio
import contextlib
import os
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import bed_lease, bed_reap
from tests._fixtures import bed_hygiene
from tests._fixtures.bed_hygiene import HygieneSnapshot, QdiscReads
from tests._fixtures.labdata import lab_data_path
from tests.unit.link.test_manage_impair import FILTER_SCOPED_ONE, QDISC_SCOPED_ONE

pytestmark = pytest.mark.interpreter_agnostic

LAB = lab_data_path("tech1")
ME = os.getpid()
CLEAN_ROOT = "qdisc noqueue 0: root refcnt 2"
FOREIGN_ROOT = "qdisc tbf 8002: root refcnt 2 rate 1Mbit burst 10Kb lat 50ms"
"""A handle that is not the kernel default and a discipline otto never places: foreign."""


@dataclass
class FakeHost:
    id: str
    os_type: str = "unix"
    hop: "str | None" = None
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeLab:
    hosts: dict


def _snap(**over) -> HygieneSnapshot:
    base = {
        "tunnel_procs": frozenset(),
        "impair_timers": frozenset(),
        "nc_listeners": frozenset(),
        "qdiscs": {"eth2": QdiscReads(CLEAN_ROOT)},
        "staging": frozenset(),
        "history_digest": "abc  -",
        "docker_containers": frozenset(),
        "docker_networks": frozenset(),
    }
    base.update(over)
    return HygieneSnapshot(**base)


class Doubles:
    """Every outward call recorded; the snapshots a host answers, in order."""

    def __init__(self) -> None:
        self.calls: list = []
        self.snapshots: dict[str, list] = {}
        self.unreachable: set[str] = set()
        self.docker_parents: list[str] = []
        self.builds: list = []
        self.leased: list = []
        self.health_status = 0
        self.timers_cancelled = 2
        self.tunnel_ids = ["t-1"]
        self.repair_failures: list[str] = []

    async def snapshot_host(self, host):
        if host.id in self.unreachable:
            raise ConnectionError(f"{host.id} is down")
        queue = self.snapshots.setdefault(host.id, [_snap()])
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def record(self, name):
        async def _call(*args):
            self.calls.append((name, *[getattr(a, "id", a) for a in args]))
            if name == "remove_tunnels":
                return list(args[1])
            if name == "cancel_timers":
                return self.timers_cancelled
            if name == "repair_links":
                return list(self.repair_failures)
            return None

        return _call


@pytest.fixture
def doubles(monkeypatch, tmp_path):
    d = Doubles()
    hygiene = types.SimpleNamespace(**{**vars(bed_hygiene), "snapshot_host": d.snapshot_host})
    monkeypatch.setattr(bed_reap, "_hygiene", lambda: hygiene)
    for name in (
        "remove_tunnels",
        "cancel_timers",
        "repair_links",
        "clear_qdisc",
        "kill_listeners",
        "remove_staging",
    ):
        monkeypatch.setattr(bed_reap, name, d.record(name))

    async def run_lab_health(*, restart_qemu):
        d.calls.append(("run_lab_health", restart_qemu))
        return d.health_status

    monkeypatch.setattr(bed_reap, "run_lab_health", run_lab_health)

    async def reap_docker(host):
        d.calls.append(("reap_docker", host.id))
        return ["container c1"]

    async def remove_labelled_images(host, label):
        d.calls.append(("remove_labelled_images", host.id, label))
        return ["sha256:img"]

    docker = types.SimpleNamespace(
        docker_parents=lambda hosts: list(d.docker_parents),
        reap_docker=reap_docker,
        remove_labelled_images=remove_labelled_images,
        E2E_IMAGE_LABEL="otto-e2e-699",
    )
    monkeypatch.setattr(bed_reap, "_docker", lambda: docker)

    @contextlib.contextmanager
    def lease_unix_host(candidates):
        d.leased.append(("enter", *candidates))
        try:
            yield candidates[0]
        finally:
            d.leased.append(("exit", *candidates))

    from tests._fixtures import _host_pool

    monkeypatch.setattr(_host_pool, "lease_unix_host", lease_unix_host)

    async def build_extension(build_dir, zver):
        d.calls.append(("build_extension", build_dir, zver))
        Path(build_dir, "zephyr").mkdir(parents=True)
        Path(build_dir, "zephyr", "x.stripped.llext").write_text("")

    builds = types.SimpleNamespace(
        declared_builds=lambda: list(d.builds), build_extension=build_extension
    )
    monkeypatch.setattr(bed_reap, "_builds", lambda: builds)
    monkeypatch.setattr(bed_lease, "drain", lambda record, budget: [])
    return d


@pytest.fixture
def lab(monkeypatch):
    hosts = {
        "test1": FakeHost("test1"),
        "test3": FakeHost("test3"),
        "hopped": FakeHost("hopped", hop="test1"),
        "zephyr": FakeHost("zephyr", os_type="zephyr"),
    }
    fake = FakeLab(hosts)
    monkeypatch.setattr(bed_lease, "load_labs", lambda lab_file, names: fake)
    return fake


@pytest.fixture
def cell(tmp_path):
    return bed_lease.Cell("tech1", LAB, ["unix"], [], tmp_path / "lease")


def _reap(cell, capsys, **kw) -> tuple[int, str]:
    status = bed_reap.reap(cell, ME, **kw)
    return status, capsys.readouterr().out


def _names(doubles) -> list[str]:
    return [c[0] for c in doubles.calls]


# --- the hosts ------------------------------------------------------------------


def test_unix_bed_hosts_are_the_lab_files_direct_ssh_hosts_that_loaded(lab):
    """lab_health.py's one rule over the lab file, kept to the hosts the loaded lab carries."""
    assert [h.id for h in bed_reap.unix_bed_hosts(lab, LAB)] == ["test1", "test3"]


# --- the lease --------------------------------------------------------------------


def test_a_bed_another_live_run_holds_is_refused_before_any_probe(
    cell, doubles, lab, monkeypatch, capsys
):
    monkeypatch.setattr(bed_lease, "acquire", lambda cell, pid, purpose: bed_lease.EXIT_HELD)
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "held by another live run; no reap" in out
    assert doubles.calls == []


def test_this_runs_own_survivors_refuse_the_reap(cell, doubles, lab, monkeypatch, capsys):
    monkeypatch.setattr(bed_lease, "drain", lambda record, budget: [4242, 4243])
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "2 process(es) running (pids 4242 4243); no reap" in out
    assert doubles.calls == []


def test_a_lease_that_changed_hands_during_the_drain_is_a_refusal(
    cell, doubles, lab, monkeypatch, capsys
):
    other = bed_lease.Record(
        owner="x@y",
        user="x",
        pid=1,
        pid_started=1,
        groups=[],
        checkout="/x",
        since="2026-01-01T00:00:00+00:00",
        expires="2026-01-01T04:00:00+00:00",
        purpose="",
        resources=[],
    )
    monkeypatch.setattr(bed_lease, "drained_run", lambda cell, record, budget: (other, []))
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "changed hands under the reap; no reap" in out
    assert doubles.calls == []


def test_the_reap_acquires_for_the_run_and_leaves_the_lease_held(cell, doubles, lab, capsys):
    """The lane's release follows; the reap itself never frees the bed."""
    status, _ = _reap(cell, capsys)
    assert status == 0
    record = bed_lease.read_record(cell)
    assert record is not None
    assert record.pid == ME
    assert record.purpose == bed_reap.PURPOSE


# --- what is removed ----------------------------------------------------------------


def test_a_clean_bed_is_reported_clean_and_nothing_is_run(cell, doubles, lab, capsys):
    status, out = _reap(cell, capsys)
    assert status == 0
    assert out.strip().splitlines()[-1] == "bed-reap: clean"
    assert doubles.calls == []
    assert all(h.closed for h in bed_reap.unix_bed_hosts(lab, LAB))


def test_every_kind_of_leftover_is_removed_through_its_own_remover(cell, doubles, lab, capsys):
    dirty = _snap(
        tunnel_procs=frozenset({"100 t-1", "101 t-1", "102 t-2"}),
        impair_timers=frozenset({"200 edge eth1.100"}),
        nc_listeners=frozenset({"300 nc -l -w 30 9000", "301 nc -l 9001"}),
        staging=frozenset({"/tmp/otto-abc", "/tmp/otto_old"}),
        qdiscs={
            "eth1.100": QdiscReads(QDISC_SCOPED_ONE, FILTER_SCOPED_ONE),
            "eth2": QdiscReads(""),
        },
    )
    doubles.snapshots["test1"] = [dirty, _snap()]
    status, out = _reap(cell, capsys)
    assert status == 0, out
    assert doubles.calls == [
        ("remove_tunnels", lab, ["t-1", "t-2"]),
        ("cancel_timers", "test1"),
        ("kill_listeners", "test1", [300, 301]),
        ("remove_staging", "test1", ["/tmp/otto-abc", "/tmp/otto_old"]),
        ("clear_qdisc", "test1", "eth1.100"),
        ("repair_links", lab),
    ]
    for line in (
        "removed test1: otto-tunnel t-1",
        "removed test1: otto-tunnel t-2",
        "removed test1: 2 otto-impair timer(s)",
        "removed test1: nc listener pid 300",
        "removed test1: staging entry /tmp/otto_old",
        "removed test1: otto qdisc on eth1.100 (scoped)",
        "clean",
    ):
        assert f"bed-reap: {line}" in out, (line, out)


def test_a_foreign_qdisc_is_kept_and_keeps_the_bed_dirty(cell, doubles, lab, capsys):
    foreign = _snap(qdiscs={"eth2": QdiscReads(FOREIGN_ROOT)})
    doubles.snapshots["test3"] = [foreign, foreign]
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "clear_qdisc" not in _names(doubles)
    assert "repair_links" not in _names(doubles)
    assert "test3 still carries 1 leftover(s)" in out
    assert "foreign qdisc (not otto's, kept)" in out
    assert "bed-reap: clean" not in out


def test_what_survives_its_removal_is_the_verdict(cell, doubles, lab, capsys):
    stuck = _snap(nc_listeners=frozenset({"300 nc -l 9000"}))
    doubles.snapshots["test1"] = [stuck, stuck]
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "removed test1: nc listener pid 300" in out
    assert "test1 still carries 1 leftover(s):\n  nc listener: 300 nc -l 9000" in out


def test_a_repair_failure_is_reported_as_failed(cell, doubles, lab, capsys):
    doubles.snapshots["test1"] = [_snap(impair_timers=frozenset({"200 edge eth1.100"})), _snap()]
    doubles.repair_failures = ["edge: clear did not take on test1"]
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "bed-reap: failed edge: clear did not take on test1" in out


def test_an_unreachable_host_is_reported_and_the_others_are_reaped(cell, doubles, lab, capsys):
    doubles.unreachable = {"test1"}
    doubles.snapshots["test3"] = [_snap(staging=frozenset({"/tmp/otto-x"})), _snap()]
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "bed-reap: unreachable test1: test1 is down" in out
    assert ("remove_staging", "test3", ["/tmp/otto-x"]) in doubles.calls
    assert "test1 still carries" not in out, "no after-snapshot of a host never reached"


def test_a_remover_that_raises_is_a_failure_and_the_host_is_still_judged(
    cell, doubles, lab, monkeypatch, capsys
):
    async def boom(host, pids):
        raise RuntimeError("kill: permission denied")

    monkeypatch.setattr(bed_reap, "kill_listeners", boom)
    stuck = _snap(nc_listeners=frozenset({"300 nc -l 9000"}))
    doubles.snapshots["test1"] = [stuck, stuck]
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "bed-reap: failed test1: kill: permission denied" in out
    assert "unreachable" not in out
    assert "test1 still carries 1 leftover(s)" in out, "the after-snapshot is still taken"


def test_the_verdict_is_taken_after_the_docker_and_product_steps(cell, doubles, lab, capsys):
    """An orphan stack the docker step removes is not residual: the after-snapshot comes last."""
    orphan = _snap(docker_containers=frozenset({"c1 unix-project1-e2e-1a2b-api-1"}))
    doubles.snapshots["test1"] = [orphan, orphan, _snap()]
    doubles.docker_parents = ["test1"]
    calls = doubles.calls

    async def reap_docker(host):
        calls.append(("reap_docker", host.id))
        doubles.snapshots["test1"].pop(0)  # the stack is gone from the next read
        return ["container c1"]

    doubles_docker = bed_reap._docker()
    doubles_docker.reap_docker = reap_docker
    status, out = _reap(cell, capsys, full=True)
    assert status == 0, out
    assert [c[0] for c in calls] == ["reap_docker", "remove_labelled_images", "run_lab_health"]
    assert "still carries" not in out


def test_a_failing_builds_or_parents_lookup_is_a_failure_not_a_crash(
    cell, doubles, lab, monkeypatch, capsys
):
    def boom():
        raise RuntimeError("no embedded product")

    monkeypatch.setattr(bed_reap._builds(), "declared_builds", boom)
    monkeypatch.setattr(
        bed_reap._docker(), "docker_parents", lambda hosts: (_ for _ in ()).throw(KeyError("x"))
    )
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "bed-reap: failed product builds: no embedded product" in out
    assert "bed-reap: failed docker parents: 'x'" in out


# --- docker, the product, the consoles ---------------------------------------------


def test_docker_is_reaped_on_the_parents_under_their_own_pool_lease(cell, doubles, lab, capsys):
    doubles.docker_parents = ["test1", "test3"]
    status, out = _reap(cell, capsys)
    assert status == 0, out
    assert doubles.leased == [
        ("enter", "test1"),
        ("exit", "test1"),
        ("enter", "test3"),
        ("exit", "test3"),
    ]
    assert ("reap_docker", "test1") in doubles.calls
    assert ("remove_labelled_images", "test3", "otto-e2e-699") in doubles.calls
    assert "removed test1: docker container c1" in out
    assert "removed test3: docker image sha256:img" in out


def test_a_docker_failure_names_the_parent_and_the_reap_goes_on(
    cell, doubles, lab, monkeypatch, capsys
):
    async def boom(host):
        raise RuntimeError("daemon not running")

    doubles.docker_parents = ["test1"]
    monkeypatch.setattr(bed_reap._docker(), "reap_docker", boom)
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "bed-reap: failed test1: docker: daemon not running" in out
    assert doubles.leased[-1] == ("exit", "test1")


def test_a_declared_build_whose_artifact_is_missing_is_rebuilt_from_scratch(
    cell, doubles, lab, tmp_path, capsys
):
    half = tmp_path / "build-3.7"
    (half / "zephyr").mkdir(parents=True)
    (half / "zephyr" / "stale.o").write_text("")
    whole = tmp_path / "build-4.4"
    (whole / "zephyr").mkdir(parents=True)
    (whole / "zephyr" / "x.stripped.llext").write_text("")
    doubles.builds = [
        types.SimpleNamespace(
            os_version="3.7",
            build_dir=str(half),
            zver="v3_7",
            artifact=half / "zephyr/x.stripped.llext",
        ),
        types.SimpleNamespace(
            os_version="4.4",
            build_dir=str(whole),
            zver="v4_4",
            artifact=whole / "zephyr/x.stripped.llext",
        ),
    ]
    status, out = _reap(cell, capsys)
    assert status == 0, out
    assert [c for c in doubles.calls if c[0] == "build_extension"] == [
        ("build_extension", str(half), "v3_7")
    ]
    assert not (half / "zephyr" / "stale.o").exists()
    assert f"removed product build dir {half} (artifact missing)" in out
    assert f"removed product 3.7 rebuilt into {half}" in out


def test_a_rebuild_that_fails_is_reported_as_failed(
    cell, doubles, lab, monkeypatch, tmp_path, capsys
):
    async def boom(build_dir, zver):
        raise RuntimeError("west: no toolchain")

    monkeypatch.setattr(bed_reap._builds(), "build_extension", boom)
    doubles.builds = [
        types.SimpleNamespace(
            os_version="3.7", build_dir=str(tmp_path / "b"), zver="v3_7", artifact=tmp_path / "b/x"
        )
    ]
    status, out = _reap(cell, capsys)
    assert status == bed_reap.EXIT_DIRTY
    assert "bed-reap: failed product 3.7: west: no toolchain" in out


def test_the_console_step_runs_only_with_full_and_restart_qemu_passes_through(
    cell, doubles, lab, capsys
):
    _reap(cell, capsys)
    assert "run_lab_health" not in _names(doubles)
    _reap(cell, capsys, full=True)
    assert doubles.calls[-1] == ("run_lab_health", False)
    _reap(cell, capsys, full=True, restart_qemu=True)
    assert doubles.calls[-1] == ("run_lab_health", True)


def test_an_unhealthy_console_step_is_a_failure(cell, doubles, lab, capsys):
    doubles.health_status = 1
    status, out = _reap(cell, capsys, full=True)
    assert status == bed_reap.EXIT_DIRTY
    assert "bed-reap: failed the console step reported an unhealthy bed" in out


def test_run_lab_health_runs_the_makefiles_console_targets_in_order(monkeypatch):
    """The console lines live in the Makefile: the reap runs its targets, a red one stops it."""
    seen = []
    statuses = {"qemu-restart": 0, "console-logout": 0}

    class _Proc:
        def __init__(self, target):
            self.target = target

        async def wait(self):
            return statuses[self.target]

    async def spawn(*argv, cwd):
        assert argv[:2] == ("make", "--no-print-directory")
        assert cwd == bed_reap.REPO_ROOT
        seen.append(argv[2])
        return _Proc(argv[2])

    monkeypatch.setattr(bed_reap.asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(bed_reap.run_lab_health(restart_qemu=False)) == 0
    assert seen == ["console-logout"]
    seen.clear()
    assert asyncio.run(bed_reap.run_lab_health(restart_qemu=True)) == 0
    assert seen == ["qemu-restart", "console-logout"]
    seen.clear()
    statuses["qemu-restart"] = 2
    assert asyncio.run(bed_reap.run_lab_health(restart_qemu=True)) == 2
    assert seen == ["qemu-restart"], "a failed restart stops before the logout"


# --- the CLI --------------------------------------------------------------------


def _main(root: Path, *argv: str) -> int:
    saved = os.environ.get(bed_lease.ROOT_ENV)
    os.environ[bed_lease.ROOT_ENV] = str(root)
    try:
        return bed_reap.main(["--lab", str(LAB), *argv])
    finally:
        if saved is None:
            del os.environ[bed_lease.ROOT_ENV]
        else:
            os.environ[bed_lease.ROOT_ENV] = saved


def test_a_unit_only_quick_reaps_nothing(tmp_path, doubles, lab, capsys):
    status = _main(
        tmp_path, "--pid", str(ME), "--unless-all-under", "tests/unit", "--", "tests/unit/x.py"
    )
    assert status == 0
    assert "no bed path in PATHS, no reap" in capsys.readouterr().out
    assert doubles.calls == []
    assert not (tmp_path / "tech1.json").exists()


def test_restart_qemu_without_full_is_a_usage_error(tmp_path, doubles, lab, capsys):
    assert _main(tmp_path, "--pid", str(ME), "--restart-qemu") == bed_lease.EXIT_USAGE
    assert "--restart-qemu needs --full" in capsys.readouterr().err


def test_the_reap_shares_the_lease_verbs_argument_shape(tmp_path, doubles, lab, capsys):
    """One parser builder for the cell and the run: a stray PATHS is the lease's usage error."""
    assert _main(tmp_path, "--pid", str(ME), "tests/unit/x.py") == bed_lease.EXIT_USAGE
    assert "PATHS are only read after --unless-all-under" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _main(tmp_path)  # --pid is required, as the lease's acquire and release require it


def test_a_pid_that_does_not_exist_is_a_usage_error(tmp_path, doubles, lab, capsys):
    proc_free = 2**22 - 1
    assert _main(tmp_path, "--pid", str(proc_free)) == bed_lease.EXIT_USAGE


def test_main_reaps_through_the_cell_the_lab_file_names(
    tmp_path, doubles, lab, monkeypatch, capsys
):
    monkeypatch.setattr(
        bed_lease,
        "load_cell",
        lambda lab_file, root, **kw: bed_lease.Cell("tech1", lab_file, ["unix"], [], root),
    )
    assert _main(tmp_path, "--pid", str(ME), "--full") == 0
    assert doubles.calls[-1] == ("run_lab_health", False)
    assert (tmp_path / "tech1.json").exists(), "the lease the lane releases after"
