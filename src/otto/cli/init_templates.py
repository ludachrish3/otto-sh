"""Templates ``otto init`` scaffolds into a new repo.

String constants only — all scaffolding logic stays in :mod:`otto.cli.init`.
``SETTINGS_TEMPLATE`` follows the sshd_config comment convention: prose
comments are ``# text`` (hash-space), commented-out TOML is ``#key = value``
(no space), and the ``#:schema`` editor directive is neither. The drift tests
in ``tests/unit/cli/test_init_templates.py`` rely on that convention to
uncomment and validate the whole surface against ``SettingsModel``.
"""

from typing import Any

SETTINGS_TEMPLATE = """\
#:schema ./schemas/settings.schema.json
# {name} — otto repo settings. Reference: docs/guide/configuration/settings.md.
# Lines starting "#key" or "#[section]" are optional settings: remove the
# leading "#" to enable them. Your editor autocompletes every field from the
# schema line above (regenerate with `otto schema export`).

name = "{name}"
version = "{version}"

# Where otto looks for things. Relative paths resolve against this repo's
# root (the directory holding .otto/); "~" expands to your home directory.
tests = ["tests"]     # defines where test discovery happens
libs = ["pylib"]      # added to sys.path at startup
init = ["{init_module}"]           # modules imported at startup (register instructions)

# Host-data sources, read in order — later sources override earlier ones per
# host record (a warning names both). The built-in "json" backend reads
# lab.json from directories, or a .json file directly; custom backends are
# selected by registered name with their kwargs inline. See
# docs/guide/configuration/host-sources.md.
[[lab.sources]]
backend = "json"
paths = ["lab_data"]

# --- [dependencies] — other OTTO_SUT_DIRS projects this repo depends on ------
# Entries are "name" or "name <op> X.Y.Z[, <op> X.Y.Z ...]"; names match other
# repos' `name` fields (case/punctuation-insensitive). Required deps must be
# present and compatible or this repo fails to load; optional deps warn when
# present but incompatible.
#[dependencies]
#required = ["other-project >= 1.0"]
#optional = ["nice-to-have-project"]

# --- [project] — which labs and hosts this project targets -------------------
# Optional until this repo registers a product/dev-tool provider; REQUIRED
# after (bootstrap fails loud without it). Regexes are FULL matches: "bench"
# does not match "bench-2"; write "bench.*" to prefix-match.
#[project]
#lab_patterns = ["example_lab"]   # labs this project applies to
#host_patterns = [".*"]           # hosts of interest within those labs

# --- [[products]] / [[dev_tools]] — attach things to hosts without a provider -
# Same schema, different seam. See
# docs/guide/configuration/declared-products-tools.md.
#[[products]]
#name = "firmware"
#kind = "file"
#artifact = "build/fw.bin"
#match = {{ "metadata.hw_version" = "rev2" }}
#[[dev_tools]]
#name = "trace-probe"
#kind = "file"
#artifact = "tools/probe.sh"

# --- [logging.levels] — the per-library noise floor --------------------------
# Nothing needs registering: otto configures the root logger, so every logger
# in the process is already captured. This table only says what ENTERS otto's
# sinks per logger; --log-level still decides what shows.
#[logging.levels]
#asyncssh = "DEBUG"               # un-quiet one of otto's defaults
#noisy_vendor_sdk = "ERROR"       # quiet a library of your own

# --- [host_preferences."<selector>"] — scoped term/transfer preferences ------
# The quoted selector is a regex fullmatched against host ids; ".*" = all.
# Ordered lists are intersected with each host's own menu at build time.
#[host_preferences.".*"]
#term = ["ssh", "telnet"]
#transfer = ["scp", "sftp"]
#impairer = ["tc"]
# Option tables may also sit under a selector: ssh_options, telnet_options,
# sftp_options, scp_options, ftp_options, nc_options, and userland_options
# (facts about the device, not a protocol). Their fields are not listed
# here — the schema autocompletes them. Example:
#[host_preferences.".*".ssh_options]
#port = 22

# --- [os_profiles.<name>] — named OS-profile bundles for lab.json hosts ------
# `base` is the host class the profile builds on; any host field may follow
# as a default applied to every host that selects this profile.
#[os_profiles.my-os]
#base = "unix"
#valid_terms = ["ssh"]

# --- [reservations] — reservation gate; see docs/guide/cli/reservation/ ------
# Backend-specific settings live in [reservations.<backend>].
#[reservations]
#backend = "none"
#url = ""

# --- [inventory] + [creds] — where a referenced host's facts come from ------
# A lab.json host that says "inventory": "<key>" gets the fields listed in
# `supplies` from the inventory record under that key, and its creds from the
# creds store under the same key. Creds layer by login: lab.json over the
# inventory record over the creds store, field by field, and lab.json's order
# is the login order. The usual home for both tables is ~/.otto/settings.toml
# (declared once per user); a table here overrides it for this repo. Grow
# `supplies` as inventory.json takes over more fields.
# See docs/guide/configuration/inventory.md.
[inventory]
backend = "json"
path = "lab_data/inventory.json"
supplies = ["ip"]

# [creds] is optional: without it, creds come from inventory records and
# lab.json alone. Other backends: netbox for [inventory] (below), and any
# registered name for either table.
[creds]
backend = "json"
path = "lab_data/creds.json"

# A NetBox inventory instead of the json file — the token never sits in a file:
#   backend = "netbox"
#   url = "https://netbox.example"
#   token_env = "NETBOX_TOKEN"
#   filter = {{ site = "lab-a", status = "active" }}
#   cache_ttl = "24h"

# --- [coverage] — coverage tiers + remote gcov collection --------------------
# Embedded build settings live in [coverage.embedded] (see the coverage docs).
#[coverage]
#hosts = "example-device"
#gcda_remote_dir = "/tmp/gcda"
#[coverage.tiers.nightly]
#kind = "e2e"
#precedence = 10
#color = "#22c55e"
#harvest_dirs = ["cov/nightly"]
#max_age = "180d"
#[[coverage.exclusions.rules]]
#kind = "marker"
#name = "GCOV_EXCL"          # family: _LINE / _START / _STOP
#[[coverage.exclusions.rules]]
#kind = "preprocessor"
#macros = ["DEBUG_LOG"]
#[[coverage.exclusions.rules]]
#kind = "path"
#patterns = ["vendor/**"]
#[coverage.report]
#high = 80
#medium = 70
#[coverage.tickets]
#pattern = "[A-Z]{{2,10}}-[0-9]+"
#url = "https://example.atlassian.net/browse/{{0}}"
#[coverage.overrides]
## Manual-testing override file (defaults to .otto/coverage-overrides.toml)
#file = ".otto/coverage-overrides.toml"

# --- [docker] — image builds + compose stacks --------------------------------
#[docker]
#registry_url = "docker.io"
#[[docker.images]]
#name = "{name}-test"
#dockerfile = "docker/Dockerfile"
#context = "."
#target = "test"
#[docker.images.build_args]
#PORT = 8080
#[[docker.composes]]
#path = "docker/compose.yaml"
#services = ["{name}-svc"]
## Use-case fragments: what `otto docker up` deploys. See
## docs/guide/cli/docker/use-cases.md for provider competition (provides,
## priority), placement (role, placement) and env templating (env, pass_env).
#[[docker.use_cases]]
#name = "integration"
#composes = ["compose"]

# --- [monitor] — dashboard TLS (optional); see the monitor guide -------------
# tls_key without tls_cert is rejected; tls_cert alone is fine (bundled PEM).
#[monitor]
#tls_cert = "~/.otto/tls/monitor-cert.pem"
#tls_key = "~/.otto/tls/monitor-key.pem"

# --- [env] — orchestration-venv preference (optional) -----------------------
# This repo's standing choice of installer for `otto env`; --backend wins over
# it. Omit to auto-detect (uv when on PATH, else stdlib venv + pip).
#[env]
#backend = "uv"
"""

EXAMPLE_LAB_NAME = "example_lab"
"""The lab the scaffold declares — the one name every printed next step passes to ``--lab``."""

EXAMPLE_INVENTORY_KEY = "device-01.lab.example"
"""The inventory key the scaffold's three files share — the machine's own name, never an otto id.

``.example`` is the reserved documentation TLD, as ``192.0.2.1`` is TEST-NET.
"""

EXAMPLE_HOST_ENTRY = {
    "inventory": EXAMPLE_INVENTORY_KEY,
    "os_type": "unix",
    "valid_terms": ["ssh"],
    "valid_transfers": ["scp", "sftp"],
}

EXAMPLE_ELEMENT_ENTRY = {
    "_comment": (
        "Example element — replace these values. An element is the smallest unit that "
        "joins a lab: 'labs' lists regex patterns full-matched against lab names, and "
        "'hosts' are the machines/boards it holds. Full schema: "
        "docs/guide/configuration/lab-config.md or `otto schema export`. The host below "
        "is REFERENCED: its 'inventory' value is the machine's own name (typically its "
        "DNS hostname), never an otto id; its address lives in inventory.json and its "
        "creds in creds.json under that key."
    ),
    "name": "example-device",
    "labs": [EXAMPLE_LAB_NAME],
    "hosts": [EXAMPLE_HOST_ENTRY],
}

LAB_JSON_TEMPLATE: dict[str, Any] = {
    "$schema": "../.otto/schemas/lab.schema.json",
    "_comment": (
        "otto lab database: 'labs' declares each lab (its reservable resources and "
        "metadata); 'elements' groups hosts and says which labs they join; 'links' "
        "declares data-plane routes (see docs/guide/configuration/lab-config.md). "
        "Keys starting with _ are comments; $schema wires editor autocomplete. A host "
        'that says "inventory" gets its machine facts from inventory.json and its '
        "creds from creds.json under that key; everything otto-specific stays here, "
        "and a creds entry here overrides the same login below it."
    ),
    "labs": {EXAMPLE_LAB_NAME: {"resources": ["example-device"]}},
    "elements": [EXAMPLE_ELEMENT_ENTRY],
    "links": [],
}

INVENTORY_JSON_TEMPLATE: dict[str, Any] = {
    "$schema": "../.otto/schemas/inventory.schema.json",
    "_comment": (
        "Machine facts by inventory key — true whatever tool asks. Only the fields "
        "[inventory] supplies may appear; the rest stay in lab.json. Never rename a key: "
        "every lab.json entry naming it breaks."
    ),
    EXAMPLE_INVENTORY_KEY: {"ip": "192.0.2.1"},
}

CREDS_JSON_TEMPLATE: dict[str, Any] = {
    "$schema": "../.otto/schemas/creds.schema.json",
    "_comment": (
        "Credentials by inventory key: the lowest layer. An entry with the same login in "
        "inventory.json or lab.json overrides these field by field. Replace CHANGE_ME; keep "
        "this file out of version control or at mode 0600."
    ),
    EXAMPLE_INVENTORY_KEY: [{"login": "admin", "password": "CHANGE_ME"}],
}

LAB_README_TEMPLATE = """\
# lab_data/

Three files, one key. `otto init` wrote them together and they describe one
example host between them:

- **`lab.json`** — otto's lab database: the `labs` table, the `elements` that
  hold host entries, and `links`. Everything otto-specific about a host lives
  here (`os_type`, the term/transfer menus, `hop`, …). The example host does
  not carry an address or a password: it says `"inventory": "<key>"` instead.
- **`inventory.json`** — machine facts by inventory key, true whatever tool
  asks: the address today, interfaces and location once you widen `supplies`
  in `.otto/settings.toml`. Only the fields `supplies` lists may appear here.
- **`creds.json`** — credentials by the same inventory key. Written at mode
  `0600`; keep it that way, or out of version control. Optional: without the
  `[creds]` table, creds come from `inventory.json` records or `lab.json`.

The **inventory key** is the only thing the files share. It is the machine's
own name — typically its DNS hostname — and never an otto id or element name;
renaming one breaks every `lab.json` entry that names it.

**Creds compose by login: lab.json over inventory.json over creds.json,
field by field.** An entry in a higher file overrides the fields it states
for the same login and cannot remove one (`null` states nothing); lab.json's
order is the login order (the first entry is the default login). A cred
change you want to try before the team's files change goes in `lab.json`.
The full rules, the other backends (NetBox, your own store) and the
`~/.otto/settings.toml` home for a shared inventory are in
`docs/guide/configuration/inventory.md`.

## `lab.json`

It is a JSON object with three sections, each optional:

- **`labs`** — the table of labs this file declares, keyed by lab name. A lab
  exists only once some file declares it; `--lab`/`OTTO_LAB` selects one by
  name.
- **`elements`** — the things that hold hosts: a device, a board, a VM. Each
  element says which labs it joins and carries its own host entries. The
  scaffolded `lab.json` has one example element; edit or replace it, and add
  as many more as your lab needs.
- **`links`** — declared data-plane routes between hosts (routes not used for
  ssh/telnet access, carrying UDP/HTTP/RTP/etc.). Empty by default; see the
  `links` section below.

Older otto repos listed every host in a top-level `hosts` array. That shape is
gone: otto refuses a `lab.json` that still has one and names where each field
moved. See "Migrating from the hosts array" in
`docs/guide/configuration/lab-config.md`.

## The `labs` table

Each key is a lab name; its value declares what belongs to the lab as a whole:

- **`resources`** — the reservation identifiers this lab claims, so two
  sessions cannot take the same physical bench at once. These are declared,
  never derived from the hosts: two labs that share elements contend only if
  they declare a resource identifier in common (`otto init` warns when they
  do not).
- **`metadata`** — an opaque object for your own data; otto never reads it.

An empty value (`{}`) is a perfectly good declaration — it says the lab
exists and reserves nothing.

## Fields in the example element

- **`name`** — the element's name, and the base of the id otto derives for
  each host it holds. This is the name you pass to `--lab`-scoped commands
  and `get_host()`.
- **`id`** — an optional non-negative integer the author assigns to the
  element — data otto carries as `host.element.id` and never uses for
  identity.
- **`labs`** — the labs this element joins, written as regular expressions
  FULL-matched against declared lab names: `"bench"` does not match
  `"bench-2"`; write `"bench.*"` for that, or `".*"` to join every declared
  lab. At least one pattern is required — an element that joins nothing is a
  mistake, not an empty element.
- **`metadata`** — an opaque object shared by this element's hosts, reached
  as `host.element.metadata`; otto never reads it.
- **`hosts`** — one or more host entries. Each is validated against a
  pydantic spec before otto will use it (`UnixHostSpec` /
  `EmbeddedHostSpec`, see `docs/guide/configuration/lab-config.md`).

## Fields in the example host entry

- **`inventory`** — the inventory key (see above). An inline host carries
  **`ip`** here instead.
- **`os_type`** — `"unix"` for a UnixHost-backed entry (SSH/telnet-capable
  Linux/BSD-like systems) or `"embedded"` for an EmbeddedHost-backed entry
  (Zephyr and similar). Determines which spec class validates the rest of
  the entry.
- **`valid_terms`** — the ordered menu of term backends this host supports
  (e.g. `"ssh"`, `"telnet"`). The first entry is the default unless a
  `[host_preferences]` selector in `settings.toml` overrides it.
- **`valid_transfers`** — the ordered menu of file-transfer backends this
  host supports (e.g. `"scp"`, `"sftp"`, `"ftp"`, `"nc"`). Same
  first-entry-is-default rule as `valid_terms`.
- **`creds`** — optional on a referenced host: entries here layer over the
  same login in `inventory.json` and `creds.json`; an inline host lists every
  `{"login": ..., "password": ...}` here, the first being the default login.
- **`metadata`** — an opaque object for your own per-host data; otto never
  reads it.

`element`, `element_id` and `labs` are NOT host fields any more: identity
lives on the element (`name`), its data on `id` and `metadata`, membership on
the element's `labs` patterns. otto rejects a host entry carrying any of
them, naming the key.
`resources` may be declared on the lab (`labs` table), the element, or the
host — see the reservation docs.

Interfaces (when present) are keyed by their network-device name (`eth0`,
`eth1`, …), so impairment/capture can read the device straight off the key.

## Fields in a `links` entry

Each `links` entry describes one data-plane route between two hosts:

- **`endpoints`** — exactly two, each `{"host": <id>, "interface": <netdev>}`.
  `interface` is required only when the host defines more than one interface;
  with one (or none) otto assumes it and its IP.
- **`protocol`** — optional, defaults to `"tcp"`. Informational for declared
  links (documents what the route carries: udp/http/rtp/…).
- **`name`** — optional friendly handle; the id is otherwise derived from the
  endpoints.

A link belongs to every lab either endpoint belongs to, so it may span labs.

## Keys starting with `_`

`lab.json` is plain JSON, which has no comment syntax. Any key beginning
with `_` (like `_comment` above) is stripped before validation, so it is
otto's sanctioned way to leave a note inline — at the top level, inside the
`labs` table, and inside element/host/link entries. Use it freely.

## Where to go next

- Every host field, and the full `lab.json` reference:
  `docs/guide/configuration/lab-config.md` (the reference the doctor keeps
  complete — a field otto accepts but that page omits is a bug)
- Machine-readable schema (for editor validation or codegen):
  `otto schema export`
- Confirm otto sees your hosts once you've edited this file:
  `otto --lab example_lab --list-hosts`
"""

OPTIONS_TEMPLATE = '''\
"""Repo-wide options shared by every suite and instruction.

``@options`` (``from otto import options``) is pydantic's dataclass
decorator: fields declared here become validated CLI flags on every
``otto test`` suite and every ``otto run`` instruction whose options class
inherits ``RepoOptions``. See docs/library/options-classes.md.
"""

from typing import Annotated

import typer

from otto import options


@options
class RepoOptions:
    """Inherit me from a suite's inner Options or an @instruction options class."""

    message: Annotated[
        str, typer.Option(help="Message the sample suite and instruction log.")
    ] = "hello from {name}"
'''

TEST_EXAMPLE_TEMPLATE = '''\
"""Example otto test suite — runs hostless so it passes out of the box."""

import logging
from typing import Annotated

import pytest
import typer

from otto import options
from otto.suite import OttoSuite

from {options_module} import RepoOptions

logger = logging.getLogger(__name__)


@options
class _Options(RepoOptions):
    """This suite's options: the repo-wide flags plus its own ``--greeting``."""

    greeting: Annotated[str, typer.Option(help="Greeting the example test logs.")] = "hello"


class TestExample(OttoSuite):
    """A minimal suite: `otto test TestExample` (auto-registered by its Test* name)."""

    Options = _Options

    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def banner(cls, suite_options: _Options) -> str:
        """Suite-wide setup: runs once before the first test (the setup_class of old).

        A fixture defined ON the class at class scope is a classmethod. Make it
        `async` and it runs on the suite's event loop — open host sessions here,
        `yield` them, and close them after the yield.
        """
        text = f"{{suite_options.message}} ({{suite_options.greeting}})"
        logger.info("suite starting: %s", text)
        return text

    async def test_logs_message(self, banner: str, repo_marker: str) -> None:
        logger.info(banner)
        assert repo_marker == "from-conftest"

    async def test_expect_and_artifacts(self, expect, test_dir) -> None:
        """`expect` records a failure without stopping the test; `test_dir` is this
        test's own artifact directory under the run's output dir."""
        expect(len("otto") == 4, "four letters")
        (test_dir / "note.txt").write_text("artifacts go here")
        assert (test_dir / "note.txt").exists()


def test_example_function() -> None:
    """Plain pytest functions run too: `otto test --tests test_example_function`."""
    assert True
'''

CONFTEST_TEMPLATE = '''\
"""Repo-wide fixtures — available to every test under tests/ (any depth)."""

import pytest


@pytest.fixture
def repo_marker() -> str:
    """Trivial example fixture the scaffolded suite consumes."""
    return "from-conftest"


# Fixtures can hand tests live lab hosts. Class scope = opened once per suite,
# on the suite's event loop, shared by every test in it, closed after the last
# (a conftest fixture is a plain function — no @classmethod needed). Uncomment
# once your lab_data/ is real:
# import pytest_asyncio
#
# @pytest_asyncio.fixture(scope="class")
# async def primary_host():
#     from otto.config import get_host
#
#     host = get_host("example-device")
#     yield host
#     await host.close()
#
# A module- or session-scoped async fixture must pin its loop scope to match,
# e.g. @pytest_asyncio.fixture(scope="session", loop_scope="session").
'''

INSTRUCTIONS_TEMPLATE = '''\
"""{name} instructions — functions exposed as `otto run` subcommands."""

import logging
from typing import Annotated

import typer

from otto import options
from otto.cli.run import instruction

from {options_module} import RepoOptions

logger = logging.getLogger(__name__)

# `install`, `uninstall`, `cleanup`, `get-logs`, `install-tools` and `status`
# already exist — otto registers them for every lab, over your registered
# products. Do NOT define instructions with those names here: they are refused
# at startup. To change what they do for this repo, subclass ProjectActions and
# register it from this module:
#
#     from otto.project import ProjectActions, register_project_actions
#
#     @register_project_actions
#     class RepoActions(ProjectActions):
#         async def install(self):
#             ...                       # your work
#             return await super().install()
#
# One override point, so `otto run install`, a script, a suite, and an
# ensure("installed") marker all pick it up. See docs/guide/cli/run/defaults.md.


@options
class _Options(RepoOptions):
    """This instruction's options: the repo-wide flags plus its own ``--loud``."""

    loud: Annotated[bool, typer.Option(help="Uppercase the message.")] = False


@instruction(options=_Options)
async def smoke(opts: _Options) -> None:
    """Log the repo-wide message — replace with your first real instruction."""
    logger.info(opts.message.upper() if opts.loud else opts.message)
'''

VSCODE_SETTINGS_TEMPLATE = r"""{
  "json.schemas": [
    { "fileMatch": ["**/lab.json"], "url": "./.otto/schemas/lab.schema.json" },
    { "fileMatch": ["**/reservations.json"], "url": "./.otto/schemas/reservations.schema.json" },
    { "fileMatch": ["**/inventory*.json"], "url": "./.otto/schemas/inventory.schema.json" },
    { "fileMatch": ["**/creds*.json"], "url": "./.otto/schemas/creds.schema.json" }
  ],
  "evenBetterToml.schema.associations": {
    ".*/settings\\.toml$": "./.otto/schemas/settings.schema.json"
  }
}
"""

VSCODE_EXTENSIONS_TEMPLATE = """\
{
  "recommendations": ["tamasfe.even-better-toml"]
}
"""
