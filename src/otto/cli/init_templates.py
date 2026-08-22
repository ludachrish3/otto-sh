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

# --- [logging] — extra top-level logger prefixes routed into otto's sinks ----
#[logging]
#capture = ["my_library"]

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
#[coverage.exclusions]
#markers = ["GCOV_EXCL"]
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
#default_host = "{name}-svc"
#services = ["{name}-svc"]

# --- [monitor] — dashboard TLS (optional); see the monitor guide -------------
# tls_key without tls_cert is rejected; tls_cert alone is fine (bundled PEM).
#[monitor]
#tls_cert = "~/.config/otto/tls/monitor-cert.pem"
#tls_key = "~/.config/otto/tls/monitor-key.pem"
"""

EXAMPLE_HOST_ENTRY = {
    "_comment": (
        "Example host — replace these values. Full host schema: "
        "docs/guide/configuration/lab-config.md or `otto schema export`. The `labs` list "
        "names the labs this host belongs to (select with --lab/OTTO_LAB)."
    ),
    "ip": "192.0.2.1",
    "element": "example-device",
    "os_type": "unix",
    "valid_terms": ["ssh"],
    "valid_transfers": ["scp", "sftp"],
    "creds": [{"login": "admin", "password": "CHANGE_ME"}],
    "resources": ["example-device"],
    "labs": ["example_lab"],
}

LAB_JSON_TEMPLATE: dict[str, Any] = {
    "$schema": "../.otto/schemas/lab.schema.json",
    "_comment": (
        "otto lab database: 'hosts' lists every lab host; 'links' declares "
        "data-plane routes between them (see docs/guide/configuration/lab-config.md). "
        "Keys starting with _ are comments; $schema wires editor autocomplete."
    ),
    "hosts": [EXAMPLE_HOST_ENTRY],
    "links": [],
}

LAB_README_TEMPLATE = """\
# lab_data/

This directory holds `lab.json` — otto's lab database for this repo. It is a
JSON object with two array sections:

- **`hosts`** — every lab host. Each entry is validated against a pydantic spec
  before otto will use it (`UnixHostSpec` / `EmbeddedHostSpec`, see
  `docs/guide/configuration/lab-config.md`). The scaffolded `lab.json` has one example
  host; edit or replace it, and add as many more as your lab needs.
- **`links`** — declared data-plane routes between hosts (routes not used for
  ssh/telnet access, carrying UDP/HTTP/RTP/etc.). Empty by default; see the
  `links` section below.

## Fields in the example host entry

- **`ip`** — the host's IP address (or hostname), used to open term/transfer
  sessions.
- **`element`** — the host's unique id within this repo's host database. This
  is the name you pass to `--lab`-scoped commands and `get_host()`.
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
- **`creds`** — an ordered list of `{"login": ..., "password": ...}` objects;
  the first entry is the default login unless `user` pins another one.
  Replace `"CHANGE_ME"` with a real credential (or point it at your secrets
  manager per your repo's convention) before connecting to a real host.
- **`resources`** — a set of resource names this host claims, used by
  reservations to prevent two sessions from using the same physical device
  at once. Usually just the host's own name.
- **`labs`** — the list of lab names this host belongs to. A host can belong
  to more than one lab; select which lab is active with `--lab`/`OTTO_LAB`.

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
otto's sanctioned way to leave a note inline — both at the top level and
inside host/link entries. Use it freely.

## Where to go next

- Full host schema reference: `docs/guide/configuration/lab-config.md`
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

from typing import Annotated

import typer

from otto import options
from otto.suite import OttoSuite

from {options_module} import RepoOptions


@options
class _Options(RepoOptions):
    """This suite's options: the repo-wide flags plus its own ``--greeting``."""

    greeting: Annotated[str, typer.Option(help="Greeting the example test logs.")] = "hello"


class TestExample(OttoSuite[_Options]):
    """A minimal suite: `otto test TestExample` (auto-registered by its Test* name)."""

    Options = _Options

    async def test_logs_message(self, suite_options: _Options, repo_marker: str) -> None:
        self.logger.info("%s (%s)", suite_options.message, suite_options.greeting)
        assert repo_marker == "from-conftest"


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


# Fixtures can hand tests live lab hosts; uncomment once your lab_data/ is real:
# @pytest.fixture
# async def primary_host():
#     from otto.config import get_host
#
#     host = get_host("example-device")
#     yield host
#     await host.close()
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
# One override point, so `otto run install`, a script, a suite, and the
# ensure_installed fixture all pick it up. See docs/guide/cli/run/defaults.md.


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
    { "fileMatch": ["**/reservations.json"], "url": "./.otto/schemas/reservations.schema.json" }
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
