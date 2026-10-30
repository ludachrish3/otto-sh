# Exact command output in tests (#712) — design

## Why

otto's tests stripped a command's `value` before comparing it
(`res.value.strip() == "test2"`). That hid #711, a timing-dependent extra
empty line, for months. #711 (889adab54) and fe4e58d6a made `value` exact on
the ssh exec channel and on calibrated shell sessions. The canonical contract
is docs/architecture/utilities/results.md, `(command-output-contract)`:

- **The value:** the command's output with exactly one trailing newline
  removed. Nothing else is stripped or added.
- **Trailing spaces, tabs and the program's own `\r` are kept.**

Per route, today:

- **ssh exec channel** (`session.py`, `_drain` in the channel exec): exact.
  It drops one `\n` per line and joins the lines with `\n`.
- **Shell route, bash and ash frames:** exact once the terminal's mapping is
  measured. `onlcr` is folded back, and telnet `CR NUL` is decoded. When the
  mapping is `unknown(...)`, the transport's CRs are left in, so on that
  terminal a `\r` is real padding.
- **Zephyr frames:** a separate contract, documented in the same section.
  They trim both ends and delete every `\r`, so a Zephyr `value` is never
  padded. No strip on a Zephyr cell is justified.
- **Local exec** (`local_host.py:330`): diverges. It runs
  `data.decode().rstrip()` on every line, which drops trailing spaces, tabs
  and `\r` from every line. That is a second, different implementation of the
  same decision ("what a line of output contributes to `value`").
  - Docker container exec (`docker_host.py`) inherits whatever route its
    parent uses.

A strip in a test hides regressions of the contract. Local exec's per-line
`rstrip()` breaks the contract from the product side.

## Goals

1. **Local exec follows the contract** (Chris, 2026-10-30: fix it in #712).
   - The ssh-channel drain and the local drain share one function that turns
     a raw line into its contribution to `value`.
   - One test drives both routes through that function with the same inputs:
     trailing space, tab, a program `\r`, a blank line, no final newline, and
     the asyncssh `''` partial.
   - `results.md` states that the contract covers local hosts.
   - The commit is a breaking (`!`) user-facing behaviour change.
2. **Every test that compares or parses a command's `value` uses it exactly**
   wherever the output is known exactly. That includes `str(x.value).strip()`
   and the #711 tolerances spelled differently, such as
   `set(x.value.split("\n")) <= … | {""}` (test_docker_chaos.py:510/513).
3. **A strip survives only where the output really is padded.**
   - It carries an inline
     `# ast-grep-ignore: no-stripped-command-value -- <padding source>`
     that names the device, route or command that pads it.
   - The PR records the raw `repr(value)` observed for each strip that is
     kept.
   - None is known in tree today; the only candidate is an `unknown(...)`
     terminal mapping.
4. **An ast-grep rule stops the pattern from coming back.**

## Non-goals

- The strip sites in `src/otto` that parse device output (a separate
  question). Local exec's construction of `value` is not one of them: it is
  in scope as Goal 1.
- Zephyr's contract.

## Decisions

- **Rule `no-stripped-command-value`** (`.ast-grep/rules/`, with rule tests
  in `.ast-grep/rule-tests/`).
  - **What it flags:** `$X.value.strip($$$)`, `.rstrip($$$)` and
    `.lstrip($$$)`, plus `str($X.value).strip($$$)` and its r/l variants,
    anywhere under `tests/**`, including inside f-strings. It does not flag
    only strips inside an equality. That would miss
    `expected = result.value.strip()` with a later comparison, helpers that
    `return result.value.strip()`, and `assert not x.value.strip()`.
  - **Ignores:** the same ones the other tests-scoped rules use (project1,
    project2, project3, project_broken, project_e2e, firmware), following the
    sgconfig discipline that user-example input data is not otto's tests.
    project1 and project3 are rewritten anyway: their tests should show
    users exact assertions. They are just not guarded by the rule.
    project5 and project_ctr are not in the ignore list, so the rule guards
    them as it does for the other rules.
  - **The `note:`** lists accepted residuals in the house style: an alias
    (`v = r.value; v.strip()`), `str.strip(r.value)`, `.value.split()`, and
    a non-command `.value`. The last is the known false positive: an
    `ast.Constant`'s `node.value` in `tests/unit/test_slot_installers.py:516`
    gets an ignore comment saying it is not a command result.
  - `.stdout.strip()` (subprocess output, a different contract) is out of
    scope.
- **Rewrites, by site shape:**
  - `x.value.strip() == E`, `!= E`, and the tuple
    `(rc, x.value.strip()) == (0, E)` become `x.value == E`, `!= E` and
    `(rc, x.value) == (0, E)`.
  - `assert not x.value.strip()`, and `s = x.value.strip(); assert not s`,
    become `assert x.value == ""`.
  - `x.value.strip().splitlines() == L`, or a known `len`, becomes a direct
    comparison of `x.value` with the joined string. A set-diff of lines
    (session_stability 465/478) keeps `splitlines()`, without the strip.
  - `.strip().splitlines()[-1]` (exec_user:94 drops the sudo/su preamble):
    assert the exact whole value if it is known; otherwise keep `[-1]`, with
    a comment saying what the preceding lines are.
  - `E in x.value.strip()` and `.strip().endswith(E)`: if the output is
    known exactly, use `==`. If containment is the point, drop the strip and
    keep the `in`.
  - `int(x.value.strip() or 0)` becomes `int(x.value or 0)`. This proves
    nothing about exactness; it is only cleanup.
  - Failure messages: `f"{x.value.strip()!r}"` becomes `f"{x.value!r}"`, so
    a stray byte stays visible.
  - Helpers return `result.value`. The implementer reads what the helper's
    command prints (the product's printf, `stat -c`, and so on) before
    deciding. A test run alone does not decide it.
  - `| {""}` and the like: tolerances for #711's phantom line are removed.
  - Plain `"abc" in x.value` with no strip is left alone. It is not this
    issue.
- **A strip is removed for good only once its real target shows the exact
  value.** When the value turns out padded, keep the strip with its reason if
  the padding is real; if otto adds bytes it should not, file a product bug
  rather than hide it.

## Evidence

Every changed test passes once on its real target, on the committed tree:

- **Unit:** the default lane, plus the new shared-drain test.
- **Conformance, both venues:**
  - hermetic `make conformance` with `OTTO_CONFORMANCE_CELLS=all`;
  - the lab `make conformance-bed`.
- **Integration (M_UNIX / coverage-unix):** host, docker, cov and busybox_bed
  sites, plus the `integration + hops` sites in tests/e2e
  (test_tunnel_e2e 793/795, test_link_impair_e2e 272, which needs root netem
  on test3).
- **e2e:** the console and host sites.
- **Chaos, run once singly:** test_privilege_chaos and test_docker_chaos.
  Chaos is never a gate lane.
- **Project drivers:**
  - project1, via tests/integration/cov (`coverage_e2e`);
  - project3, via the embedded lane;
  - project5 test_kmod_demo, via `make kmodcov`;
  - project5 test_cov_container and project_ctr, via the docker
    image-coverage run.

Runs are coordinated with the peer sessions (pgrep plus asking first). The
landing is gated by `make gate-scope` → `make gate-tip`, then
`make gate-fresh`.
