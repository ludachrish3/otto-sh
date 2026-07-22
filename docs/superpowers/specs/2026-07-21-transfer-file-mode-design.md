# File permission mode on transfers to hosts — design

**Date:** 2026-07-21
**Status:** Approved design, pending implementation plan
**Origin:** Feature request — a file-permission parameter for file transfers to
hosts, with octal number support on the CLI.

Today `put` moves bytes and leaves permissions to whatever the backend's
default happens to be: SFTP writes `0o644`, `nc` inherits the receiving
shell's umask, `scp` carries the source file's mode. A test binary uploaded to
a target is therefore not reliably executable, and the fix is a second manual
`chmod` at every call site. `mode` closes that gap.

## Goals

- One `mode` parameter on `put`, honoured identically by every backend that
  has a permission model to honour it with.
- CLI octal that cannot be misread: `--mode 755` means `0o755`, never decimal
  `755`.
- A host family with no permission model (embedded) **fails loudly**, before
  transferring any bytes.
- One parse site, one unsupported-error site, and a batched `chmod` — not one
  per file.
- Custom backends registered via `register_transfer_backend` inherit working
  `mode` support without code changes.

## Non-goals

- `mode` on `get`. The request was transfers *to* hosts; local umask makes a
  downloaded-file mode largely moot, and it doubles the surface for no
  demonstrated need.
- Symbolic modes (`u+x`, `a+rx`). They are *relative* to the file's existing
  bits, which after a transfer differ per backend (scp: source mode umask-
  masked; sftp: `0o644`; nc: shell-redirect umask). `u+x` would therefore not
  be reproducible across backends — a footgun, not a feature.
- Ownership (`chown`/`chgrp`). Separate concern, needs privilege escalation.
- A general-purpose `chmod` verb on `PosixFileOps`. Not requested; the
  batched-command helper here is available if one is wanted later.

## Approach

Three options were weighed:

- **A — an `_apply_mode` hook on the transfer layer.** `put_files` grows
  `mode`; a single hook applies it. Chosen.
- **B — native per backend.** SFTP `setstat`, FTP `SITE CHMOD`, `nc` folding
  `&& chmod` into its receive pipeline, `scp` post-chmod. Marginally more
  atomic and saves `nc` a round trip, but it is four-plus implementations to
  keep consistent, `SITE CHMOD` is not universally supported by FTP servers,
  and every future backend must reimplement it. Rejected.
- **C — host-level, via a new `PosixFileOps.chmod`.** Logic duplicates across
  `UnixHost` / `DockerContainerHost` / `LocalHost`, `EmbeddedHost` needs its
  own guard, and — decisively — a direct `put_files` caller would silently
  lose `mode`, because the transfer layer never learns about it. Rejected.

**A** puts the behaviour where the dest paths are already known and where the
per-file `Result` mapping is already assembled, so all four unix backends
(`scp`, `sftp`, `ftp`, `nc`) are served by one implementation on their shared
`UnixFileTransfer` base.

| Class | `_apply_mode` | Covers |
| --- | --- | --- |
| `BaseFileTransfer` | not implemented; `supports_mode = False` | anything that does not opt in |
| `UnixFileTransfer` | one batched `chmod` via `self._exec_cmd` | `scp`, `sftp`, `ftp`, `nc` |
| `LocalFileTransfer` | `Path.chmod` in a thread | `local` |
| `EmbeddedFileTransfer` | *inherits the unsupported default* | `console`, `tftp` |

## API surface

```python
# BaseFileTransfer — the single seam
async def put_files(
    self,
    src_files: list[Path],
    dest_dir: Path,
    show_progress: bool = True,
    mode: int | str | None = None,   # NEW
) -> Result: ...

# UnixHost / EmbeddedHost / LocalHost / DockerContainerHost — declare + pass through
async def put(
    self,
    src_files: Annotated[list[Path] | Path, Arg(variadic=True, elem_type=Path, ...)],
    dest_dir: Path,
    mode: Annotated[
        int | str | None,
        Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
    ] = None,
    show_progress: Annotated[bool, Exclude] = True,
) -> Result: ...
```

The union is `int | str | None` because the two entry points differ in kind:
the Python API passes `mode=0o755` — already an `int`, already unambiguous —
while the CLI passes the string `"755"`, which is *always* base-8.

`param_synth` collapses `int | str | None` to a plain `str` Typer option with
a `None` default (`_normalize_scalar` returns the `Opt`/union fallback `str`,
dropping the `Optional`). This was verified end-to-end against Typer 0.27:
absent → `None`, `--mode 755` → `"755"`. **No `param_synth` change is
required**, and none should be made as part of this work.

## Octal parsing — `parse_file_mode`

New in `transfer/base.py`, beside `validate_filename_lengths`, and returning a
`Result` exactly like that neighbour so the two validators fold identically.

| Input | Result | Note |
| --- | --- | --- |
| `None` | ok, `value=None` | no mode requested |
| `0o755` (int) | ok, `0o755` | Python API — already a mode, **never** re-read as base-8 |
| `"755"` / `"0755"` / `"0o755"` | ok, `0o755` | one `int(s, 8)` accepts all three prefixes |
| `"4755"` | ok, `0o4755` | setuid/setgid/sticky permitted, as `chmod` permits them |
| `"789"` | error | `invalid octal mode '789': digits must be 0-7` |
| `"rwx"`, `""` | error | `invalid octal mode 'rwx'` |
| `-1`, `"77777"` | error | `mode 0o77777 out of range (0 to 0o7777)` |

Range is `0 <= mode <= 0o7777`. `int(s, 8)` is the whole parser: Python accepts
a `0o` prefix when the base is explicitly 8, and a bare or single-`0`-prefixed
string too, so all three accepted spellings collapse to one call with no
prefix-stripping branch.

## Capability + error semantics

A declarative class attribute, mirroring the existing `host_families` pattern
on the same class:

```python
class BaseFileTransfer:
    supports_mode: bool = False   # UnixFileTransfer / LocalFileTransfer set True
```

`put_files` checks it **pre-flight** — beside the existing filename-length
validation, before `_run_put` is reached. A `put --mode` against a Zephyr
target must fail before a single byte moves, not after a 200 MB upload.

Pre-flight order is fixed and tested, cheapest and most-specific first:

1. `parse_file_mode` — a typo'd `--mode 789` is the user's own input and is
   reported without consulting the host at all.
2. `supports_mode` — a valid mode against a backend that can never apply it.
3. `validate_filename_lengths` — the existing check, unchanged and last.

Each returns immediately, so exactly one diagnostic is produced per failure
rather than a merged report:

```text
host 'zephyr1': ConsoleFileTransfer has no permission model; cannot apply
mode 0o755. Drop the mode argument (--mode on the CLI) or transfer with a
backend that supports it.
```

The message names the host (`self._name`) and the backend class, which is
already available without threading the protocol name into the instance.

Both failure kinds — bad octal and unsupported backend — fold through
`aggregate_transfer` into the same per-file error mapping a filename-length
rejection already produces, so `Result.value` keeps its documented
`dict[Path, Result]` shape on every path.

After a successful `_run_put`, `_apply_mode(dest_paths, mode)` runs **one**
batched `chmod` over just the files that succeeded — entries that are
`Status.Error` or `Status.Skipped` are excluded, since chmod-ing a path that
was never written would fail and mask the real error. When *no* file
succeeded, `_apply_mode` is not called at all: there is nothing to chmod, and
an empty `chmod` invocation would turn a clean transfer failure into a
confusing second one. A chmod failure
downgrades those files to `Status.Error` while **preserving `value=dest_path`**:
the bytes landed, the permissions did not, and a caller can tell the two apart.

A shared `chmod_command(mode, paths)` helper builds the `shlex`-quoted
`chmod <octal> <paths...>` string, so the sites that issue it cannot drift.

`_apply_mode`'s default raises `NotImplementedError` (the same pattern as
`create`), so a backend that sets `supports_mode = True` without implementing
it fails immediately and loudly rather than silently no-op'ing.

### Docker

`DockerContainerHost.put` stages to the parent then `docker cp`s into the
container, so it is the one host that does not inherit the behaviour for free.
It applies `chmod` explicitly *inside* the container via `self.exec` after the
copies land, using the same `chmod_command` helper — deliberately **not** by
stamping the staging copy and relying on `docker cp`'s mode-preservation
semantics, which would put undocumented third-party behaviour in the trust
path.

### Dry run

`_dry_run_transfer` gains the mode in its banner when one is set:
`[DRY RUN] PUT: app -> /opt/bin (mode 0o755)`.

## Testing

The decimal trap is the highest-value guard and must be **proven red**:
annotate `mode` as a plain `int` and `--mode 755` yields `755`, not `0o755`
(`493`). A guard that cannot fail against the wrong implementation is not a
regression test.

**Unit** — `tests/unit/host/test_transfer_mode.py` (new), beside the existing
`test_transfer_per_file.py`:

- `parse_file_mode` driven by the table above — every accept and every reject,
  including `0o755`-as-`int` passing through untouched.
- **Pre-flight proof:** `put --mode` on an unsupported backend leaves
  `_run_put` *uncalled*. Asserting only the error would still pass if bytes
  had already moved.
- `_apply_mode` receives only the dest paths that succeeded; failed and
  `Skipped` files are excluded.
- A chmod failure yields `Status.Error` with `value` still holding `dest_path`.
- `UnixFileTransfer._apply_mode` issues **exactly one** exec for N files —
  guards the batching, not merely the outcome.
- `chmod_command` quoting: paths containing spaces and quotes.
- `LocalFileTransfer` performs a real chmod under `tmp_path`, asserted via
  `stat().st_mode & 0o7777`.
- Embedded `put --mode` fails with the host name in the message.

**CLI** — `--mode 755` arrives as `0o755` at the method boundary; `--mode 789`
exits non-zero carrying the parse message; the dry-run banner shows
`(mode 0o755)`.

**e2e** — the Docker in-container assertion is the only case that must run
against real `docker cp`, being the one place the design touches semantics
that cannot be verified by reading. It stays within the existing docker e2e
test; docker is not spread into new tests.

## Documentation

- Docstrings on the four `put` methods and the new helpers. No
  `from __future__ import annotations` (it trips Sphinx nitpicky `-W`), and a
  **clean** docs rebuild rather than an incremental one, since incremental
  Sphinx misses broken `:doc:`/`:meth:` refs in docstrings.
- `docs/guide/hosts/index.md` (transfer section, ~L88-113) — the per-class
  semantics table is exactly where "embedded has no permission model" belongs.
- `docs/guide/cli-reference.md` (`put` / `get` arguments, ~L261-269) — add
  `--mode`.
- No manual `CHANGELOG.md` edit; `cliff.toml` generates it from conventional
  commits.

## Gates

`make coverage` as the task gate, plus `nox -s lint` and `make typecheck`
(`ty` runs only at the nox typecheck session, so a typecheck round is budgeted
after the source edits).
