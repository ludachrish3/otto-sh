# Contributing

## Development environment

Vagrant can be used to develop and test changes to `otto`. After
[installing Vagrant](https://developer.hashicorp.com/vagrant/install), run:

```bash
vagrant up            # start the dev VM and three test VMs
vagrant ssh           # connect to the `dev` VM (the default)
```

The `dev` VM is privately networked with three test VMs (`test`, `test2`,
`test3`). Develop and test from the `dev` VM — unit, integration, and
end-to-end tests assume connectivity to the test VMs.

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
uv run coverage html -d coverage_report
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
