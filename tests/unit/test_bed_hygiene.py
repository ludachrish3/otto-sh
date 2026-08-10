"""Contract tests for the BedHygiene diff — canned probe outputs, no bed.

The oracle's one hard rule: PRE-EXISTING DIRT IS SNAPSHOTTED OUT. A leftover
is a line present after that was absent before; something dirty going in must
never be blamed on the scenario (2026-07-21 misattribution class), and must
never mask a NEW leftover of the same kind either.
"""

import re
from dataclasses import dataclass, field

import pytest

from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.bed_hygiene import (
    _DOCKER_NET_PROBE,
    _DOCKER_PS_PROBE,
    _NC_LISTENER_PROBE,
    HygieneSnapshot,
    diff_snapshots,
    format_hygiene_report,
    snapshot_host,
)


def _snap(**over):
    base = {
        "tunnel_procs": frozenset(),
        "impair_timers": frozenset(),
        "nc_listeners": frozenset(),
        "qdiscs": {"eth2": "qdisc noqueue 0: root refcnt 2"},
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
    after = _snap(qdiscs={"eth2": "qdisc prio 1: root refcnt 2 bands 11"})
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert "eth2" in leftovers[0]
    assert "prio" in leftovers[0]


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
    report = format_hygiene_report("tomato", ["eth2: qdisc changed", "new nc listener"])
    assert "tomato" in report
    assert "eth2: qdisc changed" in report
    assert "new nc listener" in report


def test_diff_reports_new_docker_containers_only():
    before = _snap(docker_containers=frozenset({"old x"}))
    after = _snap(docker_containers=frozenset({"old x", "abc123 otto-repo1-e2e-x-api-1"}))
    leftovers = diff_snapshots(before, after)
    joined = "\n".join(str(item) for item in leftovers)
    assert "abc123 otto-repo1-e2e-x-api-1" in joined
    assert "old x" not in joined


def test_diff_reports_new_docker_networks_only():
    before = _snap(docker_networks=frozenset({"bridge"}))
    after = _snap(docker_networks=frozenset({"bridge", "otto-repo1-e2e-x_default"}))
    leftovers = diff_snapshots(before, after)
    joined = "\n".join(str(item) for item in leftovers)
    assert "otto-repo1-e2e-x_default" in joined
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
            _DOCKER_PS_PROBE: "abc123 otto-repo1-e2e-x-api-1\ndef456 otto-repo1-e2e-x-db-1\n",
            _DOCKER_NET_PROBE: "bridge\notto-repo1-e2e-x_default\n",
        }
    )
    snap = await snapshot_host(host)
    assert snap.docker_containers == frozenset(
        {"abc123 otto-repo1-e2e-x-api-1", "def456 otto-repo1-e2e-x-db-1"}
    )
    assert snap.docker_networks == frozenset({"bridge", "otto-repo1-e2e-x_default"})


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


# ── nc listener probe: the GET direction spelling ────────────────────────────
#
# Regression guard for a blind spot the probe carried from the day it was
# written: it matched `nc -l` only, so every `nc -Nl` listener — the whole GET
# direction of the nc transfer backend — was invisible to the authority whose
# job is finding leaked listeners. Found 2026-08-10 when a wider sweep turned
# three leaked pairs on a lab host into six.


def _probe_regex() -> re.Pattern[str]:
    """The pattern `_NC_LISTENER_PROBE` hands to `pgrep -af`, as Python regex.

    Extracted from the real constant rather than restated, so a probe edit that
    reopens the hole fails here instead of quietly passing a copy of itself.

    Pulled out by splitting on the quotes rather than by matching the literal
    command text: the repo-wide scan (`test_no_unbracketed_pkill_patterns`)
    rejects any source line carrying a `pgrep -f` pattern that is not built by
    an inline `argv_pattern(...)` call, and it cannot tell a line that BUILDS a
    probe from one that PARSES it. Writing the command text here to reach the
    pattern would trip a guard that is right to be blunt.
    """
    _, _, rest = _NC_LISTENER_PROBE.partition('"')
    pattern, sep, _ = rest.partition('"')
    assert sep, f"probe no longer has a quoted pattern: {_NC_LISTENER_PROBE!r}"
    return re.compile(pattern)


@pytest.mark.parametrize(
    "argv",
    [
        "nc -l -w 30 9000",  # PUT direction
        "nc -Nl -w 30 9001",  # GET direction — the spelling that was invisible
        "nc -l 9000",  # no -w at all
        "nc -lp 9000",  # GNU netcat spelling: `l` is not the last flag letter
        "nc -klv 9000",  # multiple flags around the `l`
        "bash -c nc -Nl -w 30 9002 < /etc/hostname",  # the wrapper shell
    ],
)
def test_probe_matches_every_listener_spelling(argv):
    assert _probe_regex().search(argv), f"probe would not find a leaked {argv!r}"


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
