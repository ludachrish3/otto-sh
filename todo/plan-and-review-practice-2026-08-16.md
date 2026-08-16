# Plan-and-review practice — lessons the dry-run workstream paid for

Process follow-ups, not code. Recorded because the evidence for each one is
about to be discarded: it lived in a git-ignored SDD ledger, and every item
below is a habit that will otherwise be re-learned at full price on the next
workstream.

The companion engineering follow-ups are
[dry-run-ledgered-minors-2026-08-16.md](dry-run-ledgered-minors-2026-08-16.md)
and
[treewide-guards-invisible-to-scoped-runs-2026-08-16.md](treewide-guards-invisible-to-scoped-runs-2026-08-16.md).

## 1. A plan's premises are unverified claims about the codebase

Measured on the dry-run plan: **six premises were false, and four of eleven
tasks existed only because an implementer refused to build what the brief
asked for.** Not implementer error and not sloppy prose — each false premise
read as a confident statement of fact:

- A `@property` over a frozen dataclass field, as specified, is
  **unconstructible** — `object.__setattr__` honours data descriptors.
- Putting `CommandNotRunError` in `host/errors.py` is an **import cycle**; tach
  counts function-scope imports as edges.
- "The preamble IS the validation" — false. References resolve inside command
  *bodies*, so validate-and-stop as written would have exited 0 on an unknown
  host. The fabrication bug reappearing inside its own fix.
- `otto host <id> exec` does not exist; the verb is `run`.
- The spec's "falls out nearly for free" was false on all three counts, and its
  §4 headline example (`rebuild_connections()` stopped by `is_ok=False`) was
  false too — `_soft_reboot` swallows the decline and returns Success, so what
  actually protects the teardown is control flow, never the value. Both
  corrected by a published erratum in the spec.

**The follow-up to consider:** a claim of the form "X follows from Y",
"Z is already handled", or "this falls out for free" is a testable assertion.
Before dispatch, each one gets a one-line check — a grep, a REPL line, a
`git grep` for the verb — and the result written beside it. The cost is minutes;
the observed cost of skipping it was four re-scoped tasks.

The generalisable half: **a hardened return value protects callers that branch
on it; only an early return protects actions below it.** Most of the false
premises above were one confusion between those two.

## 2. Apply mutate-and-observe-red to a *reviewer's* specified guard too

The repo already requires new gates to be proven red against their motivating
defect — see the principles in
[test-infra-remediation-plan-2026-08-06.md](test-infra-remediation-plan-2026-08-06.md),
which calls a never-red gate "the review's #1 recurring defect class".

The new sighting extends it in an uncomfortable direction. A reviewer
*demanded* a guard and justified it with an explicit mechanism: "an inherited
dataclass field appears in every subclass's `dataclasses.fields()`, so a
move-to-`BaseHost` mutation reddens deterministically." It was relayed
verbatim. **`BaseHost` is a plain `ABC`** — only concrete classes carry
`@dataclass` — so a field moved up appears in no subclass's `fields()` and the
specified guard would have stayed green under the exact mutation it named. The
implementer measured it and landed a three-observable pin instead.

The reviewer's own post-mortem is the sharpest part: its sweep **had** thrown
`TypeError: must be called with a dataclass type` on `RemoteHost`, and it
dropped that from the sweep instead of reading it as evidence.

**The follow-up to consider:** the mutate-and-observe-red requirement is
currently understood as something implementers owe reviewers. It runs the other
way as well. A confident mechanism sentence is a claim about this codebase, not
a proof — and when a sweep throws on one input, the throw is the finding.

## 3. The user-facing doc is the contract's strongest test — schedule it early

The docs page was planned as the small task at the end. It refused to be
written, because the contract sentence it was supposed to state was false: it
found that **every `otto host <id> <verb> -n` invocation tracebacked** — exit 1,
full traceback, on the flagship command group — a crash that 91 targeted seam
tests structurally could not reach.

Writing the page is the only activity in a workstream that requires pasting a
*real transcript of the real binary*. Unit tests assert against the harness;
the page cannot.

**The follow-up to consider:** for contract or behaviour-change work, move the
user-facing page to the middle of the plan, before the polish tasks. Treat
"I cannot paste a transcript that demonstrates this sentence" as a failing test,
not a docs blocker. (Why the 91 tests could not reach it is its own defect —
[test-harness-declares-registration-2026-08-16.md](test-harness-declares-registration-2026-08-16.md).)

## 4. "Stop executing the crashing line" is suppression, not repair

An eager f-string inside a `logger.debug(...)` evaluated a declined `.value`
and tracebacked the CLI. The cheapest fix is a `logger.isEnabledFor(DEBUG)`
gate around it — and that fix **scores green under mutation**, because the
guard runs at the default level where the gated line never executes. Measured:
guard green at INFO, traceback with rc=1 at `--log-level DEBUG`. The bug fully
present and fully invisible.

**The follow-up to consider:** add to the review rubric — when a fix works by
making a line unreached rather than safe, ask which configuration still reaches
it, and whether any guard runs in that configuration. Log levels, verbosity
flags, feature flags and `if DEBUG:` blocks all open this gap.

## 5. Two mechanical rules that each cost a gate failure or a rework

- **A named arm above the wide arm.** A sentinel exception only survives a
  blanket `except Exception` if a named arm sits above it
  (`except CommandNotRunError: raise`). Two sites in this workstream converted
  a decline into a misfiled real failure for want of that line.
- **The review package base is the commit recorded before dispatch**, never
  `HEAD~1` — which silently truncates every multi-commit task to its last
  commit. Cheap to get wrong, invisible when wrong.
