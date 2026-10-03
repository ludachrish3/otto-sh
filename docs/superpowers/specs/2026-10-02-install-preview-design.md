# The install preview: `otto -n run install` shows the steps

**Date:** 2026-10-02
**Status:** approved in chat (Chris, 2026-10-02: "I like the plan-method approach. I'll take all of your recommendations"); this document is the written form for review before planning.
**Origin:** first external-user feedback, `todo/user-feedback-design-items-2026-10-02.md` §2; Chris: "For the install instruction dry run, I'd really like the stage and install steps and remote commands to be printed. Right now too little is shown, so it's hard to figure out what an installation would do prior to touching a host."

## 1. Intent

`otto --dry-run run install` prints, per repo and host, every file transfer and remote command the real install would make, in the order it would make them, built from configuration alone. What cannot be known without a host's answer is named, individually, as `not checked:`. The same holds for `uninstall` and `install-tools`.

Today the dry run stops at the CLI seam and prints only the options and the lab name (`docs/cli/dry-run.md`, "Lab-level verbs"), because `otto run` keeps the seam default. Nothing in `Product` describes its steps without doing them.

## 2. Shape

### 2.1 `Product.plan()` and `DevTool.plan()`

A new method on the two ABCs, synchronous, pure, never contacting a host:

```python
@dataclass
class ProductPlan:
    """What a product's hooks would do on one host, from configuration alone."""

    stage: list[str]
    """One line per transfer or command `stage` makes, in order."""
    install: list[str]
    """One line per transfer or command `install` makes, in order."""
    uninstall: list[str]
    """One line per transfer or command `uninstall` makes, in order."""
    unchecked: list[str]
    """One line per fact a real run reads off the host that this preview could not; each says what the missing read decides."""


class Product(ABC):
    def plan(self, host: "Host") -> ProductPlan: ...
```

Lines use the vocabulary the dry-run primitives already announce (`docs/cookbook/dry-run-contract.md`): a transfer is `PUT <local> -> <remote dir>`, a load is `LOAD <local> as <name>`, a command is the command string verbatim, and an elevated command is `sudo <command>`. A placeholder for the login home is the literal `<login home>` (`otto.host.product.LOGIN_HOME` already names it for the collision check).

The base implementation does not guess. `Product.plan` returns empty step lists and one `unchecked` line: `` `<module>.<Class>`: stage, install and uninstall are Python code and are not previewed; implement plan() to describe them ``. A code-defined product (a provider's `Product` subclass) therefore shows as an honest gap until its author implements `plan()`.

The four built-in product kinds and the three dev-tool kinds implement it:

| kind | stage | install | uninstall | unchecked |
| --- | --- | --- | --- | --- |
| `shell` | `PUT <artifact> -> <stage dir>` | `install` command, or nothing | `uninstall` command, or nothing | the login home when neither `stage_dir` nor the host's `default_dest_dir` is declared |
| `docker_image` (reference) | — | `docker pull <image>` when `pull`; `docker run -d --name <c> -v <cov>:<cov> [run_args] <image>` | `docker rm -f <c>` (the image stays in the daemon's cache: only an image a tarball loaded is removed) | `command -v docker` on the host; without `pull`, that `<image>` is present (`docker image inspect`) |
| `docker_image` (tarball) | `PUT <tarball> -> <stage dir>` | `docker load -i <stage dir>/<tarball>`; `rm -f <staged>`; `docker run -d --name <c> … <ref from docker load>` | `docker rm -f <c>`; `docker rmi <image the container ran>` | the loaded reference and the image to remove, both read from the daemon |
| `kmod` | — | with `coverage = "module"`, the `kmodcov` dev tool's install lines first, then: `PUT <.ko> -> <stage dir>`; `sudo insmod <stage dir>/<file> [params] [cov_dir=…]`; `rm -f <staged>` | `sudo rmmod <name>` | the login home (as `shell`); `sudo` is assumed when the configured user is not root — a real run measures the host's elevation method; module residency (`/proc/modules`) decides whether `rmmod` runs at all |
| `embedded` | — | `LOAD <artifact> as <name>`; one `exec` line per `call_after_load` entry | the loader's unload command, `<max_unload_rounds>` times at most | the device's answer to each load/unload round |
| dev tool `shell` | as `shell` | as `shell` | as `shell` | as `shell` |
| dev tool `kmod`, `kmodcov` | — | as `kmod`'s load | as `kmod`'s unload | residency: `install` skips a module that is already loaded |

Each cell is the exact string the kind's real hook passes to `host.put`, `host.run`, `host.exec` or `host.load`; §3 is the test that keeps it so.

### 2.2 The walk

A new library function in `otto.project` (module `plan.py`) walks the lab the way the real instruction does and collects the plans:

```python
@dataclass
class ProductPlanEntry:
    name: str                                  # the product's (or dev tool's) name
    plan: ProductPlan


@dataclass
class HostPlan:
    host_id: str
    products: list[ProductPlanEntry]           # in the order the host installs them
    gaps: list[str]                            # per-host unchecked lines, WITHOUT the host prefix


@dataclass
class RepoPlan:
    repo: str
    hosts: list[HostPlan]
    gaps: list[str]                            # repo-level unchecked lines


def plan_instruction(name: str, ctx: OttoContext, kwargs: dict[str, Any]) -> list[RepoPlan]:
    """`name` is `install`, `uninstall` or `install-tools`; same walk order, same scope, no contact."""
```

`kwargs` is the CLI's parsed flag mapping, the same one the real run builds its options from, so the walk reads `--ensure`, `--product-logs`, `--debug-logs`, `--toolchain` and `--no-dev` as the real run does. A `name` with no preview raises `ValueError`.

- Repos come from the same `_walk_order` the real run uses (dependency order; reverse for `uninstall`), filtered by the same applicability rules, so a repo the real run would skip is absent here and the walk's warnings print as they do today.
- Hosts are the repo's admissible fleet in lab order (the real run runs them concurrently; the preview lists them).
- Products are the host's owned products in declaration order (for `install-tools`, its owned dev tools). A product's own `unchecked` lines become host gaps, prefixed with the product's name (`kcov: the login home — ...`). `install` lists every product's `stage` lines first, then every product's `install` lines — the order `BaseHost.install` really uses. `uninstall` lists each product's `uninstall` lines. `install-tools` lists each dev tool's `stage` then `install`.
- Overrides are gaps, not guesses, and each replaces the steps it would have described. When the repo's `ProjectActions` subclass overrides the method that carries the instruction (decorated with `@instruction` or not: the check is method identity against the base), the repo gets one repo-level gap and every host of that repo is listed with no products: `` repo `r1`: `pkg.Actions.install` replaces the default install; its steps are not previewed ``. When the host's class overrides `install` / `stage` (for `install`), `uninstall`, or `install_dev_tools` (for `install-tools`), that host gets one host-level gap and no products: `` `pkg.Host.install` replaces the default; its steps are not previewed ``. Detection is `type(x).method is not Base.method`.
- `--ensure` adds one gap per repo, and the text is the same lab-wide sentence under every repo: `whether the lab is already installed (--ensure skips the whole install when it is; a partial install is torn down first because --recover-partial is on)`, or, with `--no-recover-partial`, `...; --no-recover-partial installs over a partial install as it stands)`. It is lab-wide because the converge runs before any repo's body, so an overriding repo is covered too.
- `install-tools --no-dev` installs no dev tool, so every host is still listed, with no products and no override gap (the host verb is never reached, so an override of it would name a step that does not run).
- `install-tools --toolchain` adds one gap to every host, under every repo: `toolchain tools: installed on this host once after every repo (--toolchain); not previewed`.
- `uninstall --product-logs` adds, per host, `product logs: each product's get_logs files and its debug_log_globs are collected before anything is removed`; it is withheld when the repo or the host overrides the verb, since the default contract it states is then not claimed. `uninstall --debug-logs` adds, per host, `debug logs: <globs> are swept once after every repo`, or `debug logs: no globs declared; nothing is swept` when the host declares none.

### 2.3 Rendering

`otto run install`, `uninstall` and `install-tools` opt in to a preview: `ProjectInstructionSpec` gains `dry_run_preview: bool` (set by `instruction(dry_run_preview=True)`), the published leaf reads it off the spec, and prints the plan before `finish_dry_run` (called with the seam default, `preview=False`) prints the standard block and exits. The keyword is public but honoured only for those three names: `instruction()` refuses `dry_run_preview=True` on any other, because `plan_instruction` can plan nothing else (`PREVIEWABLE_INSTRUCTIONS`). The renderer lives in `otto.project.render` (next to `render_status`), plain indented text (a plan is a step list, not a table):

```text
[DRY RUN] Commands and file transfers will be skipped. No device will be contacted.
repo1
  test1
    stage    agent  PUT build/agent.tar.gz -> /opt/stage
    install  agent  tar -xzf /opt/stage/agent.tar.gz -C /opt/agent && /opt/agent/install.sh
    install  kcov   PUT build/kcov.ko -> <login home>
                    sudo insmod '<login home>/kcov.ko'
                    rm -f '<login home>/kcov.ko'
  not checked:
    test1: kcov: the login home — product 'kcov' declares no stage_dir and test1 no default_dest_dir
    test1: kcov: whether kcov is resident on test1 (cat /proc/modules): uninstall runs rmmod only then
    test1: kcov: sudo is assumed because the login user is not root; a real run measures how test1 elevates
dry run: no command body was run and no device was contacted
  would run: otto run install
  options:
    InstallOptions: ensure=False, recover_partial=True
  lab: bench (3 hosts)
```

A gap that is true of the whole lab (`--ensure`'s, a host's `--toolchain` or debug-log line) is carried under every repo, so the renderer prints each fully rendered gap line once, at its first occurrence, and a repo left with none prints no `not checked:` block. A product with no steps in a phase prints nothing for that phase. A host with no step lines prints only its host line, with its gaps in the repo's `not checked:` block. `unchecked` is never empty for a host that falls through to the login home, assumes an elevation, or carries a code-defined product; a host whose plan is complete prints no `not checked:` block.

Secrets: the lines are the commands the kinds already log at INFO on a real run (`@host | <cmd>`); the preview adds no new exposure.

## 3. Honesty: the plan is what the hooks do

For every built-in kind, a unit test runs the real `stage`, `install` and `uninstall` against a recording host double — `put`, `run`, `exec`, `load` and `unload` record their arguments and answer success; `default_dest_dir` is declared so no login-home read is needed — and asserts the recorded lines equal `plan(host).stage + plan(host).install`, then `plan(host).uninstall`, line for line. The recorder renders a `run(cmd, sudo=True)` as `sudo <cmd>` and a `put(files, dest)` as `PUT <file> -> <dest>`, which is the plan's vocabulary. Where a hook branches on a host answer (docker's `command -v docker`, `/proc/modules`, the tarball reference), the recorder answers the clean path, and the test asserts that the plan's `unchecked` names that very read.

A second test per kind removes `default_dest_dir` and asserts the plan says `<login home>` and lists it as unchecked, without the recorder being asked for a home.

This is the differential that keeps the preview from drifting into a second, wrong description of install. A kind that changes a command without changing its plan goes red.

## 4. Errors and edge cases

- Under a dry run, `resolve_stage_dir` still raises `HostCommandError` if anything calls it (its `login_home` probe declines); the preview uses `stage_dir_key`, which never contacts the host. Nothing in the preview path calls `resolve_stage_dir`.
- A product whose `plan()` raises is a bug in that product; the exception surfaces as it would on a real run, naming the product and host.
- A repo with products but no hosts in scope prints the walk's existing warning and no plan.
- `otto run status` keeps the seam default (out of scope; it would list check commands).
- The real run is unchanged.

## 5. Documentation

- `docs/cli/dry-run.md`, "Lab-level verbs": `install`, `uninstall` and `install-tools` now preview; the example above; `status` and user instructions keep the seam default.
- `docs/cookbook/dry-run-contract.md`: a product kind implements `plan()`; the vocabulary; the recorder differential as the contract.
- `docs/configuration/declared-products-tools.md`: "see what an install would do" → `otto -n run install`.
- API docs for `ProductPlan`, `Product.plan`, `plan_instruction`.

## 6. Out of scope

- `status` under a dry run.
- A per-product outcome line on real runs and showing `--ensure`'s "already installed" — a separate small item, approved alongside this spec.
- `--list-products` / `--list-tools` — a separate small item (the kind and origin stamped on a product by that item are reused here for the gap lines' class names, but nothing here depends on it).
- Previewing user-defined instructions.

## 7. Work items

One plan: (1) `ProductPlan`, `Product.plan`/`DevTool.plan` defaults and the seven kind implementations with the §3 recorder tests; (2) `plan_instruction` and the override/`--ensure`/logs gaps; (3) the `instruction(dry_run_preview=)` seam, the renderer, CLI wiring and its tests; (4) docs; (5) final review, then the full gates once.
