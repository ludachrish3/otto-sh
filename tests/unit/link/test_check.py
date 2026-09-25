"""check_link's sandbox pass against a scripted netem sandbox host (no bed).

The host doubles live in :mod:`tests.unit.link._check_fakes`; the ``--live``
cycle has its own file, ``test_check_live.py``.
"""

import dataclasses

import pytest
from rich.console import Console

from otto.check import (
    CheckHostUnreachableError,
    FeatureResult,
    UnmeasuredReason,
    Verdict,
    render_sections,
)
from otto.check.fingerprint import parse_fingerprint
from otto.link import check
from otto.link._check_rows import NETEM_MISSING_HINT, NETEM_UNKNOWN_HINT
from otto.link.check import (
    FEATURES,
    LIVE_FEATURES,
    NA_LEGEND,
    LinkCheckReport,
    check_link,
    link_sections,
)
from otto.link.model import Link
from otto.link.netem import NetEmImpairer
from otto.link.probes import PROBE_TIMEOUT_S, timed_connect_command
from otto.link.sandbox import new_sandbox
from tests.conftest import active_context
from tests.unit.check.test_fingerprint import MODERN

from ._check_fakes import EDGE, NS_IP, SandboxHost, bed, patch_sleep


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sleep(monkeypatch)


def _verdicts(report: LinkCheckReport, host: int = 0) -> dict[str, Verdict]:
    return {r.feature: r.verdict for r in report.hosts[host].sandbox}


def _rows(report: LinkCheckReport, host: int = 0) -> dict:
    return {r.feature: r for r in report.hosts[host].sandbox}


class TestFeatureSelection:
    @pytest.mark.asyncio
    async def test_unknown_feature_is_a_value_error_listing_the_valid_ones(self) -> None:
        lab, test1, *_ = bed()
        with pytest.raises(ValueError, match="'bogus'") as info:
            await check_link(lab, "edge", features=["delay", "bogus"])
        for name in FEATURES:
            assert name in str(info.value)
        assert test1.commands == []

    @pytest.mark.asyncio
    async def test_read_back_always_runs_even_when_narrowed(self) -> None:
        lab, *_ = bed()
        report = await check_link(lab, "edge", features=["delay"], from_host="test1")
        assert report.features == ["read-back", "delay"]
        assert [r.feature for r in report.hosts[0].sandbox] == ["read-back", "delay"]

    @pytest.mark.asyncio
    async def test_features_come_back_in_canonical_order(self) -> None:
        lab, *_ = bed()
        report = await check_link(
            lab, "edge", features=["side", "delay", "loss"], from_host="test1"
        )
        assert report.features == ["read-back", "delay", "loss", "side"]


class TestRefusalAndDryRun:
    @pytest.mark.asyncio
    async def test_refused_link_is_the_result(self) -> None:
        lab, test1, test2, test3 = bed()
        report = await check_link(lab, "bare")
        assert report.refusal is not None
        assert "no named interface" in report.refusal
        assert report.refusal_hint is not None
        assert "lab-links" in report.refusal_hint
        assert report.hosts == []
        assert not report.ok
        assert test1.commands + test2.commands + test3.commands == []

    @pytest.mark.asyncio
    async def test_dry_run_contacts_nothing_and_plans_everything(self) -> None:
        lab, test1, test2, test3 = bed()
        with active_context(dry_run=True):
            report = await check_link(lab, "edge", live=True)
        assert test1.commands + test2.commands + test3.commands == []
        assert report.hosts == []
        plan = report.dry_run_plan
        tested = ", ".join(FEATURES)
        live = ", ".join(LIVE_FEATURES)
        assert plan[:2] == ["placement a->b on test1/eth1.100", "placement b->a on test2/eth1.200"]
        for host in ("test1", "test2"):
            assert (
                f"would fingerprint {host} (one read-only command) and check it can become root"
                in plan
            )
            assert f"would sweep leftover otto-check-* namespaces on {host}" in plan
            assert (
                f"would build netns otto-check-<id> on {host} "
                f"(198.18.0.1/30 on ock<id> ↔ 198.18.0.2 inside) and test: {tested}"
            ) in plan
        assert plan.count("would run echo listeners inside it on tcp 5205, 5211, 5299") == 2
        impairs = [line for line in plan if line.startswith("would impair edge ")]
        assert len(impairs) == 2
        assert impairs[0].startswith("would impair edge a->b on test1/eth1.100 for about ")
        assert impairs[1].startswith("would impair edge b->a on test2/eth1.200 for about ")
        for line in impairs:
            assert line.endswith(f"s ({live}), each step with expire 60s")
        assert (
            "would run echo listeners on test2 10.10.202.12 tcp 5205, 5211, 5299 for a->b" in plan
        )
        assert (
            "would run echo listeners on test1 10.10.201.11 tcp 5205, 5211, 5299 for b->a" in plan
        )
        assert plan[-1] == "no device was contacted — nothing was measured"

    @pytest.mark.asyncio
    async def test_dry_run_names_no_listeners_when_no_row_needs_them(self) -> None:
        lab, *_ = bed()
        with active_context(dry_run=True):
            report = await check_link(lab, "edge", live=True, features=["delay"])
        assert not any("listeners" in line for line in report.dry_run_plan)

    @pytest.mark.asyncio
    async def test_dry_run_in_path_says_placement_is_resolved_at_run_time(self) -> None:
        lab, test1, test2, test3 = bed()
        with active_context(dry_run=True):
            report = await check_link(lab, "dataplane")
        assert test1.commands + test2.commands + test3.commands == []
        assert report.dry_run_plan[0] == (
            "placement on middlebox test3: resolved from its live address table at run time"
        )
        assert (
            "would fingerprint test3 (one read-only command) and check it can become root"
            in report.dry_run_plan
        )
        assert not any("impair" in line for line in report.dry_run_plan)


class TestPlacement:
    @pytest.mark.asyncio
    async def test_in_path_groups_both_directions_into_one_sandbox(self) -> None:
        lab, test1, test2, test3 = bed()
        report = await check_link(lab, "dataplane", features=["read-back"])
        assert [h.host_id for h in report.hosts] == ["test3"]
        assert [(p.netdev, p.direction.value) for p in report.hosts[0].placements] == [
            ("eth1.200", "a->b"),
            ("eth1.100", "b->a"),
        ]
        assert sum(1 for c in test3.commands if c.startswith("ip netns add ")) == 1
        assert test1.commands + test2.commands == []

    @pytest.mark.asyncio
    async def test_from_narrows_to_one_host(self) -> None:
        lab, test1, *_ = bed()
        report = await check_link(lab, "edge", features=["read-back"], from_host="test2")
        assert [h.host_id for h in report.hosts] == ["test2"]
        assert test1.commands == []


class TestSkips:
    @pytest.mark.asyncio
    async def test_an_elevation_that_fails_skips_every_sandbox_row(self) -> None:
        said = "sudo: 3 incorrect password attempts"
        lab, test1, *_ = bed(elevation=(False, said))
        report = await check_link(lab, "edge", from_host="test1")
        rows = report.hosts[0].sandbox
        assert [r.feature for r in rows] == FEATURES
        assert {r.verdict for r in rows} == {Verdict.SKIPPED}
        [hint] = {r.hint for r in rows}
        assert hint is not None
        assert hint.startswith("otto could not become root on test1: it ran `id -u` through ")
        assert hint.endswith(f"which said: {said}")
        assert {r.output for r in rows} == {said}
        assert all(r.commands == ["id -u"] for r in rows)
        assert not any(c.startswith("ip netns add") for c in test1.commands)
        assert report.hosts[0].fingerprint is not None
        assert report.hosts[0].fingerprint.privileged is False
        assert report.ok

    @pytest.mark.asyncio
    async def test_a_sudo_that_asks_for_a_password_still_runs_the_sandbox(self) -> None:
        """No ``sudo -n``: a password sudo otto answers from the lab's credentials is root."""
        lab, test1, *_ = bed(elevation=(True, "[sudo] password for vagrant: \n0\n"))
        report = await check_link(lab, "edge", features=["delay"], from_host="test1")
        assert _verdicts(report) == {"read-back": Verdict.PASS, "delay": Verdict.PASS}
        assert "id -u" in test1.sudo_commands, "asked through otto's elevation"
        assert not any("sudo -n" in c for c in test1.commands)
        assert report.hosts[0].fingerprint is not None
        assert report.hosts[0].fingerprint.privileged is True

    @pytest.mark.asyncio
    async def test_no_netns_skips_every_sandbox_row(self) -> None:
        lab, *_ = bed(fingerprint=MODERN.replace("netns=1", "netns=0"))
        report = await check_link(lab, "edge", from_host="test1")
        assert {r.verdict for r in report.hosts[0].sandbox} == {Verdict.SKIPPED}
        assert {r.hint for r in report.hosts[0].sandbox} == {"needs ip netns support on test1"}

    @pytest.mark.asyncio
    async def test_no_tc_skips_every_sandbox_row(self) -> None:
        lab, *_ = bed(fingerprint=MODERN.replace("tool:tc=1", "tool:tc=0"))
        report = await check_link(lab, "edge", from_host="test1")
        assert {r.hint for r in report.hosts[0].sandbox} == {"tc not found on test1"}

    @pytest.mark.asyncio
    async def test_sandbox_setup_failure_skips_with_its_output(self) -> None:
        lab, test1, *_ = bed()
        test1.answer("ip link add", "RTNETLINK answers: Operation not permitted", ok=False)
        report = await check_link(lab, "edge", from_host="test1")
        rows = report.hosts[0].sandbox
        assert {r.verdict for r in rows} == {Verdict.SKIPPED}
        assert {r.output for r in rows} == {"RTNETLINK answers: Operation not permitted"}
        assert {r.hint for r in rows} == {"could not build the sandbox netns on test1"}
        assert any(c.startswith("ip netns del otto-check-") for c in test1.commands), (
            "a half-built sandbox must still be torn down"
        )

    @pytest.mark.asyncio
    async def test_a_down_host_is_a_host_named_error(self) -> None:
        lab, *_ = bed(fail_all=True)
        with pytest.raises(CheckHostUnreachableError, match="'test1'"):
            await check_link(lab, "edge")

    @pytest.mark.asyncio
    async def test_leftover_sandboxes_are_swept_and_reported(self) -> None:
        lab, test1, *_ = bed(stale="otto-check-abc123 (id: 0)\nuser-ns\n")
        report = await check_link(lab, "edge", features=["read-back"], from_host="test1")
        assert report.hosts[0].swept == ["otto-check-abc123"]
        assert "ip netns del otto-check-abc123 2>/dev/null || true" in test1.commands
        [section] = link_sections(report)
        assert "swept leftover sandbox otto-check-abc123 from an earlier run" in section.subheadings


class TestSandboxRows:
    @pytest.mark.asyncio
    async def test_happy_path_all_pass(self) -> None:
        lab, test1, test2, _ = bed()
        report = await check_link(lab, "edge")
        assert [h.host_id for h in report.hosts] == ["test1", "test2"]
        for index in range(2):
            assert _verdicts(report, index) == dict.fromkeys(FEATURES, Verdict.PASS), _rows(
                report, index
            )
        assert report.ok
        assert report.failed() == []
        assert len(report.results()) == 2 * len(FEATURES)
        for host in (test1, test2):
            assert any(c.startswith("ip netns del otto-check-") for c in host.commands)

    @pytest.mark.asyncio
    async def test_every_result_carries_the_commands_and_output(self) -> None:
        lab, *_ = bed()
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for row in rows.values():
            assert row.commands, row.feature
        assert any("netem delay 100ms" in c for c in rows["delay"].commands)
        assert any(c.startswith("ping -c 10 -i 0.2 ") for c in rows["delay"].commands)
        assert rows["delay"].output is not None
        assert "packets transmitted" in rows["delay"].output

    @pytest.mark.asyncio
    async def test_rejected_apply_is_unsupported_with_stderr(self) -> None:
        lab, test1, *_ = bed()
        test1.answer("netem rate", "Error: Specified qdisc kind is unknown.", ok=False)
        report = await check_link(lab, "edge", from_host="test1")
        rate = _rows(report)["rate"]
        assert rate.verdict is Verdict.UNSUPPORTED
        assert rate.output == "Error: Specified qdisc kind is unknown."
        assert rate.commands == [next(c for c in test1.commands if "netem rate" in c)]
        others = {f: v for f, v in _verdicts(report).items() if f != "rate"}
        assert others == dict.fromkeys(others, Verdict.PASS)
        assert not report.ok
        assert report.failed() == [rate]

    @pytest.mark.asyncio
    async def test_noisy_control_marks_measured_rows_unmeasured(self) -> None:
        lab, *_ = bed(noisy=True)
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for name in FEATURES[1:-1]:  # every row but read-back and expire
            assert rows[name].verdict is Verdict.UNMEASURED, name
            assert rows[name].reason is UnmeasuredReason.NOISY_BASELINE
            assert rows[name].measured is not None
            assert "loss" in rows[name].measured
        assert rows["read-back"].verdict is Verdict.PASS
        assert rows["expire"].verdict is Verdict.PASS
        assert report.ok

    @pytest.mark.asyncio
    async def test_missing_probe_tool_is_unmeasured_missing_tool(self) -> None:
        lab, test1, *_ = bed(fingerprint=MODERN.replace("tool:socat=1", "tool:socat=0"))
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for name in ("rate", "port range", "side"):
            assert rows[name].verdict is Verdict.UNMEASURED
            assert rows[name].reason is UnmeasuredReason.MISSING_TOOL
            assert rows[name].detail == "needs socat or python3 on test1"
        assert rows["delay"].verdict is Verdict.PASS
        assert not any("TCP-LISTEN" in c for c in test1.commands)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("feature", ["rate", "port range", "side"])
    async def test_a_probe_runner_without_a_backend_is_missing_tool(self, feature: str) -> None:
        host = SandboxHost(id="test1")
        sb = new_sandbox("abc123")
        fp = parse_fingerprint("test1", "10.10.200.11", MODERN)
        ctx = check._SandboxCtx(host, fp, NS_IP, sb=sb, imp=NetEmImpairer(), dev=sb.veth)
        row = await check._run_row(ctx, feature)
        assert row.verdict is Verdict.UNMEASURED
        assert row.reason is UnmeasuredReason.MISSING_TOOL
        assert row.detail == "needs socat or python3 on test1"
        assert host.commands == [f"tc qdisc del dev {sb.veth} root"], "nothing but the clear ran"

    @pytest.mark.asyncio
    async def test_python3_backend_probes_pass_too(self) -> None:
        fingerprint = MODERN.replace("tool:socat=1", "tool:socat=0").replace(
            "tool:python3=0", "tool:python3=1"
        )
        lab, test1, *_ = bed(fingerprint=fingerprint)
        report = await check_link(
            lab, "edge", features=["rate", "port range", "side"], from_host="test1"
        )
        assert _verdicts(report) == dict.fromkeys(
            ["read-back", "rate", "port range", "side"], Verdict.PASS
        )
        assert any(
            c.startswith("ip netns exec otto-check-") and "python3" in c for c in test1.commands
        )

    @pytest.mark.asyncio
    async def test_a_listener_that_never_answers_skips_the_probe_rows(self) -> None:
        lab, test1, *_ = bed()
        test1.answer("TCP-LISTEN:5299", "socat[7] E bind: Address already in use", ok=False)
        test1.answer(
            f"TCP:{NS_IP}:5299,",
            "2026/09/25 10:00:00 socat[42] E connect(5, AF=2 198.18.0.2:5299, 16): "
            "Connection refused\n1000.000000 1000.000300\n",
            ok=False,
        )
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for name in ("rate", "port range", "side"):
            assert rows[name].verdict is Verdict.SKIPPED, name
            assert rows[name].detail == "listener did not start"
            assert rows[name].output is not None
            assert "Address already in use" in rows[name].output
            assert "Connection refused" in rows[name].output
        assert rows["delay"].verdict is Verdict.PASS

    @pytest.mark.asyncio
    async def test_a_clockless_probe_is_unmeasured_no_clock(self) -> None:
        lab, test1, *_ = bed()
        test1.answer(f"TCP:{NS_IP}:5211,", " \n")
        report = await check_link(lab, "edge", features=["port range"], from_host="test1")
        row = _rows(report)["port range"]
        assert row.verdict is Verdict.UNMEASURED
        assert row.reason is UnmeasuredReason.NO_CLOCK

    @pytest.mark.asyncio
    async def test_every_row_clears_the_device_after_itself(self) -> None:
        lab, test1, *_ = bed()
        report = await check_link(lab, "edge", from_host="test1")
        veth = next(c for c in test1.commands if c.startswith("ip link add ")).split()[3]
        clear = f"tc qdisc del dev {veth} root"
        root_apply = f"tc qdisc replace dev {veth} root "
        events = [c for c in test1.commands if c == clear or c.startswith(root_apply)]
        applies = [i for i, c in enumerate(events) if c != clear]
        assert applies, "no row applied anything"
        for this, following in zip(applies, [*applies[1:], len(events)], strict=True):
            assert clear in events[this + 1 : following + 1], (
                f"{events[this]!r} was not cleared before the next apply"
            )
        assert test1.commands.count(clear) >= len(report.features)

    @pytest.mark.asyncio
    async def test_read_back_names_the_shape_that_did_not_read_back(self) -> None:
        lab, test1, *_ = bed()
        test1.answer("tc filter show", "")  # the scoped tree reads back with no filters
        report = await check_link(lab, "edge", features=["read-back"], from_host="test1")
        row = _rows(report)["read-back"]
        assert row.verdict is Verdict.FAIL
        assert row.detail is not None
        assert "port-scoped" in row.detail
        assert "whole-link" not in row.detail
        assert row.measured == "foreign", "a prio root with no otto filters is not otto's tree"
        assert row.wanted == "5200:5210/tcp dst [delay 100ms]"

    @pytest.mark.asyncio
    async def test_expire_that_does_not_fire_fails(self) -> None:
        lab, test1, *_ = bed()
        # the timer launches but never clears
        test1.answer("sleep 3 &&", "Running as unit: run-u42.service")
        report = await check_link(lab, "edge", features=["expire"], from_host="test1")
        row = _rows(report)["expire"]
        assert row.verdict is Verdict.FAIL
        launch = next(c for c in test1.commands if "sleep 3 &&" in c)
        assert launch in row.commands, "the evidence names the launch line otto really ran"
        assert row.output is not None
        assert "run-u42.service" in row.output

    @pytest.mark.asyncio
    async def test_expire_without_bash_is_skipped_with_the_refusal(self) -> None:
        lab, *_ = bed(has_bash=False)
        report = await check_link(lab, "edge", features=["expire"], from_host="test1")
        row = _rows(report)["expire"]
        assert row.verdict is Verdict.SKIPPED
        assert row.hint
        assert "bash" in row.hint


class TestEvidence:
    @pytest.mark.asyncio
    async def test_read_back_keeps_the_judges_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def judged(_expected: object, _got: object) -> FeatureResult:
            return FeatureResult("read-back", Verdict.FAIL, detail="the judge's own words")

        monkeypatch.setattr(check, "judge_readback", judged)
        lab, *_ = bed()
        report = await check_link(lab, "edge", features=["read-back"], from_host="test1")
        detail = _rows(report)["read-back"].detail
        assert detail is not None
        assert "the judge's own words" in detail
        assert "did not read back as written" in detail

    @pytest.mark.asyncio
    async def test_a_failed_timed_probe_fails_with_its_output(self) -> None:
        fingerprint = MODERN.replace("tool:socat=1", "tool:socat=0").replace(
            "tool:python3=0", "tool:python3=1"
        )
        lab, test1, *_ = bed(fingerprint=fingerprint)
        refused = "ConnectionRefusedError: [Errno 111] Connection refused"
        test1.answer(timed_connect_command("python3", NS_IP, 5211), refused, ok=False)
        report = await check_link(lab, "edge", features=["port range"], from_host="test1")
        row = _rows(report)["port range"]
        assert row.verdict is Verdict.FAIL
        assert row.output is not None
        assert refused in row.output

    @pytest.mark.asyncio
    async def test_no_ping_is_missing_tool_for_every_measured_row(self) -> None:
        lab, test1, *_ = bed(fingerprint=MODERN.replace("tool:ping=1", "tool:ping=0"))
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for name in FEATURES[1:-1]:  # every row but read-back and expire
            assert rows[name].verdict is Verdict.UNMEASURED, name
            assert rows[name].reason is UnmeasuredReason.MISSING_TOOL
            assert rows[name].detail == "needs ping on test1"
        assert rows["read-back"].verdict is Verdict.PASS
        assert rows["expire"].verdict is Verdict.PASS
        assert not any("ping -c" in c for c in test1.commands), "no control ping is attempted"


class TestSections:
    @pytest.mark.asyncio
    async def test_sections_heading_columns_and_legend(self) -> None:
        lab, *_ = bed()
        report = await check_link(lab, "edge", features=["delay"], from_host="test1")
        [section] = link_sections(report)
        assert section.heading == (
            "test1  eth1.100 10.10.201.11  (a->b)   "
            "iproute2 6.1.0 · kernel 6.8.0-86-generic · aarch64 · gnu · elevated"
        )
        assert section.subheadings == [
            "proven range: iproute2 within · kernel within · aarch64 within · gnu within"
        ]
        assert section.columns == ["sandbox"]
        assert section.legend == []
        assert section.summary_name == "test1"
        assert [row.label for row in section.rows] == ["read-back", "delay"]
        assert [len(row.cells) for row in section.rows] == [1, 1]

        live = dataclasses.replace(report, live_requested=True)
        [section] = link_sections(live)
        assert section.columns == ["sandbox", "live"]
        assert section.legend == [NA_LEGEND]
        assert [row.cells[1] for row in section.rows] == [None, None]

    @pytest.mark.asyncio
    async def test_a_host_with_both_placements_lists_each(self) -> None:
        lab, *_ = bed()
        report = await check_link(lab, "dataplane", features=["read-back"])
        [section] = link_sections(report)
        assert section.heading.startswith(
            "test3  eth1.200 10.10.202.13 (a->b), eth1.100 10.10.201.13 (b->a)   iproute2 6.1.0"
        )
        assert report.hosts[0].addresses == {"eth1.200": "10.10.202.13", "eth1.100": "10.10.201.13"}

    @pytest.mark.asyncio
    async def test_the_heading_shows_the_netdev_address_not_the_management_one(self) -> None:
        lab, test1, *_ = bed()
        assert test1.ip != EDGE.a.ip, "the premise: management and netdev addresses differ"
        report = await check_link(lab, "edge", features=["read-back"], from_host="test1")
        assert report.hosts[0].addresses == {"eth1.100": EDGE.a.ip}
        [section] = link_sections(report)
        assert f"eth1.100 {EDGE.a.ip}  (a->b)" in section.heading
        assert test1.ip not in section.heading

    @pytest.mark.asyncio
    async def test_an_endpoint_without_an_ip_shows_no_address(self) -> None:
        lab, *_ = bed()
        unaddressed = Link(a=dataclasses.replace(EDGE.a, ip=""), b=EDGE.b, name="noaddr")
        lab.links.append(unaddressed)
        report = await check_link(lab, "noaddr", features=["read-back"], from_host="test1")
        assert report.hosts[0].addresses == {}
        [section] = link_sections(report)
        assert section.heading.startswith("test1  eth1.100  (a->b)   iproute2")


PYTHON3 = MODERN.replace("tool:socat=1", "tool:socat=0").replace("tool:python3=0", "tool:python3=1")


def _render(report: LinkCheckReport) -> str:
    console = Console(record=True, width=200, color_system=None)
    render_sections(console, link_sections(report))
    return console.export_text()


class TestRowsNotAborts:
    """A command that fails on a host that answered is a row's verdict, never the end of the run."""

    @pytest.mark.asyncio
    async def test_a_probe_that_stalls_fails_its_row_and_the_rest_still_run(self) -> None:
        lab, test1, *_ = bed()
        stalled = "echoed 65536 of 262144 bytes\n1000.000000 1015.200000\n"
        test1.answer("head -c 262144", stalled, ok=False)
        report = await check_link(lab, "edge", from_host="test1")
        rate = _rows(report)["rate"]
        assert rate.verdict is Verdict.FAIL
        assert rate.detail == f"probe did not finish within {PROBE_TIMEOUT_S} s"
        assert rate.output == stalled
        assert rate.commands == [next(c for c in test1.commands if "head -c 262144" in c)]
        others = {f: v for f, v in _verdicts(report).items() if f != "rate"}
        assert others == dict.fromkeys(others, Verdict.PASS)

    @pytest.mark.asyncio
    async def test_a_probe_that_fails_fast_is_not_called_a_stall(self) -> None:
        lab, test1, *_ = bed()
        test1.answer("head -c 262144", "echoed 0 of 262144 bytes\n1000.0 1000.2\n", ok=False)
        report = await check_link(lab, "edge", features=["rate"], from_host="test1")
        assert _rows(report)["rate"].detail == "the timed probe did not complete"

    @pytest.mark.asyncio
    async def test_a_tree_read_that_fails_fails_its_row(self) -> None:
        lab, test1, *_ = bed()
        said = 'Cannot find device "ock1"'
        test1.answer("tc qdisc show", said, ok=False)
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for name in ("read-back", "expire"):
            assert rows[name].verdict is Verdict.FAIL, name
            assert rows[name].detail == "reading the tree back failed"
            assert rows[name].output == said
            assert rows[name].commands[0].startswith("tc qdisc show dev ock")
        assert rows["delay"].verdict is Verdict.PASS, "the other rows still ran"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [False, True])
    async def test_a_middlebox_whose_addresses_cannot_be_read_skips_its_rows(
        self, live: bool
    ) -> None:
        lab, _, _, test3 = bed()
        said = "RTNETLINK answers: Operation not permitted"
        test3.rules.insert(0, ("ip -o addr show", False, said))
        report = await check_link(lab, "dataplane", live=live)
        [host] = report.hosts
        assert host.host_id == "test3"
        rows = [*host.sandbox, *host.live]
        assert [r.feature for r in host.live] == (
            [f for f in FEATURES if f in LIVE_FEATURES] if live else []
        )
        assert {r.verdict for r in rows} == {Verdict.SKIPPED}
        assert {r.hint for r in rows} == {
            "could not resolve test3's interfaces from its address table"
        }
        assert {r.output for r in rows} == {said}
        assert all(r.commands == ["ip -o addr show"] for r in rows)
        [section] = link_sections(report)
        assert section.heading == "test3  (interfaces unresolved)"
        assert report.ok


class TestReadinessTrustsOnlyOttosEcho:
    @pytest.mark.asyncio
    async def test_a_foreign_service_on_the_port_is_a_listener_that_did_not_start(self) -> None:
        lab, test1, *_ = bed(fingerprint=PYTHON3)
        foreign = "echo answered b'S', not x\n3.1\n"
        test1.answer(timed_connect_command("python3", NS_IP, 5299, within=1), foreign, ok=False)
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for name in ("rate", "port range", "side"):
            assert rows[name].verdict is Verdict.SKIPPED, name
            assert rows[name].detail == "listener did not start"
            assert rows[name].output is not None
            assert "echo answered b'S', not x" in rows[name].output
        assert rows["delay"].verdict is Verdict.PASS


class TestAScopedRejectIsPortRangesToReport:
    @pytest.mark.asyncio
    async def test_read_back_passes_on_the_whole_tree_and_says_why_it_stopped(self) -> None:
        lab, test1, *_ = bed()
        test1.answer("tc filter add", "RTNETLINK answers: Operation not supported", ok=False)
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        read_back = rows["read-back"]
        assert read_back.verdict is Verdict.PASS
        assert read_back.detail == "port-scoped tree not applied: tc rejected it (see port range)"
        rejected = next(c for c in test1.commands if c.startswith("tc filter add"))
        assert rejected in read_back.commands
        assert read_back.output is not None
        assert "RTNETLINK answers: Operation not supported" in read_back.output
        assert rows["port range"].verdict is Verdict.UNSUPPORTED
        assert rows["side"].verdict is Verdict.UNSUPPORTED
        assert "port-scoped tree not applied" in _render(report)


class TestReadBackKeepsBothFindings:
    @pytest.mark.asyncio
    async def test_a_failed_whole_tree_keeps_its_detail_when_the_scoped_one_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def judged(_expected: object, _got: object) -> FeatureResult:
            return FeatureResult("read-back", Verdict.FAIL, detail="the judge's own words")

        monkeypatch.setattr(check, "judge_readback", judged)
        lab, test1, *_ = bed()
        test1.answer("tc filter add", "RTNETLINK answers: Operation not supported", ok=False)
        report = await check_link(lab, "edge", features=["read-back"], from_host="test1")
        row = _rows(report)["read-back"]
        assert row.verdict is Verdict.FAIL
        assert row.detail is not None
        assert row.detail.startswith("the judge's own words; ")
        assert "the whole-link tree did not read back as written" in row.detail
        assert row.detail.endswith("port-scoped tree not applied: tc rejected it (see port range)")


class TestElevationPromptNobodyAnswers:
    @pytest.mark.asyncio
    async def test_every_sandbox_row_is_skipped_and_the_run_completes(self) -> None:
        lab, test1, *_ = bed(elevation_hangs=True)
        report = await check_link(lab, "edge", from_host="test1")
        rows = report.hosts[0].sandbox
        assert [r.feature for r in rows] == FEATURES
        assert {r.verdict for r in rows} == {Verdict.SKIPPED}
        [hint] = {r.hint for r in rows}
        assert hint is not None
        assert hint.startswith("otto could not become root on test1: `id -u` got no answer")
        assert "password the lab does not declare" in hint
        assert not any(c.startswith("ip netns add") for c in test1.commands)
        assert report.ok


class TestNetemModule:
    @pytest.mark.asyncio
    async def test_a_missing_module_names_itself_on_every_rejected_row(self) -> None:
        lab, test1, *_ = bed(fingerprint=MODERN.replace("netem=1", "netem=0"))
        test1.answer(" netem ", "Error: Specified qdisc kind is unknown.", ok=False)
        report = await check_link(lab, "edge", features=["delay", "loss"], from_host="test1")
        rows = report.hosts[0].sandbox
        assert {r.verdict for r in rows} == {Verdict.UNSUPPORTED}
        assert {r.hint for r in rows} == {NETEM_MISSING_HINT}
        assert "modprobe sch_netem" in NETEM_MISSING_HINT
        [section] = link_sections(report)
        assert section.heading.endswith(" · elevated · no sch_netem")

    @pytest.mark.asyncio
    async def test_a_module_otto_cannot_see_is_hinted_as_unknown(self) -> None:
        lab, test1, *_ = bed(fingerprint=MODERN.replace("netem=1", "netem=?"))
        test1.answer("netem rate", "Error: Specified qdisc kind is unknown.", ok=False)
        report = await check_link(lab, "edge", features=["rate"], from_host="test1")
        assert _rows(report)["rate"].hint == NETEM_UNKNOWN_HINT
        [section] = link_sections(report)
        assert section.heading.endswith(" · sch_netem ?")

    @pytest.mark.asyncio
    async def test_a_present_module_adds_no_hint(self) -> None:
        lab, test1, *_ = bed()
        test1.answer("netem rate", "Error: Specified qdisc kind is unknown.", ok=False)
        report = await check_link(lab, "edge", features=["rate"], from_host="test1")
        assert _rows(report)["rate"].hint is None
        [section] = link_sections(report)
        assert "sch_netem" not in section.heading


class TestSharedHint:
    @pytest.mark.asyncio
    async def test_eleven_rows_skipped_for_one_cause_print_it_once(self) -> None:
        said = "sudo: a password is required"
        lab, *_ = bed(elevation=(False, said))
        report = await check_link(lab, "edge", from_host="test1")
        [hint] = {r.hint for r in report.hosts[0].sandbox}
        assert len(report.hosts[0].sandbox) == 11
        text = _render(report)
        assert text.count(said) == 1, text
        assert f"\nhint: {hint}" in text


class TestBash:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fingerprint", [MODERN, PYTHON3])
    async def test_no_bash_is_missing_tool_not_a_listener_that_did_not_start(
        self, fingerprint: str
    ) -> None:
        lab, test1, *_ = bed(fingerprint=fingerprint.replace("tool:bash=1", "tool:bash=0"))
        report = await check_link(lab, "edge", from_host="test1")
        rows = _rows(report)
        for name in ("rate", "port range", "side", "expire"):
            assert rows[name].verdict is Verdict.UNMEASURED, name
            assert rows[name].reason is UnmeasuredReason.MISSING_TOOL
            assert rows[name].detail == "needs bash on test1"
        assert not any("setsid bash" in c for c in test1.commands), "no listener was attempted"
        assert not any("sleep 3 &&" in c for c in test1.commands), "no timer was attempted"
        assert rows["delay"].verdict is Verdict.PASS
