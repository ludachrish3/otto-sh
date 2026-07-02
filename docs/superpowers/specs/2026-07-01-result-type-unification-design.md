# Result-type unification — design

**Date:** 2026-07-01
**Status:** Approved design, pending implementation plan
**Origin:** The one unimplemented finding from the 2026-06-12 architecture
review ([todo/fable_review_outcome.md](../../../todo/fable_review_outcome.md),
interface-consistency audit; verified still open in
[todo/fable_review_verification.md](../../../todo/fable_review_verification.md)):
`run()` → `RunResult`, `oneshot()` → `CommandStatus`, `get()`/`put()` (and
`power`/`reboot`/`load`/`unload`) → `tuple[Status, str]` — three result shapes
across the host verbs. This must be settled before the contract freeze.

## Goals

- One result family for **all** `@cli_exposed` host verbs — including
  read-only queries — so callers and the CLI renderer handle a single shape.
- No information loss: command results keep `command` and `retcode`; file
  transfers report a per-file outcome, addressable by source path.
- The CLI exit code is derived from the result itself, ssh-like for command
  verbs.
- Documentation (docstrings **and** the docs tree) matches the new contracts
  everywhere a host-verb return is shown.
- No back-compat shims: `CommandStatus` and `RunResult` are deleted
  (pre-freeze, zero-users policy).

## Non-goals

- Re-homing `Status` out of `utils.py` (the utils split is a separate TODO;
  `result.py` imports `Status` from `otto.utils`).
- Structured JSON result export for CI dashboards (separate feature; the
  frozen-dataclass family is `dataclasses.asdict()`-friendly if that lands
  later).
- Changing `interact()` — it is a terminal takeover with nothing to report and
  stays `-> None`, exempt from the family.

## Core types — new module `src/otto/result.py`

```python
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from otto.utils import Status


@dataclass(frozen=True)
class Result:
    """Outcome of a host verb: status + optional payload + human diagnostic."""

    status: Status
    value: Any = None
    msg: str = ""  # human diagnostic; empty on success

    @property
    def is_ok(self) -> bool:
        return self.status.is_ok

    def __bool__(self) -> bool:  # enables: if not await h.load(...):
        return self.is_ok

    @property
    def exit_code(self) -> int:
        """CLI exit code for this result: 0 when ok, else ``status.value``."""
        return 0 if self.is_ok else self.status.value


@dataclass(frozen=True)
class CommandResult(Result):
    """Result of one shell command; ``value`` holds the command's output."""

    command: str = ""
    retcode: int = -1

    @property
    def exit_code(self) -> int:
        """ssh-like: the command's own retcode.

        0 when ok; 255 when the command never ran (retcode -1, matching ssh's
        connection-error convention); ``status.value`` when the command exited
        0 but otto marked it failed (e.g. an expect mismatch).
        """
        if self.is_ok:
            return 0
        if self.retcode == -1:
            return 255
        if self.retcode != 0:
            return self.retcode
        return self.status.value


@dataclass(frozen=True)
class Results(Result, Sequence[CommandResult]):
    """Aggregate over per-command results; itself a Result. run()-only.

    ``value`` is ``list[CommandResult]`` in execution order. ``status`` is the
    aggregate: ``Status.Success`` when every entry is ok, otherwise the first
    non-ok status (preserving RunResult semantics). Sequence dunders
    (``__len__``, ``__getitem__``, ``__iter__``) delegate to ``value``.
    """

    @property
    def only(self) -> CommandResult: ...  # raises ValueError unless exactly 1

    @property
    def first_failure(self) -> CommandResult | None: ...

    @property
    def exit_code(self) -> int:
        """0 when ok, else the first failing command's ``exit_code``."""
```

Design points:

- **Flat, not generic.** `value: Any` on the base; the typed fields that
  matter (`command`, `retcode`) live on `CommandResult`. A `Generic[T]`
  payload envelope was considered and rejected: it would keep `CommandStatus`
  alive as a payload type (two concepts instead of one) and fights dataclass
  inheritance.
- **Why both `Result` and `Results` (decided 2026-07-01):** `Results`
  subclasses `Result`, so there is one *interface* — any caller or renderer
  can treat any outcome as `Result`. The base class exists so inherently
  scalar verbs (`power`, `reboot`, `login`, `load`, `unload`, `lsmod`, and
  transfers, whose per-file detail lives in the `value` mapping) don't pay an
  `.only` tax on a container. `run()` returns `Results` — always, even for a
  single command — with `.only` for the singleton case.
- **`exit_code` lives on the family, not the renderer.** Each type knows its
  own CLI mapping; `_render_result` collapses to `typer.Exit(r.exit_code)`.
  The mapping is unit-testable without the CLI.
- **Truthiness** mirrors `requests.Response`-style ergonomics; `is_ok` remains
  for explicit call sites.
- **Deleted:** `CommandStatus` (utils.py) and `RunResult` (host/host.py),
  including `RunResult.only` (subsumed by `Results.only`).

### Exports

`Result`, `CommandResult`, `Results` are added to `_LAZY_EXPORTS` in
`otto/__init__.py`. Rationale (decided 2026-07-01): the `__init__` is 100%
lazy by stated invariant ("bare `import otto` pulls almost nothing"); an eager
import would be the first eager submodule pull, drag `otto.utils` → `asyncio`
into every `import otto`, and grow the import-budget golden snapshot. The
laziness is not about `result.py`'s own weight — it preserves the invariant
with an already-paid mechanism. PEP 562 resolution happens once and caches in
module globals, so there is no steady-state cost.

## Signatures and payload conventions

| Verb | Returns | `value` | `msg` |
| --- | --- | --- | --- |
| `run()` | `Results` | one `CommandResult` per issued command | aggregate diagnostic ("" if ok) |
| `oneshot()` | `CommandResult` | output `str` | "" / diagnostic |
| `get()` / `put()` | `Result` | `dict[Path, Result]` — source path → per-file `Result` (element `value` = destination `Path`, element `msg` = per-file diagnostic) | aggregate diagnostic |
| `power()` | `Result` | `PowerState \| None` | diagnostic |
| `reboot()` | `Result` | `None` | diagnostic |
| `load()` / `unload()` | `Result` | `None` | diagnostic |
| `lsmod()` | `Result` | `list[str]` loaded modules | diagnostic |
| `login()` | `Result` | `None` | diagnostic |
| `interact()` | `None` (exempt) | — | — |

Per-file transfer semantics (decided 2026-07-01, replacing the earlier
single-`Result`-with-`list[Path]` design): every source file gets positive
confirmation, addressable by path — keys are the source paths exactly as the
caller passed them (no resolution), so `res.value[Path("a.bin")]` is that
file's `Result`: `Success` with the destination path as its `value`, a non-ok status
with a per-file diagnostic, or `Status.Skipped` when a prior failure stopped
the batch. Note: `Skipped.is_ok` is True, so a trailing `Skipped` alone never
fails the aggregate — the triggering failure does. The aggregate `status`/
`msg` on the outer `Result` summarize the batch.

Conversion happens **at the source**, not via adapters:

- The `Host` protocol (`host/host.py`) return annotations change.
- All host classes convert: `UnixHost`, `EmbeddedHost`, `LocalHost`,
  `DockerContainerHost`, `RemoteHost` (incl. dry-run paths, which return the
  same shapes).
- The transfer ABC (`host/transfer/base.py` `get_files`/`put_files`) returns
  the per-file-mapping `Result` directly — each backend (scp/sftp/ftp/nc/
  console) reports per-file outcomes itself. A backend that genuinely batches
  (single remote command for all files) derives uniform per-file entries from
  the batch outcome. Third-party transfer backends break loudly
  (return-annotation mismatch at `ty` and a conformance docs update).
- `SessionManager`/session internals that build `CommandStatus` today build
  `CommandResult`.

## CLI rendering and exit codes

`_render_result` (`cli/expose.py`) becomes:

1. **`Result` branch** (one `isinstance(r, Result)` check covers the family):
   - Exit code: `raise typer.Exit(r.exit_code)` when non-zero — the mapping is
     the family's polymorphic property, so command verbs are ssh-like
     (`otto host web1 run 'exit 42'` exits 42; never-ran → 255) and everything
     else is status-mapped (Failed→1, Error→2, Unstable→3, Skipped→0 via
     `is_ok`). The Error→2 overlap with Click's usage-error exit code is
     accepted and documented (common CLI overload).
   - Output on ok: print the `success=` message if the decorator supplied one;
     else print `value` when it is not `None` — lists render one item per
     line; a `dict[..., Result]` value (transfers) renders one
     `src → dest` confirmation line per entry. Command results
     (`CommandResult`/`Results`) print nothing — their output already
     streamed during execution.
   - Output on failure: print `msg` in red; when the value is a mapping of
     per-item `Result`s (or `r` is a `Results`), also print each non-ok
     entry's `msg` (per-file / per-command diagnostics).
2. **`None`**: print success message or "done" (unchanged).
3. **Fallback** for plain values: `rprint(result)`, exit 0. This is a
   *documented feature* for third-party host classes whose custom
   `@cli_exposed` verbs return plain values — the registry system invites
   those. Documented in `docs/guide/extending-backends.md`.

## Documentation (required deliverable, not cleanup)

Both layers update in the same change, gated by the executable-doctest docs
build:

- **In-code:** `result.py` carries the canonical contract as docstrings with
  doctest examples (the `utils.py` `Status`/`CommandStatus` docstrings are the
  precedent — the docs gate executes them). The `Host` protocol docstrings in
  `host/host.py` restate each verb's return contract (today they say
  "Returns a ``(Status, message)`` tuple" — every one of those rewrites), and
  implementation docstrings follow. The transfer ABC documents the per-file
  mapping contract for backend authors.
- **Docs tree:** every page that shows a host-verb return updates — the
  `docs/guide/host/` pages, `docs/cookbook/` recipes (`(status, msg)`
  unpacking and `.only` usage appear there), `docs/guide/library-usage.md`,
  and `docs/guide/extending-backends.md` (transfer-backend mapping contract
  plus the plain-value fallback for custom verbs). A new **"Exit codes"**
  subsection in the CLI-facing host guide documents the mapping table
  (retcode passthrough, 255 never-ran, status values, Skipped→0, the exit-2
  overlap) — this is the user-visible contract the design exists to define.
- The implementation plan inventories the exact page list up front (grep for
  `RunResult`, `CommandStatus`, `Status,`-tuple unpacking, and `.only` across
  `docs/`).

## Migration plan (delete-first)

1. Add `src/otto/result.py` + unit tests.
2. **Delete `CommandStatus` and `RunResult`** so `ty` (at the nox typecheck
   gate) and the test suite enumerate every call site — no silent stragglers.
3. Convert in dependency order: session internals → transfer backends → host
   classes/protocol → `cli/expose.py` renderer → `configmodule`/`OttoContext`
   fan-out helpers (`run_on_all_hosts` keeps its current container shape, with
   `RunResult` values replaced by `Results`) → remaining callers.
4. Sweep tests (~56 files reference the old types) and the docs tree per the
   Documentation section; doctests execute in the docs gate, so stale examples
   fail loudly until updated.
5. Regenerate nothing schema-wise (results are not boundary models); the
   import-budget snapshot only changes if an eager import sneaks in (it must
   not).

Old tuple-unpacking call sites (`status, msg = await h.get(...)`) fail loudly:
`Result` is not iterable, so the unpack raises `TypeError` at runtime, and
`ty` flags every annotation mismatch at the typecheck gate.

## Testing

- **New** `tests/unit/result/`: aggregate-status rules, `.only` (0/1/n),
  `first_failure`, `__bool__`/`is_ok`, frozen-ness, Sequence behavior of
  `Results`, and the full `exit_code` matrix per type (incl. retcode -1 → 255
  and retcode-0-but-failed → status.value).
- **Expose layer** (`tests/unit/cli/`): renderer behavior — exit codes via
  `exit_code`, success-message vs value rendering, per-file transfer
  confirmation lines, per-entry failure diagnostics, plain-value fallback.
- **Transfer layer**: per-file mapping semantics per backend — every source
  path is a key; mid-batch failure marks the failing file non-ok and the
  remainder `Skipped`; aggregate reflects the triggering failure.
- **CLI-subprocess e2e** (`tests/e2e/`): assert real `$?` for
  `run 'exit 42'`, a failing `get`, and a passing verb — the exit code is the
  contract this design exists to define.
- Full gate at completion: `make coverage`, nox (lint = ruff check + format
  --check), typecheck (`ty` runs only here — budget a round after src edits),
  docs (executable doctests).

## Decision log

| Decision | Choice | Alternatives rejected |
| --- | --- | --- |
| Result shape | Flat `Result` + `CommandResult` subclass + `Results` container-as-Result (run()-only) | Payload-generic `Result[T]` envelope keeping `CommandStatus`; bare `list[Result]` (loses aggregate/`.only`/typed retcode) |
| Result vs Results split | Both: `Results` for run() (always, incl. single command), scalar `Result`/`CommandResult` elsewhere; one interface via subclassing | `Results`-everywhere (forces `.only` on scalar verbs for no information gain) |
| Transfer returns | Scalar `Result` with `value = dict[Path, Result]` — per-file confirmation addressable by source path, `Skipped` for not-attempted | Per-file `Results` sequence (position-based; path lookup is O(n) and awkward); single `Result` with `list[Path]` (no per-file failure attribution) |
| Scope | All `@cli_exposed` verbs incl. queries; `interact()` exempt | Effectful-ops-only (leaves two idioms + renderer fallback for first-party verbs) |
| CLI exit codes | Polymorphic `exit_code` property on the family; renderer is `typer.Exit(r.exit_code)` | Mapping logic inside the renderer (untestable without the CLI); status-mapped everywhere; binary 0/1 |
| Exports | Lazy (`_LAZY_EXPORTS`) | Eager (would break the import-light invariant, pull `utils`→`asyncio` eagerly) |
| Compat | Delete-first, no shims | Deprecation aliases (pointless pre-freeze) |
