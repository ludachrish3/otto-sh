# The transfer-progress contract measures the promise, not the loop that keeps it

Found while giving every transfer backend a declared progress granularity, a
shared invariant, a `transfer-progress` conformance surface, a pinned Rich
rendering and a derived promise table (branch `worktree-transfer-progress`,
based on main at `efe03bd1`). No commit range is cited: this branch
squashes, so any range written here is stale the moment it lands.
Spec: `docs/superpowers/specs/2026-08-26-transfer-progress-contract-design.md`.

Items 1-4 are the spec's own "Out of scope" list, filed here because that
section says the plan's final task would file them. Items 5-16 surfaced while
Tasks 1-6 ran and are in no spec at all. Item 17 came out of Task 7's own bed
run. Every line number below was re-checked against the branch tip on
2026-08-26.

**References into `ftp.py` are by FUNCTION NAME and code phrase, not by line.**
Not a style preference -- the numbers in this file drifted twice in a single
day, both times because a later edit to the same file inserted lines above them,
and the second drift happened between fixing the first and committing the fix.
A line range is only true until someone edits the file, which for a file this
list is actively about is roughly immediately. This file's job is to still be
true months from now, so where a name or a distinctive phrase locates the code
just as precisely, prefer it. Line numbers are kept only for files this
workstream is not editing, and for vendored code pinned by version.

One thing that was on this list and is NOT any more: ftp's transfer wrote to
`<dest>/<name>/<name>` whenever progress was off -- in BOTH directions, because
otto handed `aioftp.upload` and `aioftp.download` an already-joined path and
aioftp joined it again. The bed run found the PUT half; the review found the GET
half, where it was quieter and worse (a `Success` naming a directory). Both are
FIXED on this branch rather than filed.

The single sentence that ties most of this together: **the contract asserts what
a backend PROMISES and bounds the observed stream from above, so a promise that
is coarser than the loop's real behaviour is true everywhere and measured
nowhere.** Items 6 and 2 are both instances of it.

## 1. Chunked Zephyr `fs read`, so console GET can declare a stride

`ConsoleFileTransfer` declares `get=None` — one event, at completion
(`src/otto/host/transfer/console.py:89-96`). That is honest, not a shortcut:
`fs read <path>` on the Zephyr shell is one monolithic command and the whole
hexdump arrives in a single reply (`_console_get_one`, console.py:199-211; the
deferral is already written down at console.py:201-205).

A per-byte GET needs `fs read <path> <offset> <length>` and a chunk loop around
it — the shape PUT already has (`_WRITE_CHUNK = 32`, console.py:56, consumed at
console.py:282-283). Then `get` becomes a number and console GET stops being one
of the three backends (with `local` and `tftp`) whose GET promise is "one event
when it is over".

Users' call, per the spec. Nothing about the contract forces it.

## 2. Empty-file event consistency — nc and ftp are the outliers

The spec names nc; the tree says nc **and** ftp. Measured by reading every
emission site, 2026-08-26:

| backend | 0-byte PUT | 0-byte GET | where |
| --- | --- | --- | --- |
| sftp | `(0, 0)` | `(0, 0)` | `asyncssh/sftp.py:893-894` (its `_total_bytes == 0` arm) |
| scp | `(0, 0)` | `(0, 0)` | `asyncssh/scp.py:490-491`, `:626-627`, `:825-826` (its three `size == 0` arms) |
| shell | `(0, 0)` | `(0, 0)` | `shell.py:1577`, `shell.py:1872` (explicit `outcome.chunks == 0` arms) |
| console | `(0, 0)` | `(0, 0)` | `console.py:279`, `console.py:229` |
| local | `(0, 0)` | `(0, 0)` | `local_host.py` `_do_copy` — `size = dest.stat().st_size` |
| **nc** | **nothing** | **nothing** | PUT `if not block: break` (`nc.py:1099-1100`); GET `while bytes_done < total` never enters (`nc.py:1231` direct arm, `:1490` tunnelled) |
| **ftp** | **nothing** | **nothing** | `_put_files_ftp`'s `if not block: break`; `_get_files_ftp`'s `async for block in stream.iter_by_block()` yields nothing |

Both silent backends reach their loop through a "read until empty" shape, and an
empty file is empty on the first read. Neither is a failure: the file still
lands and the `Result` is `Success`.

The user-visible difference is a row that never appears. A `(0, 0)` event makes
Rich draw a finished bar — pinned by
`tests/unit/host/test_transfer_progress_render.py::test_an_empty_file_renders_finished_not_stuck`.
No event means no `add_task`, so a `put` of ten files where one is empty draws
nine rows and never says what happened to the tenth.

**Why no venue can catch this.** `assert_progress_invariants` and `events_for`
both refuse `total <= 0` with their own message
(`tests/_fixtures/progress.py:79-81`, `:134`), deliberately — a 0-byte payload is
outside the contract's domain, and the conformance surface's payloads are all
non-empty. So this stays visible but unforced until someone decides the shape.

Decide the shape first, then pin it: either every backend emits `(0, 0)`, or
none does and the empty file is documented as rowless.

## 3. tftp is reserved, not implemented

`TftpFileTransfer` declares `ProgressGranularity(put=None, get=None,
note="not implemented; both directions raise NotImplementedError")` and both
`_run_get` / `_run_put` raise (`src/otto/host/transfer/tftp.py:16-20`, `:28-48`).
It IS registered, so it appears in `TRANSFER_BACKENDS`, in the derived promise
table, and in the registry guard — with a declaration that is true.

Nothing to measure until the backend exists. When it does, its stride is the
TFTP block size (512 by default; `TftpOptions.block_size` already exists at
`src/otto/models/options.py:258` and `src/otto/host/options.py:525`), and the
`note` comes out.

## 4. An e2e pty test of the live bar

The spec ruled it out explicitly (§"Decisions taken in brainstorm", line 53:
"no golden frames, no pty scraping"), and Task 5 built the cheaper thing:
`tests/unit/host/test_transfer_progress_render.py` drives the REAL handler on a
recording `Console` and renders ONE frame via `progress.get_renderable()`,
never entering the `Live` (the file's own docstring says why — the Live's export
carries frames and cursor escapes).

What that leaves unexercised: `Live`'s refresh loop, cursor save/restore, the
transient teardown, and behaviour on a terminal resize mid-transfer. All of it
is Rich's code rather than otto's, which is the argument for not testing it — but
it is also the whole of what a user sees, so record it as a deliberate hole and
not an oversight.

## 5. `Field(gt=0)` on `ScpOptions.block_size`

`block_size: int = 16384` carries no positivity constraint at either boundary —
`src/otto/models/options.py:127` (the pydantic spec) and
`src/otto/host/options.py:328` (the runtime dataclass). So `block_size = 0`
validates cleanly out of lab data.

It then wedges INSIDE asyncssh. All three of asyncssh's scp classes run the same
loop:

```python
while offset < size:
    blocklen = min(size - offset, self._block_size)   # asyncssh/scp.py:494, :630, :829
```

With `self._block_size == 0` every `blocklen` is 0, `offset` never advances, and
the transfer hangs with no otto-authored error naming the field the operator got
wrong.

`effective_progress_granularity` already refuses a non-positive stride
(`src/otto/host/transfer/scp.py:321-322`, falling back to the class
declaration), but that only fixes the ANSWER — the transfer still hangs. The fix
belongs at the boundary, on both spellings of the field, so a bad `block_size`
fails at lab-load with the field's name in the message.

## 6. Make each transfer loop CONSUME its declaration — or add a lower bound

Where the tie is real today:

- **sftp** genuinely consumes it: `block_size=self.progress_granularity.get` /
  `.put` are passed straight to `sftp.get` / `sftp.put`
  (`src/otto/host/transfer/sftp.py:261`, `:292`). One constant, both places.
- **scp** is tied from the other end: `_kwargs()` is the dict asyncssh receives
  (`scp.py:378`, `:412`) and `effective_progress_granularity` reads that same
  dict (`scp.py:318-323`), so an `extra={"block_size": ...}` moves the promise
  and the reality together.
- **shell**, **nc**, **console** derive the declaration from the same module
  constant the loop reads (`_SHELL_CHUNK_BYTES` at shell.py:100; `_NC_BLOCK_SIZE`
  at nc.py:44; `_WRITE_CHUNK` at console.py:56). They cannot drift *silently*,
  but the tie is two independent references to one name and nothing asserts it.
- **ftp** passes no block size at all. `_FTP_BLOCK_SIZE` MIRRORS
  `aioftp.DEFAULT_BLOCK_SIZE`, and one unit pin
  (`test_the_ftp_stride_is_aioftps_own_block_size`,
  `tests/unit/host/test_transfer_registry.py:341-357`) keeps the mirror honest
  across an aioftp release.

**The blind spot is the invariant's direction.** Clauses 8a and 8b assert
`step <= G` (`tests/_fixtures/progress.py:103-110`) and clause 8c's count floor
`len(events) >= ceil(total / G)` gets LOOSER as `G` grows (`:118-122`). So:

- declaring a stride FINER than reality is caught — 8b reds naming the clause;
- declaring a stride COARSER than reality passes every venue, silently.

(The plan brief had this direction inverted. The nc example below is the
over-declaration case, and the remedy it proposes — a lower bound — only makes
sense against over-declaration.)

**The concrete consequence: `nc`'s declared `get = 8192` is pinned by nothing
anywhere.** The unit fakes feed 5-byte blocks, so any `G >= 5` passes; and the
conformance payload is SIZED FROM the declaration, so raising the declaration
raises the payload with it and the surface cannot catch an over-declaration
either. It rests on code review alone. Worth noting that
`asyncio.StreamReader.read(n)` returns AT MOST `n` bytes, so nc GET's real step
is `<= 8192` and routinely less — the declaration is an upper bound on reality,
which is exactly what the clauses already assume, which is exactly why they
cannot check it.

Two remedies; either one kills the blind spot:

1. Make every loop read its own declaration (what sftp does), so an
   over-declaration changes the product's behaviour and something notices.
2. Add a lower-bound clause: `step >= G` for every event but the last.

Both are product/test changes deliberately outside this plan's scope. (2) is
cheaper and catches more; (1) makes the declaration load-bearing, which is the
better property.

## 7. scp GET reports the BASENAME as the progress `src`

asyncssh's `_SCPSink._recv_file` forwards the SCP `C`-line name, and the SCP
protocol's `C` line carries no directory — so scp's GET arm reports `a.bin`
where every other backend reports `/path/to/a.bin`.

The user consequence: two files with the same basename in different directories
collapse to ONE progress row instead of two.

The conformance contract tolerates it NARROWLY — scp's get arm specifically,
with the reasoning written out at
`tests/conformance/test_progress_contract.py:140-190`. A backend that should
report the full path (`sftp`, `scp`'s PUT, the local copy) and regresses to a
bare basename still reds. Fixing it means otto correlating the C-line name back
to the requested path in `_make_scp_progress`; nothing in the contract forces
that.

## 8. A FLOAT `block_size` moves the stride but not the promise

`extra={"block_size": 65536.0}` reaches asyncssh through `_kwargs()` and changes
the real stride, but `effective_progress_granularity`'s guard is
`if not isinstance(size, int) or size <= 0` (`scp.py:321`), which is False for a
float — so the promise still answers 16384.

Not a regression: the pre-fix code answered 16384 too. But the guard shape is
incomplete — it treats "not an int" as "not configured" when it actually means
"configured with something asyncssh will happily use". Either coerce a
non-negative real to `int`, or refuse the type at the `extra` boundary the way
item 5 refuses the value.

(`isinstance(True, int)` is True, so `block_size=True` answers a stride of 1 —
which is what asyncssh would use too, so that one arm happens to be consistent.)

## 9. The duplicate progress spy in the integration contract — twice

`tests/integration/host/test_host_contract.py` carries TWO copies of the spy
factory, at `:289-299` (in `test_put_emits_completion_event`) and `:344-354` (in
`test_get_emits_completion_event`). Both are structurally identical to
`tests/_fixtures/progress.py::capture_progress` — the same nested
`spy_factory` / `factory` / `handler` — differing only in that they append plain
tuples where the fixture appends `ProgressEvent`.

Task 3 deliberately did NOT delete them: a peer session's bed hold forbade
RUNNING anything under `tests/integration/`, and an edit that cannot be executed
is an unverifiable change.

Two things to know before doing it:

- **It needs a bed window.** Any pytest run under `tests/integration/` reaps the
  lab bed (the autouse fixture in `tests/integration/conftest.py`), so this is
  not a change that can be verified between other people's runs.
- **The spec put it out of scope on purpose.** Its out-of-scope list ends with
  "Any change to `TestTransferProgressContract` in the integration suite", and
  both spies live inside that class (`test_host_contract.py:264`). Swapping
  scaffolding for the shared fixture asserts nothing new, but it is still a
  change to that class — so it needs someone to say the out-of-scope line has
  served its purpose, not just a quiet edit.

## 10. `match="outside"` is a substring of TWO helper messages

`tests/unit/test_progress_fixture.py:36-37` red clause 4 with
`pytest.raises(AssertionError, match="outside")`. Two messages in
`tests/_fixtures/progress.py` contain that word:

- the domain guard, `:80` — "an empty payload is **outside** this contract";
- clause 4, `:92` — "bytes_done 101 is **outside** [0, 100]".

Uniqueness today is POSITIONAL, not lexical: the parametrize table hardcodes
`total=100`, so the domain guard (`assert total > 0`) never fires and only
clause 4 can raise. A future case that parametrizes `total` would silently
match the wrong assertion and pass.

Tighten to `match=r"outside \[0, "`. No cost, no behaviour change.

## 11. Three Task 3 retrofit minors

- **A helper call inserted mid-assertion.**
  `tests/unit/host/transfer/test_shell_transfer.py:2313` puts
  `assert_progress_invariants(...)` BETWEEN `assert seen_calls == 3` (`:2312`)
  and `assert dst.exists()` (`:2319`). A helper failure now masks the
  destination check that used to run next. Appending the helper call after the
  original assertions would preserve the failure ordering. (The plan brief cites
  this as `tests/unit/host/test_shell_transfer.py`; the file lives under
  `tests/unit/host/transfer/`.)

- **Two nc GET sites fabricate `dst=""`.**
  `tests/unit/host/test_transfer_nc_get.py:371` and `:1228` build
  `ProgressEvent(src=s, dst="", ...)` from a `(src, done, total)` tuple, while
  every other product-recording site carries the product's own `dst`
  (`test_transfer_nc_put.py:189`; `test_shell_transfer.py:1814`, `:2298`,
  `:2889`; `test_embedded_transfer.py:385`). Nothing asserts `dst`, so the
  fabrication is invisible today — and would stay invisible if the product ever
  stopped reporting `dst` on those two paths, which is the case the retrofit
  exists to catch.

- **Three identical handler closures.** The same five-line
  `events.append(ProgressEvent(src=..., dst=..., done=..., total=...))` body
  appears in the three `def handler(` closures at `test_shell_transfer.py:1809`,
  `:2292` and `:2884`. One module-level helper taking the list would do — the
  middle one also carries a per-call assertion of its own, so it keeps a body.

## 12. `docs/api/host/transfer.rst` is inconsistent with the repo's pattern — and it is not a list extension

`link.rst` and `tunnel.rst` exclude their re-exported members from the PACKAGE
automodule wholesale and document each on a submodule directive.
`transfer.rst` now excludes exactly ONE name (`ProgressGranularity`) — the
minimum `-W` required in Task 6 — while the rest of
`otto.host.transfer.__all__` stays as it was.

Counted, not estimated (`otto/host/transfer/__init__.py`):

| | count | detail |
| --- | --- | --- |
| names in `__all__` | 27 | |
| from a module with its own page | 14 | `base` 12, `scp` 1, `sftp` 1 |
| still double-documented | 13 | the 14 above, minus the excluded `ProgressGranularity` |
| from a module with NO page at all | 13 | `console`, `embedded_base`, `ftp`, `nc`, `progress`, `registry`, `shell`, `tftp`, `unix_base` — 9 modules |

So roughly half the package's public surface has no submodule page to move to.
Matching the pattern means creating those pages, not extending an exclude list.

**And the "pattern" differs by package.** `link.rst` / `tunnel.rst` put every
submodule's `automodule` on the SAME package page. `docs/api/host/` instead uses
one FILE per submodule with a toctree entry (`transfer_base`, `transfer_scp`,
`transfer_sftp` — `docs/api/host/index.rst:16-18`). Following host's own idiom
means 9 new `.rst` files plus 9 toctree entries; following link's means 9
`automodule` blocks inside `transfer.rst`. Either way the `:exclude-members:`
list grows from 1 name to 14, and a cross-reference sweep follows.

**Correction to the record.** `task-6-fix-1-report.md`'s **concern 1** (the plan
brief cites it as concern 2) says the remainder is "documented at BOTH the
package path and its submodule path". That is wrong for 10 of the ~14 names it
lists — all the backend classes except `ScpFileTransfer` / `SftpFileTransfer`,
plus the three progress helpers, have no submodule page — and it mis-sizes the
work. Use the table above.

**One latent trap to fix while in there.** `docs/conf.py:119-126` sets
`autodoc_default_options = {..., "exclude-members": "model_config"}`, and a
directive-level `:exclude-members:` REPLACES that default unless its value
starts with `+` (sphinx `ext/autodoc/directive.py:69-77`; `exclude-members` is in
`AUTODOC_EXTENDABLE_OPTIONS`). `transfer.rst:14` is
`:exclude-members: ProgressGranularity` with no `+`, so `model_config` is no
longer excluded on that page. Inert today — nothing in
`otto.host.transfer.__all__` is a pydantic model — and live the day one is.
`:exclude-members: +ProgressGranularity` is the one-character fix, and
`link.rst:24` / `tunnel.rst:20` have the same shape.

## 13. The same Sphinx ambiguity trap stays armed for the other 13 names

Two documented targets for one name is the CONDITION, not the failure. The `-W`
failure needs a second ingredient: a BARE reference to that name, rendered by
autodoc onto a page belonging to a DIFFERENT module. Sphinx's fuzzy resolution
then finds both and reports **"more than one target found for
cross-reference"** — a message that does not name its cause.

MEASURED 2026-08-26 rather than reasoned about: delete `transfer.rst`'s
`:exclude-members:` line and `uv run sphinx-build -E -a -W -b html docs/
docs/_build/html` exits 1 with exactly THREE warnings, all of them the
inherited `progress_granularity: ClassVar[ProgressGranularity]` annotation
rendered on the three subclass pages that exist —
`otto.host.local_host.LocalFileTransfer`,
`otto.host.transfer.scp.ScpFileTransfer`,
`otto.host.transfer.sftp.SftpFileTransfer`. Restore the line and the same
command exits 0 with zero warnings. Those are the same 3 of Task 6's 11 (item
16).

So the condition is present for 13 names and measured to be firing for none of
them today. `TransferContext` is double-documented and appears as
`create(cls, ctx: "TransferContext")` in every backend module — and produces no
warning, so a signature annotation on its own is not the trigger.

The consequence worth writing down: **adding a submodule page can ARM the trap
for a name that is quiet today**, because the trigger is a rendering of the
ambiguous name on a page that is not that name's own, and a new page is a new
rendering. Item 12's remedy is precisely that operation — so 12 does not
"subsume" 13, it is the thing most likely to set 13 off. Do them together, with
a full `sphinx-build -E -a -W` after each page rather than at the end.

Pre-existing; not introduced by this workstream. The comment at
`docs/api/host/transfer.rst:4-11` exists so a future editor who trims the
`:exclude-members:` line learns the cause from the file rather than from the
build.

## 14. The rendering test pins two measured constants and does not say when to re-measure

`tests/unit/host/test_transfer_progress_render.py` pins:

- `_BAR = _FULL * 40` (`:36`) — Rich's `BarColumn` DEFAULT width, asserted as a
  substring on a finished row (`:83`, `:127`);
- `row.count(_FULL) == 26` (`:147`) — the measured fill at 32768/49169.

Both depend on the pinned console size (`width=100, height=25`, `:64`) AND on
Rich's `BarColumn` default width. The module docstring names the Rich version the
measurements were taken against (15.0.0, inside a pytest run, 2026-08-26) and
`_render`'s docstring explains the `TERM=dumb` / `height=` mechanism thoroughly —
but neither says the consequence: **a Rich version bump means re-measuring both
numbers**, and the red will look like a rendering bug rather than a dependency
change. Add that sentence where the mechanism is already explained.

## 15. Two docstring inaccuracies in that same file

- `_render`'s docstring (`:47-49`) says only two assertions red on a squeezed
  console, "both glyph counts on an `a.bin` row". Only one is a glyph count
  (`:147`, `row.count(_FULL) == 26`); the finished-bar one (`:83`,
  `assert _BAR in row`) is a substring check. The claim about which assertions
  red is right; the noun is wrong.
- "the widest row is 84 of 100" (`:58`) is the TYPICAL maximum, not the maximum.
  The same docstring says two sentences earlier that `TransferSpeedColumn`
  renders a variable-width field; a 10-character rate puts the row at 85. Either
  re-measure the true bound or say "typically 84" — the slack argument the
  sentence is making survives both.

## 16. `make docs` was red from Task 1 and nobody noticed until Task 6

`sphinx-build -W` failed with 11 warnings from the moment Task 1 landed
(`bcc01f47`), and it stayed red through Tasks 2, 3, 4 and 5 — four
implementations and four per-task reviews. Task 6 found it only because Task 6
was the docs task and its verify list finally named the command
(`task-6-report.md:5`, `task-6-fix-1-report.md:182-213`).

Nothing was wrong with the reviews. Reviewers check the diff in front of them;
none of them was asked to build docs, and a docstring cross-reference is not
visibly broken in a diff.

**The rule worth adopting:** the FIRST task in a plan that touches a public
docstring or a `docs/api/*.rst` runs `make docs` — not the last one that happens
to include it. The cost is one gate leg early; the cost of the alternative was
four tasks of drift and a fix round.

The same argument generalises to any gate a plan will eventually run: put it on
the verify list of the task that first makes it capable of failing.

## 17. A failing cell's leftovers are never cleaned, and the next cell inherits them

Found the hard way on 2026-08-26: one ftp defect became TWELVE bed failures,
and only eight of them were the defect.

The cleanup itself is not the problem, and the obvious "fix" would be a
regression. `remove_landed` (`tests/conformance/_controls.py:266-287`) issues
the vocabulary's `rm {path}` -- deliberately plain `rm` and NOT `rm -f`,
because `rm -f` answers 0 whether or not the file was there, which would make
`assert_bed_left_clean` a guard that cannot fail. That was measured on
2026-08-24 and the reasoning is written out at `_controls.py:289-308`. **Do not
"fix" this by reaching for `rm -rf`.**

The gap is the SUCCESS-PATH-ONLY verification. `assert_bed_left_clean` runs
after the `async with`, so a cell that FAILED never reaches it -- and a failing
cell is exactly the cell most likely to have left something behind. So:

1. the ftp cell created a directory where a file was expected (its own bug);
2. `remove_landed` ran `rm`, which correctly refused a directory, and correctly
   reported the refusal -- to nobody, because the cell had already failed;
3. the directory outlived the cell;
4. every LATER cell on the same host and worker inherited it, because the
   scratch name is namespaced by worker id only (`remote_name(worker_id, ...)`),
   not by cell. On test2 that cost three more failures: scp refused its put
   outright, sftp wrote INTO the directory and failed its get, and the leftover
   was still sitting there afterwards holding a 49169-byte payload.

The leftovers then survived the whole session and had to be removed by hand
across four hosts.

Three shapes, not equivalent, and the choice wants a decision rather than a
patch:

- **Namespace the scratch per CELL, not per worker.** Kills the inheritance
  outright and needs no new cleanup. Cheapest, and it makes every cell's litter
  its own -- but it does not clean anything up, so the bed still accumulates.
- **A session-end sweep** that removes the run's own scratch paths and REPORTS
  what it found. Cleans up, and turns "the bed was left dirty" into a visible
  fact rather than something the next run discovers. More machinery.
- **Type-aware cleanup**: when `rm` fails, ask why, and fail loudly with the
  answer ("a DIRECTORY is at the path this cell put a file"). Keeps the
  falsifiable `rm`, keeps the diagnosis, still leaves the litter.

The first and third compose well and between them address both halves. Whatever
is chosen must preserve the property `_controls.py:289-308` is protecting: the
removal is its own verification, so it has to stay capable of failing.

## Related

- `docs/superpowers/specs/2026-08-26-transfer-progress-contract-design.md` — the
  spec, whose "Out of scope" section is items 1-4 above.
- `tests/_fixtures/progress.py` — the invariant, its clause numbering, and the
  domain guard that items 2 and 10 both touch.
- `tests/conformance/test_progress_contract.py` — the `transfer-progress`
  surface, including the scp-basename tolerance of item 7 and the module
  docstring's own account of the over-declaration blind spot in item 6.
- `docs/architecture/support-matrix.md`, section
  `matrix-progress-promises` ("Transfer progress: what each backend
  promises") — the derived promise table, rendered by
  `scripts/render_support_matrix.py` and pinned against the registry by
  `tests/unit/test_support_matrix.py:5009`. The prose in
  `docs/architecture/testing.md` explains which venue measures which
  backend.
- `todo/support-matrix-refresh-at-release-2026-08-25.md` — the adjacent
  support-matrix queue.
