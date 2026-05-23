# Contributing

## Development environment

Vagrant can be used to develop and test changes to `otto`. After
[installing Vagrant](https://developer.hashicorp.com/vagrant/install), run
`vagrant up` from the repository root.

### The VMs

The `Vagrantfile` defines five machines on a private `10.10.200.0/24`
network:

| VM       | IP              | `autostart` | Purpose                                                   |
|----------|-----------------|-------------|-----------------------------------------------------------|
| `dev`    | `10.10.200.100` | yes         | Development VM - develop and run the test suite here      |
| `test1`  | `10.10.200.11`  | no          | SSH + SCP test host                                       |
| `test2`  | `10.10.200.12`  | no          | Telnet + netcat test host                                 |
| `test3`  | `10.10.200.13`  | no          | Docker-capable test host                                  |
| `zephyr` | `10.10.200.14`  | no          | Zephyr RTOS test bed (3 QEMU instances) + SSH hop to them |

The `zephyr` VM hosts **three** Zephyr QEMU instances concurrently, one per
filesystem config. They share the SSH hop (`10.10.200.14`) but live on the
QEMU-internal `192.0.2.0/24` net:

| Zephyr instance | IP          | Filesystem                      | systemd unit                         |
|-----------------|-------------|---------------------------------|--------------------------------------|
| `sprout`        | `192.0.2.1` | FAT on a RAM disk               | `zephyr-qemu-v3_7_fat_ram.service`   |
| `sprout_lfs`    | `192.0.2.3` | LittleFS on the flash simulator | `zephyr-qemu-v3_7_lfs.service`       |
| `sprout_no_fs`  | `192.0.2.5` | (none — no `fs` shell)          | `zephyr-qemu-v3_7_no_fs.service`     |

See `tests/firmware/zephyr/README.md` in the repo for the per-config
overlay layout.

Only `dev` starts on a bare `vagrant up` (the rest are `autostart: false`).
Bring the others up explicitly when you need them:

```bash
vagrant up                              # dev VM only
vagrant up test1 test2 test3 zephyr     # the test hosts
vagrant ssh                             # connect to dev (the default)
```

Develop and test from the `dev` VM — integration and end-to-end tests
assume connectivity to the test hosts.

### Files required at provision time

Most provisioning is self-contained (inline shell in the `Vagrantfile`),
but the `zephyr` VM's build step reads files from the repository checkout
through the `/vagrant` synced folder. These **must be present in your local
checkout before `vagrant up zephyr`** (a fresh `git clone` has them all —
this matters mainly if you iterate on overlays from outside the host
checkout; see the next subsection):

| File                                                                | Used for                                                          |
|---------------------------------------------------------------------|-------------------------------------------------------------------|
| `Vagrantfile`                                                       | The provisioning definition itself                                |
| `tests/firmware/zephyr/common/otto-overlay.conf`                    | Shared Kconfig overlay (shell, networking, runtime stats)         |
| `tests/firmware/zephyr/configs/v3_7_fat_ram/overlay.conf`           | FAT-on-RAM-disk Kconfig delta                                     |
| `tests/firmware/zephyr/configs/v3_7_fat_ram/app.overlay`            | FAT-on-RAM-disk devicetree (RAM disk node)                        |
| `tests/firmware/zephyr/configs/v3_7_lfs/overlay.conf`               | LittleFS Kconfig delta                                            |
| `tests/firmware/zephyr/configs/v3_7_lfs/app.overlay`                | LittleFS devicetree (flash simulator + fstab automount)           |
| `tests/firmware/zephyr/configs/v3_7_no_fs/overlay.conf`             | no-filesystem Kconfig delta (graceful-degradation target)         |

The `zephyr` VM builds an **unmodified** Zephyr shell sample
(`samples/subsys/shell/shell_module`) three times — once per filesystem
config — layering `common/otto-overlay.conf` plus the per-config
`overlay.conf` via `-DEXTRA_CONF_FILE="a;b"`, with the matching
`app.overlay` via `-DEXTRA_DTC_OVERLAY_FILE=` (the `no_fs` config omits
the DT overlay). otto ships no firmware code — the overlays only flip
standard Zephyr Kconfig options (telnet shell backend, networking,
runtime stats, filesystem), the same way a Unix host needs an
`sshd_config`. If any overlay is missing, the `west build` provisioning
step fails with a missing-file error.

The lab definition `tests/lab_data/tech1/hosts.json` is read by otto at
**runtime** (not provision time); it must be present to target the test
hosts but is not needed for `vagrant up` itself.

### Iterating on overlays from outside the host checkout

`vagrant up zephyr` (and `vagrant provision zephyr`) run on your **host**
machine, and the `zephyr` VM's `/vagrant` synced folder maps to the
**host's** otto-sh checkout. If you edit firmware overlays from anywhere
other than the host checkout — for example, from inside the `dev` VM —
those edits do **not** reach the `zephyr` VM until they land in the host
checkout.

The sync mechanism is whatever your workflow already uses for source
control. With a shared remote: commit + push from where you edited and
pull on the host. Without one: any file-copy mechanism (`scp`, `rsync`,
manual copy) that puts the changed `tests/firmware/zephyr/...` and
`Vagrantfile` files into the host checkout will do. Either way:

```bash
# on the host, in the otto-sh checkout
vagrant provision zephyr                                # rebuild all 3 Zephyr images
vagrant ssh zephyr -c 'sudo systemctl restart zephyr-qemu-v3_7_fat_ram.service'
vagrant ssh zephyr -c 'sudo systemctl restart zephyr-qemu-v3_7_lfs.service'
vagrant ssh zephyr -c 'sudo systemctl restart zephyr-qemu-v3_7_no_fs.service'
```

`west build` is incremental within each per-config build dir, so
re-provision after an overlay edit is fast on the second run.

For a tighter iteration loop on a single config from the host:

```bash
vagrant ssh zephyr
source ~/zephyr-venv/bin/activate
source ~/zephyrproject/zephyr/zephyr-env.sh
west build -d ~/build/v3_7_lfs     # incremental rebuild of just that config
sudo systemctl restart zephyr-qemu-v3_7_lfs.service
```

A fresh `git clone` on the host has all the files above by default — this
workflow only matters when you are iterating on overlays from a different
checkout.

## Development setup

Otto uses [uv](https://docs.astral.sh/uv/) for dependency management. Once
the repo is cloned in the dev VM:

```bash
make dev              # runs `uv sync` and sets up git hooks
source project_env    # optional: sets up usage with test repos
uv run pytest         # run the test suite
```

`make dev` places `otto` at `otto-sh/.venv/bin/otto`.

## Branching and commits

All work branches off `main`. `main` is protected — direct pushes are
rejected, so every change lands via a pull request.

```bash
git checkout main
git pull --rebase
git checkout -b <type>/<short-description>
```

Use one of these branch prefixes:

| Prefix     | Use for                              |
|------------|--------------------------------------|
| `feature/` | New functionality                    |
| `fix/`     | Bug fixes                            |
| `chore/`   | Tooling, deps, CI, refactors         |
| `docs/`    | Documentation only                   |

Examples: `feature/add-ssh-retry-logic`, `fix/gcda-parse-error-on-empty-file`.

Keep commits focused — one logical concern per commit. Use
[Conventional Commit](https://www.conventionalcommits.org/) prefixes in
the message subject:

| Prefix      | Meaning                                     |
|-------------|---------------------------------------------|
| `feat:`     | New feature                                 |
| `fix:`      | Bug fix                                     |
| `chore:`    | No production code change                   |
| `docs:`     | Documentation only                          |
| `test:`     | Tests only                                  |
| `refactor:` | Code restructuring, no behavior change      |
| `ci:`       | CI/CD configuration                         |

Before pushing, run `make all` locally — it mirrors CI
(`clean-dist → typecheck → coverage → docs → build`).

## Keeping your branch up to date

Always rebase, never merge, so history stays linear:

```bash
git checkout main
git pull --rebase

git checkout <your-branch>
git rebase main
```

Resolve conflicts commit by commit during the rebase
(`git add <file>` then `git rebase --continue`, or `git rebase --abort`
to start over). Push with `--force-with-lease` — it refuses to clobber
upstream commits you haven't seen:

```bash
git push origin <your-branch> --force-with-lease
```

## Pull requests

PRs target `main`. Link the related issue in the body using a closing
keyword so it auto-closes on merge:

```text
Closes #42
```

Open as a draft while work is in progress, then mark Ready for review
once `make all` is green:

```bash
gh pr create --draft --base main --title "feat: add SSH retry logic"
```

A maintainer will **squash and merge** once approved — you do not need
to squash yourself. After merge, delete the branch and pull `main`:

```bash
git checkout main
git pull --rebase
git branch -d <your-branch>
```

### PR checklist

- [ ] Commits follow the conventional commit format
- [ ] `make all` passes locally
- [ ] Branch is rebased on the latest `main`
- [ ] Related issue linked (`Closes #N`)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` for user-facing changes
- [ ] No manual edits to the version string

## Version management

Versioning is owned by maintainers and driven by
[`bump-my-version`](https://github.com/callowayproject/bump-my-version).
Do not hand-edit the `version` field in `pyproject.toml` — your PR will
be asked to revert the change.

For user-facing changes, add an entry to `CHANGELOG.md` under the
`## [Unreleased]` section. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) (`Added`,
`Changed`, `Fixed`, `Removed`). When a release is cut, the maintainer
runs `bump-my-version` to promote `[Unreleased]` to a numbered version
and update `pyproject.toml` in the same commit.

## Running tests

```bash
make test                     # run all tests
make test TESTS=test_host     # filter by keyword
make coverage                 # run tests and enforce coverage threshold
```

### Cross-version testing with nox

`make ci` runs the unit suite under one Python (whichever uv resolves by
default). To exercise the full matrix the way CI does — Python 3.10
through 3.14 — use `nox`:

```bash
make nox                       # full matrix: all Pythons + lint + typecheck + docs
uv run nox -s tests-3.12       # just one Python's tests
uv run nox -s tests-3.14 -- -k test_session    # forward args to pytest
uv run nox --list              # show every available session
```

Nox sessions are defined in `noxfile.py` and use uv as the venv backend
via `nox-uv`, so each session reuses the same lockfile-resolved deps as
local development. The toolchains themselves come from
`uv python install 3.10 3.11 3.12 3.13 3.14` (run once per machine).
`make all` is unchanged and remains the dev-VM contract — `nox` covers
the cross-Python axis that `make all` doesn't.

## Documentation

### Building docs

```bash
make docs          # HTML output + doctests
make doctest       # run doctests only (from .md/.rst files)
make docs-html     # HTML only (warnings are errors)
```

pytest also runs doctests from Python source files automatically via
`--doctest-modules`.

### Documentation layout

```text
docs/
├── overview.md          # Project overview
├── getting-started.md   # Installation and first steps
├── guide/               # Narrative user guides (Markdown)
├── cookbook/             # Recipes with doctest examples (Markdown)
├── contributing.md      # This page
└── api/                 # API reference (reStructuredText, auto-generated)
```

Narrative guides go in `guide/` or as top-level Markdown files.  API
reference pages live in `api/` and use `.. automodule::` directives to
pull documentation from docstrings.

### Docstring rules of thumb

- **New public function or class?** Add a Google-style docstring with a
  one-line summary, Args/Returns sections, and a `>>>` example if the
  function is pure and deterministic.
- **Changed a function's signature or behavior?** Update its docstring and
  any `>>>` examples to match.  Stale doctests will fail in CI.
- **Async, I/O, or nondeterministic code?** Write a docstring with `>>>`
  examples.  Also test these in `tests/unit/`.
- **Keep doctests minimal.** 2-4 lines showing the happy path is enough.
  Edge cases belong in unit tests.

### Doctest quick reference

In Python source files (collected by pytest):

```python
def add(a: int, b: int) -> int:
    """Add two numbers.

    >>> add(1, 2)
    3
    """
    return a + b
```

In Markdown documentation files (collected by Sphinx):

````markdown
```{doctest}
>>> from otto.utils import Status
>>> Status.Success
<Status.Success: 0>
```
````

Common imports (`Status`, `CommandStatus`, `LocalHost`) are pre-loaded in
doc-file doctests via `doctest_global_setup` in `docs/conf.py`.

## Coverage reports

### From pytest

```bash
make coverage
```

### Manually

```bash
uv run coverage run --source=otto --context=manual -m otto <subcommand> [args]
uv run coverage html  # writes to reports/coverage/html (see .coveragerc)
```

## Type checking

`ty` (from astral) is being trialled as a replacement for pyright. The
project keeps a `[tool.pyright]` block for Pylance/VS Code, while
`[tool.ty]` drives the CLI checker and the optional ty language server.

```bash
make typecheck     # run ty check against src/ with all rules at error
```

Config lives under `[tool.ty.*]` in `pyproject.toml`. ty is pinned to an
exact version (`ty==0.0.31`) because its 0.0.x releases allow breaking
diagnostic changes between any two versions — floating the pin would cause
unannounced CI churn.

Work the count down with per-line `# ty: ignore[rule-name]` suppressions
(justified in the surrounding context) or by fixing the underlying type.
Do not silence rules globally in `[tool.ty.rules]` — an individual
demotion there needs to be defensible in review.

Use `uv run ty explain rule <name>` for the full rationale and examples
behind any diagnostic.

For VS Code: install the "Astral ty" extension (Ctrl+Shift+P → Extensions
→ search "ty"). It reads `[tool.ty]` from `pyproject.toml`, so LSP
diagnostics and `make typecheck` stay in sync.

## Performance reports

```bash
uv run pyinstrument -o profile.txt -m otto <subcommand> [args]
```

## AI-Assisted Contributions

AI coding tools (e.g., GitHub Copilot, Claude, Cursor) are permitted for
contributions to otto. If your PR contains AI-assisted code, please note it
in the PR description. Regardless of how code was generated, contributors
are responsible for understanding, testing, and owning what they submit.
