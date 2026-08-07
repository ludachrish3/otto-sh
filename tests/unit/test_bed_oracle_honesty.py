"""Falsifiability pins for the tier-3 probe oracles (G5) — stub hosts, no bed.

The contract under pin: a probe that itself fails must make the oracle RAISE
(host-named, status-quoted), never read as "clean". Before this contract, a
timed-out ``host.exec`` handed its error text to the parsers — which parse
"Command timed out after 30s" into empty sets and equal qdisc strings, i.e. a
clean bed — so the exact scenarios chaos manufactures (SSH blackhole, reboot)
could blind their own oracles.

These pins run the REAL ``run_probe``/``snapshot_host``/hygiene-bracket code
paths; only ``build_bed_host`` is stubbed (the same seam the GitHub loopback
venue exercises). Each raising pin has a healthy-stub positive control beside
it, so a broken harness cannot masquerade as a passing guard.
"""

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.result import CommandResult, Result
from otto.utils import Status
from tests._fixtures.bed_hygiene import ProbeFailedError, argv_pattern, snapshot_host
from tests.e2e.chaos import _bed
from tests.e2e.chaos._bed import probe_text, run_probe, veggies_link_id
from tests.e2e.chaos.conftest import _hygiene_bracket_impl

_TIMEOUT_TEXT = "Command timed out after 30s"


@dataclass
class _StubHost:
    """Scripted stand-in for ``build_bed_host``'s UnixHost.

    ``fail`` scripts every exec to come back non-ok with the timeout error
    text in ``value`` — the exact shape a blackholed/rebooting bed produces,
    and the shape the pre-contract oracles happily parsed as "clean".
    """

    id: str = "stub-elem"
    fail: bool = False
    canned: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)

    async def exec(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        if self.fail:
            return CommandResult(
                status=Status.Error, value=_TIMEOUT_TEXT, command=cmd, timed_out=True
            )
        return CommandResult(status=Status.Success, value=self.canned.get(cmd, ""), command=cmd)

    async def close(self) -> None:
        pass


@pytest.fixture
def stub_bed_host(monkeypatch):
    """Route ``_bed.probe_host`` at a scripted host; return it for scripting."""
    host = _StubHost()
    monkeypatch.setattr(_bed, "build_bed_host", lambda element: host)
    return host


# ---------------------------------------------------------------------------
# run_probe: a non-ok Result coming back from the factory must raise.
# ---------------------------------------------------------------------------


def test_run_probe_raises_on_failed_probe_result(stub_bed_host):
    stub_bed_host.fail = True
    with pytest.raises(ProbeFailedError) as excinfo:
        run_probe("stub-elem", lambda h: h.exec("tc qdisc show dev eth2"))
    msg = str(excinfo.value)
    assert "stub-elem" in msg, f"probe failure must name the host: {msg}"
    assert "Error" in msg, f"probe failure must quote the status: {msg}"
    assert "tc qdisc show dev eth2" in msg, f"probe failure must name the command: {msg}"


def test_run_probe_returns_ok_result_with_payload(stub_bed_host):
    """Positive control for the stub seam: the real probe path runs the stub."""
    stub_bed_host.canned["echo CHAOS-PROBE"] = "CHAOS-PROBE\n"
    out = run_probe("stub-elem", lambda h: h.exec("echo CHAOS-PROBE"))
    assert isinstance(out, Result)
    assert out.is_ok
    assert out.value == "CHAOS-PROBE\n"


def test_run_probe_passes_through_non_result_returns(stub_bed_host):
    """A factory returning a plain value forfeits the check — documented edge.

    The tier-3 consumers were all converted to return the Result (or go via
    ``probe_text``); this pin fixes the boundary so the conversion's point —
    run_probe can only vet what it can see — stays written down.
    """

    async def _find(host):
        return ["a line"]

    assert run_probe("stub-elem", _find) == ["a line"]


# ---------------------------------------------------------------------------
# probe_text: the one-call spelling for checked text reads.
# ---------------------------------------------------------------------------


def test_probe_text_raises_on_failed_probe(stub_bed_host):
    stub_bed_host.fail = True
    with pytest.raises(ProbeFailedError):
        probe_text("stub-elem", "pgrep -af 'sleep 311' || true")


def test_probe_text_returns_checked_stripped_output(stub_bed_host):
    stub_bed_host.canned["tc qdisc show dev eth2"] = "qdisc noqueue 0: root refcnt 2\n"
    assert probe_text("stub-elem", "tc qdisc show dev eth2") == "qdisc noqueue 0: root refcnt 2"


# ---------------------------------------------------------------------------
# snapshot_host: a failed probe must raise, not populate a snapshot.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_host_raises_on_failed_probe_naming_host_and_command():
    host = _StubHost(fail=True)
    with pytest.raises(ProbeFailedError) as excinfo:
        await snapshot_host(host)
    msg = str(excinfo.value)
    assert "stub-elem" in msg, f"snapshot failure must name the host: {msg}"
    assert repr(host.commands[0]) in msg, f"snapshot failure must name the probe: {msg}"


@pytest.mark.asyncio
async def test_snapshot_host_healthy_probes_still_snapshot():
    """Positive control: the contract must not break the ordinary snapshot."""
    snap = await snapshot_host(_StubHost())
    assert snap.tunnel_procs == frozenset()
    assert snap.nc_listeners == frozenset()


# ---------------------------------------------------------------------------
# The autouse hygiene bracket: a dead probe FAILS the scenario, never "clean".
# ---------------------------------------------------------------------------


class _BracketRequest:
    """Stub request: bracket engaged (no opt-out marker), stub bed handle."""

    def __init__(self) -> None:
        self.node = SimpleNamespace(get_closest_marker=lambda name: None)

    def getfixturevalue(self, name: str):
        assert name == "chaos_bed"
        return SimpleNamespace(element="stub-elem")


def test_hygiene_bracket_errors_not_clean_when_before_probe_dies(stub_bed_host):
    stub_bed_host.fail = True
    gen = _hygiene_bracket_impl(_BracketRequest())
    with pytest.raises(ProbeFailedError):
        next(gen)  # the before-snapshot must die loudly, not snapshot the error text


def test_hygiene_bracket_completes_clean_with_healthy_probes(stub_bed_host):
    """Positive control: same harness, healthy probes — the bracket passes."""
    gen = _hygiene_bracket_impl(_BracketRequest())
    next(gen)  # before-snapshot succeeds
    with pytest.raises(StopIteration):
        next(gen)  # after-snapshot + clean diff -> normal completion


# ---------------------------------------------------------------------------
# argv_pattern: a pattern-kill probe must never match its own wrapper shell.
# ---------------------------------------------------------------------------


def test_argv_pattern_matches_target_but_never_its_own_command_line():
    """The bed found this live: ``pkill -f 'sleep 313'`` matches the remote
    wrapper shell carrying the pattern, kills it, and the probe reports
    ``Failed/retcode=-1`` — silently before G5, loudly after. The bracket
    trick must keep matching the real target while the kill command's own
    text stays unmatchable."""
    pat = argv_pattern("sleep 313")
    assert re.search(pat, "sleep 313"), "must still match the real target argv"
    own_command_line = f"bash -c pkill -f '{pat}' || true"
    assert not re.search(pat, own_command_line), (
        f"pattern {pat!r} matches its own wrapper command line — self-kill"
    )


def test_argv_pattern_is_total_by_contract():
    """An empty needle or one starting on a regex-class metacharacter would
    produce a malformed or wrong pattern (``[^]x`` negates); the helper must
    refuse loudly rather than emit it."""
    with pytest.raises(ValueError, match="alphanumeric"):
        argv_pattern("")
    with pytest.raises(ValueError, match="alphanumeric"):
        argv_pattern("^x")


# ---------------------------------------------------------------------------
# Consumer-drift scans (the helpers bind only what they can see): AST scans
# over the chaos lanes ban the two consumer shapes found live — a run_probe
# factory unwrapping `.value` before the check can vet it, and a pattern
# kill/grep not built by argv_pattern. Every Name in the factory expression
# (the argument itself, a name inside a delegating lambda, a partial's
# target) is resolved to every same-named function DEFINED IN THE SAME
# MODULE and those bodies are walked too — the original defect's real shape
# was a named local `_find`, the first cut of this scan missed it, and the
# mutation run caught the miss; the delegating-lambda shape was the final
# reviewer's catch. Blind spots, stated: a factory imported from another
# module is not walked (`run_probe(elem, snapshot_host)` — those `.value`
# reads live behind `check_probe_result` already); only string literals /
# f-strings are scanned for patterns, not strings assembled via ''.join or
# %; and _KILL_RE reads the common `-af`/`-f` spellings, not multi-flag
# forms like `pkill -x -f` or `--full`, which no lane writes.
# ---------------------------------------------------------------------------

_LANE_FIXTURE = Path(__file__).parents[1] / "_fixtures" / "bed_hygiene.py"


def _lane_sources():
    tests_root = Path(__file__).parents[1]
    files = sorted((tests_root / "e2e" / "chaos").glob("*.py"))
    files += sorted((tests_root / "integration" / "chaos").glob("*.py"))
    files.append(_LANE_FIXTURE)
    return [(path, ast.parse(path.read_text())) for path in files]


def _factory_value_offenders(tree) -> list:
    local_defs: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_defs.setdefault(node.name, []).append(node)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "run_probe":
            continue
        factory = node.args[1] if len(node.args) >= 2 else None
        if factory is None:
            factory = next((kw.value for kw in node.keywords if kw.arg == "coro_factory"), None)
        if factory is None:
            continue
        # Walk the factory expression AND every same-module def any Name in
        # it resolves to — this covers the bare-Name factory, a delegating
        # `lambda h: _find(h, extra)`, and `partial(_find, ...)`. An imported
        # name (snapshot_host) has no local def and is the stated blind
        # spot — its reads sit behind check_probe_result already.
        roots = [factory] + [
            fn
            for sub in ast.walk(factory)
            if isinstance(sub, ast.Name)
            for fn in local_defs.get(sub.id, [])
        ]
        offenders.extend(
            sub.lineno
            for root in roots
            for sub in ast.walk(root)
            if isinstance(sub, ast.Attribute) and sub.attr == "value"
        )
    return offenders


_KILL_RE = re.compile(r"p(?:kill|grep)\s+-[a-zA-Z]*f\s+['\"]?")
_INTERP = "\x00"


def _docstring_nodes(tree) -> set:
    """ids of Constant nodes that are docstrings (they may *mention* pkill)."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                skip.add(id(body[0].value))
    return skip


def _pattern_kill_offenders(tree) -> list:
    offenders = []
    skip = _docstring_nodes(tree)
    inside_joined = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip or id(node) in inside_joined:
                continue
            segments = [(node.value, None)]
        elif isinstance(node, ast.JoinedStr):
            segments = [
                (part.value, None) if isinstance(part, ast.Constant) else (_INTERP, part)
                for part in node.values
            ]
        else:
            continue
        text = "".join(seg for seg, _ in segments)
        exprs = [expr for _, expr in segments if expr is not None]
        for m in _KILL_RE.finditer(text):
            follow = text[m.end() : m.end() + 1]
            if follow == "[":
                continue  # bracket-tricked literal
            if follow == _INTERP:
                expr = exprs[text[: m.end()].count(_INTERP)]
                if any(isinstance(n, ast.Name) and n.id == "argv_pattern" for n in ast.walk(expr)):
                    continue
            offenders.append(getattr(node, "lineno", -1))
    return offenders


def test_factory_value_scan_positive_control():
    bad = ast.parse("run_probe(e, lambda h: h.exec(c).value)")
    assert _factory_value_offenders(bad), "the scan must flag a value-unwrapping factory"
    named_bad = ast.parse(
        "def outer(e):\n"
        "    async def _find(h):\n"
        "        return (await h.exec(c)).value or ''\n"
        "    return run_probe(e, _find)\n"
    )
    assert _factory_value_offenders(named_bad), (
        "the scan must flag a NAMED local factory — the original defect's real shape"
    )
    delegating_bad = ast.parse(
        "def outer(e, extra):\n"
        "    async def _find(h, x):\n"
        "        return (await h.exec(x)).value or ''\n"
        "    return run_probe(e, lambda h: _find(h, extra))\n"
    )
    assert _factory_value_offenders(delegating_bad), (
        "the scan must flag a DELEGATING lambda — the parametrized-factory shape"
    )
    kwarg_bad = ast.parse("run_probe(element=e, coro_factory=lambda h: h.exec(c).value)")
    assert _factory_value_offenders(kwarg_bad), "the keyword-arg call form must be flagged too"
    good = ast.parse("run_probe(e, lambda h: h.exec(c))")
    assert not _factory_value_offenders(good)
    imported_ok = ast.parse("run_probe(e, snapshot_host)")
    assert not _factory_value_offenders(imported_ok)


def test_pattern_kill_scan_positive_control():
    interp_bad = ast.parse("cmd = f\"pkill -f '{needle}' || true\"")
    assert _pattern_kill_offenders(interp_bad), "interpolation not via argv_pattern must flag"
    literal_bad = ast.parse("cmd = \"pkill -f 'sleep 1' || true\"")
    assert _pattern_kill_offenders(literal_bad), "a plain self-matching literal must flag"
    interp_ok = ast.parse("cmd = f\"pkill -f '{argv_pattern('sleep 1')}' || true\"")
    assert not _pattern_kill_offenders(interp_ok)
    literal_ok = ast.parse("cmd = \"pkill -f '[s]leep 1' || true\"")
    assert not _pattern_kill_offenders(literal_ok)


def test_no_value_reads_inside_run_probe_factories_across_lanes():
    offenders = [
        f"{path.name}:{line}"
        for path, tree in _lane_sources()
        for line in _factory_value_offenders(tree)
    ]
    assert not offenders, (
        "run_probe factory unwraps .value before the status check can vet it "
        f"(use probe_text, or return the Result): {offenders}"
    )


def test_no_unbracketed_pattern_kills_across_lanes():
    offenders = [
        f"{path.name}:{line}"
        for path, tree in _lane_sources()
        for line in _pattern_kill_offenders(tree)
    ]
    assert not offenders, (
        "pkill/pgrep -f pattern not built by argv_pattern — it will match its "
        f"own wrapper shell and self-kill (found live on the bed): {offenders}"
    )


# ---------------------------------------------------------------------------
# veggies_link_id's skip-unresolvable loop: ValueError records only.
# ---------------------------------------------------------------------------


def _poisoned_lab_json(tmp_path, poison):
    data = json.loads(_bed.lab_data_path().read_text())
    data["hosts"] = [*data["hosts"], poison]
    out = tmp_path / "lab.json"
    out.write_text(json.dumps(data))
    return out


def test_veggies_link_id_skips_unresolvable_records(tmp_path, monkeypatch):
    """Positive control: a ValueError record (unknown os profile) is skipped —
    the documented sprout27/zephyr-inline case — and the link still resolves."""
    expected = veggies_link_id()
    poisoned = _poisoned_lab_json(
        tmp_path, {"element": "ghost", "ip": "203.0.113.9", "os_type": "no-such-profile"}
    )
    monkeypatch.setattr(_bed, "lab_data_path", lambda: poisoned)
    assert veggies_link_id() == expected


def test_veggies_link_id_propagates_non_validation_errors(tmp_path, monkeypatch):
    """A record that breaks for a NON-validation reason (here: not a dict at
    all) is a broken fixture file, not an unregistered profile — swallowing it
    hides real corruption behind the skip meant for cross-repo records."""
    poisoned = _poisoned_lab_json(tmp_path, "just-a-string")
    monkeypatch.setattr(_bed, "lab_data_path", lambda: poisoned)
    with pytest.raises(AttributeError):
        veggies_link_id()
