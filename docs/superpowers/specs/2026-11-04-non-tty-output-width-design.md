# Non-tty output width: one width default, literal paths, literal identifiers (#785)

**Status:** revision 10, 2026-11-04. Astra confirmed r9 with changes, and r10 applies them:
- `interactive_terminal_size(fallback=)` keeps telnet's configured size, with its environment policy stated;
- BusyBox tests split into supported and unsupported builds;
- the `otto.host → otto._terminal` tach edge;
- "help and error output".

r9:

Opus r8 approved it with changes. r9 applies them:
- I1: a BusyBox `ps` fallback, measured;
- I2: the existing `ps_scan_command` core and one `PS_FAILED`;
- I3: an interactive terminal size that ignores `COLUMNS`;
- M1–M6;
- the D4 user-docs gaps.

r8:

Astra r7 approved it with changes. r8 applies them:
- the dry-run and summary ids in the literal migration;
- the scoping table's lab and host columns;
- the `_terminal` library-import wording and import-budget acceptance;
- the dumb-terminal condition stated precisely.

Earlier:

r7 folds in Astra r6 (Rework):
- **Cycle:** `usable_terminal_size` moves to a new foundation module, `otto._terminal`, to break the tach cycle.
- **Cells:** the cell promise is narrowed to "literal, folds instead of …", consistently, with no minimum-width immunity.
- **Budgets:** the claim is now "ceilings pass without increase".
- **`TERM=dumb`:** the dumb-terminal exception is aligned between D1 and D4.

r6 folded in Astra r5 (Rework):
- **A:** one `usable_terminal_size` shared by the shim and `console.py`'s pin;
- **B:** the file console's environment policy (height, `NO_COLOR`, `TERM=dumb`);
- **C:** the table-cell promise narrowed to "never substituted or cut", with Rich's fold limits stated and pinned.
- **r4** folded in Opus r3: F1 shim placement, F2 `ps -ww`, F3 breaking (later overruled: not breaking), F4 a terminal on stderr/stdin, F5 the parse rule, and M1–M4.
- **r5** folds in Astra r4 (Rework):
  1. three `ps` scans, one builder;
  2. Rich's constructor-time width pin;
  3. invalid values dropped on every path;
  4. file-log color made deterministic;
  5. literal table cells;
  6. the tunnel and link headers;
  7. wording;
  8. tests that can fail;
  9. citations.

**Review history:**

| Revision | Astra | Opus |
|---|---|---|
| 1 | Rework | Approve with changes |
| 2 | Rework: pytest hook timing; import budget | Approve with changes: N1 import budget (Critical); N2–N9 |
| 3 | (Codex error; not run) | Approve with changes: F1–F5, M1–M4 |
| 4 | Rework: findings 1–9 | — |
| 5 | Rework: A console pin vs 0×0, B file-console env, C fold limits | — |
| 6 | Rework: 1 `_shim` import cycle, 2 C still overstated, 3 budget wording, 4 dumb-terminal wording; B confirmed (16/16 byte-identical) | — |

Revision 3 replaces revision 2's console factory and installer with one environment default. The reasons are under D1.

**Issue:** #785, a round-2 naive-user study finding. Six of ten scripted trials hit it, and each one rediscovered `COLUMNS=200`.

## Problem

When **no terminal is attached** and `COLUMNS` is unset, every Rich console otto renders through falls back to 80 columns. "No terminal" means none of stdin, stdout and stderr is a tty: agents, CI, cron, `capture_output=True`. Rich probes fds 0, 1 and 2 (`rich/console.py:92, 1021-1028`), so `otto … | less` from a terminal already renders at the terminal's width.

At 80 columns:

- **Tables and help truncate.** Table identifier cells end in "…", and so do Typer's help option names (`--include-projec…`).
- **The output-directory path is mangled.** `Output directory: …` (`src/otto/logger/management.py:878`) wraps mid-path and interprets markup.
- **pytest truncates its summary.** pytest's terminal writer under `otto test` sizes itself through `shutil.get_terminal_size`, which reads `COLUMNS` and then falls back to 80, so "short test summary info" lines are cut.

The docs tell users to set `COLUMNS=200` (`docs/cli/index.md:118, :201`; `docs/use/joining.md:56-58`).

## Goals

1. With no terminal attached, otto behaves exactly as if the user had followed that advice: everything renders 200 wide, with no setup.
2. A path otto prints on its own line is one contiguous, literal token at any width: no wrap, no markup, no emoji substitution.
3. An identifier or path in a table cell is never substituted or cut off with "…". It may fold, within the limits stated in D3.
4. Log files don't change with how stdout was attached.

## Non-goals

- A machine-readable output format.
- Any width change when stdout is a terminal, or when a usable `COLUMNS` is set (including by a test). The one exception: an unusable `COLUMNS` is dropped on every path.
- Patching Typer or Rich internals.

## Design

### D1. The one width default: a `COLUMNS` value the CLI sets for its run

**Where it lives.** It sits in `src/otto/_shim.py`, beside the CLI's one existing environment default, `PYDANTIC_DISABLE_PLUGINS` (`_shim.py:~107-116`). That comment states the rule this follows: it is set in the shim, not in `otto/__init__.py`, so library use never changes the caller's environment, and every process the CLI starts inherits it.

Both entry points run the shim first: the `otto` console script (`pyproject.toml:112`) and `python -m otto` (`__main__.py`). In-process tests call `entry()` directly and never reach it, so they never leak `COLUMNS` into a pytest worker (Opus r3 F1). The policy is stdlib-only and inline in the shim; the shared descriptor check lives in `otto._terminal` (below).

**The rule.** `_default_columns(environ, stdout_columns, other_columns) -> str | None` is a pure function. It returns the value `COLUMNS` should hold, where `None` means unset; that one value covers retain, drop and set (no bare tuples, #633). Its live wrapper reads the descriptors, then sets or pops `COLUMNS`.

A **usable** `COLUMNS` is `value.isascii() and value.isdigit() and int(value) >= 1`, with no stripping. That is deliberately stricter than its consumers:
- Rich and `shutil` both read full-width `１２０` as 120.
- Rich accepts `0`, giving a zero-width console.
- Rich accepts `²`, and `int()` then raises.

ASCII-only is the defensible line (Astra r4 3).

The steps run in order:

1. **A usable `COLUMNS` is retained**, whatever the descriptors are.
2. **An unusable one is always dropped,** on every descriptor configuration, including a stdout tty. So `COLUMNS=0` or `COLUMNS=²` never survives to a consumer.
3. **A terminal on stdout.** If fd 1 is a terminal with a readable size of at least 1×1, nothing is set. Rich and `shutil.get_terminal_size` (which probes `sys.__stdout__`) both see the terminal.
4. **A terminal on stderr or stdin only.** Otherwise, if fd 2 or fd 0 is a terminal with a readable size of at least 1×1, its columns are set. That covers a stdout tty of 0×0 as well. This is `otto test | tee log` run from a terminal: Rich probes stdin, then stdout, then stderr and finds the terminal, but pytest would fall back to 80 (Opus r3 F4).
5. **No terminal.** Otherwise, `NON_TTY_COLUMNS = 200` is set.

**A launch-time snapshot.** The value set in steps 4 and 5 is fixed when otto launches. A later terminal resize is not followed, which is acceptable for a non-stdout terminal. pytest clamps widths below 40 to 80 (`_pytest/_io/terminalwriter.py:26`), so a terminal narrower than 40 still gives pytest 80.

**Ordering.** The shim sets the value before importing the CLI graph, and this ordering is load-bearing:
- Rich reads `COLUMNS` in `Console.__init__` and stores it in `_width` (`rich/console.py:684`). Its size calculation then prefers the stored value (`:1040`).
- So `otto.console.CONSOLE` (`console.py:8`) and every other console the CLI builds are pinned at construction to the value the shim chose. A later change to `COLUMNS` reaches `shutil` and pytest, but not an already-built console (`tests/conftest.py:2494` documents this).
- `console.py:16` also reads the terminal size at import, which is unaffected on the tty path.

Completion and `--version` answered by the shim itself don't render widths. The cost is one `isatty` probe per descriptor, at most three `os.get_terminal_size` calls and a dict write. The shim gains one stdlib-only otto module (below). The existing import-budget ceilings must pass without an increase; if one would rise, the plan stops and asks, since ceilings never rebaseline.

**One "usable terminal" decision, not two (Astra r5 A).** `src/otto/console.py:15-16` pins `CONSOLE.size = os.get_terminal_size()` whenever the call succeeds. A stdout that reports 0×0 therefore pins a zero-width console, undoing step 4, and a real stdout tty overwrites the constructor width either way.

So "is this descriptor a terminal with a usable size" becomes one stdlib function, `usable_terminal_size(fd) -> os.terminal_size | None`, which returns the size only when `isatty(fd)` and both dimensions are at least 1.
- **Where it lives.** It goes in a new foundation module, `src/otto/_terminal.py`, which imports only `os` and depends on nothing in otto (Astra r6 1).
  - Importing it from `_shim.py` would create the cycle `otto.console → otto._shim → otto.cli → otto.logger → otto.console`, which tach forbids even for function-local imports (`tach.toml:15`).
  - `tach.toml` declares `otto._terminal` as a foundation module with no dependencies. `otto._shim`, `otto.console` and `otto.host` (for `interactive_terminal_size`) gain the edge to it. The existing `otto.host → otto.console` edge doesn't cover it, and explicit downward edges are mandatory (`tach.toml:24`).
  - `console.py:15-16` pins the stdout size only when there is no usable `COLUMNS` and stdout has a usable size; otherwise it leaves the constructor's width alone. Without the first condition, `COLUMNS=100 otto …` in a terminal would render `get_console()` at 100 but `CONSOLE` at the terminal width (Opus r8 M2).
  - `usable_columns(value) -> int | None`, the ASCII-digits rule, lives in `otto._terminal` beside `usable_terminal_size`. The shim and the pin both call it.
- **Who calls it.** The shim's steps 3 and 4, and the console pin.
- **Library imports.** `console.py` imports `otto._terminal`, never `otto._shim`, so importing `otto.console` as a library never applies the CLI's `COLUMNS` default. Only the shim does.
- **Import cost.** The shim imports `otto._terminal` lazily, after its `--version` and completion fast paths, so the module-scope and warm-TAB pins hold. `test_the_shim_entry_imports_only_stdlib_at_module_level` (`tests/unit/test_shim.py:450`) gets `otto._terminal` added to its deferred-import allowlist. That is a structural update, not a ceiling change.
  - The module adds file operations on full CLI paths. `test_import_budget` (`tests/unit/import_budget/test_import_budget.py:183`) must pass under its existing ceilings: for example, `dispatch_project_warm`'s 2,789 file ops and its 4.4× ratio against `version_project` (`scripts/import_budget.py:443`). If any would rise, the plan stops and asks.

**In-process tests are unaffected.** They call `entry()` directly, never reach the shim, and keep their own `COLUMNS` handling. A root-conftest autouse guard **snapshots and restores** `COLUMNS` around **every** test, never popping it, so an ambient value still reaches the tests that rely on it. It mirrors the environment guard at `tests/unit/test_shim.py:26-40`.

Every test site has to be covered, because seven call `_shim.main()` in-process: `tests/unit/cli/test_names_consumer.py`, `test_stale_tab_repair.py`, `tests/unit/shim/*`, `import_budget/test_import_budget.py` and `tests/_fixtures/generated_project.py`. On a no-tty xdist worker, each would otherwise leave `COLUMNS=200` behind.

**What it reaches, with no per-consumer code.** Each of these consoles is built after the shim, so it is constructed at the shim's value:
- `otto.console.CONSOLE`;
- Rich's global console (`rich.print`/`get_console`);
- `env.py:91`'s `Console()`;
- the crash traceback console (`formatters.py:319`, `width=CONSOLE.width`);
- Typer's help and usage-error consoles (`width=MAX_WIDTH`, which is None unless `TERMINAL_WIDTH` is set).

Beyond the consoles:
- **pytest's terminal writer** under `otto test`, through `shutil.get_terminal_size` (`_pytest/terminal.py`), read once at reporter creation. That covers the summary trim and the progress column.
- **Child processes otto starts.** They inherit the value, exactly as if the user had exported it, which the docs told them to do. Opus (r3) and Astra (r4) probed the children:
  - ssh: by default asyncssh sends no `COLUMNS`. Its `send_env` is empty unless the user's ssh config adds `SendEnv` (`asyncssh/connection.py:8464`).
  - **The remote pty size (Opus r8 I3).** otto sizes it from `shutil.get_terminal_size`, which reads `COLUMNS` first. That covers `host/interact.py:311` `_initial_term_size` (the ssh `term_size` at `:953-958`, and the resize polls at `:980, :1109, :1116`) and telnet's NAWS (`host/telnet.py:722`, `:1002`).
    - Under step 4 (`otto host x login | tee log`), the launch snapshot would freeze the remote pty at its launch width through every resize.
    - Both helpers make the same decision, the size of the local interactive terminal. They move onto one function in `otto._terminal`: `interactive_terminal_size(fallback: os.terminal_size = os.terminal_size((80, 24))) -> os.terminal_size`. It returns `usable_terminal_size(0)`, else that of fds 1 and 2, else `fallback`.
      - **Telnet's fallback.** telnet's initial NAWS (`telnet.py:1002`) falls back today to the configured `(options.cols, options.rows)`, whose default width of 400 is chosen to avoid automation wrapping (`options.py:405`). It passes those as `fallback` at connection time, so that behaviour is kept, including when the tty's size is unreadable.
      - **Environment policy, stated rather than claimed as "as today".** It ignores `COLUMNS` and `LINES`, explicit values included. It prefers stdin over stdout, because stdin is the interactive terminal a login reads from. Before, `shutil` honoured `COLUMNS`/`LINES` and probed stdout. Ignoring an injected `COLUMNS` is the point, since resizes keep reaching the remote pty. A login with no terminal at all asks for the fallback.
  - docker and compose size from the terminal.
  - git's `--stat` gets wider.
  - pytest children get the intended width.
  - Click's `CliRunner` forces width 80 for plain Click help (`click/testing.py:320`). Rich-rendered output inside it can still change.
  - No `top` invocation exists in `src/otto`. Built-in monitoring reads `free -b`, `df -h`, `ss -s` and `/proc` (`monitor/parsers.py:257`).
  - **procps `ps` is the exception.** It honours `COLUMNS` and truncates `args` to it, where a pipe is otherwise unlimited. `LocalSession` runs bash over pipes, not a pty (`session.py:1687`). Three scans use `ps -eo … args=`:
    - the daemon sentinel scan, `src/otto/host/daemon.py:226` (`split_token` fails, and the daemon vanishes from list and reap);
    - the tunnel sweep, `src/otto/tunnel/check_probes.py:41` `SWEEP_COMMAND` (undecodable tokens are dropped silently at `_tunnel_echoes.py:103`);
    - the link sweep, `src/otto/link/check_live.py:101` `SWEEP_SCAN` (the tag search at `:568` misses a cut tag).

    Astra reproduced a cut at exactly 60 columns under `COLUMNS=60`, and a full line with `-ww`. All three bugs already exist for any user who exports `COLUMNS` as the docs advise.

    **Fix, a separate commit in this PR (Opus r8 I1, I2).** The shared builder already exists: `otto.host.daemon.ps_scan_command` (`daemon.py:194`). It has two callers, `tunnel/discovery.py:30` and `link/sentinel.py:37`. The two sweeps restate its command string, and their docstrings say "the same scan as `ps_scan_command`".
    - **One core.** Extract the common core of `ps_scan_command`: the fields as separate `-eo` flags (`daemon.py:197-202` explains why), and an optional failure-marker leg. Call it from all three scans.
    - **One marker.** `PS_FAILED = "@ps-failed"`, defined twice today (`tunnel/check_probes.py:37`, `link/check_live.py:97`), becomes one constant beside the core.
    - **The command.** The core spells `{ ps -ww -eo … 2>/dev/null || ps -eo … ; }`:
      - BusyBox `ps` rejects `-w` ("invalid option -- 'w'", BusyBox 1.36.1 on the dev VM), so it takes the fallback.
      - BusyBox `ps` doesn't cut `args` to `COLUMNS` on a pipe; measured, 168 characters with `COLUMNS=60` and without.
      - Without the fallback, the daemon scan's `2>/dev/null | grep … || true` would turn a rejected flag into an empty "clean" answer, the exact failure this fix prevents.
    - **Why `-ww`, not unsetting `COLUMNS`.** `-ww` also fixes `ps` on pty-backed sessions (telnet consoles), where procps cuts `args` to the pty width whatever `COLUMNS` says.

**Why this beats revision 2's console factory:**

- Rich, Typer and pytest all already treat `COLUMNS` as the user's stated width. So one environment default reaches every consumer without importing Rich or `typer.rich_utils` on the light surfaces (Opus N1).
- It doesn't depend on pytest hook timing (Astra 1, Opus N2).
- In-process tests never pass through the shim, so the many tests that `monkeypatch.setenv("COLUMNS", …)` are unaffected (Opus N5; `tests/conftest.py:2493-2522`).
- It keeps the crash traceback at the launch terminal's width when one was attached to stdout (Opus N6), because otto sets nothing in that case.
- The objection that it leaks to children doesn't hold: the children see the same environment a user following the documented advice gave them. The one lossy child, `ps`, is fixed.

**Accepted limit, stated in the docs.** The controlling condition is Rich's own: a console it classifies as a dumb terminal (a terminal, real or forced, with `TERM` `dumb` or `unknown`) falls back to 80x25 unless both its width and height are stored (`rich/console.py:1010-1016`). A stored height means `LINES` parsed as a number; `LINES=bad` leaves the fallback active (`:688`).

Which consoles are forced to a terminal:
- **Rich** forces any console on `FORCE_COLOR` or `TTY_COMPATIBLE=1` (`:955`).
- **Typer** additionally forces its own help and error consoles on `PY_COLORS` or `GITHUB_ACTIONS` (`typer/rich_utils.py:78`). Those two don't force an ordinary Rich console.

So `COLUMNS=200` alone gives 80x25 there, while `COLUMNS=200` with `LINES=25` gives 200x25. That combination declares an 80-column terminal, and Rich honours it on purpose. otto's own CI sets `TERM=dumb`, so the width tests remove `TERM`. The `COLUMNS` docs row names the exception.

**Library use.** Importing otto as a library never sets `COLUMNS`, because only the shim applies the default. A library caller that opens an interactive login does get `interactive_terminal_size`'s policy, which ignores `COLUMNS`/`LINES`.

### D2. `print_literal(text, *, console=None)`: "print this exactly", in one place

`print_literal` lives in `src/otto/console.py` and prints with `markup=False, emoji=False, highlight=False, soft_wrap=True`.

- **Default console.** It uses Rich's `get_console()`, the console conftest already resets per test. The output-dir caller passes `CONSOLE`.
- **Styled output.** It also accepts a `rich.text.Text`, whose styles survive `markup=False`. A caller that wants a styled prefix builds it with `Text.assemble(("Wrote schemas to ", "green"), str(path))`, so the path itself stays a plain span.

**Every existing copy of the decision moves onto it:**

- `src/otto/cli/invoke.py:1952`, `_print_value(literal=True)`, where the spelling comes from;
- `src/otto/cli/run.py:115-116`, which uses the identical spelling;
- `src/otto/cli/tunnel.py:187`, `src/otto/cli/link.py:115` and `src/otto/cli/inventory.py:71`. These have no `emoji=False` today, so `:smile:` in an id or path is substituted. Inventory also prints exported paths, at `:356`.
- `src/otto/cli/listing.py:21` (`_print_line`);
- `src/otto/cli/host.py:113`. Today it's `rprint(f"  - {known_id}")`, which renders host ids as markup.
- every header and summary that prints user-controlled ids with emoji still on, through `Text.assemble`, consistent with their `_row`s:
  - the check headers, `src/otto/cli/tunnel.py:222` and `src/otto/cli/link.py:522`;
  - the dry-run plans, `tunnel.py:201` and `link.py:129` (`_print_dry_run_plan`);
  - the link partial-scan and read-failure summaries, `link.py:471, :482`.

**New callers:**

- `_print_output_dir` (`management.py:878`), which keeps its `\n` prefix;
- `cov.py:574-575`;
- `schema.py:74-78`, using `Text.assemble` for its cyan, yellow and green styling;
- the monitor URL line (`monitor/server.py:994-999`).

### D3. `id_column(table, header, **kwargs)`: identifier and path columns are literal and fold

`id_column` lives in `src/otto/console.py` and adds a column with `overflow="fold"`. Its companion, `literal_cell(value) -> Text`, is a plain `Text(str(value))`, with no markup and no emoji substitution.

Folding alone isn't literal. `escape()` protects markup but not emoji: a folded `/tmp/:smile:/[red]x[/red]` rendered as `/tmp/😄/[red]x[/red]` (Astra r4 5), and `project/render.py:100` passes names as markup-capable strings. So every cell in an `id_column` is added through `literal_cell`, replacing the `escape()` calls on those cells (`cli/listing.py:102`). `literal_cell` and `print_literal` are the same decision, "this text is literal", made for a cell and for a line. They share one definition in `console.py`: `print_literal(value)` prints `literal_cell(value)`, or the given `Text`, with `soft_wrap=True`, so the flag set lives in one place.

**Boundary with `escape_relayed` (Opus r8 M3).** `otto.logger.relay.escape_relayed` (`relay.py:24`) stays the helper for foreign text that must remain a markup string: log records, and progress descriptions (`host/transfer/progress.py:59`). `literal_cell` and `print_literal` cover everything else that is displayed. `relay.py`'s module docstring points to them.

**The promise (Astra r5 C, r6 2).** Markup and emoji are never interpreted, and an over-long cell folds rather than ending in "…". That is all. The token may fold, so it doesn't stay contiguous; D2 is the contiguous-token promise, for standalone lines only.

Rich's wrapping still has two limits, stated in the contributor docs rather than engineered around:
- whitespace that falls exactly on a fold boundary is dropped (`"ab cd"` at width 2 renders `ab` / `cd`);
- a double-width character can't render in a column one cell wide (`rich/text.py:1230-1248`, `:873-880`).

These limits apply at any width: a space on a fold boundary goes at width 200 too, and a cell's width is not the console's, so wide characters can drop in a many-column table. They're stated in both the contributor docs and the user docs (D4). A test pins them so a Rich upgrade that changes them is noticed.

**The rule,** stated in `docs/contributing.md`: every table column whose cells hold an identifier, a path, an address or a value echoed from config uses `id_column`. The sites converted here, audited by content:

- `cli/listing.py:56, :91`: name, variant, project, hosts, artifact, stage dir, match
- `coverage/instrumentation.py:98`: host, product
- `cli/env.py:107`: project, distribution
- `cli/cache.py:220`: key
- `cli/tunnel.py:429`: ID, ENDPOINTS, VIA
- `cli/docker.py:1063`: fragment, host, env keys
- `cli/reservation.py:170`: resource, owner
- `cli/inventory.py:255/274/317/443`: field and value, key, ip, left/right
- `project/render.py:98/175/211`: the project and name columns, and the scoping table's lab and host columns (`:175`)
- `host/userland.py:1286`: capability, value
- `host/survey/report.py:38/125`: protocol and the identifier columns of `_COLUMNS`
- `check/render.py:273`: feature

Prose columns keep their defaults.

**Layering.** `tach.toml` gains an `otto.console` edge for every module that newly imports it, as `tach check --exact` demands: `otto.cli`, `otto.coverage`, `otto.project` and `otto.check`. `otto.host` already has the edge (`tach.toml:549`). Regenerate `docs/architecture/modules.md`'s graph and dependents table. D1 adds one stdlib-only foundation module, `otto._terminal`, with no otto dependencies.

### D4. Docs

- **`docs/cli/index.md`.** Its `COLUMNS` row (`:201`) sits next to the `PYDANTIC_DISABLE_PLUGINS` row (`:202`), whose shape it follows. It should say: "Unless you set a usable value (a positive whole number), the CLI sets `COLUMNS` for its run: to your terminal's launch-time width when stdout is redirected but stdin or stderr is still a terminal, else to 200 when no terminal is attached. An unusable value is dropped. Every process the CLI starts inherits it, including the tests `otto test` runs in-process. A usable value you set is always kept; to keep 80 columns in your own tests, set `COLUMNS=80`. Importing otto as a library leaves it alone. A login's remote terminal still follows your real terminal's size. A narrow width folds table cells instead of ending them in "…" (a space on a fold, or a double-width character in a narrow cell, can still be dropped), and a path otto prints on its own line is never wrapped. One exception, with forced color and `TERM=dumb`, is described under the table." The exception moves to a short paragraph under the table: when output is forced to a terminal and `TERM` is `dumb` or `unknown`, Rich renders 80 columns unless `LINES` is also set to a number. The forcing variables are `FORCE_COLOR` or `TTY_COMPATIBLE=1`, and, for otto's help and error output, `PY_COLORS` or `GITHUB_ACTIONS`. At `:118`, drop the `COLUMNS=200` advice and say that narrow widths fold.
- **`docs/use/joining.md:56-58`:** replace the advice with a link to that row.
- **`docs/contributing.md`:** add one contributor home for `print_literal`, `id_column`, the id-column rule and the width default. Check the wrap-drift note at `:~1772`.

### D5. Log files don't depend on output width

**Width.** The file-log capture console (`formatters.py:85`) stops snapshotting `CONSOLE.width`.
- `TRACEBACK_FILE_WIDTH` (`:33`, used at `:375, :395, :405`) is renamed `LOG_FILE_WIDTH`, giving one name for one decision: the log file's width.
- The capture console is built with `width=LOG_FILE_WIDTH`, and `_stylize` (`:634`) renders with `soft_wrap=True`.
- Rewrite the docstring at `:86-93`, which says log lines wrap "exactly as on screen".

**Color (Astra r4 4).** Today the capture console detects color from the attached terminal. With `rich=True`, `:618` keeps whatever ANSI it captured, so the same record is plain without a terminal and colored with one.

The traceback file renderer already decides color explicitly (`:393-400`). That decision becomes one constructor, `_file_console(rich: bool)`, called by both the capture console and the traceback renderer. Its environment policy is that **the file console reads nothing from the environment or the descriptors**:
- **Instances:** one capture console per mode, built lazily, since `_console` (`formatters.py:85`) is a module singleton while `rich` belongs to each `RichFormatter` (`:568`). A formatter takes the console for its mode.
- **Size:** `width=LOG_FILE_WIDTH` and `height=LOG_FILE_HEIGHT`, a new constant beside it. Rich returns a stored width and height before it checks for a dumb terminal (`rich/console.py:1010-1016`). Today's width-only constructor renders 80 wide in rich mode under `TERM=dumb` (Astra r5 B), so both are set.
- **Color:** `color_system="truecolor"` with `force_terminal=True` in rich mode, and `None` with `force_terminal=False` in plain mode.
- **`NO_COLOR`:** `no_color=False` explicitly, because Rich otherwise reads `NO_COLOR` (`:725-728`). A rich log file is an explicit choice, so it is colored. A user who wants no color picks a plain log file.
- **Interactivity:** `force_jupyter=False` and `force_interactive=False`, as today.

The traceback renderer gains the same height and `no_color` fix. It's one constructor, so it can't drift.

**The result:**
- A string or markup record is always one line in the file.
- A renderable record (a Table or Panel) renders at the fixed file width.
- In both plain and rich modes, the file is byte-identical however stdout was attached.

## Breaking?

**No (maintainer ruling, 2026-11-04).** Changing display format is not breaking: the functionality does not change. Exit codes, and the actions and effects of every command, are the same, so existing user code written against an older otto does not start failing. (Chris first put it as "correcting the behavior and giving another view option that happens to allow the old output format".)

Every change here is display format: width, wrapping, folding, literal rendering and log-file layout. The `ps -ww` and interactive-size fixes restore correct function. A user who wants the old width sets `COLUMNS=80`, which is a view option, not a migration. This overrides Opus r3 F3, which had classed the in-process `COLUMNS` as breaking.

Commit subject: `fix(cli): …`, with no `BREAKING CHANGE:` footer. The docs row still says how to get 80 columns (D4).

The `ps -ww` commit is a plain `fix(host):`, since it fixes an existing truncation.

## Tests

**Setup.** Width integration runs otto in a fresh subprocess, built with `tests/e2e/_otto_subprocess.py:otto_subprocess_env`:

- `stdin=DEVNULL`, with stdout and stderr both captured;
- `COLUMNS`, `LINES`, `TERMINAL_WIDTH`, `TERM`, `CI` and `BUILD_NUMBER` removed. pytest skips its trim when `CI` or `BUILD_NUMBER` is set (`_pytest/terminal.py:1567`).
- an isolated `OTTO_HOME`;
- ANSI stripped before anything is measured.

RED means the test reproduces today's bug. The others are preservation checks.

1. **Policy (unit).** The pure `_default_columns` is tested over:
   - every `COLUMNS` value listed in D1, on every descriptor configuration. An unusable value is dropped on a stdout tty too, and `0` and `²` are pinned;
   - a stdout tty: nothing set;
   - a 100-column tty on fd 2 or fd 0 alone: `"100"`;
   - a 0×0 stdout tty with a 100-column stderr tty: `"100"`;
   - no tty: `"200"`;
   - a usable value: retained.

   A missing helper would fail mechanically, so this is the policy's spec, not the bug's reproduction. Tests 2-5 reproduce the bug.
2. **Shim ordering and the console pin (fresh interpreter).** A subprocess runs `python -m otto` with no tty, with a probe that observes `otto.console.CONSOLE.width` after `console.py`'s pin has run. It reads 200. A spy at `entry()` can't distinguish set-before-import from set-after; this can.

   **RED:** with stdout faked to report 0×0 and a 100-column stderr terminal, the constructed `CONSOLE.width` is 100, not 0. That proves the shim and the pin share `usable_terminal_size`.

   Real descriptor probing (the wrapper over `os.isatty`/`os.get_terminal_size`) gets its own unit test with fake descriptors, separate from the pure policy.
3. **`--list-products` (RED).** Complete fixture rows in the 7-8-column table (`cli/listing.py:57, :92`):
   - one row that overflows at 80 and fits at 200, with no "…";
   - one row with a 250-character artifact of ASCII path characters with no spaces, which folds, and whose cell lines rejoin into the full path.
4. **`otto --help` (RED).** The exact names `--include-projects` (`cli/main.py:590`) and `--no-rich-log-file` (`:648`) appear whole.
5. **Output-dir line (RED, fails today at `logger/management.py:878`).** The plan names the actual hostless command. `--xdir` is built from valid component lengths, contains `[red]x[/red]`, and is longer than 80 characters. The `Output directory:` line is one line holding the exact path.
6. **`otto test` (RED).** A fixture suite, at verbosity below 2 (pytest skips the trim at `-vv`, `_pytest/terminal.py:1565`), has a failure message of 150 characters. The `FAILED … - msg` summary line is longer than 80 columns and holds the whole message within 200.
7. **Explicit width.**
   - **Preservation:** `COLUMNS=60` renders `--list-products` 60 wide.
   - **Regression (RED):** at 60 wide, folded identifier cells built from ASCII, space-free fixtures rejoin whole.
8. **Child inheritance.** Through the shim (`python -m otto`), with `COLUMNS` absent and no tty, a command that runs `printenv COLUMNS` on `LocalHost` returns `200`. Calling `LocalHost` directly would bypass the fix.
9. **Process scans (RED, real `ps`).** A harmless long-argv process (`sleep` under an argv longer than 200 characters, per the precedent at `tests/unit/link/test_check_live.py:476`) is found whole by each of the three scans (daemon, tunnel sweep, link sweep) under `COLUMNS=60`. A command-string assertion alone can't reproduce procps truncation.
   - **BusyBox.** The dev VM's BusyBox 1.36.1 rejects `-w` and also rejects the `etime` field (`ps: bad -o argument 'etime'`), so a complete scan fails there whatever the width fix does. Two tests:
     - **Unsupported build (the dev VM's BusyBox, first on `PATH`).** The sweeps emit `PS_FAILED`, as their contracts already say (`check_probes.py:46`, `check_live.py:107`).
     - **Supported build** (a BusyBox `ps` that accepts every field the scan needs; the plan names the build and where it comes from, e.g. one of the cached BusyBox binaries). The `||` fallback runs and the 200-character token is found whole by all three scans.
     - The daemon scan's no-result case is recorded as a pre-existing behaviour of unsupported builds. This change makes it neither better nor worse.
   - One `PS_FAILED` constant is shared, asserted by importing it from both sweeps.
   - **Interactive size.** `interactive_terminal_size` with `COLUMNS=200` set and a 100-column stdin tty returns 100. A tty whose size is unreadable gives telnet's configured `(cols, rows)` fallback, not 80×24. Under step 4, a simulated resize reaches the ssh `term_size` poll and telnet's NAWS with the new width (RED today: they read `COLUMNS`).
10. **File log (RED).** In fresh formatter construction (or a fresh process), with and without a terminal-like stdout, and in both plain and rich modes:
    - a markup record longer than `LOG_FILE_WIDTH` is one line;
    - a Table record renders at `LOG_FILE_WIDTH`;
    - the bytes are identical across attachment;
    - the bytes are identical under `TERM=dumb`, `TERM=unknown` and `NO_COLOR=1`. The rich-mode `TERM=dumb` case is RED today: it renders 80 wide.

    Changing `COLUMNS` after construction would test nothing, because the console already snapshotted its width.
11. **Literal output.** Each converted caller is driven with `[red]`, `:smile:` and a 300-character token through a narrow console, and each prints verbatim on one line.
    - **RED:** tunnel and link (rows, check headers, dry-run plans, and link's partial-scan and read-failure summaries), inventory, listing and host, because the emoji or markup cases fail today. Also output-dir, cov, schema (styled prefix plus plain path) and monitor.
    - **Preservation:** `run` and `_print_value(literal=True)`, which already set all four flags.
    - **Table cells (RED):** bracket and emoji cases render literally in an `id_column` cell, in `listing` and in `project/render.py`. Long space-free ASCII lab and host ids in the scoping table fold at width 60 with no "…" (RED: they end in "…" today).
    - **Stated limits (preservation, pins Rich):** a path with a space and a wide character, each at a narrow width, render with exactly the D3 limits.
12. **Import budget (preservation).** The existing import-budget tests stay green, with no ceiling change.

## Review focus

- **An invalid `COLUMNS`.** It is dropped on every path, then the descriptor rules apply. `COLUMNS=0` would otherwise give Rich a zero-width console.
- **Local shell over pipes.** `LocalSession` runs bash over pipes, not a pty (`session.py:1687`), so a program in it sees `COLUMNS` and no terminal. That is the same state a user's `export COLUMNS=200` produces today. The lossy consumers found, the three `ps` scans, are fixed by the shared `ps -ww` builder. An arbitrary user command run through otto may still honour `COLUMNS`, as it would under the documented export.
- **A single identifier wider than 200.** It folds rather than ending in "…". Space-free ASCII loses nothing; the D3 limits still apply to spaces and wide characters.
