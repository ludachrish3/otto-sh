"""Every test lane declares its own runaway guard; none inherits a default.

ONE rule, stated in full: the vitest configs, the browser lane's ``expect()``
ceiling, and pytest's per-test ``timeout`` must each be written down in this
repo, at or above a floor. Nothing here inspects test bodies or reasons about
assertions — "What this does NOT cover" below is exhaustive.

A *runaway guard* is a per-test timeout, a hook timeout, an ``expect()``
ceiling: a bound that discriminates nothing. No test passes or fails
*because* of where it sits, so a tight value buys nothing and only converts
machine load into red builds. The 2026-08-08 sweep of this class (commits
``f2c05328``, ``4d2582fc``) found the pattern that motivates the gate:
wherever such a bound had been *written down* it got argued about and made
generous (pyproject's ``timeout = 180``, carrying a comment about
``timeout_func_only``), and wherever it was a *library default* it stayed
tight and invisible — vitest's 5000ms across 1013 tests, Playwright's
``expect()`` 5000ms across 202 assertions. A default never appears in a diff,
so it is never reviewed. Requiring the number to EXIST is therefore the whole
mechanism; the floors only stop a declaration from restating the default it
replaced.

The vitest configs are DISCOVERED (any ``web/vite*.config.ts`` carrying a
``test:`` block) rather than listed, which is the direct lesson of the
``expect()`` finding: the repo's real decision lived in one of two suites'
conftests and had no way to reach the other.

**What this does NOT cover, deliberately.** The other half of this class is a
*discriminator* — a bound written INTO an assertion to prove a property, as
in ``assert elapsed < 5.0``. Those cannot be found by requiring a
declaration, and no gate in this repo looks for them. An earlier draft
carried a scanner that inferred them from the AST by modelling the clock
domain; it worked, and it is what grew the ``serial_timing`` roster from 4
tests to 25 — but as a standing rule it was a heuristic needing a written
list of the shapes it could not see, and a gate whose coverage cannot be
stated in one sentence is worse than no gate, because readers assume it is
complete. The roster it produced is kept; the inference is not. Finding a NEW
discriminator is a human job, backed at runtime by the root conftest, which
fails any ``serial_timing`` test that reaches an xdist worker.
"""

import ast
import re
from pathlib import Path

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

# ── Part A: every lane declares its own runaway guard ───────────────────────
#
# Floors, not values. The gate's job is to stop a bound reverting to an
# unreviewed default, not to dictate the number: a lane may raise its bound
# freely, and lowering one past the floor is a decision that has to argue with
# this file. The floors come from measurement, not taste. The reviewbar test
# that failed the TS lane costs 166ms standalone and 274ms under coverage, and
# was killed at 5076ms — ~18x inflation from machine contention alone. The
# whole TS suite's genuinely slowest test is 1155ms, so 18x worst-case lands
# near 21s: 30s clears the worst case actually observed on this hardware with
# room to spare, and still leaves a hang caught inside a minute.
#
# The browser floor is NOT that derivation borrowed — an earlier draft said it
# was, and the numbers do not transfer: the ~10x slowdown recorded in
# tests/e2e/conftest.py (one dashboard run at ~340s against a ~33s norm) was
# measured over a WHOLE RUN, while this bound ceilings a single assertion's
# poll. 30s here is the value the repo already chose for Playwright's action
# timeout, held as a floor so `expect()` cannot silently fall back below the
# default that same conftest had already rejected as too tight. Python's 180s
# is the one bound that was always a decision; its floor sits well below it so
# this pin cannot be read as ratifying a number pyproject calls provisional.
#
# None of the three reaches a per-test `@pytest.mark.timeout(N)`, of which the
# suite holds ~54, nine below this module's own Python floor. Extending Part A
# to those is real work with real dispositions, and is deliberately not done
# here (review R2).
_TS_TIMEOUT_FLOOR_MS = 30_000
_BROWSER_TIMEOUT_FLOOR_MS = 30_000
_PYTEST_TIMEOUT_FLOOR_S = 60

_TS_BOUNDS = ("testTimeout", "hookTimeout")


def _live_ts(text: str) -> str:
    """*text* with comments blanked, so a commented-out bound cannot green.

    The same trap the addopts and serial-lane scanners were reviewed for: an
    annotated removal (delete the line, leave a comment naming it) is the most
    plausible human edit, and it must stay red.

    String-aware by necessity, not by ambition. The first cut was two regexes,
    and the block-comment one silently ate 70 lines of the real config: this
    file's glob literals contain both delimiters — ``"src/**/*.test.ts"`` holds
    a ``/*`` in ``**/*`` and a ``*/`` in ``**/`` — so a non-greedy ``/\\*.*?\\*/``
    paired a ``/*`` inside a ``//`` comment on line 21 with a ``*/`` inside
    another on line 91, taking the ``test:`` block with it. It failed loudly
    only because the caller asserts it found a config at all; that assertion is
    load-bearing, not decoration. Newlines are preserved so line-anchored
    matching still works — including across an escaped newline inside a
    string, which an earlier version silently ate while the docstring claimed
    otherwise. Regex literals are NOT modelled: ``/https?:\\/\\//`` contains
    the pair ``//`` and blanks the rest of its line. That direction is a false
    RED, so it fails loudly rather than greening a missing bound.
    """
    out: list[str] = []
    state: str | None = None  # None | "//" | "/*" | a quote character
    index = 0
    while index < len(text):
        char, pair = text[index], text[index : index + 2]
        if state is None:
            if pair in {"//", "/*"}:
                state, index = pair, index + 2
                out.append("  ")
                continue
            if char in "\"'`":
                state = char
            out.append(char)
        elif state == "//":
            if char == "\n":
                state = None
                out.append(char)
            else:
                out.append(" ")
        elif state == "/*":
            if pair == "*/":
                state, index = None, index + 2
                out.append("  ")
                continue
            out.append("\n" if char == "\n" else " ")
        else:  # inside a string literal
            if char == "\\":
                # Blank the escape pair, but never a newline: line numbers and
                # line-anchored matching downstream depend on it surviving.
                out.append(" ")
                out.append("\n" if text[index + 1 : index + 2] == "\n" else " ")
                index += 2
                continue
            if char == state:
                state = None
            out.append(char)
        index += 1
    return "".join(out)


def ts_test_configs() -> list[Path]:
    """Every ``web/vite*.config.ts`` that configures a vitest run.

    Discovered, not listed. ``vite.covapp.config.ts`` is build-only today and
    is skipped because it declares no ``test:`` block; if it ever grows one it
    inherits the requirement automatically. That generalization is the direct
    lesson of the ``expect()`` finding, where the repo's decision was real but
    lived in one of the two suites' conftests and could not reach the other.
    """
    return [
        path
        for path in sorted((PROJECT_ROOT / "web").glob("vite*.config.ts"))
        if ts_test_block(path.read_text()) is not None
    ]


def ts_test_block(text: str) -> str | None:
    """The body of the ``test: { ... }`` block, comments blanked, or None.

    Brace-matched rather than regexed to the end of file, because *where* a
    bound sits decides whether vitest reads it at all. A first cut searched
    the whole file, which meant hoisting the bounds out of ``test:`` to the
    config root — where vitest ignores them entirely — left the gate green.
    """
    live = _live_ts(text)
    # Not line-anchored: an inline `{ test: { ... } }` is legal TS, and failing
    # to FIND a block is the dangerous direction — ts_test_configs would drop
    # that config from the gate silently rather than report it.
    opener = re.search(r"\btest\s*:\s*\{", live)
    if opener is None:
        return None
    depth, start = 0, opener.end() - 1
    for index in range(start, len(live)):
        if live[index] == "{":
            depth += 1
        elif live[index] == "}":
            depth -= 1
            if depth == 0:
                return live[start + 1 : index]
    return live[start + 1 :]  # unbalanced: hand back what there is, never None


def ts_bound_gaps(text: str) -> list[str]:
    """Declared-bound problems in one vitest config: missing, or under floor.

    Reads the LAST assignment of each bound inside the ``test:`` block, not
    the first: a later one wins at runtime, so a conditional re-tightening
    appended after an innocent declaration (``...(process.env.CI ? {
    testTimeout: 5_000 } : {})``) is the effective value. Taking the first
    match let exactly that shape through, and it is fully type-legal.
    """
    block = ts_test_block(text)
    if block is None:
        return ["no `test:` block — vitest's own defaults apply unseen"]
    gaps: list[str] = []
    for name in _TS_BOUNDS:
        matches = re.findall(rf"\b{name}\s*:\s*([0-9_]+)", block)
        if not matches:
            gaps.append(f"{name} is not declared (vitest's 5000ms default applies unseen)")
            continue
        if (value := int(matches[-1].replace("_", ""))) < _TS_TIMEOUT_FLOOR_MS:
            gaps.append(f"{name} = {value}ms is below the {_TS_TIMEOUT_FLOOR_MS}ms floor")
    return gaps


def test_every_vitest_config_declares_its_own_runaway_guards() -> None:
    configs = ts_test_configs()
    assert configs, (
        "no web/vite*.config.ts declares a `test:` block — the TS lane's "
        "testTimeout/hookTimeout would be vitest's unreviewed 5000ms default; "
        "if the config moved, point this scanner at its new home"
    )
    offenders = [
        f"web/{path.name}: {gap}" for path in configs for gap in ts_bound_gaps(path.read_text())
    ]
    assert not offenders, (
        "vitest lane bound(s) undeclared or too tight — a harness bound whose "
        "only job is to stop a hang must be stated and generous (it "
        "discriminates nothing, so tightness buys nothing and converts machine "
        "load into red builds):\n  " + "\n  ".join(offenders)
    )


_EXPECT_RECEIVERS = frozenset({"expect", "_expect"})


def browser_expect_ceiling(text: str) -> int | None:
    """The ``timeout=`` passed to ``expect.set_options``, or None if uncalled.

    A literal or a module constant both resolve; anything else reads as
    undeclared, because a bound this pin cannot evaluate is a bound the next
    reader cannot either. The receiver has to BE ``expect`` — matching any
    object's ``.set_options`` meant an unrelated helper could satisfy the pin
    while the one call that reaches Playwright's assertion timeout was gone.
    """
    tree = ast.parse(text)
    constants = {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value.value, int)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "set_options":
            continue
        receiver = node.func.value
        if not isinstance(receiver, ast.Name) or receiver.id not in _EXPECT_RECEIVERS:
            continue
        for kw in node.keywords:
            if kw.arg != "timeout":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                return kw.value.value
            if isinstance(kw.value, ast.Name):
                return constants.get(kw.value.id)
    return None


def test_browser_lane_declares_its_expect_ceiling() -> None:
    """``expect()``'s 5000ms is reachable from exactly one place.

    ``playwright/_impl/_assertions.py`` resolves it as
    ``self._timeout or 5_000``, and ``self._timeout`` is populated only by
    ``expect.set_options`` — never by ``page.set_default_timeout``. That is how
    this repo ran 202 assertions at 5000ms while its own conftest recorded, in
    a comment, that Playwright's 30s default was already too tight for a loaded
    gate host. The call has to exist at the shared parent of both browser
    suites, which is why the pin names ``tests/e2e/conftest.py`` and not a
    per-suite conftest: the previous mitigation lived in one suite's conftest
    and left the other 135 assertions uncovered.
    """
    conftest = (TESTS_ROOT / "e2e" / "conftest.py").read_text()
    # Written is not run. The fixture that installs the ceiling is autouse; drop
    # that one keyword and every assertion silently reverts to 5000ms while this
    # pin still sees the call sitting there.
    assert re.search(
        r"@pytest\.fixture\(autouse=True\)\s*\n\s*def _generous_browser_ceilings", conftest
    ), (
        "tests/e2e/conftest.py's _generous_browser_ceilings is no longer an "
        "autouse fixture — the expect()/action ceilings below would be declared "
        "but never installed"
    )
    ceiling = browser_expect_ceiling(conftest)
    assert ceiling is not None, (
        "tests/e2e/conftest.py no longer calls expect.set_options(timeout=...) — "
        "every Playwright assertion silently reverts to the library's 5000ms, "
        "which page.set_default_timeout cannot influence"
    )
    assert ceiling >= _BROWSER_TIMEOUT_FLOOR_MS, (
        f"the browser expect() ceiling is {ceiling}ms, below the "
        f"{_BROWSER_TIMEOUT_FLOOR_MS}ms floor — a loaded gate host was measured "
        "at ~10x its norm, and this bound discriminates nothing"
    )


def pytest_ini_section(text: str) -> str:
    """The ``[tool.pytest.ini_options]`` table only.

    A bare ``^timeout =`` search over the whole file reads whichever table
    happens to come first — so a decoy ``timeout`` under any earlier section
    masked a real 5 s under pytest's own. Which table a key sits in IS its
    meaning in TOML.
    """
    body = text.split("[tool.pytest.ini_options]", 1)
    if len(body) == 1:
        return ""
    rest = body[1]
    following = re.search(r"^\[", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def test_python_lane_declares_its_per_test_timeout() -> None:
    text = pytest_ini_section((PROJECT_ROOT / "pyproject.toml").read_text())
    match = re.search(r"^timeout\s*=\s*(\d+)", text, re.MULTILINE)
    assert match is not None, (
        "pyproject.toml no longer sets pytest-timeout's `timeout` — a hung test "
        "would run until the CI job limit killed the whole lane, losing the "
        "per-test attribution that makes a hang diagnosable"
    )
    assert int(match.group(1)) >= _PYTEST_TIMEOUT_FLOOR_S, (
        f"pytest's per-test timeout is {match.group(1)}s, below the "
        f"{_PYTEST_TIMEOUT_FLOOR_S}s floor — this lane runs real subprocesses "
        "and real VMs, and the bound exists to catch hangs, not slow tests"
    )


def test_declared_bound_scanners_observe_red() -> None:
    """Positive controls: each Part A scanner seen failing on its own shape."""
    good = "export default { test: {\n  testTimeout: 60_000,\n  hookTimeout: 60_000,\n} }\n"
    assert ts_bound_gaps(good) == []
    assert ts_bound_gaps(good.replace("  testTimeout: 60_000,\n", "")) == [
        "testTimeout is not declared (vitest's 5000ms default applies unseen)"
    ]
    assert ts_bound_gaps(good.replace("60_000", "5000")) == [
        "testTimeout = 5000ms is below the 30000ms floor",
        "hookTimeout = 5000ms is below the 30000ms floor",
    ]
    # An annotated removal — the plausible human edit — must not green on the
    # comment text, in either TS comment spelling.
    declaration = "  testTimeout: 60_000,"
    assert ts_bound_gaps(good.replace(declaration, f"  //{declaration}")) != []
    assert ts_bound_gaps(good.replace(declaration, f"  /*{declaration} */")) != []
    # ... and a config with no test: block is not a vitest config to gate.
    assert ts_test_block("export default { build: {} }") is None

    # Review N2: hoisted OUT of `test:`, where vitest never reads them.
    hoisted = "export default { testTimeout: 60_000, hookTimeout: 60_000, test: {} }"
    assert len(ts_bound_gaps(hoisted)) == 2
    # Review N3: a later conditional re-tightening is the EFFECTIVE value.
    reconditioned = good.replace("} }", "  ...(process.env.CI ? { testTimeout: 5_000 } : {}),\n} }")
    assert ts_bound_gaps(reconditioned) == ["testTimeout = 5000ms is below the 30000ms floor"]

    # Review N4: which TOML table a key sits in IS its meaning.
    decoy = "[tool.other]\ntimeout = 600\n\n[tool.pytest.ini_options]\ntimeout = 5\n"
    assert (
        re.search(r"^timeout\s*=\s*(\d+)", pytest_ini_section(decoy), re.MULTILINE).group(1) == "5"
    )
    assert pytest_ini_section("[tool.other]\ntimeout = 600\n") == ""

    assert browser_expect_ceiling("expect.set_options(timeout=60_000)\n") == 60_000
    # Review: any object's .set_options must not satisfy the browser pin — only
    # `expect`'s reaches Playwright's assertion timeout.
    assert browser_expect_ceiling("harness.set_options(timeout=60_000)\n") is None
    assert browser_expect_ceiling("_MS = 60_000\nexpect.set_options(timeout=_MS)\n") == 60_000
    assert browser_expect_ceiling("page.set_default_timeout(60_000)\n") is None, (
        "set_default_timeout must never satisfy this pin — it is exactly the "
        "call that does NOT reach expect()"
    )
