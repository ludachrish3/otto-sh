"""check_link's ``--live`` cycle against faked impair/repair/read and netem-modelling hosts.

``FakeManage`` stands in for :func:`~otto.link.manage.impair_link`,
:func:`~otto.link.manage.repair_link` and
:func:`~otto.link.manage.read_link_state`: it records every call, keeps the
link's per-direction state, and applies each impairment to the origin
endpoint's :class:`SandboxHost` model — so the pings and timed connects the
cycle runs there measure exactly what was applied, and a step that impaired
the wrong thing measures the wrong thing.
"""

import dataclasses
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field

import pytest
import pytest_asyncio

from otto.check import FeatureResult, UnmeasuredReason, Verdict
from otto.link import check_live
from otto.link._check_rows import RANGE_SELECTOR, origin_end
from otto.link.check import LinkCheckReport, check_link, link_sections
from otto.link.check_live import LIVE_DELAY_MS, LIVE_FEATURES, combine_directions
from otto.link.impairer import FIRST_SELECTOR_BAND
from otto.link.manage import (
    VERIFY_FAILED,
    DirectionState,
    LinkCommandFailedError,
    LinkHostUnreachableError,
    LinkState,
)
from otto.link.model import Link
from otto.link.netem import NetEmImpairer
from otto.link.params import ImpairmentParams, Selector
from otto.link.placement import FlowDirection
from tests.unit.check.test_fingerprint import MODERN

from ._check_fakes import EDGE, FakeLab, SandboxHost, bed, patch_sleep

A_TO_B, B_TO_A = FlowDirection.A_TO_B, FlowDirection.B_TO_A
ALREADY = f"link {EDGE.id} already carries an impairment — repair it first, or run without --live"


@dataclass(frozen=True)
class Call:
    verb: str
    from_host: str | None = None
    expire: int | None = None
    selector: Selector | None = None
    params: ImpairmentParams | None = None


@dataclass
class FakeManage:
    """impair/repair/read for one link, applied to the origin hosts' netem models."""

    lab: FakeLab
    link: Link
    calls: list[Call] = field(default_factory=list)
    states: dict[FlowDirection, DirectionState] = field(
        default_factory=lambda: {A_TO_B: DirectionState(), B_TO_A: DirectionState()}
    )
    refuse: dict[str, str] = field(default_factory=dict)
    """Origin host -> the ValueError message ``impair`` refuses it with."""
    unverified: Selector | None = None
    """``impair`` with this selector fails its post-apply verify."""
    unreachable_on_rate: bool = False
    """``impair`` with a rate raises a down-host error (a step that raises)."""
    stuck: list[ImpairmentParams] = field(default_factory=list)
    """Whole-link params a repair fails to clear: it raises, as the real one does."""

    def _direction(self, from_host: str | None) -> FlowDirection:
        return A_TO_B if from_host == self.link.a.host else B_TO_A

    def _host(self, direction: FlowDirection) -> SandboxHost:
        return self.lab.hosts[origin_end(self.link, direction).host]

    def _dev(self, direction: FlowDirection) -> str:
        return origin_end(self.link, direction).interface or ""

    async def impair_link(
        self,
        _lab: object,
        _ident: str,
        params: ImpairmentParams,
        *,
        from_host: str | None = None,
        expire: int | None = None,
        selector: Selector | None = None,
    ) -> None:
        self.calls.append(Call("impair", from_host, expire, selector, params))
        if from_host in self.refuse:
            raise ValueError(self.refuse[from_host])
        if self.unreachable_on_rate and params.rate:
            raise LinkHostUnreachableError(f"host {from_host!r} unreachable running 'tc'")
        d = self._direction(from_host)
        if selector is not None and selector == self.unverified:
            raise LinkCommandFailedError(
                f"{VERIFY_FAILED} on {from_host}/{self._dev(d)}: expected [x], observed [clean]"
            )
        imp, dev = NetEmImpairer(), self._dev(d)
        if selector is None:
            self._host(d).netem(imp.apply_command(dev, params))
            self.states[d] = DirectionState(whole=params)
            return
        self._host(d).netem(
            imp.scoped_root_command(dev),
            imp.scoped_band_command(dev, FIRST_SELECTOR_BAND, params),
            *imp.scoped_filter_commands(dev, FIRST_SELECTOR_BAND, selector),
        )
        self.states[d] = DirectionState(scoped={selector: params})

    async def repair_link(
        self, _lab: object, _ident: str, *, selector: Selector | None = None
    ) -> None:
        self.calls.append(Call("repair", selector=selector))
        for d, state in self.states.items():
            if state.whole is not None and state.whole in self.stuck:
                # repair_link cancels the timer, clears, re-reads, and raises
                # when the impairment is still there.
                raise LinkCommandFailedError(
                    f"repair failed on {self._host(d).id}/{self._dev(d)}: impairment still present"
                )
            if selector is None or selector in state.scoped:
                self.states[d] = DirectionState()
                self._host(d).netem(NetEmImpairer().clear_command(self._dev(d)))

    async def read_link_state(self, _lab: object, link: Link) -> LinkState:
        assert link.id == self.link.id, "the live pass reads only the link it checks"
        return LinkState(self.link, dict(self.states))

    def impairs(self) -> list[Call]:
        return [c for c in self.calls if c.verb == "impair"]


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sleep(monkeypatch)


def _fake(
    monkeypatch: pytest.MonkeyPatch, lab: FakeLab, link: Link = EDGE, **kw: object
) -> FakeManage:
    fake = FakeManage(lab, link, **kw)  # ty: ignore[invalid-argument-type]
    monkeypatch.setattr(check_live, "impair_link", fake.impair_link)
    monkeypatch.setattr(check_live, "repair_link", fake.repair_link)
    monkeypatch.setattr(check_live, "read_link_state", fake.read_link_state)
    return fake


def _live(report: LinkCheckReport, host_id: str) -> dict[str, FeatureResult]:
    [host] = [h for h in report.hosts if h.host_id == host_id]
    return {r.feature: r for r in host.live}


def _verdicts(report: LinkCheckReport, host_id: str) -> dict[str, Verdict]:
    return {f: r.verdict for f, r in _live(report, host_id).items()}


@pytest.mark.asyncio
async def test_live_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    lab, test1, test2, _ = bed()
    fake = _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True)
    for host_id in ("test1", "test2"):
        assert _verdicts(report, host_id) == dict.fromkeys(LIVE_FEATURES, Verdict.PASS), _live(
            report, host_id
        )
    assert report.ok
    assert fake.impairs()[0].params == ImpairmentParams(delay_ms=LIVE_DELAY_MS)
    delay = _live(report, "test1")["delay"]
    assert any(c.startswith(f"ping -c 10 -i 0.2 -W 2 {EDGE.b.ip}") for c in delay.commands), (
        "probes aim at the far endpoint's link address"
    )
    assert "impair_link edge from test1: delay 50ms, expire 60s" in delay.commands
    assert delay.commands[-1] == "repair_link edge", "-v shows what was impaired and repaired"
    port_range = _live(report, "test1")["port range"]
    assert "impair_link edge from test1: delay 50ms on 5200:5210/tcp dst, expire 60s" in (
        port_range.commands
    )
    assert port_range.commands[-1] == "repair_link edge 5200:5210/tcp dst"
    read_back = _live(report, "test1")["read-back"]
    assert read_back.output == "read back: a->b whole [delay 50ms]; b->a clean"
    assert [s.columns for s in link_sections(report)] == [["sandbox", "live"]] * 2
    assert fake.states == {A_TO_B: DirectionState(), B_TO_A: DirectionState()}
    for host in (test1, test2):
        assert host.whole is None
        assert not host.scoped


@pytest.mark.asyncio
async def test_live_refuses_to_touch_an_already_impaired_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, _, test2, _ = bed()
    users = DirectionState(whole=ImpairmentParams(delay_ms=10.0))
    fake = _fake(monkeypatch, lab)
    fake.states[B_TO_A] = users  # the OTHER direction: a bare repair would clear it too
    report = await check_link(lab, "edge", live=True, from_host="test1")
    rows = _live(report, "test1")
    assert sorted(rows) == sorted(LIVE_FEATURES)
    assert {r.verdict for r in rows.values()} == {Verdict.SKIPPED}
    assert {r.hint for r in rows.values()} == {ALREADY}
    assert fake.calls == [], "nothing was impaired or repaired"
    assert fake.states[B_TO_A] == users
    assert test2.commands == [], "no probe, listener or sweep reached the far endpoint"


@pytest.mark.asyncio
async def test_live_runs_directions_one_at_a_time_with_expire_60_and_repairs_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, *_ = bed()
    fake = _fake(monkeypatch, lab)
    await check_link(lab, "edge", live=True)
    impairs = fake.impairs()
    assert len(impairs) == 2 * 5, "five steps per direction"
    assert {c.expire for c in impairs} == {60}
    assert [c.from_host for c in impairs] == ["test1"] * 5 + ["test2"] * 5
    for index, call in enumerate(fake.calls):
        if call.verb == "impair":
            follow = fake.calls[index + 1]
            assert follow == Call("repair", selector=call.selector), (
                f"{call} was not repaired before the next step"
            )
    assert len(fake.calls) == 2 * len(impairs)


@pytest.mark.asyncio
async def test_safety_refusal_skips_the_direction_and_names_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, *_ = bed()
    rule = "test1/eth1.100 is test1's management interface — refusing to impair it"
    fake = _fake(monkeypatch, lab, refuse={"test1": rule})
    report = await check_link(lab, "edge", live=True)
    refused = _live(report, "test1")
    assert {r.verdict for r in refused.values()} == {Verdict.SKIPPED}
    assert {r.hint for r in refused.values()} == {rule}
    assert _verdicts(report, "test2") == dict.fromkeys(LIVE_FEATURES, Verdict.PASS)
    assert [c.from_host for c in fake.impairs()].count("test1") == 1, "the first refusal stops it"
    assert fake.calls[1].verb == "impair", "a refused impair is not repaired"
    assert report.ok


@pytest.mark.asyncio
async def test_verify_mismatch_fails_read_back_only(monkeypatch: pytest.MonkeyPatch) -> None:
    lab, *_ = bed()
    _fake(monkeypatch, lab, unverified=RANGE_SELECTOR)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    rows = _live(report, "test1")
    assert rows["read-back"].verdict is Verdict.FAIL
    assert rows["read-back"].detail is not None
    assert rows["read-back"].detail.startswith(f"{VERIFY_FAILED} on test1/eth1.100")
    assert rows["port range"].verdict is Verdict.SKIPPED
    assert {f: rows[f].verdict for f in ("delay", "side", "rate", "loss")} == dict.fromkeys(
        ("delay", "side", "rate", "loss"), Verdict.PASS
    )
    assert report.failed() == [rows["read-back"]]


@pytest.mark.asyncio
async def test_sandbox_failure_skips_the_live_row(monkeypatch: pytest.MonkeyPatch) -> None:
    lab, test1, *_ = bed()
    test1.answer("netem rate", "Error: Specified qdisc kind is unknown.", ok=False)
    fake = _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True)
    [sandbox_rate] = [r for r in report.hosts[0].sandbox if r.feature == "rate"]
    assert sandbox_rate.verdict is Verdict.UNSUPPORTED, (
        "the premise: rate failed in test1's sandbox"
    )
    rate = _live(report, "test1")["rate"]
    assert rate.verdict is Verdict.SKIPPED
    assert rate.detail == "failed in sandbox"
    assert not [c for c in fake.impairs() if c.from_host == "test1" and c.params and c.params.rate]
    assert _live(report, "test2")["rate"].verdict is Verdict.PASS


@pytest.mark.asyncio
async def test_listeners_are_killed_even_when_a_step_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, _, test2, _ = bed()
    fake = _fake(monkeypatch, lab, unreachable_on_rate=True)
    with pytest.raises(LinkHostUnreachableError):
        await check_link(lab, "edge", live=True, from_host="test1")
    listeners = [c for c in test2.commands if "TCP-LISTEN" in c]
    assert len(listeners) == 3, "the far endpoint carries the echo listeners"
    tag = listeners[0].split("exec -a ")[1].split()[0]
    assert tag.startswith("otto-check-")
    kill = f"pkill -f '[o]{tag[1:]}'"
    assert kill in test2.commands, "the listeners were not killed"
    assert test2.commands.index(kill) > test2.commands.index(listeners[-1])
    verbs = [c.verb for c in fake.calls]
    assert verbs.count("repair") == verbs.count("impair") - 1, (
        "only the raising step went unrepaired"
    )


@pytest.mark.asyncio
async def test_heal_check_fails_read_back_when_state_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repair that raises (as the real one does) halts the cycle; the link is left impaired."""
    lab, *_ = bed()
    stuck = ImpairmentParams(delay_ms=LIVE_DELAY_MS)
    fake = _fake(monkeypatch, lab, stuck=[stuck])
    report = await check_link(lab, "edge", live=True)
    assert fake.states[A_TO_B] == DirectionState(whole=stuck), "the premise: it did not heal"
    assert len(fake.impairs()) == 1, "no step ran after the failed repair"
    a_rows, b_rows = _live(report, "test1"), _live(report, "test2")
    read_back = a_rows["read-back"]
    assert read_back.verdict is Verdict.FAIL
    assert read_back.detail == "link did not heal after repair"
    assert read_back.measured is not None
    assert "whole [delay 50ms]" in read_back.measured
    assert read_back.hint is not None
    assert "otto link repair edge" in read_back.hint
    assert a_rows["delay"].verdict is Verdict.PASS
    for name in ("port range", "side", "rate", "loss"):
        assert a_rows[name].verdict is Verdict.SKIPPED, name
        assert a_rows[name].hint is not None
        assert a_rows[name].hint.startswith("repair failed: repair failed on test1/eth1.100")
    assert {r.verdict for r in b_rows.values()} == {Verdict.SKIPPED}, "the other direction too"
    assert {(r.hint or "").split(":")[0] for r in b_rows.values()} == {"repair failed"}
    assert not report.ok


@pytest.mark.asyncio
async def test_leftover_listeners_are_swept_and_said(monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _, test2, _ = bed()
    test2.answer("pgrep -af", "4242 socat TCP-LISTEN:5299,fork,reuseaddr PIPE otto-check-abc123\n")
    _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    said = "swept otto-check process otto-check-abc123 on test2 (earlier or concurrent run)"
    assert report.live_swept == [said]
    assert "pkill -f '[o]tto-check-[0-9a-f]{6}'" in test2.commands
    [section] = link_sections(report)
    assert said in section.subheadings


@pytest.mark.asyncio
async def test_a_pgrep_without_a_says_so_and_sweeps_anyway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, _, test2, _ = bed()
    usage = "pgrep: invalid option -- 'a'\nUsage: pgrep [-flvx] PATTERN\n"
    test2.answer("pgrep -af", usage, ok=False)
    _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    [said] = report.live_swept
    assert said.startswith("could not list otto-check processes on test2 (pgrep -a said: Usage:")
    assert "pkill -f '[o]tto-check-[0-9a-f]{6}'" in test2.commands


@pytest.mark.asyncio
async def test_a_far_host_missing_from_the_lab_skips_its_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, *_ = bed()
    del lab.hosts["test2"]
    fake = _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    rows = _live(report, "test1")
    assert {r.verdict for r in rows.values()} == {Verdict.SKIPPED}
    assert {r.hint for r in rows.values()} == {
        "endpoint host 'test2' of link edge is not in the loaded lab"
    }
    assert fake.impairs() == []


@pytest.mark.asyncio
async def test_a_far_endpoint_without_an_ip_skips_its_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, *_ = bed()
    unaddressed = Link(a=EDGE.a, b=dataclasses.replace(EDGE.b, ip=""), name="noip")
    lab.links.append(unaddressed)
    fake = _fake(monkeypatch, lab, unaddressed)
    report = await check_link(lab, "noip", live=True, from_host="test1")
    rows = _live(report, "test1")
    assert {r.verdict for r in rows.values()} == {Verdict.SKIPPED}
    [hint] = {r.hint for r in rows.values()}
    assert hint is not None
    assert hint.startswith("endpoint test2 of link noip has no ip to probe")
    assert fake.impairs() == []


@pytest.mark.asyncio
async def test_a_noisy_live_control_leaves_measured_rows_unmeasured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, *_ = bed(noisy=True)
    _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    rows = _live(report, "test1")
    for name in ("delay", "port range", "side", "rate", "loss"):
        assert rows[name].verdict is Verdict.UNMEASURED, name
        assert rows[name].reason is UnmeasuredReason.NOISY_BASELINE
    assert rows["read-back"].verdict is Verdict.PASS
    assert report.ok


def test_a_middlebox_row_is_the_worst_of_its_two_directions() -> None:
    passed = FeatureResult("delay", Verdict.PASS)
    failed = FeatureResult("delay", Verdict.FAIL, detail="no ping replies")
    [row] = combine_directions({A_TO_B: [passed], B_TO_A: [failed]})
    assert row.verdict is Verdict.FAIL
    assert row.detail == "b->a: no ping replies"
    assert combine_directions({A_TO_B: [passed], B_TO_A: [passed]}) == [passed]


@pytest.mark.asyncio
@pytest.mark.parametrize("fingerprint", [MODERN, "python3"])
async def test_live_listeners_bind_the_far_endpoints_link_address(
    monkeypatch: pytest.MonkeyPatch, fingerprint: str
) -> None:
    if fingerprint == "python3":
        fingerprint = MODERN.replace("tool:socat=1", "tool:socat=0").replace(
            "tool:python3=0", "tool:python3=1"
        )
    lab, _, test2, _ = bed(fingerprint=fingerprint)
    test2.fingerprint = fingerprint
    _fake(monkeypatch, lab)
    await check_link(lab, "edge", live=True, from_host="test1")
    listeners = [c for c in test2.commands if c.startswith("setsid bash -c ")]
    assert len(listeners) == 3
    for cmd in listeners:
        body = shlex.split(cmd)[3]
        if "python3" in body:
            assert f"s.bind(('{EDGE.b.ip}', " in shlex.split(body)[2], cmd
        else:
            assert f",bind={EDGE.b.ip}," in body, cmd


@pytest.mark.asyncio
async def test_a_far_endpoint_without_bash_is_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, _, test2, _ = bed()
    test2.fingerprint = MODERN.replace("tool:bash=1", "tool:bash=0")
    _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    rows = _live(report, "test1")
    for name in ("port range", "side", "rate"):
        assert rows[name].verdict is Verdict.UNMEASURED, name
        assert rows[name].reason is UnmeasuredReason.MISSING_TOOL
        assert rows[name].detail == "needs bash on test2"
    assert not any("setsid bash" in c for c in test2.commands)


@pytest.mark.asyncio
async def test_a_socat_probe_host_without_bash_is_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The socat client's clock is bash's; the far end has bash, the probing end does not."""
    lab, test1, test2, _ = bed(fingerprint=MODERN.replace("tool:bash=1", "tool:bash=0"))
    _fake(monkeypatch, lab)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    rows = _live(report, "test1")
    for name in ("port range", "side", "rate"):
        assert rows[name].verdict is Verdict.UNMEASURED, name
        assert rows[name].detail == "needs bash on test1"
    assert not any("EPOCHREALTIME" in c for c in test1.commands)
    assert not any("setsid bash" in c for c in test2.commands)


@pytest.mark.asyncio
async def test_a_foreign_qdisc_is_named_and_no_repair_is_suggested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab, *_ = bed()
    fake = _fake(monkeypatch, lab)
    fake.states[B_TO_A] = DirectionState(foreign=True)
    report = await check_link(lab, "edge", live=True, from_host="test1")
    rows = _live(report, "test1")
    assert {r.verdict for r in rows.values()} == {Verdict.SKIPPED}
    [hint] = {r.hint for r in rows.values()}
    assert hint == (
        "link edge carries a qdisc otto did not create on b->a — otto will not touch it, "
        "so --live cannot run; see docs/cli/link/safety (Clearing is not creating)"
    )
    assert "repair" not in hint
    assert fake.calls == []


def _inner_bash_scripts(cmd: str) -> list[str]:
    """Every script a ``bash -c <script>`` inside *cmd*'s ``sh -c`` word hands bash."""
    words = shlex.split(shlex.split(cmd)[2]) if cmd.startswith("sh -c ") else shlex.split(cmd)
    return [words[i + 2] for i in range(len(words) - 2) if words[i : i + 2] == ["bash", "-c"]]


class TestEveryCommandSurvivesElevation:
    """Otto elevates by prefix (``sudo -S -p '…' <cmd>``), which covers one simple command.

    So every command the check sends through ``check_root_run`` must reach
    ``host.run`` as ONE ``sh -c`` word, and the fully elevated line — built by
    the same ``_elevate`` every posix host uses — must parse in real bash. A
    ``for`` loop, a ``;`` list or a trailing ``&`` sent bare would split
    around ``sudo``. The commands are captured from real check runs (sandbox
    with a leftover to sweep, then live, for both probe backends), so a new
    command is covered the day it is added.
    """

    @pytest_asyncio.fixture
    async def captured(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Every distinct text the check handed ``host.run`` across both backends' runs."""
        python3 = MODERN.replace("tool:socat=1", "tool:socat=0").replace(
            "tool:python3=0", "tool:python3=1"
        )
        raw: list[str] = []
        for fingerprint in (MODERN, python3):
            lab, test1, test2, _ = bed(fingerprint=fingerprint, stale="otto-check-abc123 (id: 0)")
            test2.fingerprint = fingerprint
            test2.answer("pgrep -af", "4242 socat PIPE otto-check-abc123\n")
            _fake(monkeypatch, lab)
            await check_link(lab, "edge", live=True)
            raw += test1.raw_commands + test2.raw_commands
        return list(dict.fromkeys(raw))

    def test_every_elevated_command_parses_in_bash(self, captured: list[str]) -> None:
        if shutil.which("bash") is None:
            pytest.skip("bash not installed")
        from otto.host.local_host import LocalHost

        for needle in (
            "ip netns add", "ip netns pids", "pgrep -af", "pkill -f", "ping -c", "TCP-LISTEN",
            "python3 -c", "EPOCHREALTIME", "tc qdisc replace", "tc qdisc del", "tc filter add",
            "id -u", "grep -qx x", "wc -c", ",bind=", "s.bind(", "-T 15",
        ):  # fmt: skip
            assert any(needle in c for c in captured), f"premise: no captured command has {needle}"
        inner_scripts = [script for cmd in captured for script in _inner_bash_scripts(cmd)]
        for needle in ("grep -qx x", "wc -c", "TCP-LISTEN", "sleep 3 &&"):
            assert any(needle in script for script in inner_scripts), (
                f"premise: no inner bash script has {needle}"
            )
        for cmd in captured:
            elevated, _expects = LocalHost()._elevate(cmd)
            parsed = subprocess.run(
                ["bash", "-n", "-c", elevated], check=False, capture_output=True, text=True
            )
            assert parsed.returncode == 0, f"{elevated!r}: {parsed.stderr}"
            for script in _inner_bash_scripts(cmd):
                # The socat clients' and listeners' own bash layer must parse too.
                inner = subprocess.run(
                    ["bash", "-n", "-c", script], check=False, capture_output=True, text=True
                )
                assert inner.returncode == 0, f"{script!r}: {inner.stderr}"
        for cmd in captured:
            # Parsing is not enough: `sudo … s=$X; echo …` parses and still runs its
            # tail unelevated. check_root_run's `sh -c`, or the expire row's timer
            # launch (otto.link.manage), which arrives as its own `bash -c` word.
            words = shlex.split(cmd)
            assert words[0] in ("sh", "bash"), cmd
            assert words[1:2] == ["-c"], cmd
            assert len(words) == 3, f"not one shell word: {cmd!r}"
