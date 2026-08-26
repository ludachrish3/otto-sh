# Transfer progress contract — design

**Date:** 2026-08-26
**Status:** approved in brainstorm; awaiting spec review

## The gap today

Every transfer backend drives the same callback, `handler(src, dst, bytes_done,
bytes_total)`, and the Rich bar in `src/otto/host/transfer/progress.py` renders
whatever arrives. Nothing states, and nothing measures, what a correct stream
of events looks like. What exists:

- **Live, every host kind** (`tests/integration/host/test_host_contract.py::
  TestTransferProgressContract`, put and get): asserts only that *at least one
  event has `done == total > 0`*. Completion existence — not that the bar
  moves, not that it never exceeds the total, not that the total holds still.
  Each host runs its one configured transfer backend.
- **Conformance matrix:** progress is not a surface. The published page says
  nothing about it.
- **Unit:** uneven. nc GET pins an exact `(5,10),(10,10)` sequence on both
  arms; nc PUT pins that `bytes_done` never runs ahead of drained bytes;
  shell pins the last event; console pins a monotonic `[32,64,96,100]`. ftp,
  scp and sftp have no otto test of their event stream; the Rich rendering
  has none at all.

The visual bugs this project has had — a bar that sits at 0 and jumps to 100,
a speed column reporting the impossible, a bar that never finishes — are
violations of invariants nobody wrote down. This design writes them down
once, has every backend declare what it promises, and measures the promise in
every venue the repo has: the bed (as a matrix surface), the hermetic
conformance venue, the unit tests that already capture events, and a render
of the real bar.

## Decisions taken in brainstorm

1. **The live venue is a new conformance surface**, `transfer-progress`, not
   the integration contract. It rides the existing `(host, term, transfer)`
   cell crossing, so BusyBox rows measure `shell` and `nc`, GNU rows measure
   `ftp/nc/scp/sftp` over `ssh` and `telnet`, Zephyr rows measure
   `console`; it lands as a row in `schemas/support_matrix.json` under the
   collator's directional gate. The integration contract stays as the cheap
   per-host smoke it is (it is the only venue covering `LocalHost`).
2. **"Incremental" is measured against a granularity each backend declares**
   (a product addition), because the backends' strides differ by three
   orders of magnitude (console PUT 32 bytes; sftp 16 KiB) and console GET is
   one monolithic `fs read` — accepted as a declared `None`, with a
   follow-up filed to chunk it if users care.
3. **One invariant, two venues:** a single helper defines correct progress;
   the conformance surface and the existing unit tests both call it. No new
   hermetic fakes for ftp/scp/sftp — their streams come from aioftp/asyncssh
   and are measured live.
4. **Rendering is unit-tested against the real handler** on a recording Rich
   console; no golden frames, no pty scraping.
5. **The declarations render into the support-matrix page**, derived from
   the registry with a docs-sync guard, so users can read each backend's
   expected chunk behaviour beside the verdicts — including why console GET
   is monolithic.

## Venue note (stated here because it was not obvious)

BusyBox is measured on the real bed exactly as Linux is: the five pinned QEMU
guests on `test1` are `bed-busybox[bb*:telnet:{shell,nc}]` cells in the
conformance bed venue (250 of the 1,173 cell lines in the 2026-08-26 run) and
`BUSYBOX_BACKENDS` in `make coverage`'s integration leg. The hermetic
`busybox-artifact[…]` cells are a THIRD venue BusyBox has and Linux does not.

**What the hermetic venue actually exercises, measured 2026-08-26 rather than
assumed** (an earlier draft of this note was wrong, and the error mattered):
`_busybox_cells` builds `Cell("busybox-<v>", "local", "local")` — a `LocalHost`
with the artifact applets on `PATH` and `LocalFileTransfer` as the backend. So
the hermetic draw is eight cells (`local`, `loopback-ssh:sftp`,
`loopback-ssh:scp`, and five `busybox-artifact[*:local:local]`), and it
exercises **no `shell` and no `nc` cell at all**.

For this surface, then — enumerated from `bed_space()` rather than recalled,
because an earlier draft of the sentence below was wrong twice over:

| kind | transfers it draws |
| --- | --- |
| `bed-busybox` | `nc`, `shell` |
| `bed-unix` (GNU) | `ftp`, `nc`, `scp`, `sftp` — **no `shell`** |
| `bed-zephyr` | `console` |

So `shell` is measured on the BED only and ONLY on the five BusyBox guests;
`nc` is bed-only too but measured on BOTH families; `ftp` and `console` are
bed-only; and `sftp` and `scp` are the backends the default gate exercises
hermetically. A mutation aimed at `shell` reds nothing hermetically — the
equivalent hermetic observation has to be taken on `sftp` or `scp`.

## 1. Product: every backend declares its progress granularity

In `src/otto/host/transfer/base.py`:

```python
@dataclass(frozen=True)
class ProgressGranularity:
    """What a backend promises the progress bar, per direction.

    ``put`` / ``get``: the most ``bytes_done`` may advance between two
    consecutive events (the last event may advance less). ``None`` means the
    backend emits exactly ONE event, at completion — a whole-file transfer
    with no intermediate observation. A ``None`` arm MUST explain itself in
    ``note``; the registry refuses one that does not.
    """

    put: int | None
    get: int | None
    note: str = ""
```

`BaseFileTransfer` gains `progress_granularity: ClassVar[ProgressGranularity]`
with NO default. `register_transfer_backend` refuses a class that does not
declare it — the same shape as its existing empty-`host_families` refusal,
with the same `register_hint`. A `None` arm whose `note` is empty, or a
non-positive stride, is refused by the dataclass itself (`__post_init__`),
so a bad declaration cannot even be constructed.

Declarations:

| backend | put | get | basis |
| --- | --- | --- | --- |
| shell | 4096 | 4096 | `_SHELL_CHUNK_BYTES`; both directions chunk |
| nc | 8192 | 8192 | `_NC_BLOCK_SIZE`; both arms read in blocks of it |
| ftp | 8192 | 8192 | aioftp `DEFAULT_BLOCK_SIZE`: GET's `iter_by_block()` default, and PUT reads `f.read(aioftp.DEFAULT_BLOCK_SIZE)` per `stream.write` (`ftp.py:182`) |
| sftp | 16384 | 16384 | otto passes `block_size=16384` to `sftp.get`/`sftp.put` explicitly (today `-1`, server-negotiated, so any declaration would be a guess) |
| scp | 16384 | 16384 | already `ScpOptions.block_size` (default 16384), passed to `asyncssh.scp` today; the class declaration is that default, the instance answers the configured value |
| console | 32 | `None` | `_WRITE_CHUNK`; note: *"get is one `fs read` command on the Zephyr shell — the bytes arrive in a single reply and the one event arrives when it completes"* |
| tftp | `None` | `None` | note: *"not implemented; both directions raise"* |
| local | `None` | `None` | `LocalFileTransfer` (concrete, but never registered): `shutil.copy2` is one blocking C call with no progress hook, so the one event arrives at completion. It is the backend `LocalHost` drives in the integration contract, so it declares like the rest |

scp's stride is already `ScpOptions.block_size` (default 16384, user-
configurable), so the class attribute is the DEFAULT and an instance method
`effective_progress_granularity()` answers the configured value; every other
backend's instance answer is its class declaration. The sftp `block_size` and
the sftp declaration both read ONE module constant (`_SFTP_BLOCK_SIZE`), so
they cannot drift and the promise is true by construction rather than by
observation.

The call sites pass that constant rather than reading the declaration back,
and the reason is a TYPE constraint worth recording: a `ProgressGranularity`
arm is `int | None` because a `None` arm is legal, while asyncssh declares
`block_size: int`. Reading the declaration at the call site hands asyncssh an
`int | None` and fails `ty`. An earlier draft did exactly that and shipped a
type error the whole workstream's gates missed, because `ty` runs only at the
typecheck target and no task's verify list named it. The agreement between
the kwarg and the declaration is therefore held by a TEST that asserts them
against each other, which is where it belonged anyway.
This is the only behaviour change in the product: sftp reads in 16 KiB
requests instead of the server's negotiated maximum. The bed measures the
cost (it is a LAN; the plan records the before/after time of the gnu ssh
cells and reverts to a larger stride if the difference is material).

`ftp` MIRRORS `aioftp.DEFAULT_BLOCK_SIZE` as a module constant
(`_FTP_BLOCK_SIZE`) rather than reading it in the class body: `aioftp` is
imported lazily inside the transfer methods and is absent from the `host`
import-budget snapshot, so a class-body read would pull the package into every
`otto host` invocation. A unit pin against the real constant
(`test_the_ftp_stride_is_aioftps_own_block_size`) is what keeps the mirror
honest across an aioftp release — measured, not assumed.

## 2. The invariant, defined once

`tests/_fixtures/progress.py`:

```python
@dataclass(frozen=True)
class ProgressEvent:
    src: str
    dst: str
    done: int
    total: int

def capture_progress() -> tuple[list[ProgressEvent], Callable]:
    """A spy factory in the shape ``make_rich_progress_factory`` returns, and
    the list it appends to. One handler per factory() call, as the product
    does — so a two-file transfer is two streams in one list, split by src."""

def assert_progress_invariants(
    events: list[ProgressEvent], *, src: str, total: int, granularity: int | None
) -> None:
```

The invariants, each with a named refusal message:

1. non-empty;
2. every event's `src == src`;
3. `total` is the same value in every event and equals `total` (the size the
   caller knows);
4. `0 <= done <= total`;
5. `done` strictly increasing;
6. final `done == total`;
7. `granularity is None` → exactly one event;
8. `granularity == G` → (a) every step `<= G`; (b) the FIRST event `<= G` (the
   bar cannot begin at 100%); (c) `len(events) >= ceil(total / G)`.

**Clause 8c cannot fail, and the implementation says so in a comment.** It is
implied by 6 + 8a + 8b: if every step is at most `G` and the final `done`
equals `total`, then `n*G >= total` already. It was proved unfalsifiable by
deletion (removing it reds no test) and is RETAINED anyway, because the clause
numbering is the contractual map between this document and the helper's
refusal messages. Seven of the eight clauses are measurable; a reader must not
count 8c as coverage.

The helper is itself tested by mutation in `tests/unit/test_progress_fixture.py`:
for each clause, a stream that violates exactly that clause must raise with
that clause's message, and the compliant stream must pass. `total == 0` is
out of the helper's domain (see out of scope) and is refused with its own
message so no caller can pass an empty payload by accident.

## 3. The conformance surface `transfer-progress`

New module `tests/conformance/test_progress_contract.py`, in the transfer
contract's idiom (module docstring stating what the contract is about; the
same `applicable_cell` predicate — cells with a `remote_scratch`; worker-
namespaced filenames via `remote_name`; cleanup through `remove_landed` and
`assert_bed_left_clean`).

### The contract

`test_progress_events_track_the_bytes_in_both_directions(resolved_cell,
remote_scratch, tmp_path, worker_id)` — one cell verdict covering both
directions, as `transfer-roundtrip` already does:

- **PUT arm.** `G` is the put arm of the backend INSTANCE's promise. There is
  no `host.transfer` accessor: the backend is reached through the private
  `_file_transfer` attribute both host families agree on (the registry lookup
  answers for `sftp`/`scp` but raises for the `local` cell), and the surface
  asks it `effective_progress_granularity()` — the instance method, never the
  class attribute, so scp's configured `block_size` is what gets measured.
  Payload size
  `3*G + 17` bytes when `G` is an int (four events at the declared stride,
  the last a partial), `64` bytes when `None`. Binary-hostile filler (a
  deterministic byte pattern including `\x00`, `\r\n` and non-UTF-8), not
  `b"x" * n`. `host.put([src], remote_scratch)` under the spy factory patched
  over `otto.host.transfer.progress.make_rich_progress_factory` (the pattern
  `test_host_contract.py` uses; the base class reads it lazily from that
  module). Then `assert_progress_invariants(events, src=..., total=len(payload),
  granularity=G)`.
- **GET arm.** A second payload sized from `progress_granularity.get`, staged
  on the host with `show_progress=False` (no events, so the spy list holds
  only the arm under test), then `host.get([remote], tmp_path)` under the spy
  and the same assertion against `granularity.get`.
- Both landed files removed; the bed asserted clean.

### The positive control

`@pytest.mark.positive_control("transfer-progress")`
`test_control_the_instrument_refuses_a_bar_that_jumps(...)`: run the PUT arm
for real on the cell, keep its captured stream, and require the instrument
to refuse two mutations of THAT stream — the final event dropped (clause 6)
and the stream collapsed to one `0 → total` event (clause 8, and clause 7's
converse on a `None` cell, where the mutation is instead a spurious
intermediate event). A control that reds on synthetic input proves the
helper; this one proves the helper reds on this cell's own data.

### Matrix plumbing

- `tests/_fixtures/support_matrix.py::SURFACES`: `Surface(id="transfer-progress",
  title="transfer: progress events track the bytes, both directions",
  contract="tests/conformance/test_progress_contract.py::test_progress_events_track_the_bytes_in_both_directions")`
  placed after `transfer-mode`, before `timeout` (the declared row order).
- `scripts/render_support_matrix.py::VOICE["transfer-progress"]`: short
  "progress", capability "watch a transfer's progress bar and trust that it
  moves with the bytes"; unbranched.
- `tests/unit/test_support_matrix.py` pins move: six surfaces → seven; five
  unbranched → six; the whole-grid enumerator follows `len(SURFACES)`.
- `schemas/support_matrix.json`: the row enters as `untested` for every
  profile and is measured by `make conformance-bed` — the plan's final task,
  after which the artifact is committed with the flips. Hermetic cells
  (`busybox-artifact[…]`) run the contract on every default gate but, by the
  2026-08-24 ruling, populate no profile column.

## 4. Unit retrofit and two guards

The tests that already capture events call `assert_progress_invariants` on
their captured stream, keeping every exact pin they have:
`tests/unit/host/test_transfer_nc_get.py` (plain and tunneled
`test_progress_handler_called_during_read`), `test_transfer_nc_put.py`
(`test_progress_handler_reports_bounded_bytes`),
`tests/unit/host/transfer/test_shell_transfer.py`
(`test_handler_reaches_bytes_done_equals_total` and the GET call-count test),
`tests/unit/host/test_embedded_transfer.py` (console PUT monotonic test and
GET single-event test). Each passes `granularity=<Backend>.progress_granularity.<dir>`
so a backend whose loop grows COARSER than its declaration reds hermetically.

**The detector is one-sided, and this is the honest statement of it.** Every
stride clause bounds the observed step from ABOVE (`step <= G`), so a
declaration LARGER than the real stride satisfies all of them. Sizing the
conformance payload from the declaration (`3*G + 17`) does not rescue this:
with a declared `G'` twice the real stride `g`, the payload is `6g + 17`, the
events arrive every `g`, and 8a/8b/8c all pass. **Nothing in this plan reds an
over-large declaration.** That is a weak promise rather than a false one — "the
bar advances at most `G` between ticks" stays true — but the published table
would then print a stride far coarser than reality. Two consequences to hold:
a backend whose loop gets FINER while its declaration stands is invisible; and
where the unit fake's step is far below the declaration (nc GET's 5-byte
`FakeReader` against 8192), the declaration is pinned by nothing hermetic, and
the conformance surface does not pin it either. The follow-up worth taking is
to make each loop CONSUME its declaration the way sftp and scp now pass
`block_size=` from it — then declaration and loop cannot drift at all — or to
add a lower-bound clause (every non-final step `>= G`). Neither is in scope
here.

Guards, in `tests/unit/host/test_transfer_registry.py`:

- every registered backend declares a `ProgressGranularity`, and every
  `None` arm carries a note (iterates the registry; the registration-time
  refusal is exercised with a throwaway class that omits each);
- **sftp** passes its declared stride to asyncssh as `block_size` (the same
  module constant the declaration is built from, not a read-back of the
  attribute -- see §1),
  pinned on the fake connection's call kwargs in both directions. **scp does
  not, and cannot be pinned the same way:** it splats
  `**self._scp_options._kwargs()` into `asyncssh.scp`, so there is no
  otto-authored `block_size=` argument to observe. Its promise is instead read
  back through `effective_progress_granularity()`, which consults that SAME
  dict — `_kwargs()` ends `kw.update(self.extra)`, so an `extra`-supplied
  `block_size` replaces the declared default exactly as a field would. An
  earlier draft of this bullet claimed the kwargs pin covered both backends; it
  never did.

## 5. Rendering test

`tests/unit/host/test_transfer_progress_render.py` drives the REAL
`make_rich_progress_handler` against `make_transfer_progress()`'s column set
on `Console(record=True, width=100, height=25, force_terminal=True,
color_system=None)`, with streams generated by a small `events_for(total,
granularity)` helper that itself satisfies the invariant (asserted once in the
module). Assertions are both semantic and on `export_text()`, rendering ONE
frame via `progress.get_renderable()` rather than entering the Live (whose
export carries frames and cursor escapes):

**`height=25` is load-bearing, and its absence is silent.** `tests/conftest.py`
sets `TERM=dumb`; rich's `Console.size` returns `(self._width, self._height)`
only when BOTH are set, and otherwise falls through to a hardcoded `(80, 25)`
for a dumb terminal, DISCARDING `width=`. At 80 columns the table squeezes the
bar and every glyph-count assertion reds. The constants in this section were
first measured outside pytest and were wrong inside it — a constant measured
outside the test venue is not a measurement of the test venue.

- one file: the task's `completed == total` and `finished`; the exported
  row names the host and the file's basename and the `DownloadColumn` shows
  `total/total`. **This bullet originally said to read the expected string back
  from Rich's own formatter rather than hand-write it. That was wrong and the
  implementation correctly does the opposite:** an expected value computed by
  the same code that produced the actual value is vacuity in disguise — the
  defect class this workstream caught in the unit retrofit. The assertions
  carry MEASURED literals (`"49.2/49.2 kB"`, `"0/0 bytes"`), which is the
  stronger spelling.
- two files in one stream: two rows, each finished with its own total;
- the empty-file shape `(0, 0)` that shell and console emit: the task is
  `finished` and the row renders (the plan MEASURES what Rich does with
  `total=0` before pinning the exact text; the requirement is "finished, not
  stuck at an empty bar");
- a stream that violates the invariant is NOT rendered here — the helper
  refuses it upstream; the render test asserts only what compliant streams
  look like.

## 6. Documentation, derived

- `scripts/render_support_matrix.py` gains `_progress_promises_section()`
  under "What a `measured-ok` cell guarantees": fixed prose (what a stride
  is; that the bar's advance between two ticks is bounded by it; that `None`
  means one tick at the end) followed by a table built by iterating the
  transfer registry — backend, put stride, get stride, note. The console GET
  note therefore appears verbatim on the page.
- `tests/unit/test_support_matrix.py` (docs-sync family) pins the rendered
  table to the registry: one row per registered backend, values equal to the
  declarations, every `None` cell followed by its note — the same
  count-and-content shape the gap-registry section is pinned with.
- `docs/architecture/testing.md`: the conformance surface list gains the row
  and one paragraph on what the surface measures and where (the venue note
  above, in the page's voice).
- `docs/guide/configuration/host-options.md` transfer section: one sentence
  pointing at the support-matrix page for per-backend progress behaviour.

## 7. Testing honesty

Every new test is written red first and its red observed; the plan requires
the observed failure line in each task report. The conformance contract's
red is observed by mutating a backend's loop (e.g. reporting
`bytes_done = total` on the first event in shell PUT) in the hermetic venue.
Guards inject the hostile condition; none inherits it from the artifact.

## 8. Bed re-measure and cost

`make conformance-bed` runs once at the end (≈75 s tonight). Added bytes per
cell: sftp/scp ≈ 49 KiB per direction on the four GNU hosts; nc 24 KiB; shell
12 KiB; console PUT 113 bytes, GET 64 bytes. The before/after time of the gnu
ssh cells is recorded in the plan's last task to judge the sftp stride.

## Out of scope (follow-ups filed by the plan's final task)

- Chunked Zephyr `fs read` so console GET can declare a stride (users' call).
- Empty-file event consistency: nc emits NO event for a 0-byte GET while
  shell and console emit `(0, 0)`; the contract's payloads are non-empty, so
  this stays visible but unforced.
- tftp (not implemented).
- An e2e pty test of the live bar; golden frames of Rich output.
- Any change to `TestTransferProgressContract` in the integration suite.
