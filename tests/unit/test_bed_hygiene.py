"""Contract tests for the BedHygiene diff — canned probe outputs, no bed.

The oracle's one hard rule: PRE-EXISTING DIRT IS SNAPSHOTTED OUT. A leftover
is a line present after that was absent before; something dirty going in must
never be blamed on the scenario (2026-07-21 misattribution class), and must
never mask a NEW leftover of the same kind either.
"""

import re
from dataclasses import dataclass, field

import pytest

from otto.link.netem import NetEmImpairer
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.bed_hygiene import (
    _DOCKER_IMAGES_PROBE,
    _DOCKER_NET_PROBE,
    _DOCKER_PS_PROBE,
    _NC_LISTENER_PROBE,
    _NETDEV_PROBE,
    HygieneSnapshot,
    QdiscReads,
    diff_snapshots,
    format_hygiene_report,
    format_residual_report,
    listener_pids,
    netdevs_listed,
    probe_lines,
    residual_dirt,
    snapshot_host,
    tunnel_ids,
)
from tests.unit.link.test_manage_impair import FILTER_SCOPED_ONE, QDISC_SCOPED_ONE

CLEAN_ROOT = "qdisc noqueue 0: root refcnt 2"
NETEM_ROOT = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms"
FOREIGN_ROOT = "qdisc tbf 8002: root refcnt 2 rate 1Mbit burst 10Kb lat 50ms"


def _snap(**over):
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


def test_identical_snapshots_diff_empty():
    assert diff_snapshots(_snap(), _snap()) == []


def test_new_listener_named_old_listener_ignored():
    before = _snap(nc_listeners=frozenset({"111 nc -l -w 30 9000"}))
    after = _snap(nc_listeners=frozenset({"111 nc -l -w 30 9000", "222 nc -l -w 30 9001"}))
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert "222" in leftovers[0]
    assert "111" not in leftovers[0]


def test_qdisc_change_reported_with_device_and_both_states():
    before = _snap()
    after = _snap(qdiscs={"eth2": QdiscReads("qdisc prio 1: root refcnt 2 bands 11")})
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert "eth2" in leftovers[0]
    assert "prio" in leftovers[0]


def test_a_filter_change_under_the_same_root_is_a_qdisc_change():
    """The scoped reads are the pair: a selector added under an unchanged root shows."""
    before = _snap(qdiscs={"eth2": QdiscReads(QDISC_SCOPED_ONE, "")})
    after = _snap(qdiscs={"eth2": QdiscReads(QDISC_SCOPED_ONE, FILTER_SCOPED_ONE)})
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert leftovers[0].startswith("eth2: qdisc changed")


def test_a_netdev_that_came_or_went_is_a_change_only_with_a_tree_on_it():
    """A veth or VLAN a scenario adds is the link layer's; a qdisc on it is ours to notice."""
    clean = QdiscReads(CLEAN_ROOT)
    assert diff_snapshots(_snap(), _snap(qdiscs={"eth2": clean, "veth9": clean})) == []
    assert diff_snapshots(_snap(qdiscs={"eth2": clean, "veth9": clean}), _snap()) == []
    assert diff_snapshots(_snap(), _snap(qdiscs={"eth2": clean, "veth9": QdiscReads("")})) == []
    added = diff_snapshots(_snap(), _snap(qdiscs={"eth2": clean, "veth9": QdiscReads(NETEM_ROOT)}))
    assert added == [f"veth9: qdisc changed: before=None after={QdiscReads(NETEM_ROOT)!r}"]
    for tree in (FOREIGN_ROOT, NETEM_ROOT):
        gone = diff_snapshots(_snap(qdiscs={"eth2": clean, "veth9": QdiscReads(tree)}), _snap())
        assert gone == [f"veth9: qdisc changed: before={QdiscReads(tree)!r} after=None"]
    scoped = QdiscReads(QDISC_SCOPED_ONE, FILTER_SCOPED_ONE)
    gone = diff_snapshots(_snap(qdiscs={"eth2": clean, "veth9": scoped}), _snap())
    assert gone == [f"veth9: qdisc changed: before={scoped!r} after=None"]


def test_a_new_e2e_image_is_reported_an_old_one_ignored():
    before = _snap(docker_images=frozenset({"sha256:old"}))
    after = _snap(docker_images=frozenset({"sha256:old", "sha256:new"}))
    assert diff_snapshots(before, after) == ["new docker image: sha256:new"]


def test_history_digest_change_reported():
    leftovers = diff_snapshots(_snap(), _snap(history_digest="def  -"))
    assert len(leftovers) == 1
    assert "history" in leftovers[0]


def test_history_digest_empty_both_sides_no_crash_and_clean():
    # "" only happens when the probe itself returned nothing (timed-out or
    # failed exec) — with nothing on either side there is nothing to compare,
    # so this must not raise and must not be reported as a leftover.
    assert diff_snapshots(_snap(history_digest=""), _snap(history_digest="")) == []


def test_history_digest_empty_after_side_reported_as_probe_failure_not_change():
    # after-probe returned nothing (e.g. host unreachable post-scenario): this
    # is a probe/read failure, not evidence the history changed — must not
    # crash on the old `.split()[0]` and must not say "digest changed".
    leftovers = diff_snapshots(_snap(history_digest="abc  -"), _snap(history_digest=""))
    assert len(leftovers) == 1
    assert "probe" in leftovers[0]
    assert "suppression leak" not in leftovers[0]


def test_history_digest_empty_before_side_reported_as_probe_failure_not_change():
    # Symmetric case: before-probe returned nothing, after has a real digest.
    leftovers = diff_snapshots(_snap(history_digest=""), _snap(history_digest="def  -"))
    assert len(leftovers) == 1
    assert "probe" in leftovers[0]
    assert "suppression leak" not in leftovers[0]


def test_report_names_host_and_lists_leftovers():
    report = format_hygiene_report("test2", ["eth2: qdisc changed", "new nc listener"])
    assert "test2" in report
    assert "eth2: qdisc changed" in report
    assert "new nc listener" in report


def test_diff_reports_new_docker_containers_only():
    before = _snap(docker_containers=frozenset({"old x"}))
    after = _snap(docker_containers=frozenset({"old x", "abc123 otto-project1-e2e-x-api-1"}))
    leftovers = diff_snapshots(before, after)
    joined = "\n".join(str(item) for item in leftovers)
    assert "abc123 otto-project1-e2e-x-api-1" in joined
    assert "old x" not in joined


def test_diff_reports_new_docker_networks_only():
    before = _snap(docker_networks=frozenset({"bridge"}))
    after = _snap(docker_networks=frozenset({"bridge", "otto-project1-e2e-x_default"}))
    leftovers = diff_snapshots(before, after)
    joined = "\n".join(str(item) for item in leftovers)
    assert "otto-project1-e2e-x_default" in joined
    assert "bridge" not in joined


# ---------------------------------------------------------------------------
# Snapshot-level tests: verify snapshot_host correctly runs probes and
# populates fields. Uses canned-output stub host.
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedHost:
    """Stub host returning canned output keyed by exact command string."""

    canned_outputs: dict[str, str] = field(default_factory=dict)

    async def exec(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        output = self.canned_outputs.get(cmd, "")
        return CommandResult(status=Status.Success, value=output, command=cmd)


@pytest.mark.asyncio
async def test_snapshot_host_captures_docker_containers_and_networks():
    host = _ScriptedHost(
        canned_outputs={
            _DOCKER_PS_PROBE: "abc123 otto-project1-e2e-x-api-1\ndef456 otto-project1-e2e-x-db-1",
            _DOCKER_NET_PROBE: "bridge\notto-project1-e2e-x_default",
        }
    )
    snap = await snapshot_host(host)
    assert snap.docker_containers == frozenset(
        {"abc123 otto-project1-e2e-x-api-1", "def456 otto-project1-e2e-x-db-1"}
    )
    assert snap.docker_networks == frozenset({"bridge", "otto-project1-e2e-x_default"})


@pytest.mark.parametrize(
    ("value", "lines"),
    [("", []), ("a", ["a"]), ("a\nb", ["a", "b"]), ("a\n", ["a", ""])],
)
def test_probe_lines_surfaces_a_stray_empty_line(value, lines):
    """A trailing newline in an exact value is a stray line, so it shows as ``""``."""
    assert probe_lines(value) == lines


@pytest.mark.asyncio
async def test_snapshot_host_keeps_a_stray_empty_line_visible():
    """A value ending in a newline has a stray empty line, and the snapshot must show it."""
    host = _ScriptedHost(canned_outputs={_DOCKER_NET_PROBE: "bridge\n"})
    snap = await snapshot_host(host)
    assert snap.docker_networks == frozenset({"bridge", ""})


@pytest.mark.asyncio
async def test_snapshot_host_reads_every_listed_netdev_as_a_qdisc_and_filter_pair():
    """A bare `tc qdisc show` names the netdevs; each is then read with the impairer's pair."""
    qdisc_cmd, filter_cmd = NetEmImpairer().scoped_read_commands(netdev="eth1.100")
    host = _ScriptedHost(
        canned_outputs={
            _NETDEV_PROBE: (
                "qdisc noqueue 0: dev lo root refcnt 2\n"
                f"{QDISC_SCOPED_ONE.splitlines()[0]} dev eth1.100\n"
                "qdisc netem 40: dev eth1.100 parent 1:4 limit 1000 delay 200ms\n"
            ),
            qdisc_cmd: QDISC_SCOPED_ONE,
            filter_cmd: FILTER_SCOPED_ONE,
            _DOCKER_IMAGES_PROBE: "sha256:a\n",
        }
    )
    snap = await snapshot_host(host)
    assert sorted(snap.qdiscs) == ["eth1.100", "lo"]
    assert snap.qdiscs["eth1.100"] == QdiscReads(QDISC_SCOPED_ONE, FILTER_SCOPED_ONE)
    assert snap.qdiscs["lo"] == QdiscReads("", "")
    assert snap.docker_images == frozenset({"sha256:a", ""})


def test_tunnel_ids_and_listener_pids_read_the_snapshots_own_lines():
    """The reap reads the snapshot through these, the inverse of the lines snapshot_host writes."""
    snap = _snap(
        tunnel_procs=frozenset({"100 t-1", "101 t-1", "102 t-2", "garbage"}),
        nc_listeners=frozenset({"300 nc -l 9000", "7 nc -lk 1", "x nc", ""}),
    )
    assert tunnel_ids(snap) == ["t-1", "t-2"]
    assert listener_pids(snap) == [7, 300]
    assert tunnel_ids(_snap()) == []
    assert listener_pids(_snap()) == []


def test_netdevs_listed_in_first_seen_order_once_each():
    text = (
        "qdisc noqueue 0: dev lo root refcnt 2\n"
        "qdisc prio 1: dev eth2 root refcnt 2 bands 11\n"
        "qdisc netem 40: dev eth2 parent 1:4 limit 1000\n"
        "qdisc fq_codel 0: dev eth1 root refcnt 2\n"
        "not a qdisc line dev\n"
    )
    assert netdevs_listed(text) == ["lo", "eth2", "eth1"]
    assert netdevs_listed("") == []


# ---------------------------------------------------------------------------
# residual_dirt: the ABSOLUTE predicate the bed reap judges by (not new-only)
# ---------------------------------------------------------------------------


def test_a_clean_snapshot_has_no_residual_dirt():
    assert residual_dirt(_snap()) == []
    assert (
        residual_dirt(_snap(docker_containers=frozenset({"abc otto-project1-live-x_api_1"}))) == []
    )


def test_every_suite_owned_namespace_is_residual_whatever_came_before():
    dirt = residual_dirt(
        _snap(
            tunnel_procs=frozenset({"100 t-1"}),
            impair_timers=frozenset({"200 edge eth1.100"}),
            nc_listeners=frozenset({"300 nc -l 9000"}),
            staging=frozenset({"/tmp/otto-x"}),
            docker_images=frozenset({"sha256:a"}),
            docker_containers=frozenset({"abc unix-project1-e2e-1a2b-api-1", "def keep-me"}),
            docker_networks=frozenset({"unix-project1-noexist-x_default", "bridge"}),
        )
    )
    assert dirt == [
        "otto-tunnel daemon: 100 t-1",
        "otto-impair timer: 200 edge eth1.100",
        "nc listener: 300 nc -l 9000",
        "staging entry: /tmp/otto-x",
        "docker image: sha256:a",
        "docker container: abc unix-project1-e2e-1a2b-api-1",
        "docker network: unix-project1-noexist-x_default",
    ]


@pytest.mark.parametrize(
    ("reads", "expected"),
    [
        (QdiscReads(CLEAN_ROOT), []),
        (QdiscReads(""), []),
        (QdiscReads(NETEM_ROOT), [f"eth2: otto qdisc (whole): {NETEM_ROOT!r}"]),
        (
            QdiscReads(QDISC_SCOPED_ONE, FILTER_SCOPED_ONE),
            [f"eth2: otto qdisc (scoped): {QDISC_SCOPED_ONE.strip()!r}"],
        ),
        (QdiscReads(FOREIGN_ROOT), [f"eth2: foreign qdisc (not otto's, kept): {FOREIGN_ROOT!r}"]),
    ],
)
def test_residual_qdiscs_are_classified_by_the_impairers_own_parser(reads, expected):
    assert residual_dirt(_snap(qdiscs={"eth2": reads})) == expected


def test_the_residual_report_names_the_host_and_counts():
    report = format_residual_report("test1", ["nc listener: 300 nc", "staging entry: /tmp/otto-x"])
    assert report == (
        "bed-reap: test1 still carries 2 leftover(s):\n  nc listener: 300 nc\n"
        "  staging entry: /tmp/otto-x"
    )


@pytest.mark.asyncio
async def test_snapshot_host_on_dockerless_host_yields_empty_sets():
    # Docker-less host: both probes return empty
    host = _ScriptedHost(canned_outputs={_DOCKER_PS_PROBE: "", _DOCKER_NET_PROBE: ""})
    snap = await snapshot_host(host)
    assert snap.docker_containers == frozenset()
    assert snap.docker_networks == frozenset()


# ---------------------------------------------------------------------------
# Tier-3 bracket laziness: the bed lease must not be instantiated for tests
# that opt out via @pytest.mark.no_hygiene_bracket. On a GitHub runner
# (loopback docker venue) there is no bed route at all, so an eager
# `chaos_bed` parameter would fail every opted-out test at fixture setup.
# ---------------------------------------------------------------------------


class _StubNode:
    def __init__(self, marker: object) -> None:
        self._marker = marker

    def get_closest_marker(self, name: str) -> object:
        assert name == "no_hygiene_bracket"
        return self._marker


class _StubRequest:
    def __init__(self, marker: object) -> None:
        self.node = _StubNode(marker)
        self.fixture_requests: list[str] = []

    def getfixturevalue(self, name: str):
        self.fixture_requests.append(name)
        raise _LeaseTouchedError(name)


class _LeaseTouchedError(Exception):
    pass


def test_opted_out_bracket_never_touches_the_bed_lease():
    import pytest

    from tests.e2e.chaos.conftest import _hygiene_bracket_impl

    request = _StubRequest(marker=object())  # marker present -> opt out
    gen = _hygiene_bracket_impl(request)
    next(gen)  # runs to the bare yield without requesting any fixture
    assert request.fixture_requests == []
    with pytest.raises(StopIteration):
        next(gen)


def test_bracketed_test_requests_the_lease_lazily():
    import pytest

    from tests.e2e.chaos.conftest import _hygiene_bracket_impl

    request = _StubRequest(marker=None)  # no marker -> bracket engages
    gen = _hygiene_bracket_impl(request)
    with pytest.raises(_LeaseTouchedError):
        next(gen)  # first thing the engaged branch does is request chaos_bed
    assert request.fixture_requests == ["chaos_bed"]


# ── nc listener probe: every spelling otto has ever spawned ──────────────────
#
# Regression guard for a blind spot the probe carried from the day it was
# written: it matched `nc -l` only, so every `nc -Nl` listener — the whole GET
# direction of the nc transfer backend at the time — was invisible to the
# authority whose job is finding leaked listeners. Found 2026-08-10 when a
# wider sweep turned three leaked pairs on a lab host into six.
#
# THE ARGV TABLE BELOW IS IN THREE GROUPS, and the split is load-bearing
# rather than tidy. otto's listener spelling CHANGED on 2026-08-25 (every
# listener is `nc -l -p PORT` now, in both directions, and no `-N` is emitted
# anywhere), so the table has to carry both what otto spawns today and what it
# used to spawn: an orphan outlives the code that made it, and a listener a
# July build leaked onto the bed is exactly the leftover this probe exists to
# find. The third group is spellings otto never emits at all — a human's, or
# another tool's — which the probe must still catch on a shared lab host.
# Matching the flag CLUSTER rather than a list of spellings is what makes all
# three groups fall out of one pattern; `test_the_probe_is_falsifiable_on_the_
# current_spelling` is the check that the newest group is not passing by
# accident of the widest row.


def _probe_regex() -> re.Pattern[str]:
    """The pattern `_NC_LISTENER_PROBE` hands to `pgrep -af`, as Python regex.

    Extracted from the real constant rather than restated, so a probe edit that
    reopens the hole fails here instead of quietly passing a copy of itself.

    Pulled out by splitting on the quotes rather than by matching the literal
    command text: the checkout-wide scan (`test_no_unbracketed_pkill_patterns`)
    rejects any source line carrying a `pgrep -f` pattern that is not built by
    an inline `argv_pattern(...)` call, and it cannot tell a line that BUILDS a
    probe from one that PARSES it. Writing the command text here to reach the
    pattern would trip a guard that is right to be blunt.
    """
    _, _, rest = _NC_LISTENER_PROBE.partition('"')
    pattern, sep, _ = rest.partition('"')
    assert sep, f"probe no longer has a quoted pattern: {_NC_LISTENER_PROBE!r}"
    return re.compile(pattern)


# What otto spawns TODAY. Both directions emit the same universal listener
# (`src/otto/host/transfer/nc.py`: `-l -p PORT` is the one form every measured
# netcat accepts), so unlike every earlier era there is no second otto
# spelling to miss.
_CURRENT_SPELLINGS = [
    "nc -l -p 9000",  # PUT's receiver, `< /dev/null > dst`
    "nc -l -p 9001",  # the tunnelled GET's sender, `< src` — same argv
    "bash -c nc -l -p 9002 < /etc/hostname",  # the wrapper shell
    # The hard-cap wrapper (`_nc_listener_prefix`), which since 2026-08-10 is
    # the NORMAL argv on every host that has a `timeout`. On GNU the wrapper
    # stays resident as the listener's parent and wears this argv; the `nc`
    # child wears the bare form above.
    "timeout 3600 nc -l -p 9000",  # GNU coreutils, and BusyBox from 1.31.0
    "timeout -t 3600 nc -l -p 9001",  # BusyBox up to 1.28.1
    # BusyBox >= 1.30 execs the program IN PLACE (measured: the pid ends up
    # with argv `nc …`, no wrapper) and forks a detached watchdog that setsids
    # and reparents to init wearing the full command. So on a BusyBox host the
    # listener itself matches the bare rows above, and this row is the
    # watchdog — a second hit per listener, which is the right way round for a
    # probe whose job is finding leftovers.
    "/usr/bin/busybox timeout 3600 nc -l -p 9001",
]

# RETIRED on 2026-08-25 with the universal spelling, and STILL REQUIRED TO
# MATCH: a leaked listener outlives the otto build that spawned it. These are
# what the bed's own orphans wear — including the three leaked pairs the
# 2026-08-10 sweep found, which were all `-Nl`.
_RETIRED_SPELLINGS = [
    "nc -l -w 30 9000",  # PUT's receiver until 2026-08-25
    "nc -Nl -w 30 9001",  # the tunnelled GET's — the spelling that was invisible
    "bash -c nc -Nl -w 30 9002 < /etc/hostname",  # the wrapper shell
    "timeout 3600 nc -l -w 30 9000",  # GNU coreutils
    "timeout -t 3600 nc -Nl -w 30 9001",  # BusyBox up to 1.28.1
    "/usr/bin/busybox timeout 3600 nc -Nl -w 30 9001",  # the BusyBox >= 1.30 watchdog
]

# Never otto's, and never allowed to be invisible either: this probe reads a
# SHARED lab host, so a listener a person or another tool left behind is a
# leftover the bed's before/after diff has to see.
_FOREIGN_SPELLINGS = [
    "nc -l 9000",  # no -w and no -p at all
    "nc -lp 9000",  # GNU netcat spelling: `l` is not the last flag letter
    "nc -klv 9000",  # multiple flags around the `l`
]


@pytest.mark.parametrize(
    "argv",
    [
        *(pytest.param(a, id=f"current-{i}") for i, a in enumerate(_CURRENT_SPELLINGS)),
        *(pytest.param(a, id=f"retired-{i}") for i, a in enumerate(_RETIRED_SPELLINGS)),
        *(pytest.param(a, id=f"foreign-{i}") for i, a in enumerate(_FOREIGN_SPELLINGS)),
    ],
)
def test_probe_matches_every_listener_spelling(argv):
    assert _probe_regex().search(argv), f"probe would not find a leaked {argv!r}"


def test_the_probe_is_falsifiable_on_the_current_spelling():
    """The current group is not carried by the retired one, proved by mutation.

    Every row above passes against today's pattern, which is the state a table
    can reach by being written after the fact — so the rows that were ADDED
    when the spelling changed have to be shown to be load-bearing, not decor.
    A pattern that admits ` -w` or a bare port after the flag cluster matches
    every retired and foreign row and NO current one, because `-l -p 9000` puts
    an option where those expect a number.

    TWO ASSERTIONS PER CURRENT ROW, and the second is why this test reddens on
    a narrowing rather than only describing one. The mutation half is a
    statement about a regex this test wrote, which cannot fail when
    `_NC_LISTENER_PROBE` changes; the real-probe half reads the CONSTANT, so a
    future narrowing that reopens the hole for `-l -p` fails here as well as
    in `test_probe_matches_every_listener_spelling` above.
    """
    narrowed = re.compile(r"[n]c -[A-Za-z]*l[A-Za-z]*( -w| [0-9]|$)")
    for argv in _RETIRED_SPELLINGS + _FOREIGN_SPELLINGS:
        assert narrowed.search(argv), f"the mutation was meant to keep {argv!r}"
    for argv in _CURRENT_SPELLINGS:
        assert _probe_regex().search(argv), (
            f"the real probe does not match {argv!r}, so the isolation below is "
            f"about a spelling this probe would leak rather than find"
        )
        assert not narrowed.search(argv), (
            f"the mutation still matches {argv!r}, so it does not isolate the "
            f"current spelling and proves nothing about the rows that carry it"
        )


@pytest.mark.parametrize(
    "argv",
    [
        "nc 10.0.0.1 9000",  # a CLIENT, not a listener — must not match
        "ncat -l 9000",  # different executable; `nc` is what otto spawns
        "sleep 30",
    ],
)
def test_probe_ignores_non_listeners(argv):
    assert not _probe_regex().search(argv), f"probe would misreport {argv!r} as a listener"


def test_probe_cannot_match_its_own_wrapper_shell():
    """The bracket trick must survive the widening.

    The remote wrapper's argv carries the probe text verbatim; if the pattern
    matched itself, every run would report a phantom listener. `argv_pattern`
    exists for this, and widening the flag cluster must not have bypassed it.
    """
    assert not _probe_regex().search(_NC_LISTENER_PROBE)


def test_the_staging_probe_lists_the_chaos_hold_file():
    from tests._fixtures import bed_hygiene

    assert "/run/chaos/shutdown_hold" in bed_hygiene._STAGING_PROBE
