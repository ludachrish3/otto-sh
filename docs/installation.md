# Installation

This page covers every supported way to install otto: quick connected installs, the
recommended project-managed setup for teams, fully air-gapped installation, and how to
read otto's documentation (and its dependencies' documentation) offline.

If you just want otto on your machine right now, the short version on the
{doc}`getting-started/index` page is enough. Come back here when you are setting up a team,
working without internet access, or managing otto alongside other Python dependencies.

## Requirements

- **Python 3.10 or later** (otto is tested on CPython 3.10 through 3.14).
- Linux, macOS, or Windows.
- Internet access to PyPI — or one of the offline sources described under
  [Air-gapped installation](#air-gapped-installation).

## Quick install

Install the latest release from PyPI into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install otto-sh
```

The distribution is named `otto-sh`; the command it installs is `otto`. To install an
exact version, pin it:

```bash
pip install otto-sh==%OTTO_VERSION%
```

Verify the result:

```bash
otto --version
```

## Recommended: manage otto as a project dependency

Otto is not just a CLI you run — it **imports your code**. The `pylib/` instruction
modules and test suites listed in `.otto/settings.toml` load into otto's own Python
process, so any Python package your instructions or tests import (`pyserial`,
`protobuf`, `requests`, …) must be installed into the same environment as otto itself.
A per-project environment, declared in one committed file, is the setup that survives
this — every teammate and CI runner gets otto *and* your extra packages in one step.

You do not need to know Python packaging to use this. The file below plays the same
role for your test tooling that `CMakeLists.txt` plays for your build: it names what
the rig needs, and one tool makes it so.

### With uv (recommended)

[uv](https://docs.astral.sh/uv/) is a single static binary — no Python needed to
install it, which matters on C/C++ build hosts. Add a `pyproject.toml` at the root of
the repository that holds your otto project:

```toml
[project]
name = "my-project-tests"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "otto-sh>=%OTTO_VERSION%",
    # Python packages your instructions/tests import go here, e.g.:
    # "pyserial>=3.5",
]
```

`name` and `version` are required by the file format but otherwise unused — your repo
does not become a Python package, and no build configuration is needed: uv treats a
`pyproject.toml` without a build backend as a *virtual* project and installs only its
dependencies.

```bash
uv sync            # creates .venv/ and installs otto + your extra packages
uv run otto --version
```

Commit `pyproject.toml` and the generated `uv.lock` (exact, reproducible versions for
every teammate); add `.venv/` to `.gitignore`. Day-to-day, either prefix commands with
`uv run` or activate the environment once per shell (`source .venv/bin/activate`) and
call `otto` directly.

### With pip and venv

pip cannot install a project's dependencies from `pyproject.toml` without turning the
repo into a buildable Python package, which a C/C++ repo should not be. The pip
equivalent is a `requirements.txt` (the same shape as a `conanfile.txt`):

```text
otto-sh==%OTTO_VERSION%
# Python packages your instructions/tests import go here, e.g.:
# pyserial>=3.5
```

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Commit `requirements.txt`; add `.venv/` to `.gitignore`, exactly as on the uv path.

Both layouts are fully supported; pick one per repository rather than mixing them.

### Choosing a Python interpreter

Use the **system Python** when it is 3.10+ — uv discovers and uses system interpreters
automatically, and pip's venv flow above uses whatever `python3` is. On air-gapped
hosts this is usually the answer: your OS package mirror already serves a modern
Python.

If the OS Python is too old, uv can install interpreters itself. Offline, point
`UV_PYTHON_INSTALL_MIRROR` at a directory of
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
archives — `file://` URLs work, so a directory of fetched archives is enough; no
server required. See
[uv's Python install docs](https://docs.astral.sh/uv/concepts/python-versions/) for
the current mechanics.

## Multi-project workspaces

Everything above sets up **one** repo. When several repos are under test at
once (`OTTO_SUT_DIRS` naming more than one), a second question appears: which
environment do they all run in?

Otto is a single process on a single interpreter. Every active repo's
instruction modules and test code import into *that* interpreter, so a per-repo
virtualenv never participates at runtime — there is no arrangement in which
each repo brings its own. Three environments exist, and it is worth naming all
three because only the middle one is otto's business:

1. **A repo's own venv.** For single-repo development. Each repo manages it
   with its own tools from its own `pyproject.toml`. otto does not touch it.
2. **The orchestration venv.** Where multi-project runs happen: one per user
   per workspace, and necessarily a **superset** — it has to satisfy the
   imports of every active repo at once.
3. **Anything else otto happens to run from.** Legal for single-repo work,
   discouraged once several repos are active, because nothing guarantees it
   holds all of their requirements.

{doc}`guide/cli/env/index` builds and maintains the second:

```console
$ export OTTO_SUT_DIRS=~/work/repo-a,~/work/repo-b
$ otto env create
created ~/.otto/134b91c0-repo-a-repo-b/env
  installed (editable): repo-b
  skipped, no pyproject.toml: repo-a
  backend: uv

Activate it with:
  source ~/.otto/134b91c0-repo-a-repo-b/env/bin/activate
```

Each repo that has a `pyproject.toml` is installed **editable**, so your
checkouts stay live; repos without one are skipped and said so, since their
`libs` reach `sys.path` at bootstrap anyway. otto installs itself too, matching
how your otto is installed — an editable checkout stays that checkout.

### The uv-workspace alternative

If your repos are already members of a
[uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/), you
have the same property by another route: one lockfile across the members, one
environment, resolved together. That needs no otto involvement at all, and it
is the better answer when you control all the repos and can make them share a
lockfile.

`otto env` exists for the common case where they are independent checkouts —
different teams, different release cadences — that were never designed to.

## Installing from a GitHub release

Each tagged release on
[GitHub Releases](https://github.com/ludachrish3/otto-sh/releases) attaches the same
`.whl` and `.tar.gz` artifacts that are published to PyPI (plus documentation
tarballs — see [Offline documentation](#offline-documentation)). Useful when you can
reach GitHub but not PyPI, or to pin an exact build:

```bash
VERSION=%OTTO_VERSION%
curl -LO "https://github.com/ludachrish3/otto-sh/releases/download/v${VERSION}/otto_sh-${VERSION}-py3-none-any.whl"
pip install "otto_sh-${VERSION}-py3-none-any.whl"
```

`pip` still needs internet access for otto's runtime dependencies. For a fully
offline install, use the downloaded wheel as the starting point for the air-gapped
flow below. In Step 1, point `pip download` at that local file instead of the package
name — `pip download ./otto_sh-%OTTO_VERSION%-py3-none-any.whl --dest ./wheels` with the
same `--python-version`, `--platform`, and `--only-binary` flags — and its dependencies
land in `./wheels` alongside it. The otto wheel itself is pure Python
(`py3-none-any`), but its dependencies are not: run one pass per target Python, under
a matching interpreter, exactly as {ref}`Step 1 <air-gap-download>` describes.

## From source (development)

Working on otto itself? `uv sync` in a clone installs otto plus its dev tools. Note
that building a *wheel* from source additionally requires Node (see `.nvmrc`) to
build the embedded web frontends first: `make web-install && make web && uv build
--wheel`. For installing released versions this is never necessary — the published
wheels already embed the frontends.

## Air-gapped installation

Otto is designed for air-gapped networks. The flow has four parts: **download**
everything on an internet-connected machine, **transfer** it across the gap, **serve**
it to the isolated hosts, and **install** from that source.

### Getting uv into the air gap

OS package repositories generally do not carry uv (or carry versions far too old), so
bring it across the gap explicitly, either way:

- **Standalone binary:** download the archive for your platform (plus its published
  `sha256` checksum) from [uv's releases](https://github.com/astral-sh/uv/releases),
  transfer it like the wheels below, and unpack it onto `PATH`. The `curl | sh`
  installer is only a fetcher for these same artifacts, so nothing is lost offline.
- **As a wheel:** uv is also published on PyPI, so `pip download uv` alongside otto
  (next step) places it in the same wheel directory, installable with the system pip.

Pin whatever version you transfer — `uv self update` cannot work offline. The current
artifact list lives in
[uv's installation docs](https://docs.astral.sh/uv/getting-started/installation/).

(air-gap-download)=

### Step 1: Download all wheels (internet-connected machine)

Fetch otto and every runtime dependency straight from PyPI as wheel files. No clone
of the otto repository and no Node toolchain is needed — the published wheel already
embeds the web frontends.

Two things vary per Python version, and both have to be right or the bundle is
incomplete: the **binary wheels**, which several dependencies ship one-per-interpreter,
and the **dependency set itself**, which environment markers change from version to
version.

```{warning}
**Run each download under an interpreter whose minor version matches the target.**
`--python-version` chooses which *wheel files* are compatible and enforces
`Requires-Python` — but it does **not** change how environment markers are evaluated.
Pip resolves `; python_version < "3.11"` against the interpreter that is *running*, so
downloading for 3.10 while standing on 3.13 silently omits every marker-conditional
dependency (`exceptiongroup`, `backports-asyncio-runner`, and the older
`markdown-it-py` fork). Nothing warns you: the download succeeds, the archive
transfers, and the failure surfaces at `pip install` time *inside the gap*, where it
is most expensive to fix.
```

Loop over every Python version your air-gapped hosts run, collecting all of it into a
single directory:

```bash
# `--no-project` matters: without it, `uv run --python` rebuilds the current
# directory's .venv at each version. `--with pip` supplies pip to the ephemeral env.
for PYVER in 3.10 3.11 3.12 3.13 3.14; do
    uv run --no-project --python "$PYVER" --with pip -- python -m pip download otto-sh \
        --dest ./wheels \
        --python-version "$PYVER" \
        --platform manylinux2014_x86_64 \
        --only-binary :all:
done
```

Trim the list to the versions you actually deploy — every extra version costs only the
handful of wheels unique to it. One shared `./wheels` directory is correct and
intended: wheel filenames encode their own compatibility, identical files collapse, and
`--find-links` serves the whole set to every interpreter. For the five versions above
otto resolves to roughly 55 wheels, of which `pydantic-core`, `cffi`, and `tomli`
contribute five each.

Without uv on the staging machine, run the same `pip download` under each interpreter
directly (`python3.12 -m pip download …`); the rule is the interpreter, not the tool.

To pin an exact otto version, use `pip download otto-sh==%OTTO_VERSION%`. To bring uv
itself across, add `pip download uv --dest ./wheels` with the same flags — its wheels
are platform-native.

```{note}
**Platform-specific** and **Python-version-specific** are separate axes, and otto's
dependencies sit on both. `--platform` must match the target host's architecture;
`--python-version` (plus the matching interpreter, above) must match its Python. The
{ref}`wheel-matrix table <native-extension-dependencies>` lists which dependency is
which. Common platform tags:

- `manylinux2014_x86_64` — most Linux x86-64 systems
- `manylinux2014_aarch64` — Linux ARM64
- `macosx_11_0_arm64` — macOS Apple Silicon
- `win_amd64` — Windows 64-bit

Hosts on more than one architecture need one pass per `--platform` as well, into the
same directory.
```

**uv-flavored variant.** uv has no `pip download` equivalent, so the lockfile-faithful
route goes through an export. From a project repo with the recommended
`pyproject.toml` + `uv.lock`:

```bash
uv export --no-dev --no-hashes > requirements.txt
for PYVER in 3.10 3.11 3.12 3.13 3.14; do
    uv run --no-project --python "$PYVER" --with pip -- python -m pip download \
        -r requirements.txt --dest ./wheels \
        --python-version "$PYVER" --platform manylinux2014_x86_64 --only-binary :all:
done
```

This downloads the *exact* locked versions of otto and your extra packages. The export
is written once and is version-agnostic — it keeps each dependency's markers — so the
per-interpreter rule applies to the `pip download` half exactly as it does above.

### Step 2: Transfer across the gap

Any of these work; the checksum manifest is recommended regardless of medium:

```bash
# Pack and fingerprint on the connected side:
tar czf wheels.tar.gz wheels/
sha256sum wheels.tar.gz > wheels.tar.gz.sha256

# ...move via removable media, or over an approved network path:
scp wheels.tar.gz wheels.tar.gz.sha256 bastion:/staging/
ssh bastion scp /staging/wheels.tar.gz /staging/wheels.tar.gz.sha256 target:/opt/

# Verify on the far side before unpacking:
sha256sum -c wheels.tar.gz.sha256
tar xzf wheels.tar.gz
```

`rsync` over an approved path works the same way for repeated syncs.

### Step 3: Serve the wheels inside the air gap

Three tiers, smallest first — pick the first one that fits:

**Tier 1 — a plain directory.** No infrastructure; point installers at the directory:

```bash
pip install --no-index --find-links ./wheels/ otto-sh
# or
uv pip install --no-index --find-links ./wheels/ otto-sh
```

**Tier 2 — your own package index.** For a team, serve the same directory over HTTP
with [pypiserver](https://pypi.org/project/pypiserver/) (pure Python — install it from
the wheels directory itself after adding it to the download list in Step 1):

```bash
pip install --no-index --find-links ./wheels/ pypiserver
pypi-server run -p 8080 ./wheels/
```

Point **pip** at it machine-wide:

```bash
pip config set global.index-url http://pypi.internal:8080/simple/
pip config set global.trusted-host pypi.internal    # plain HTTP needs this
```

Point **uv** at it in the project's `pyproject.toml` — committed once, so every
teammate's `uv sync` works with no per-machine setup:

```toml
[[tool.uv.index]]
url = "http://pypi.internal:8080/simple/"
default = true

[tool.uv]
# Only needed for plain HTTP; drop when the index serves HTTPS.
allow-insecure-host = ["pypi.internal"]
```

**Tier 3 — an artifact manager you already run.** Artifactory, Nexus, and GitLab's
package registry all speak the same PyPI protocol. If your organization already runs
one, upload the wheels there and use the config above with its URL — skip Tier 2.

### Step 4: Install on the air-gapped host

With a project checkout (recommended setup from
[above](#recommended-manage-otto-as-a-project-dependency)) and Tier 2/3 configured,
installation is just:

```bash
uv sync
```

With Tier 1, or without a project checkout:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --no-index --find-links ./wheels/ otto-sh
otto --version
```

## Offline documentation

Otto's documentation lives at
[otto-sh.readthedocs.io](https://otto-sh.readthedocs.io), and its pages link into the
documentation of Python itself and of otto's dependencies. Every GitHub release
attaches two documentation tarballs so all of it can be read inside an air gap:

- `otto-docs-%OTTO_VERSION%.tar.gz` — the standard HTML build. External links point at
  the real public URLs. Use with **Option A** below.
- `otto-docs-offline-%OTTO_VERSION%.tar.gz` — the same build with external
  documentation links rewritten to relative paths. Use with **Option B**.

### Option A (recommended): mirror the real URLs

If your network can resolve public hostnames to internal servers — split-horizon DNS
pointing each docs hostname at an internal web server, with TLS certificates from your
organization's internal CA (documentation links are `https://`, so a plain-HTTP mirror
will not be followed) — serve each site at its true hostname. Every link then works
exactly as it does on the internet, in otto's docs and between the dependency docs
themselves. A wildcard DNS zone for `*.readthedocs.io` covers four of the hostnames
at once.

| Hostname | Content source |
| -------- | -------------- |
| `otto-sh.readthedocs.io` | `otto-docs-%OTTO_VERSION%.tar.gz` from the GitHub release |
| `docs.python.org` | [Official HTML archive](https://docs.python.org/3/archives/) |
| `docs.pytest.org` | [RTD htmlzip](https://docs.pytest.org/_/downloads/en/stable/htmlzip/) |
| `rich.readthedocs.io` | [RTD htmlzip](https://rich.readthedocs.io/_/downloads/en/stable/htmlzip/) |
| `asyncssh.readthedocs.io` | No official archive — `wget` mirror (below) |
| `telnetlib3.readthedocs.io` | No official archive — `wget` mirror (below) |
| `typer.tiangolo.com` | No official archive — `wget` mirror, or accept dead links |
| `docs.pydantic.dev` | No official archive — `wget` mirror, or accept dead links |

For the sites without an official downloadable archive, capture a static mirror on the
connected side:

```bash
wget --mirror --page-requisites --adjust-extension --no-parent \
    https://asyncssh.readthedocs.io/en/stable/
```

The last four rows are optional — otto's docs link into them far less often than into
Python, pytest, and rich. Unmirrored hostnames simply leave those links unresolvable.

### Option B: relocatable bundle (no DNS control needed)

Unpack `otto-docs-offline-%OTTO_VERSION%.tar.gz` and the dependency docs as sibling
directories:

```text
docs-bundle/
├── otto/          ← otto-docs-offline-%OTTO_VERSION%.tar.gz
├── python/        ← docs.python.org HTML archive
├── pytest/        ← pytest htmlzip
├── rich/          ← rich htmlzip
├── asyncssh/      ← wget mirror (optional)
└── telnetlib3/    ← wget mirror (optional)
```

Each sibling directory must start at the *content* root. The python.org archive and the
RTD htmlzips unpack into versioned directories — rename those to `python/`, `pytest/`,
and `rich/`. A `wget` mirror instead writes a host tree
(`asyncssh.readthedocs.io/en/stable/…`), so add `-nH --cut-dirs=2 -P asyncssh` to the
command above to put the pages under the site's version path (`/en/stable/`, or
`/en/latest/` for telnetlib3) straight into `asyncssh/`.

Every rewritten link is a depth-correct relative path, so the bundle works from any
location with no configuration: open `docs-bundle/otto/index.html` directly
(`file://`), or serve the tree from any static server at any path:

```bash
python3 -m http.server --directory docs-bundle 8000
```

Links into typer and pydantic docs are **not** rewritten and remain live URLs: neither
publishes an offline archive, and unlike the Read the Docs sites their mkdocs layouts do
not mirror cleanly with `wget`. Sibling directories you leave unpopulated yield broken
links only for those pages.

## Dependencies reference

### Direct runtime dependencies

Otto's direct runtime dependencies (declared in `pyproject.toml` under
`[project] dependencies`):

| Package | Min version | Purpose |
| ------- | ----------- | ------- |
| `aioftp` | 0.27.2 | Async FTP client for file transfers |
| `aiosqlite` | 0.21.0 | Async SQLite for persisting monitor metrics |
| `asyncssh` | 2.22.0 | SSH connections to remote hosts |
| `fastapi` | 0.135.1 | Monitor dashboard web server |
| `packaging` | 24.0 | Requirement/marker/specifier evaluation for the dependency preflight |
| `pydantic` | 2.6 | Boundary validation models for lab JSON, host records, and settings |
| `pydantic-settings` | 2.2 | Environment-variable settings (`OTTO_*`) |
| `pynetbox` | 7.4.0 | NetBox REST client for the `netbox` inventory backend |
| `pysnmp` | 7.1.0 | Async SNMP manager for separate-channel host monitoring |
| `pytest` | 9.1.1 | Test runner; otto imports user test files at runtime |
| `pytest-asyncio` | 1.4.0 | Async test support for pytest |
| `pytest-timeout` | 2.3.1 | Per-test timeouts for `otto test` (`@pytest.mark.timeout`) |
| `pyyaml` | 6.0.3 | Parses rendered compose YAML to collect `env_file:` sidecar references |
| `requests` | 2.20.0 | HTTP adapter mounted directly by the `netbox` inventory backend to bound each request |
| `rich` | 15.0.0 | Terminal formatting, panels, and tables |
| `sse-starlette` | 3.3.3 | Server-sent events for live dashboard updates |
| `starlette` | 0.52.1 | ASGI request types used directly by the monitor server |
| `telnetlib3` | 4.0.1 | Async Telnet client for telnet-based hosts |
| `tomli` | 2.4.0 | TOML parser for `.otto/settings.toml` |
| `typer` | 0.26 | CLI framework (builds `otto run`, `otto test`, etc.) |
| `typing-extensions` | 4.12.0 | Backport of `typing.override` (PEP 698) for Python < 3.12 |
| `uvicorn` | 0.42.0 | ASGI server for the monitor dashboard |

(native-extension-dependencies)=

### Native-extension dependencies

The direct dependencies above pull in further packages of their own — about 50 in a
complete Linux runtime install. Most are pure Python and ship a single
`py3-none-any` wheel that works everywhere. These six do not: they carry **native
(C/Rust) extensions**, so their wheels are platform-specific, and the "Wheel matrix"
column says whether they are *also* Python-version-specific.

| Package | Pulled in by | Wheel matrix | Notes |
| ------- | ------------ | ------------ | ----- |
| `cffi` | cryptography | per-version | C FFI bindings |
| `charset-normalizer` | requests (direct) | per-version + pure fallback | HTTP body charset detection |
| `cryptography` | asyncssh | abi3 | SSH encryption; links against OpenSSL |
| `pydantic-core` | pydantic | per-version | Rust-based data validation |
| `pyyaml` | otto (direct) | per-version | LibYAML-backed C parser; no pure wheel is published |
| `tomli` | otto (direct) | per-version + pure fallback | TOML parsing |

Reading the matrix column:

`abi3`
: Built against CPython's stable ABI, so **one wheel covers a range** of versions —
  `cryptography`'s `cp39-abi3` wheel installs on 3.9 and every later interpreter.

`per-version`
: **One wheel per CPython minor.** `pydantic-core` publishes `cp310`, `cp311`,
  `cp312`, `cp313`, and `cp314` builds; the 3.12 wheel will not install on 3.13.
  Note that `pydantic` itself is pure Python — it is this compiled core that has to
  match the interpreter.

`per-version + pure fallback`
: Per-version binaries for speed, plus a `py3-none-any` wheel that any interpreter can
  fall back to. Missing the binary costs performance, not correctness.

This is why {ref}`Step 1 <air-gap-download>` loops over interpreters instead of
downloading once: a bundle built only for 3.12 carries exactly one of
`pydantic-core`'s five wheels.

```{note}
This table is gated, not hand-maintained on trust:
`scripts/check_docs_wheel_matrix.py` re-derives the package set, each matrix label,
and the per-version wheel coverage from the committed `uv.lock` on every docs build,
and fails the build on drift. A dependency that starts or stops shipping binary
wheels breaks the docs build rather than the next air-gapped install.
```

Dev dependencies (pytest plugins for otto's own tests, sphinx, ruff, etc.) are
declared in the `[dependency-groups] dev` section of `pyproject.toml` and are **not**
included in the otto wheel.  They are only installed by `uv sync` in a clone of the
otto repository, for development purposes.

## Air-gapped considerations

Beyond installation, keep the following in mind when running otto without internet
access:

Monitor dashboard assets
: The monitor's web dashboard bundles all of its static assets inside the otto
  wheel.  **No CDN or external network access is needed** to serve the dashboard.

SSH host key verification
: Otto disables SSH host-key verification by default: a default-constructed
  `SshOptions()` passes `known_hosts=None` to asyncssh, so nothing has to be seeded
  before a first connection to a new host. If your security requirements call for
  verification, set `known_hosts` on the host's SSH options to a known-hosts file
  (asyncssh expands `~` itself, so `~/.ssh/known_hosts` works) — only then does that
  file need to be populated on the air-gapped host in advance. Tunnel legs to SSH
  *hop* hosts are always unverified; a hopped host's own connection still honors its
  `known_hosts`. See [Connection options](library/connection-options.md).

Log retention
: Otto stores logs and artifacts under the `--xdir` directory.  On isolated systems
  with limited disk, use the `--log-days` setting (default: 30 days) to control
  automatic cleanup.

Python availability
: Ensure the air-gapped host has Python 3.10+ installed — see
  [Choosing a Python interpreter](#choosing-a-python-interpreter).
