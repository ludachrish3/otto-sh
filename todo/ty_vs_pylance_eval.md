# ty vs. Pylance: periodic re-evaluation

A re-runnable rubric for deciding whether to keep Pylance (pyright) as the VS Code LSP or switch to astral's `ty`. The CLI side of the trial is already wired up — `make typecheck` runs `ty check` with all rules at error. This file tracks the LSP-side and ergonomics comparison.

Append a new dated entry to each section on every re-run; do not overwrite prior entries — the file's history is the trend line.

## How to run this evaluation

1. Note the current `ty` version: `uv run ty --version`.
2. Pick the same representative files each time so results are comparable:
   - [src/otto/host/remoteHost.py](../src/otto/host/remoteHost.py) — async I/O, generics, many attribute accesses
   - [src/otto/host/host.py](../src/otto/host/host.py) — `Self` type usage, subclass-heavy
   - [src/otto/storage/protocol.py](../src/otto/storage/protocol.py) — `Protocol` usage
   - [src/otto/suite/plugin.py](../src/otto/suite/plugin.py) — pytest-internal typing edges
   - [src/otto/logger/logger.py](../src/otto/logger/logger.py) — the one inline pyright suppression
3. For the LSP comparison, toggle the active language server in VS Code by disabling one extension and enabling the other on this workspace (`.vscode/settings.json`, `python.languageServer`). Reload the window.
4. Fill in one row per tool in each section below. Record what you *observe*, not what you expect.

## Editor experience

Checks to perform on the representative files with each LSP active:

| Check                                                          | Pylance | ty |
|----------------------------------------------------------------|---------|----|
| Go-to-definition across packages (`otto.host` → `otto.storage`)|         |    |
| Find-all-references across the package                         |         |    |
| Hover shows accurate inferred types on generics / `Protocol`   |         |    |
| Autocomplete on instance attributes set in `__init__`          |         |    |
| Autocomplete inside `async` / `await` / context managers       |         |    |
| Rename-symbol correctness (no missed call sites)               |         |    |
| Inlay hints for inferred types                                 |         |    |
| Cold-start responsiveness (time to first diagnostic)           |         |    |
| Recovery from syntax errors (still useful mid-edit?)           |         |    |
| Extension-host memory footprint (rough, from VS Code's monitor)|         |    |

## Diagnostic parity (strict)

Open the same representative files under each LSP and record the Problems panel.

| File                                   | Pylance strict count | ty count |
|----------------------------------------|---------------------:|---------:|
| src/otto/host/remoteHost.py            |                      |          |
| src/otto/host/host.py                  |                      |          |
| src/otto/storage/protocol.py           |                      |          |
| src/otto/suite/plugin.py               |                      |          |
| src/otto/logger/logger.py              |                      |          |

Findings unique to one tool — classify each as **real bug**, **strictness style**, or **false positive**:

- Pylance-only:
  - (none yet)
- ty-only:
  - (none yet)

Pyright strict checks without a ty equivalent (or vice versa) — this is the main gap being watched:

- (none identified yet)

## Ergonomics

- Error-message quality on a contrived failure: write a small type-incorrect snippet in a scratch file and compare the diagnostic text. ty is advertised as having richer messages — verify that claim on otto's own types.
- Suppression comments: confirm `# pyright: ignore[...]` and `# ty: ignore[...]` each work with their respective tool and do *not* cross-confuse the other.
- `typings/telnetlib3/` stubs: both tools should resolve `telnetlib3` via these stubs without spurious "unresolved-import" errors.

## Stability signals

Update each run:

| Date       | ty version | Notes on release-note breaking diagnostic changes since last eval | Has astral shipped a strict preset? |
|------------|-----------:|-------------------------------------------------------------------|-------------------------------------|
| 2026-04-17 | 0.0.31     | Initial adoption — baseline 45 diagnostics under `all = "error"`  | No                                  |

## Decision gate

Update after each run. Options: **keep Pylance**, **switch to ty**, or **re-evaluate in N months**.

- **2026-04-17**: Re-evaluate in ~3 months (target: 2026-07). Rationale: ty is still 0.0.x with explicit breaking-change policy, no strict preset, and we haven't driven the baseline down yet. Pylance remains the daily LSP; ty runs advisory via `make typecheck` and is available as an alternate LSP for anyone who wants to try it.
