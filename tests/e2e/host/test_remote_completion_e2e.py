"""E2E: remote-path completion lists a real SSH host's directory.

Requires the live Vagrant bed (``vagrant up test1 test2 test3``). On
bed-unreachable these tests FAIL with a clear host-named error — they never
skip.

In-process (not a subprocess): the shell-completion plumbing is covered by
unit tests; what needs the bed is the exec-based listing itself. The completer
is therefore called directly with a ``SimpleNamespace`` context chain — its
walk is mock-defensive by design (see
:func:`otto.cli.remote_completion._collect_chain_params`). Host *mutation*
still goes through the product CLI (``otto host <id> run``), as a subprocess,
mirroring ``test_host_transfer_e2e.py``.

xdist group
-----------
Pinned to ``host_transfer_e2e`` alongside the transfer e2e so the two never
race for the same leased VM and subprocess-coverage finalisation stays on a
single worker.
"""

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from otto.config.home import workspace_key
from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import REPO1, run_otto

# ---------------------------------------------------------------------------
# Constants (mirroring tests/e2e/host/test_host_transfer_e2e.py)
# ---------------------------------------------------------------------------

# Lab that contains test1/test2/test3 (tech1 lab data).
_LAB = "unix"

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("host_transfer_e2e")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_on_host(host_id: str, command: str, xdir: Path) -> subprocess.CompletedProcess[str]:
    """Run *command* on *host_id* through the real ``otto`` entry-point.

    ``-R`` bypasses the (command-time) reservation gate, which is appropriate
    for automated e2e tests that hold no named reservation (both it and
    ``--lab`` are root options, so their relative order is immaterial). The
    environment — subprocess coverage plus the otto keys — comes from the
    shared ``run_otto`` harness.
    """
    return run_otto(
        ["host", host_id, "run", command],
        xdir=xdir,
        sut_dirs=REPO1,
        lab=_LAB,
        extra_argv_prefix=["-R"],
        timeout=180,
    )


def _must_run_on_host(host_id: str, command: str, xdir: Path) -> None:
    """Run *command* on *host_id*; FAIL (never skip) naming the host on any failure."""
    result = _run_on_host(host_id, command, xdir)
    assert result.returncode == 0, (
        f"Live bed unusable: ``otto host {host_id} run {command!r}`` exited "
        f"{result.returncode} (host {host_id!r}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _ctx(host_id: str, lab: str = _LAB, as_user: "str | None" = None, term: "str | None" = None):
    """Build the Click-context chain the completer walks (``SimpleNamespace`` is enough).

    *term* mirrors the real ``otto host <id> --term ssh`` override, which the
    completer honours through ``_apply_option_overrides`` exactly as
    ``resolve_cli_host`` does — see the listing test for why it must.
    """
    from types import SimpleNamespace

    root = SimpleNamespace(params={"labs": [lab], "as_user": as_user}, parent=None)
    group = SimpleNamespace(params={"host_id": host_id, "hop": "", "term": term}, parent=root)
    return SimpleNamespace(params={}, parent=group)


@pytest.fixture
def unix_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one Unix host from the pool; yield its host id (e.g. ``test1``)."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        yield element


# ---------------------------------------------------------------------------
# Test: live listing + sidecar cache
# ---------------------------------------------------------------------------


def test_remote_completion_lists_live_host(monkeypatch, tmp_path: Path, unix_host: str) -> None:
    """The completer must list a directory it just created on a real SSH host,
    then serve the *same* answer from the sidecar cache after the host changed.

    Three observations, one leased VM:

    1. a live ``ls`` over the host's exec seam returns both entries, the
       directory marked with a trailing ``/``;
    2. a second completion after the directory was removed on the host still
       answers from the cache (proving the sidecar was written and read);
    3. ``kind="dir"`` filters the cached view down to directories only.

    Every completion here goes through the ``--term ssh`` override, and must:
    the pool leases whichever of test1/test2/test3 is free, and
    ``test2``'s configured default term is ``telnet`` (``valid_terms =
    ['telnet', 'ssh']``). The completer is SSH-only by design — it lists over
    the host's exec seam — so a telnet-defaulted host completes to ``[]``
    immediately, and this test failed under ``make coverage`` for exactly that
    reason once the lease drew test2. The override is what a user typing
    ``otto host test2 --term ssh get <TAB>`` gets, so the test now takes
    the same path (and covers the override-copy branch of ``_load_host``,
    which no other e2e touched).
    """
    from otto.cli.remote_completion import remote_path_completer

    monkeypatch.setenv("OTTO_SUT_DIRS", str(REPO1))
    # The sidecar cache lives in the WORKSPACE HOME now, keyed by OTTO_SUT_DIRS
    # -- pinning OTTO_XDIR no longer relocates it, and without OTTO_HOME this
    # test would read and write the developer's real ~/.otto.
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "otto-home"))
    cache_file = tmp_path / "otto-home" / workspace_key([REPO1]) / "remote_completion_cache.json"

    nonce = f"otto-completion-{uuid.uuid4().hex[:8]}"
    remote_dir = f"/tmp/{nonce}"
    try:
        _must_run_on_host(
            unix_host, f"mkdir -p {remote_dir}/sub && touch {remote_dir}/file.txt", tmp_path
        )

        # --- 1. live listing ---
        # Retried, and ONLY here: the listing runs under a hard 2s product
        # deadline (a UX bound, unit-pinned elsewhere), and a genuinely slow
        # connect under full-suite load degrades by design to []. Pressing TAB
        # again is the real-world response to that, and the completer is
        # idempotent, so the test does the same. Belt only — the observed gate
        # failure was the telnet default above, not the deadline (measured
        # 0.2-0.4s per listing on this bed, idle and under parallel load). It
        # still fails loud, naming the host, if the listing never succeeds — no
        # skip, no widened product deadline. Later assertions ride the cache.
        started = time.monotonic()
        got: "list[str]" = []
        for attempt in range(3):
            if attempt:
                time.sleep(1.5)
            got = remote_path_completer(_ctx(unix_host, term="ssh"), f"{remote_dir}/")
            if got:
                break
        assert got == [f"{remote_dir}/file.txt", f"{remote_dir}/sub/"], (
            f"Live remote-path completion on host {unix_host!r} for {remote_dir!r} "
            f"returned {got!r} after 3 attempts; the completer fails closed to [] on "
            f"any error, so an empty list here means the exec-based listing did not "
            f"succeed (SSH-only; 2s listing deadline)."
        )
        assert cache_file.is_file(), (
            f"Expected the sidecar listing cache at {cache_file} after a live listing "
            f"on host {unix_host!r}"
        )
        assert remote_dir in json.loads(cache_file.read_text())["listings"][unix_host], (
            f"Expected {remote_dir!r} under listings[{unix_host!r}] in {cache_file}"
        )

        # --- 2. the cache, not the host, answers the second call ---
        _must_run_on_host(unix_host, f"rm -rf {remote_dir}/sub", tmp_path)
        again = remote_path_completer(_ctx(unix_host, term="ssh"), f"{remote_dir}/")
        elapsed = time.monotonic() - started
        assert again == got, (
            f"Second completion for {remote_dir!r} on host {unix_host!r} returned {again!r}, "
            f"not the cached {got!r} — the sidecar cache did not serve it "
            f"({elapsed:.1f}s since the first listing; the listing TTL is 45s)."
        )

        # --- 2b. a typed basename fragment filters the same view ---
        # Drives posixpath.split -> SplitPath(prefix="fi") against a real listing,
        # which the trailing-slash calls above never do (they all mean prefix="").
        by_prefix = remote_path_completer(_ctx(unix_host, term="ssh"), f"{remote_dir}/fi")
        assert by_prefix == [f"{remote_dir}/file.txt"], (
            f"Prefix completion for {remote_dir}/fi on host {unix_host!r} returned "
            f"{by_prefix!r}; only entries starting with 'fi' may be offered."
        )

        # --- 3. kind="dir" filters the same cached view ---
        _must_run_on_host(unix_host, f"mkdir -p {remote_dir}/sub", tmp_path)
        only_dirs = remote_path_completer(_ctx(unix_host, term="ssh"), f"{remote_dir}/", kind="dir")
        assert only_dirs == [f"{remote_dir}/sub/"], (
            f"kind='dir' completion for {remote_dir!r} on host {unix_host!r} returned "
            f"{only_dirs!r}; only the directory entry may be offered."
        )
    finally:
        _run_on_host(unix_host, f"rm -rf {remote_dir}", tmp_path)  # best-effort cleanup


# ---------------------------------------------------------------------------
# Test: the reservation gate refuses before any host contact
# ---------------------------------------------------------------------------


def _reservation_repo(root: Path, holder: str) -> Path:
    """Write a SUT repo whose lab's resources are booked to *holder* in a JSON file.

    Minimal ``.otto/settings.toml`` + a one-host ``unix`` lab + a
    ``version: 1`` reservation file, mirroring the fixture construction in
    ``tests/unit/reservations/test_wiring.py`` / ``test_json_backend.py``.
    """
    make_sut_repo(
        root,
        name="reservation_fixture",
        extra=(
            "[[lab.sources]]\n"
            'backend = "json"\n'
            'paths = ["lab_data"]\n'  # search paths anchor at the repo root
            "\n"
            "[reservations]\n"
            'backend = "json"\n'
            "\n"
            "[reservations.json]\n"
            'path = "reservations.json"\n'
        ),
        files={
            "lab_data/lab.json": json.dumps(
                {
                    "hosts": [
                        {
                            "ip": "10.10.200.11",
                            "element": "test1",
                            "os_type": "unix",
                            "valid_terms": ["ssh"],
                            "valid_transfers": ["scp"],
                            "is_virtual": True,
                            "creds": [{"login": "vagrant", "password": "vagrant"}],
                            "resources": ["test1"],
                            "labs": [_LAB],
                        }
                    ],
                    "links": [],
                }
            )
        },
    )
    _write_reservations(root, holder)
    return root


def _write_reservations(root: Path, holder: str) -> None:
    """(Re)write the repo's reservation file, booking ``test1`` to *holder*."""
    (root / "reservations.json").write_text(
        json.dumps({"version": 1, "reservations": [{"user": holder, "resources": ["test1"]}]})
    )


def test_completion_without_reservation_is_empty(monkeypatch, tmp_path: Path) -> None:
    """A lab whose resources are booked to somebody else must complete to ``[]``
    **without the host ever being loaded**, and the same fixture must let
    completion through once the booking names the effective user.

    The second half is the control: without it a malformed fixture (bad
    reservation path, unparsable lab) would produce the same ``[]`` through the
    completer's fail-closed catch-all and the test would pass for the wrong
    reason.
    """
    import otto.cli.remote_completion as rc
    from otto.reservations.identity import resolve_username

    repo = _reservation_repo(tmp_path / "repo", holder=f"someone-else-{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))

    touched: list[str] = []

    def _forbidden(chain):
        touched.append(chain.host_id)
        raise AssertionError("the completer must not load the host past a refused gate")

    monkeypatch.setattr(rc, "_load_host", _forbidden)

    # --- refused: booked to another user ---
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home_refused"))
    assert rc.remote_path_completer(_ctx("test1"), "/tmp/") == []
    assert touched == [], (
        "The reservation gate must refuse before the host is loaded — "
        f"_load_host was called for {touched!r}"
    )

    # --- control: the same fixture, booked to the effective user, gets past the gate ---
    _write_reservations(repo, resolve_username(None).username)
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home_allowed"))  # fresh cache
    assert rc.remote_path_completer(_ctx("test1"), "/tmp/") == []  # _load_host raises
    assert touched == ["test1"], (
        "With the lab's resources booked to the effective user the gate must allow "
        f"completion to proceed to host load; _load_host calls: {touched!r}"
    )
