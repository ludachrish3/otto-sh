# Daemon Toolkit + Tunnel Carrier Seam Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the managed-remote-process machinery duplicated between `otto.link` and `otto.tunnel` into `otto.host.daemon` (renamed from `otto.host.detached`), and add a `TunnelCarrier` registry seam so socat is the first pluggable tunnel transport instead of a hardcoded one.

**Architecture:** Toolkit, not framework — `otto.host.daemon` is pure string builders and parsers with **no I/O**; link and tunnel keep their own orchestration, error policies, and reporting. The carrier seam mirrors `otto.link.impairer` exactly (stateless class + `Registry` + `register_*`/`build_*` wrappers, first-party registrant registers at module import).

**Tech Stack:** Python 3.10+ dataclasses, `otto.registry.Registry`, pytest (unit tier), existing live-bed e2e as the behavioral-identity proof.

**Spec:** `docs/superpowers/specs/2026-07-11-daemon-toolkit-and-tunnel-carrier-design.md` (committed on this branch). Read it before starting any task.

## Global Constraints

- **Wire and command bytes are invariant.** Sentinel tokens (`otto-tunnel:v1:…`, `otto-impair:v1:…`) and both ps scan command strings must be byte-identical before/after. Golden-string tests pin them. Any existing test that pins wire bytes must pass UNMODIFIED.
- **Behavior-identical bar:** existing tunnel + link suites pass with nothing but import-path updates, plus exactly TWO sanctioned message changes (Task 7: the unsupported-protocol error now names the carrier; nothing else).
- **No link↔tunnel import edge** in either direction. Both may import `otto.host.daemon`.
- **Clean-break renames** — no aliases, no back-compat shims (house policy).
- Never `from __future__ import annotations` (breaks Sphinx `-W`).
- Rename sweeps must be verified with **case-insensitive** whole-tree grep (`grep -rin`), src + tests + docs. Historical documents under `docs/superpowers/specs/` and `docs/superpowers/plans/` dated before 2026-07-11 are archives — do NOT edit them.
- Lint gate = `uv run ruff check .` AND `uv run ruff format --check .` (implementers routinely forget the second).
- `ty` (typecheck) does NOT run in pytest or `make coverage` — run `make typecheck-python` after src edits.
- Tests must never skip on host-down; the unit tier (`tests/unit`) needs no lab.
- Commits: conventional prefix + `Assisted-by: Claude (Fable 5)` trailer; verify with `git log -1` after committing.
- Run unit tests as shown (single `-n0` pass or plain invocation); do NOT loop or brute-force repeat runs on this VM.

## File Structure

| File | Fate |
|---|---|
| `src/otto/host/detached.py` | `git mv` → `src/otto/host/daemon.py`, then grown (Tasks 1–3) |
| `tests/unit/host/test_detached.py` | `git mv` → `tests/unit/host/test_daemon.py`, then grown |
| `src/otto/tunnel/sentinel.py` | payload codec only; framing via daemon (Task 4) |
| `src/otto/tunnel/discovery.py` | gains `DISCOVERY_PS_COMMAND`; parse loop + `parse_etime` move out (Tasks 2, 4) |
| `src/otto/tunnel/socat.py` | loses re-export + ps constant; gains `SocatCarrier` (Tasks 1, 4, 6) |
| `src/otto/tunnel/carrier.py` | NEW — `TunnelCarrier`, `CARRIERS`, `register_carrier`, `build_carrier` (Task 6) |
| `src/otto/tunnel/manage.py` | `kill_command` use; carrier wiring (Tasks 4, 7) |
| `src/otto/tunnel/model.py` | docstring ride-along fix (Task 8) |
| `src/otto/tunnel/__init__.py` | carrier exports (Task 7) |
| `src/otto/link/sentinel.py` | rewritten over the toolkit (Task 5) |
| `src/otto/link/manage.py` | import path (Task 1); `kill_command` (Task 5) |
| `src/otto/link/__init__.py` | comment strings only (Task 1) |
| `src/otto/cli/tunnel.py` | `--carrier` option (Task 7) |
| `tests/unit/tunnel/test_carrier.py` | NEW (Task 6) |

---

### Task 1: Rename `otto.host.detached` → `otto.host.daemon`

Pure move + parameter generalization. No new functionality.

**Files:**
- Rename: `src/otto/host/detached.py` → `src/otto/host/daemon.py`
- Rename: `tests/unit/host/test_detached.py` → `tests/unit/host/test_daemon.py`
- Modify: `src/otto/link/manage.py:22`, `src/otto/tunnel/socat.py:10`, `src/otto/tunnel/manage.py` (imports), `src/otto/link/__init__.py:23,89` (comments), `tests/unit/link/test_lazy_exports.py` (module-name strings), `tests/unit/link/test_manage_impair.py:321` (comment)
- Check: `docs/` pages that reference `otto.host.detached` (Sphinx autodoc/api pages — `grep -rn "host.detached" docs/ --include=*.md --include=*.rst`)

**Interfaces:**
- Produces: `otto.host.daemon.launch_command(sentinel: str, argv: list[str]) -> str` — same behavior as today's `launch_command(sentinel, socat_args)`; ONLY the parameter name generalizes.

- [ ] **Step 1: git mv both files**

```bash
git mv src/otto/host/detached.py src/otto/host/daemon.py
git mv tests/unit/host/test_detached.py tests/unit/host/test_daemon.py
```

- [ ] **Step 2: Generalize the parameter and docstrings in `daemon.py`**

In `src/otto/host/daemon.py`:

1. Signature: `def launch_command(sentinel: str, socat_args: list[str]) -> str:` → `def launch_command(sentinel: str, argv: list[str]) -> str:`
2. In the body, both uses of `socat_args`: `tagged = " ".join(shlex.quote(a) for a in (sentinel, *argv))`
3. In the function docstring, replace the sentence beginning ``` ``socat_args`` is the FULL program argv (it begins with ``"socat"``) ``` so it reads:

```
``argv`` is the FULL program argv (its first element is the program to run,
e.g. ``"socat"``), so the template must NOT hardcode a program name —
hardcoding one runs ``prog prog <args…>`` and dies on the bogus duplicate.
```

4. Module docstring: replace the last sentence (`Extracted from ``otto.tunnel.socat`` (#2b) so both tunnels and link-impairment timers use one proven launcher without a tunnel<->link import edge.`) with:

```
Extracted from ``otto.tunnel.socat`` (#2b), renamed from ``otto.host.detached``
(2026-07-11): the module owns the daemon lifecycle vocabulary — launch,
discover (ps scan), reap — shared by tunnels and link-impairment timers
without a tunnel<->link import edge.
```

Also change the docstring's first line from `Detached, sentinel-tagged process launching on remote hosts.` to `Sentinel-tagged daemons on remote hosts: launch, discover, reap.`

- [ ] **Step 3: Update every import site**

- `src/otto/link/manage.py:22`: `from ..host.detached import launch_command` → `from ..host.daemon import launch_command`
- `src/otto/tunnel/socat.py:10`: DELETE the line `from ..host.detached import launch_command  # noqa: F401  # back-compat re-export (#3 Task 1)` entirely.
- `src/otto/tunnel/manage.py`: `launch_command` is currently imported in the `from .socat import (...)` block — remove it there and add a new import line `from ..host.daemon import launch_command`.
- `src/otto/link/__init__.py`: in the comment at line 23 and the `__getattr__` docstring at line 89, replace `otto.host.detached` with `otto.host.daemon`.
- `tests/unit/host/test_daemon.py`: update its import(s) from `otto.host.detached` to `otto.host.daemon`, and rename any `socat_args=` keyword usages to `argv=` (run `grep -n "detached\|socat_args" tests/unit/host/test_daemon.py` and fix every hit).
- `tests/unit/link/test_lazy_exports.py`: replace every `otto.host.detached` string (docstring at line 5, and the sys.modules-name strings near lines 50/55/69) with `otto.host.daemon`. These are load-bearing string literals — the test checks `'otto.host.daemon' in sys.modules`.
- `tests/unit/link/test_manage_impair.py:321`: comment `otto.host.detached.launch_command` → `otto.host.daemon.launch_command`.
- Any `docs/` hits from `grep -rn "host.detached" docs/ --include=*.md --include=*.rst` OUTSIDE `docs/superpowers/` archives: update the module path.

- [ ] **Step 4: Case-insensitive sweep to verify nothing is left**

```bash
grep -rin "detached" src/ tests/ --include=*.py | grep -vi "detached UDP listener\|recover-marker write detached\|Launch a detached"
```

Expected: zero hits referring to the MODULE (`otto.host.detached` / `host/detached`). Prose uses of the word "detached" describing process state (the three grep-excluded phrases, e.g. `tests/e2e/test_tunnel_e2e.py:255`, `src/otto/host/session.py:405`, `src/otto/link/manage.py` timer docstring) are fine and stay.

- [ ] **Step 5: Run the affected unit suites**

```bash
uv run pytest tests/unit/host/test_daemon.py tests/unit/link tests/unit/tunnel -n0 -q
```

Expected: all pass, zero import errors.

- [ ] **Step 6: Lint + typecheck**

```bash
uv run ruff check . && uv run ruff format --check . && make typecheck-python
```

Expected: `All checks passed!` / `N files already formatted` / ty passes.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(host)!: rename otto.host.detached -> otto.host.daemon

Clean break, no alias. launch_command's socat_args param generalizes to
argv. Drops tunnel/socat.py's back-compat re-export; import sites and
the link lazy-export module-name pins updated.

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

---

### Task 2: Toolkit — ps scan command, ps parser, etime, kill

**Files:**
- Modify: `src/otto/host/daemon.py` (add functions), `src/otto/tunnel/discovery.py` (remove `parse_etime`, import it), `tests/unit/tunnel/test_discovery.py` (move `parse_etime` tests out)
- Test: `tests/unit/host/test_daemon.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (all in `otto.host.daemon`):
  - `ps_scan_command(prefix: str) -> str`
  - `parse_etime(text: str) -> int` (moved verbatim from `otto.tunnel.discovery`)
  - `DaemonProcess` frozen dataclass: `pid: int`, `age_seconds: int`, `token: str`
  - `parse_ps_output(output: str, prefix: str) -> list[DaemonProcess]`
  - `kill_command(pids: Iterable[int]) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_daemon.py`:

```python
from otto.host.daemon import (
    DaemonProcess,
    kill_command,
    parse_etime,
    parse_ps_output,
    ps_scan_command,
)


class TestPsScanCommand:
    def test_tunnel_prefix_is_byte_identical_to_the_retired_literal(self):
        # STABILITY CONTRACT: this exact command string is what shipped in
        # otto.tunnel.socat.DISCOVERY_PS_COMMAND. Never change these bytes.
        assert ps_scan_command("otto-tunnel") == (
            "ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' otto-tunnel:' || true"
        )

    def test_impair_prefix_is_byte_identical_to_the_retired_literal(self):
        assert ps_scan_command("otto-impair") == (
            "ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' otto-impair:' || true"
        )


class TestParseEtime:
    # Moved from tests/unit/tunnel/test_discovery.py (the function moved).
    def test_bare_seconds(self):
        assert parse_etime("42") == 42

    def test_mm_ss(self):
        assert parse_etime("02:03") == 123

    def test_hh_mm_ss(self):
        assert parse_etime("01:02:03") == 3723

    def test_days(self):
        assert parse_etime("2-01:02:03") == 2 * 86400 + 3723

    def test_garbage_is_zero_not_an_error(self):
        assert parse_etime("garbage") == 0


class TestParsePsOutput:
    PREFIX = "otto-test"

    def test_extracts_pid_age_and_token(self):
        out = parse_ps_output(f"  123 01:00 bash {self.PREFIX}:v1:a:b extra", self.PREFIX)
        assert out == [DaemonProcess(pid=123, age_seconds=60, token=f"{self.PREFIX}:v1:a:b")]

    def test_skips_short_lines(self):
        assert parse_ps_output("123 01:00", self.PREFIX) == []

    def test_skips_non_numeric_pid(self):
        assert parse_ps_output(f"abc 01:00 {self.PREFIX}:v1:a", self.PREFIX) == []

    def test_skips_lines_without_our_token(self):
        assert parse_ps_output("123 01:00 socat TCP4-LISTEN:9 TCP4:h:9", self.PREFIX) == []

    def test_foreign_prefix_not_matched(self):
        assert parse_ps_output("123 01:00 other-tool:v1:a", self.PREFIX) == []

    def test_token_must_start_a_word(self):
        # The token is found by str.startswith on whitespace-split words.
        assert parse_ps_output(f"123 01:00 x{self.PREFIX}:v1:a", self.PREFIX) == []

    def test_multiple_lines(self):
        text = f"1 00:01 {self.PREFIX}:v1:a\n\n2 00:02 {self.PREFIX}:v1:b\n"
        assert [p.pid for p in parse_ps_output(text, self.PREFIX)] == [1, 2]


class TestKillCommand:
    def test_sorts_pids(self):
        assert kill_command([30, 10, 20]) == "kill 10 20 30"

    def test_single_pid(self):
        assert kill_command([7]) == "kill 7"
```

Also: MOVE any existing `parse_etime` test cases out of `tests/unit/tunnel/test_discovery.py` (find them with `grep -n "parse_etime" tests/unit/tunnel/test_discovery.py`). Delete them there — the class above replaces them. If the existing cases cover inputs not listed above, carry those cases into `TestParseEtime` instead of dropping them.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/host/test_daemon.py -n0 -q
```

Expected: FAIL with `ImportError: cannot import name 'ps_scan_command'`.

- [ ] **Step 3: Implement in `src/otto/host/daemon.py`**

Add imports at the top (keep the existing `import shlex`):

```python
from collections.abc import Iterable
from dataclasses import dataclass
```

Append after `launch_command`:

```python
_ETIME_MAX_FIELDS = 3
_MIN_PS_FIELDS = 3


def ps_scan_command(prefix: str) -> str:
    """Portable ``ps`` scan for daemons whose argv[0] starts with ``<prefix>:``.

    Each field is its own ``-eo`` flag rather than one comma-joined
    ``-eo pid=,etime=,args=`` (found via live-bed e2e against a centos:7
    container): procps-ng 3.3.10 silently mis-parses the comma-combined form
    (columns bleed into each other), while the separate-flag form produces
    identical output on modern procps (4.x) too. Formatted ``etime`` (not
    ``etimes``) keeps 2.6.32-era userland working; ``|| true`` so a no-match
    grep (exit 1) is not a command failure.
    """
    return f"ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' {prefix}:' || true"


def parse_etime(text: str) -> int:
    """Procps ``etime`` (``[[DD-]HH:]MM:SS`` or bare ``SS``) → seconds.

    Returns ``0`` for anything unparseable rather than raising — one host
    emitting a malformed ``etime`` must not take down a whole scan.
    """
    try:
        days = 0
        if "-" in text:
            d, _, text = text.partition("-")
            days = int(d)
        parts = [int(p) for p in text.split(":")]
        while len(parts) < _ETIME_MAX_FIELDS:
            parts.insert(0, 0)
        h, m, s = parts[-3], parts[-2], parts[-1]
        return days * 86400 + h * 3600 + m * 60 + s
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class DaemonProcess:
    """One sentinel-tagged daemon seen in a ps scan (token not yet decoded)."""

    pid: int
    age_seconds: int
    token: str


def parse_ps_output(output: str, prefix: str) -> list[DaemonProcess]:
    """Reconstruct tagged daemons from :func:`ps_scan_command` output.

    Domain modules decode each :attr:`DaemonProcess.token` with their own
    sentinel parser; anything undecodable is theirs to skip.
    """
    needle = f"{prefix}:"
    out: list[DaemonProcess] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < _MIN_PS_FIELDS or not fields[0].isdigit():
            continue
        token = next((w for w in fields[2:] if w.startswith(needle)), None)
        if token is None:
            continue
        out.append(
            DaemonProcess(pid=int(fields[0]), age_seconds=parse_etime(fields[1]), token=token)
        )
    return out


def kill_command(pids: Iterable[int]) -> str:
    """``kill <sorted pids>`` line for reaping tagged daemons on one host."""
    return f"kill {' '.join(str(p) for p in sorted(pids))}"
```

- [ ] **Step 4: Move `parse_etime` out of `src/otto/tunnel/discovery.py`**

In `src/otto/tunnel/discovery.py`:
1. Delete the whole `parse_etime` function AND the `_ETIME_MAX_FIELDS = 3` constant.
2. Add `from ..host.daemon import parse_etime` to the imports.
3. Leave everything else untouched in this task (the parse loop is Task 4).

Check for other importers: `grep -rn "parse_etime" src/ tests/ --include=*.py` — update any remaining `from otto.tunnel.discovery import ... parse_etime` to import from `otto.host.daemon`.

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/unit/host/test_daemon.py tests/unit/tunnel/test_discovery.py -n0 -q
```

Expected: PASS.

- [ ] **Step 6: Lint + typecheck**

```bash
uv run ruff check . && uv run ruff format --check . && make typecheck-python
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(host): daemon toolkit — ps scan/parse, etime, kill builders

ps_scan_command is the one home for the procps-ng 3.3.10 separate-flags
lesson; golden tests pin both retired literals byte-for-byte. parse_etime
moves from tunnel/discovery.

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

---

### Task 3: Toolkit — sentinel framing

**Files:**
- Modify: `src/otto/host/daemon.py`
- Test: `tests/unit/host/test_daemon.py`

**Interfaces:**
- Produces (all in `otto.host.daemon`):
  - `enc(value: str | int | None) -> str` — percent-encode (`safe=""`); `None` → `""`
  - `dec(segment: str) -> str` — `unquote`
  - `encode_token(prefix: str, version: str, segments: Sequence[str]) -> str` — segments are the PAYLOAD only, already-final strings; framing never re-encodes
  - `split_token(token: str, prefix: str, version: str, count: int) -> list[str] | None` — `count` = payload segment count; returns exactly `count` raw payload segments or `None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_daemon.py`:

```python
from otto.host.daemon import dec, enc, encode_token, split_token


class TestSentinelFraming:
    def test_enc_percent_encodes_everything(self):
        assert enc("a:b c/d") == "a%3Ab%20c%2Fd"

    def test_enc_none_is_empty(self):
        assert enc(None) == ""

    def test_enc_int(self):
        assert enc(8080) == "8080"

    def test_dec_reverses_enc(self):
        assert dec(enc("a:b c/d")) == "a:b c/d"

    def test_encode_token_layout(self):
        assert encode_token("otto-x", "v1", ("a", "b")) == "otto-x:v1:a:b"

    def test_encode_does_not_reencode_segments(self):
        # Framing must pass final segment strings through verbatim —
        # otto-tunnel's path segment is double-encoded by its OWN codec.
        assert encode_token("otto-x", "v1", ("a%3Ab",)) == "otto-x:v1:a%3Ab"

    def test_split_round_trip(self):
        assert split_token("otto-x:v1:a:b", "otto-x", "v1", 2) == ["a", "b"]

    def test_split_wrong_prefix_is_none(self):
        assert split_token("otto-y:v1:a:b", "otto-x", "v1", 2) is None

    def test_split_wrong_version_is_none(self):
        assert split_token("otto-x:v2:a:b", "otto-x", "v1", 2) is None

    def test_split_wrong_count_is_none(self):
        assert split_token("otto-x:v1:a", "otto-x", "v1", 2) is None
        assert split_token("otto-x:v1:a:b:c", "otto-x", "v1", 2) is None

    def test_split_preserves_empty_segments(self):
        assert split_token("otto-x:v1::b", "otto-x", "v1", 2) == ["", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/host/test_daemon.py::TestSentinelFraming -n0 -q
```

Expected: FAIL with `ImportError: cannot import name 'enc'`.

- [ ] **Step 3: Implement in `src/otto/host/daemon.py`**

Extend the imports:

```python
from collections.abc import Iterable, Sequence
from urllib.parse import quote, unquote
```

Append:

```python
def enc(value: str | int | None) -> str:
    """Percent-encode one sentinel segment (``safe=""``); ``None`` → empty."""
    return quote(str(value), safe="") if value is not None else ""


def dec(segment: str) -> str:
    """Decode one percent-encoded sentinel segment."""
    return unquote(segment)


def encode_token(prefix: str, version: str, segments: Sequence[str]) -> str:
    """``<prefix>:<version>:<payload segments joined with ':'>``.

    *segments* are the payload only and must be FINAL strings (already
    percent-encoded as the domain codec requires) — framing never re-encodes,
    so a domain is free to double-encode a compound segment.
    """
    return ":".join((prefix, version, *segments))


def split_token(token: str, prefix: str, version: str, count: int) -> list[str] | None:
    """Split a wire token; ``None`` for non-matching / other-version / malformed.

    *count* is the expected PAYLOAD segment count (prefix and version are
    checked separately). Unknown versions parse to ``None``, never an error —
    the stability contract lets old parsers ignore newer wire formats.
    """
    parts = token.split(":")
    if len(parts) != count + 2 or parts[0] != prefix or parts[1] != version:
        return None
    return parts[2:]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/host/test_daemon.py -n0 -q
```

Expected: PASS.

- [ ] **Step 5: Lint + typecheck, then commit**

```bash
uv run ruff check . && uv run ruff format --check . && make typecheck-python
git add -A
git commit -m "feat(host): sentinel framing — enc/dec + encode_token/split_token

Framing joins caller-final payload segments and checks prefix/version/
count with parse-to-None semantics; it never re-encodes, so domain codecs
keep full control of their segment bytes.

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

---

### Task 4: Tunnel onto the toolkit

**Files:**
- Modify: `src/otto/tunnel/sentinel.py`, `src/otto/tunnel/discovery.py`, `src/otto/tunnel/socat.py`, `src/otto/tunnel/manage.py`
- Modify (imports only): `tests/unit/tunnel/test_manage_add.py`, `tests/unit/tunnel/test_manage_remove.py`, `tests/e2e/test_tunnel_e2e.py` — plus `tests/unit/tunnel/test_socat.py` / `test_discovery.py` if they import `DISCOVERY_PS_COMMAND` from socat (grep first)
- Test: `tests/unit/tunnel/test_sentinel.py` (golden addition only)

**Interfaces:**
- Consumes: `enc`, `dec`, `encode_token`, `split_token`, `ps_scan_command`, `parse_ps_output`, `kill_command` from `otto.host.daemon` (Tasks 2–3 signatures).
- Produces: `otto.tunnel.discovery.DISCOVERY_PS_COMMAND` (moved home — same bytes); everything else keeps its public name and signature.

- [ ] **Step 1: Add the golden-token test FIRST (it must pass before AND after)**

Append to `tests/unit/tunnel/test_sentinel.py`:

```python
class TestWireGolden:
    def test_encode_produces_the_exact_v1_bytes(self):
        # STABILITY CONTRACT (spec §5): these bytes are what live processes
        # carry in argv[0]. If this test fails, the refactor broke the wire.
        tunnel = Tunnel(
            protocol="tcp",
            service_port=8080,
            path=(TunnelHop(host="h1", interface="eth0"), TunnelHop(host="h2")),
            dest=None,
            id="tun-abc-8080",
        )
        token = encode_sentinel(
            tunnel, direction=Direction.FWD, role=Role.INGRESS, hop_index=0, carrier_port=50000
        )
        assert token == (
            "otto-tunnel:v1:tun-abc-8080:tcp:8080:50000:fwd:ingress:0::h1%2540eth0%2Ch2"
        )
        parsed = parse_sentinel(token)
        assert parsed is not None
        assert parsed.tunnel == tunnel
```

(Use the imports the file already has; add any missing ones from `otto.tunnel.model` / `otto.tunnel.sentinel`.)

- [ ] **Step 2: Run it against the CURRENT code**

```bash
uv run pytest tests/unit/tunnel/test_sentinel.py -n0 -q
```

Expected: PASS. The literal was verified by executing the pre-refactor code on 2026-07-11 — `repr(token)` printed exactly this string (path entries `h1@eth0`,`h2` → each `quote(..., safe="")` → `h1%40eth0,h2` → joined string quoted again → `h1%2540eth0%2Ch2`). If it somehow fails, STOP and re-derive from a scratch run of current code before touching anything. Commit the golden separately:

```bash
git add tests/unit/tunnel/test_sentinel.py
git commit -m "test(tunnel): golden v1 sentinel bytes ahead of the framing refactor

Assisted-by: Claude (Fable 5)"
```

- [ ] **Step 3: Rewrite `src/otto/tunnel/sentinel.py` over the framing**

Replace the imports and the three framing-mechanical functions; keep the module docstring, `ParsedSentinel`, `_encode_path`, `_decode_path` exactly as they are. The result:

```python
from dataclasses import dataclass
from urllib.parse import quote, unquote

from ..host.daemon import dec, enc, encode_token, split_token
from .model import Direction, Role, Tunnel, TunnelHop

SENTINEL_PREFIX = "otto-tunnel"
SENTINEL_VERSION = "v1"
_PAYLOAD_SEGMENTS = 9
```

Delete `_SEGMENT_COUNT = 11` and the local `def _enc(...)`. `_encode_path`/`_decode_path` keep using `quote`/`unquote` directly (their double-encoding is domain logic).

`encode_sentinel` becomes:

```python
def encode_sentinel(
    tunnel: Tunnel, *, direction: Direction, role: Role, hop_index: int, carrier_port: int
) -> str:
    """Return the wire token for one process of *tunnel*."""
    payload = (
        enc(tunnel.id),
        enc(tunnel.protocol),
        enc(tunnel.service_port),
        enc(carrier_port),
        direction.value,
        role.value,
        str(hop_index),
        enc(tunnel.dest) if tunnel.dest is not None else "",
        _encode_path(tunnel.path),
    )
    return encode_token(SENTINEL_PREFIX, SENTINEL_VERSION, payload)
```

`parse_sentinel` becomes (note: indices shift down by 2 versus the old `parts`):

```python
def parse_sentinel(token: str) -> ParsedSentinel | None:
    """Parse one wire token; ``None`` for non-otto / other-version / malformed."""
    payload = split_token(token, SENTINEL_PREFIX, SENTINEL_VERSION, _PAYLOAD_SEGMENTS)
    if payload is None:
        return None
    tunnel_id, proto = dec(payload[0]), dec(payload[1])
    if not tunnel_id or not proto:
        return None
    try:
        service_port = int(dec(payload[2]))
        carrier_port = int(dec(payload[3]))
        direction = Direction(payload[4])
        role = Role(payload[5])
        hop_index = int(payload[6])
    except ValueError:
        return None
    dest = dec(payload[7]) or None
    path = _decode_path(payload[8])
    if path is None:
        return None
    try:
        tunnel = Tunnel(
            protocol=proto, service_port=service_port, path=path, dest=dest, id=tunnel_id
        )
    except ValueError:
        return None
    return ParsedSentinel(
        tunnel=tunnel,
        direction=direction,
        role=role,
        hop_index=hop_index,
        carrier_port=carrier_port,
    )
```

Also update the module docstring sentence `11 colon-joined segments, each percent-encoded; empty segment = None.` to `Prefix + version + 9 payload segments (framing: otto.host.daemon), each percent-encoded; empty segment = None.`

- [ ] **Step 4: Move the ps constant and rewire the discovery parse loop**

In `src/otto/tunnel/discovery.py`:

1. Remove `from .socat import DISCOVERY_PS_COMMAND` and the `_PS_MIN_FIELDS = 3` constant.
2. Extend the daemon import: `from ..host.daemon import parse_etime, parse_ps_output, ps_scan_command` (keep `parse_etime` if other code in the module still calls it; if nothing does after this step, drop it from the import).
3. Define the constant here, right after the module-level constants:

```python
DISCOVERY_PS_COMMAND: str = ps_scan_command(SENTINEL_PREFIX)
"""The lab-wide daemon scan for tunnel processes. Built by
:func:`otto.host.daemon.ps_scan_command` — see it for the procps
portability story. The bytes are pinned by TestWireGolden's sibling in
tests/unit/host/test_daemon.py (STABILITY CONTRACT)."""
```

4. Rewrite `parse_process_discovery`:

```python
def parse_process_discovery(ps_output: str) -> list[Observation]:
    """Reconstruct observations from :data:`DISCOVERY_PS_COMMAND` output."""
    out: list[Observation] = []
    for proc in parse_ps_output(ps_output, SENTINEL_PREFIX):
        parsed = parse_sentinel(proc.token)
        if parsed is None:
            continue
        out.append(Observation(pid=proc.pid, age_seconds=proc.age_seconds, parsed=parsed))
    return out
```

5. The `from .sentinel import ...` line keeps `SENTINEL_PREFIX, ParsedSentinel, parse_sentinel`.

In `src/otto/tunnel/socat.py`: delete `DISCOVERY_PS_COMMAND` and its docstring block entirely (its story now lives on `ps_scan_command`). Keep `FREE_PORT_PROBE_COMMAND`, `parse_listening_ports`, `pick_free_port`, `_LISTEN`/`_DELIVER`, and the three `*_socat_args` builders exactly where they are — the port helpers are deliberately NOT moving (carrier-agnostic manage-level machinery; spec §3).

In `src/otto/tunnel/manage.py`:
1. Remove `DISCOVERY_PS_COMMAND` from any import if present (it is not imported there today — verify with grep).
2. Add `kill_command` to the daemon import line (now `from ..host.daemon import kill_command, launch_command`).
3. In `_kill_tunnel_on`: replace `kill_cmd = f"kill {' '.join(str(p) for p in sorted(pids))}"` with `kill_cmd = kill_command(pids)`.
4. In `_reap`: same replacement.

- [ ] **Step 5: Update the `DISCOVERY_PS_COMMAND` import sites**

```bash
grep -rln "from otto.tunnel.socat import" tests/
```

For each hit (`tests/unit/tunnel/test_manage_add.py`, `tests/unit/tunnel/test_manage_remove.py`, `tests/e2e/test_tunnel_e2e.py`, possibly others): move `DISCOVERY_PS_COMMAND` into a `from otto.tunnel.discovery import DISCOVERY_PS_COMMAND` import (keep `FREE_PORT_PROBE_COMMAND` etc. imported from socat).

- [ ] **Step 6: Run the tunnel suites — the golden and every existing test must pass**

```bash
uv run pytest tests/unit/tunnel tests/unit/host/test_daemon.py -n0 -q
```

Expected: PASS, including `TestWireGolden` unchanged from Step 2.

- [ ] **Step 7: Lint + typecheck, then commit**

```bash
uv run ruff check . && uv run ruff format --check . && make typecheck-python
git add -A
git commit -m "refactor(tunnel): ride the daemon toolkit — framing, ps scan, kill

Sentinel framing and the ps parse loop delegate to otto.host.daemon;
DISCOVERY_PS_COMMAND moves to discovery.py (bytes pinned unchanged).
Wire golden proves v1 tokens are byte-identical.

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

---

### Task 5: Link onto the toolkit

**Files:**
- Modify: `src/otto/link/sentinel.py` (rewrite), `src/otto/link/manage.py` (`kill_command`)
- Test: `tests/unit/link/test_impair_sentinel.py` (golden additions only)

**Interfaces:**
- Consumes: `enc`, `dec`, `encode_token`, `split_token`, `ps_scan_command`, `parse_ps_output`, `kill_command` from `otto.host.daemon`.
- Produces: same public names/signatures as today — `IMPAIR_SENTINEL_PREFIX`, `IMPAIR_SENTINEL_VERSION`, `IMPAIR_PS_COMMAND`, `encode_impair_sentinel(link_id, netdev) -> str`, `parse_impair_sentinel(token) -> tuple[str, str] | None`, `parse_impair_ps(output) -> list[tuple[int, str, str]]`.

- [ ] **Step 1: Add goldens FIRST (must pass before and after)**

Append to `tests/unit/link/test_impair_sentinel.py`:

```python
class TestWireGolden:
    def test_ps_command_is_byte_identical_to_the_retired_literal(self):
        assert IMPAIR_PS_COMMAND == (
            "ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' otto-impair:' || true"
        )

    def test_encode_produces_the_exact_v1_bytes(self):
        assert encode_impair_sentinel("lnk-1", "eth0.100") == "otto-impair:v1:lnk-1:eth0.100"
        assert encode_impair_sentinel("a:b", "e/th") == "otto-impair:v1:a%3Ab:e%2Fth"
```

(Extend the file's existing imports with `encode_impair_sentinel` if needed.)

Run against current code — must PASS:

```bash
uv run pytest tests/unit/link/test_impair_sentinel.py -n0 -q
```

Commit the goldens:

```bash
git add tests/unit/link/test_impair_sentinel.py
git commit -m "test(link): golden impair sentinel + ps-command bytes ahead of refactor

Assisted-by: Claude (Fable 5)"
```

- [ ] **Step 2: Rewrite `src/otto/link/sentinel.py`**

Full new content:

```python
"""otto-impair argv sentinel + expire-timer discovery (spec §7).

Wire format: ``otto-impair:v1:<link-id>:<netdev>`` with percent-encoded
segments. Same philosophy as the tunnel sentinel: the timer process's argv IS
the state — discoverable via ``ps``, unambiguously otto's, owner-agnostic.
Framing, ps scanning, and percent-encoding ride :mod:`otto.host.daemon`.
"""

from ..host.daemon import dec, enc, encode_token, parse_ps_output, ps_scan_command, split_token

IMPAIR_SENTINEL_PREFIX = "otto-impair"
IMPAIR_SENTINEL_VERSION = "v1"
_PAYLOAD_SEGMENTS = 2

IMPAIR_PS_COMMAND: str = ps_scan_command(IMPAIR_SENTINEL_PREFIX)
"""The per-host expire-timer scan. Built by
:func:`otto.host.daemon.ps_scan_command` — see it for the procps
portability story; bytes pinned by ``TestWireGolden``."""


def encode_impair_sentinel(link_id: str, netdev: str) -> str:
    """Sentinel token tagging one placement's expire timer."""
    return encode_token(
        IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, (enc(link_id), enc(netdev))
    )


def parse_impair_sentinel(token: str) -> tuple[str, str] | None:
    """Decode a sentinel token to ``(link_id, netdev)``; ``None`` if not ours."""
    payload = split_token(token, IMPAIR_SENTINEL_PREFIX, IMPAIR_SENTINEL_VERSION, _PAYLOAD_SEGMENTS)
    if payload is None:
        return None
    return dec(payload[0]), dec(payload[1])


def parse_impair_ps(output: str) -> list[tuple[int, str, str]]:
    """Reconstruct ``(pid, link_id, netdev)`` from :data:`IMPAIR_PS_COMMAND` output."""
    out: list[tuple[int, str, str]] = []
    for proc in parse_ps_output(output, IMPAIR_SENTINEL_PREFIX):
        parsed = parse_impair_sentinel(proc.token)
        if parsed is None:
            continue
        out.append((proc.pid, parsed[0], parsed[1]))
    return out
```

Note the ONE observable-behavior nuance to preserve: today `parse_impair_sentinel` checks only prefix/version/count and does NOT reject empty decoded values — keep that (no `if not link_id` guard; the tunnel codec's non-empty checks are ITS domain rules, not link's).

- [ ] **Step 3: Use `kill_command` in `src/otto/link/manage.py`**

1. Change the daemon import line to `from ..host.daemon import kill_command, launch_command`.
2. In `_cancel_timers`, replace `await _root_run(host, f"kill {' '.join(str(pid) for pid in pids)}")` with `await _root_run(host, kill_command(pids))` and drop the now-redundant `sorted(...)` wrapper where `pids` is built (`pids = [pid for pid, lid, dev in parse_impair_ps(result.value) if lid == link_id and dev == netdev]` — `kill_command` sorts).

- [ ] **Step 4: Run the link suites**

```bash
uv run pytest tests/unit/link -n0 -q
```

Expected: PASS, goldens unchanged.

- [ ] **Step 5: Lint + typecheck, then commit**

```bash
uv run ruff check . && uv run ruff format --check . && make typecheck-python
git add -A
git commit -m "refactor(link): ride the daemon toolkit — framing, ps scan, kill

link/sentinel.py halves: framing/encoding/ps parsing delegate to
otto.host.daemon; wire bytes pinned unchanged by the goldens.

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

---

### Task 6: The carrier seam — `otto.tunnel.carrier` + `SocatCarrier`

**Files:**
- Create: `src/otto/tunnel/carrier.py`
- Modify: `src/otto/tunnel/socat.py` (add `SocatCarrier` + registration; docstring)
- Test: `tests/unit/tunnel/test_carrier.py` (new)

**Interfaces:**
- Consumes: `Registry`, `caller_module` from `otto.registry`; the three `*_socat_args` builders in `otto.tunnel.socat`.
- Produces:
  - `TunnelCarrier` with `supported_protocols: ClassVar[frozenset[str]]`, `requirements_command: ClassVar[str]`, `tools_description: ClassVar[str]`, and methods `ingress_args(self, protocol: str, service_port: int, bind_ip: str, next_ip: str, carrier_port: int) -> list[str]`, `relay_args(self, carrier_port: int, next_ip: str) -> list[str]`, `egress_args(self, protocol: str, service_port: int, deliver_ip: str, carrier_port: int) -> list[str]`
  - `CARRIERS: Registry[type[TunnelCarrier]]`, `register_carrier(name, cls, *, overwrite=False)`, `build_carrier(name) -> type[TunnelCarrier]`
  - `SocatCarrier` registered as `"socat"` (import of `otto.tunnel.socat` triggers registration)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/tunnel/test_carrier.py`:

```python
"""The TunnelCarrier contract + CARRIERS registry (mirrors the impairer seam)."""

import pytest

from otto.tunnel.carrier import CARRIERS, TunnelCarrier, build_carrier, register_carrier
from otto.tunnel.socat import (
    SocatCarrier,
    egress_socat_args,
    ingress_socat_args,
    relay_socat_args,
)


class TestRegistry:
    def test_socat_is_registered_first_party(self):
        assert build_carrier("socat") is SocatCarrier

    def test_unknown_name_is_a_rich_error(self):
        with pytest.raises(ValueError, match="Unknown carrier 'wireguard'"):
            build_carrier("wireguard")
        with pytest.raises(ValueError, match="register_carrier"):
            build_carrier("wireguard")

    def test_register_rejects_empty_supported_protocols(self):
        class NoProtocols(TunnelCarrier):
            requirements_command = "true"
            tools_description = "nothing"

        with pytest.raises(ValueError, match="supported_protocols is empty"):
            register_carrier("broken", NoProtocols)

    def test_custom_carrier_registers_and_resolves(self):
        class FakeCarrier(TunnelCarrier):
            supported_protocols = frozenset({"tcp"})
            requirements_command = "command -v fake >/dev/null 2>&1 && echo ok || echo no"
            tools_description = "fake"

            def ingress_args(self, protocol, service_port, bind_ip, next_ip, carrier_port):
                return ["fake", "ingress"]

            def relay_args(self, carrier_port, next_ip):
                return ["fake", "relay"]

            def egress_args(self, protocol, service_port, deliver_ip, carrier_port):
                return ["fake", "egress"]

        register_carrier("fake", FakeCarrier)
        try:
            assert build_carrier("fake") is FakeCarrier
            assert "fake" in CARRIERS
        finally:
            CARRIERS.unregister("fake")


class TestSocatCarrier:
    def test_delegates_to_the_proven_builders(self):
        c = SocatCarrier()
        assert c.ingress_args("tcp", 8080, "10.0.0.1", "10.0.0.2", 50000) == ingress_socat_args(
            "tcp", 8080, "10.0.0.1", "10.0.0.2", 50000
        )
        assert c.relay_args(50000, "10.0.0.3") == relay_socat_args(50000, "10.0.0.3")
        assert c.egress_args("udp", 53, "127.0.0.1", 50001) == egress_socat_args(
            "udp", 53, "127.0.0.1", 50001
        )

    def test_ingress_argv_golden(self):
        # Pin the exact argv shipped by #2b — the carrier must not change it.
        assert SocatCarrier().ingress_args("tcp", 8080, "10.0.0.1", "10.0.0.2", 50000) == [
            "socat",
            "TCP4-LISTEN:8080,bind=10.0.0.1,fork,reuseaddr",
            "TCP4:10.0.0.2:50000",
        ]

    def test_protocols_and_requirements(self):
        assert SocatCarrier.supported_protocols == frozenset({"tcp", "udp"})
        assert "socat" in SocatCarrier.requirements_command
        assert SocatCarrier.tools_description == "socat and/or bash"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/tunnel/test_carrier.py -n0 -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'otto.tunnel.carrier'`.

- [ ] **Step 3: Create `src/otto/tunnel/carrier.py`**

```python
"""Pluggable tunnel carriers: the ``TunnelCarrier`` contract + ``CARRIERS`` registry.

Mirrors the impairer registry (``otto.link.impairer``): custom carriers
register from init modules under a name; ``otto tunnel add --carrier`` selects
one per tunnel (chain-wide). Socat is the only first-party registrant
(``otto.tunnel.socat``).

A carrier decides what each tagged process EXECUTES — nothing more. The 2n
process topology (ingress/relay/egress x fwd/rev), the sentinel v1 wire
format, free-port allocation, discovery, verify, and remove are all
carrier-agnostic (spec 2026-07-11). The carrier name is deliberately not on
the wire: a tunnel's identity is its path+protocol+port, and remove reaps by
pid, so it tears down any carrier's processes.
"""

from typing import ClassVar

from ..registry import Registry, caller_module


class TunnelCarrier:
    """Builds the argv each tunnel process executes.

    Stateless: implementations build argv lists; the orchestration layer
    (``otto.tunnel.manage``) launches them on hosts via
    ``otto.host.daemon.launch_command``.
    """

    supported_protocols: ClassVar[frozenset[str]] = frozenset()
    """Service protocols this carrier can forward (e.g. ``frozenset({"tcp"})``)."""

    requirements_command: ClassVar[str] = ""
    """Complete shell probe run on every chain host; prints ``ok`` iff satisfied."""

    tools_description: ClassVar[str] = ""
    """Human summary of the required tools, for the missing-tools error."""

    def ingress_args(
        self, protocol: str, service_port: int, bind_ip: str, next_ip: str, carrier_port: int
    ) -> list[str]:
        """Argv accepting client traffic on the service port, shipping to the carrier."""
        raise NotImplementedError

    def relay_args(self, carrier_port: int, next_ip: str) -> list[str]:
        """Argv for an intermediate-hop pass-through (same carrier port both sides)."""
        raise NotImplementedError

    def egress_args(
        self, protocol: str, service_port: int, deliver_ip: str, carrier_port: int
    ) -> list[str]:
        """Argv accepting the carrier and delivering to the local service."""
        raise NotImplementedError


CARRIERS: Registry[type[TunnelCarrier]] = Registry(
    "carrier", register_hint="otto.tunnel.register_carrier()"
)


def register_carrier(name: str, cls: type[TunnelCarrier], *, overwrite: bool = False) -> None:
    """Make a custom carrier selectable via ``--carrier <name>``.

    Call from an init module listed in ``.otto/settings.toml``. The carrier
    must declare a non-empty :attr:`TunnelCarrier.supported_protocols`;
    otherwise it could never validate any tunnel and is rejected here.
    """
    if not cls.supported_protocols:
        raise ValueError(
            f"register_carrier({name!r}): cls.supported_protocols is empty; a carrier "
            f"must declare at least one protocol (e.g. frozenset({{'tcp'}}))."
        )
    CARRIERS.register(name, cls, overwrite=overwrite, origin=caller_module())


def build_carrier(name: str) -> type[TunnelCarrier]:
    """Return the carrier class registered under *name* (rich unknown-name error)."""
    return CARRIERS.get(name)
```

- [ ] **Step 4: Add `SocatCarrier` to `src/otto/tunnel/socat.py`**

Append at the end of the file (after `pick_free_port`), and extend the imports with `from typing import ClassVar` plus `from typing_extensions import override` and `from .carrier import TunnelCarrier, register_carrier`:

```python
class SocatCarrier(TunnelCarrier):
    """socat over a TCP4 carrier — the first-party tunnel transport (#2b)."""

    supported_protocols: ClassVar[frozenset[str]] = frozenset({"tcp", "udp"})
    requirements_command: ClassVar[str] = (
        "command -v socat >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 "
        "&& echo ok || echo no"
    )
    tools_description: ClassVar[str] = "socat and/or bash"

    @override
    def ingress_args(
        self, protocol: str, service_port: int, bind_ip: str, next_ip: str, carrier_port: int
    ) -> list[str]:
        """Delegate to :func:`ingress_socat_args` (the proven builder)."""
        return ingress_socat_args(protocol, service_port, bind_ip, next_ip, carrier_port)

    @override
    def relay_args(self, carrier_port: int, next_ip: str) -> list[str]:
        """Delegate to :func:`relay_socat_args`."""
        return relay_socat_args(carrier_port, next_ip)

    @override
    def egress_args(
        self, protocol: str, service_port: int, deliver_ip: str, carrier_port: int
    ) -> list[str]:
        """Delegate to :func:`egress_socat_args`."""
        return egress_socat_args(protocol, service_port, deliver_ip, carrier_port)


register_carrier("socat", SocatCarrier)
```

The requirements string must be BYTE-IDENTICAL to the probe currently inline in `manage._require_tools` (`src/otto/tunnel/manage.py:267-268`) — copy it from there, do not retype it.

Update the socat module docstring first line to: `"""The socat carrier: pure command/argv builders + the SocatCarrier registrant — no I/O.`

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/unit/tunnel/test_carrier.py tests/unit/tunnel -n0 -q
```

Expected: PASS.

- [ ] **Step 6: Lint + typecheck, then commit**

```bash
uv run ruff check . && uv run ruff format --check . && make typecheck-python
git add -A
git commit -m "feat(tunnel): TunnelCarrier contract + CARRIERS registry; socat registrant

Argv-builder seam mirroring the impairer pattern: a carrier decides what
each tagged process executes; topology, sentinel, ports, discovery, and
remove stay carrier-agnostic.

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

---

### Task 7: Wire the carrier through manage + CLI + exports

**Files:**
- Modify: `src/otto/tunnel/manage.py`, `src/otto/cli/tunnel.py`, `src/otto/tunnel/__init__.py`
- Test: `tests/unit/tunnel/test_manage_add.py` (new cases + ONE sanctioned assertion update), `tests/unit/tunnel/test_cli.py` (new case)

**Interfaces:**
- Consumes: `build_carrier`, `TunnelCarrier` from Task 6.
- Produces: `add_tunnel(lab, hosts, *, port, protocol="tcp", dest=None, carrier="socat")`; CLI `otto tunnel add --carrier <name>`; `otto.tunnel` exports `TunnelCarrier`, `CARRIERS`, `register_carrier`, `build_carrier`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/tunnel/test_manage_add.py`. The file's helpers (verified): `_lab(**hosts)` builds a `FakeLab`, `FakeHost(id=..., ip=...)` scripts a host, and async tests run without decorators (pytest-asyncio auto mode). Both new tests raise BEFORE any host I/O, so an empty `_lab()` suffices:

```python
class TestCarrierSelection:
    async def test_unknown_carrier_is_a_rich_error_before_any_host_io(self):
        with pytest.raises(ValueError, match="Unknown carrier 'wireguard'"):
            await add_tunnel(_lab(), [("a", None), ("b", None)], port=8080, carrier="wireguard")

    async def test_protocol_unsupported_by_carrier_names_the_carrier(self):
        with pytest.raises(ValueError, match="carrier 'socat' does not support protocol 'sctp'"):
            await add_tunnel(_lab(), [("a", None), ("b", None)], port=8080, protocol="sctp")
```

Append to `tests/unit/tunnel/test_cli.py`, mirroring `test_add_command_happy_path_prints_id_endpoints_and_carriers` (line ~201; module-level `runner = CliRunner()`, `tunnel_app`, `patch` + `AsyncMock`):

```python
def test_add_passes_carrier_through():
    tunnel = Tunnel(
        protocol="tcp",
        service_port=161,
        path=(TunnelHop(host="test1"), TunnelHop(host="test2")),
    )
    added = AddedTunnel(tunnel=tunnel, carrier_fwd=49200, carrier_rev=49201)
    fake_add = AsyncMock(return_value=added)
    with (
        patch("otto.cli.tunnel.get_lab", return_value=object()),
        patch("otto.cli.tunnel.add_tunnel", fake_add),
    ):
        result = runner.invoke(
            tunnel_app,
            ["add", "--hosts", "test1,test2", "--port", "161", "--carrier", "socat"],
        )
    assert result.exit_code == 0, result.output
    assert fake_add.await_args.kwargs["carrier"] == "socat"
```

Note: `test_add_command_renders_value_error_and_exits_1_not_traceback` (line ~184) and its runtime-error sibling call `tunnel_cli.add(...)` DIRECTLY without a `carrier` kwarg — they keep working because their patched `add_tunnel` raises before the carrier value is ever inspected. Leave them untouched.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/tunnel/test_manage_add.py::TestCarrierSelection tests/unit/tunnel/test_cli.py -n0 -q
```

Expected: FAIL (`add_tunnel() got an unexpected keyword argument 'carrier'`).

- [ ] **Step 3: Wire `src/otto/tunnel/manage.py`**

1. Imports: remove `egress_socat_args`, `ingress_socat_args`, `relay_socat_args` from the `.socat` import (keep `FREE_PORT_PROBE_COMMAND`, `parse_listening_ports`, `pick_free_port`); add `from .carrier import TunnelCarrier, build_carrier`. Delete the now-unused `_SUPPORTED_PROTOCOLS = ("tcp", "udp")` constant.
2. `_process_plan` gains a carrier parameter and calls its methods — full replacement of the builder call sites:

```python
def _process_plan(
    tunnel: Tunnel,
    ips: list[str],
    p_fwd: int,
    p_rev: int,
    deliver_fwd: str,
    carrier: TunnelCarrier,
) -> list[_ProcSpec]:
```

and inside, replace `egress_socat_args(proto, svc, deliver_fwd, p_fwd)` with `carrier.egress_args(proto, svc, deliver_fwd, p_fwd)`, `relay_socat_args(p_fwd, ips[i + 1])` with `carrier.relay_args(p_fwd, ips[i + 1])`, `ingress_socat_args(proto, svc, ips[0], ips[1], p_fwd)` with `carrier.ingress_args(proto, svc, ips[0], ips[1], p_fwd)` — and the three mirrored REV-direction call sites the same way.

3. `_require_tools` becomes carrier-driven:

```python
async def _require_tools(host: Any, carrier: TunnelCarrier) -> None:
    """Fail loud + name the host when the carrier's required tools are missing."""
    try:
        result = await asyncio.wait_for(
            host.exec(carrier.requirements_command, log=LogMode.QUIET),
            _TUNNEL_HOST_TIMEOUT,
        )
    except asyncio.TimeoutError as e:
        raise RuntimeError(
            f"host {host.id!r} timed out checking for {carrier.tools_description}"
        ) from e
    if "ok" not in result.value:
        raise RuntimeError(
            f"host {host.id!r} is missing {carrier.tools_description} (required for tunnels)"
        )
```

For socat this renders the message `host 'b' is missing socat and/or bash (required for tunnels)` — byte-identical to today (the pin at `tests/unit/tunnel/test_manage_add.py:318` must keep passing UNMODIFIED). The timeout message changes from `checking for socat/bash` to `checking for socat and/or bash` — grep tests for `socat/bash`; if any assertion pins the old timeout text, update THAT assertion (this is part of the sanctioned message change).

4. `add_tunnel` signature and validation:

```python
async def add_tunnel(
    lab: "Lab",
    hosts: list[EndpointSpec],
    *,
    port: int,
    protocol: str = "tcp",
    dest: EndpointSpec | None = None,
    carrier: str = "socat",
) -> AddedTunnel:
```

Replace the protocol check at the top of the body:

```python
    protocol = protocol.lower()
    carrier_obj = build_carrier(carrier)()
    if protocol not in carrier_obj.supported_protocols:
        supported = ", ".join(sorted(carrier_obj.supported_protocols))
        raise ValueError(
            f"carrier {carrier!r} does not support protocol {protocol!r} (use {supported})"
        )
```

**Sanctioned assertion update #1:** the old message was `unsupported protocol {protocol!r} (use tcp or udp)`. Find any test pinning it (`grep -rn "unsupported protocol" tests/`) and update that single assertion to the new message shape.

5. Thread the carrier object through: `for r in resolved: await _require_tools(r.host, carrier_obj)` and `plan = _process_plan(tunnel, ips, carrier_fwd, carrier_rev, deliver_fwd, carrier_obj)`.

6. Update `add_tunnel`'s docstring: append the sentence `The *carrier* names a registered :class:`~otto.tunnel.carrier.TunnelCarrier` (chain-wide; default ``"socat"``).`

- [ ] **Step 4: Wire the CLI (`src/otto/cli/tunnel.py`)**

In `add(...)`, after the `dest` option:

```python
    carrier: str = typer.Option(
        "socat", "--carrier", help="Tunnel transport carrier (registered name)."
    ),
```

and pass it through: `added = await add_tunnel(lab, _parse_hosts(hosts), port=port, protocol=protocol, dest=dest_spec, carrier=carrier)`.

- [ ] **Step 5: Export the carrier surface from `src/otto/tunnel/__init__.py`**

Add to the imports: `from .carrier import CARRIERS, TunnelCarrier, build_carrier, register_carrier` and add `"CARRIERS"`, `"TunnelCarrier"`, `"build_carrier"`, `"register_carrier"` to `__all__` (keep the list alphabetically sorted the way it is now — uppercase names first, matching the existing ordering convention in the file).

Note: `otto.tunnel.__init__` already imports `.manage` → which imports `.carrier` and `.socat` → registration order is guaranteed (socat registers on first import of the package).

- [ ] **Step 6: Run the full tunnel + CLI suites**

```bash
uv run pytest tests/unit/tunnel tests/unit/link tests/unit/host/test_daemon.py -n0 -q
```

Expected: PASS. The complete sanctioned set of pre-existing test edits for this task (verified against the current tree — anything beyond this list means the wiring broke behavior; stop and fix the code, not the tests):

1. `tests/unit/tunnel/test_manage_resolve.py:190` and `:224` — direct `_process_plan(...)` calls gain the new final argument `SocatCarrier()` (import it from `otto.tunnel.socat`).
2. Any direct `_require_tools(host)` calls in `tests/unit/tunnel/test_manage_add.py` (find with `grep -n "_require_tools(" tests/unit/tunnel/test_manage_add.py`) gain the second argument `SocatCarrier()`.
3. Any assertion pinning the old protocol message `unsupported protocol ... (use tcp or udp)` (find with `grep -rn "unsupported protocol" tests/`) updates to the new carrier-naming message.
4. Any assertion pinning the old tools-timeout text `checking for socat/bash` updates to `checking for socat and/or bash`.

The `missing socat and/or bash (required for tunnels)` pin at `tests/unit/tunnel/test_manage_add.py:318` must pass UNMODIFIED — it is byte-identical by construction.

- [ ] **Step 7: Lint + typecheck, then commit**

```bash
uv run ruff check . && uv run ruff format --check . && make typecheck-python
git add -A
git commit -m "feat(tunnel)!: add_tunnel/--carrier select a registered TunnelCarrier

Tool probe and protocol menu come from the carrier; socat stays the
default and its error bytes stay identical. Unsupported-protocol errors
now name the carrier.

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

---

### Task 8: Ride-alongs, import budget, whole-tree gates

**Files:**
- Modify: `src/otto/tunnel/model.py` (docstring), possibly `tests/unit/import_budget/` snapshot (via `make import-snapshot`)

- [ ] **Step 1: Fix the false docstring claim in `src/otto/tunnel/model.py`**

Replace the module docstring's second paragraph sentence `Its per-hop segments ride links (``otto.link`` edges); ``otto.tunnel`` imports from ``otto.link``, never the reverse.` with:

```
Its per-hop segments conceptually ride links (``otto.link`` edges), but the
packages are fully decoupled: NEITHER imports the other; shared daemon
machinery lives in ``otto.host.daemon``.
```

- [ ] **Step 2: Import-budget check**

```bash
uv run pytest tests/unit/import_budget -n0 -q
```

If it fails: the deltas must be explainable by this branch (e.g. `otto.tunnel` surfaces now pull `otto.tunnel.carrier`; a surface that lazily avoided `otto.host.detached` must now lazily avoid `otto.host.daemon` — verify `tests/unit/link/test_lazy_exports.py` still passes, which proves the lazy seam survived). For intentional deltas run:

```bash
make import-snapshot
git diff --stat  # inspect: ONLY expected surface deltas
```

If a surface unexpectedly grew (e.g. `import otto` pulls the registry or carrier), STOP and fix the import graph instead of snapshotting it away.

- [ ] **Step 3: Whole-tree gates**

```bash
make lint && make typecheck
make docs
make coverage
```

Expected: all green. `make coverage` needs the lab VMs (they are reachable from this dev VM) and includes the live-bed tunnel/impair integration tests — these are the behavioral-identity proof for the extraction. If a live-bed test fails, treat it as a REAL regression until proven otherwise (do not dismiss as flake; see the house rule).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(tunnel): correct model.py's tunnel/link import claim; import snapshot

Assisted-by: Claude (Fable 5)"
git log -1 --format='%h %s'
```

(Fold the import-snapshot delta into this commit only if Step 2 produced one.)

---

## Verification (whole-branch, after all tasks)

1. `make coverage` green (full tiers, incl. dashboard prerequisite) — gate ≥94%.
2. `make lint`, `make typecheck`, `make docs` green.
3. Byte-identity goldens present and green: `TestPsScanCommand` (daemon), `TestWireGolden` (tunnel sentinel), `TestWireGolden` (impair sentinel), `test_ingress_argv_golden` (socat carrier).
4. `grep -rin "detached" src/ tests/` → only prose uses of the word (no module references).
5. `grep -rn "from ..link\|from otto.link" src/otto/tunnel/ && grep -rn "from ..tunnel\|from otto.tunnel" src/otto/link/` → zero hits (no cross-edge).
6. Live-bed proof: the e2e tunnel + impair suites inside `make coverage` passed.
7. Run the verify skill (end-to-end drive): `otto tunnel add --hosts <two lab hosts> --port 8080` against the live bed, confirm `otto tunnel list` shows it `ok`, then `otto tunnel remove` — plus one `--carrier socat` explicit invocation. (The library-extraction retro showed the end-to-end drive catches what unit passes miss.)
