"""THE SPLIT-BRAIN GUARD for the session: ``otto --lab X`` and
``otto.open_context(lab="X")`` prepare the same run.

Both paths decide through :mod:`otto.session`. The CLI preamble renders each
decision in today's words and exit codes; ``open_context`` raises the typed
error or logs the demotion. This module proves the two agree, in three parts,
each over repo layouts built with ``make_sut_repo`` + ``Repo`` and installed as
``bootstrap()``'s result:

1. **Lab parity.** For each layout, the CLI's lab (``ensure_lab_context`` on
   the root options the real ``--lab`` parse produces) and the library's lab
   (``open_context``) have the same name and components, host ids, per-host
   merged preferences (the resolved ``term``/``transfer``/``impairer``
   selections and option tables the factory applies — ``load_lab`` stores the
   merge nowhere else), addresses, and container placeholders. Both paths
   build through ``otto.session.build_lab``, so a fault inside it moves both
   sides together and the equality alone stays green: each layout therefore
   also asserts its WITNESS, the one fact that makes the layout what it is, on
   the library's lab. Without the witness an empty preference table compares
   equal to an empty preference table.
   The lab-parity cases drive the CLI side from ``ensure_lab_context``, not
   through ``entry()``, so a future preamble step that changed the lab after
   ``ensure_lab_context`` would not be seen here. One lab-parity case runs the
   CLI through ``entry()``: ``--field``, whose variant only the root callback
   sets, against ``open_context(variant="field")`` over a product declared
   once per variant. Its facts carry each host's products, and its witness is
   that the field entry is the one the lab carries.
   The refusal cases also go through ``entry()``.
2. **Refusal parity.** For each case the library raises the typed error and
   the real CLI — ``otto.cli.main.entry()``, so the boundary frame that
   prints an unknown lab is in the loop — exits with today's code and prints
   the library's facts in flag spelling. The error's carried payload is
   reached, not just its type: a ``RepoLoadError`` names its fatal repos and
   carries its demotions, which the CLI prints before its frame; a
   ``DependencyRefusedError`` names the repo and requirement the preflight
   reported. The cases that do not refuse are the counterpart: the library
   does not raise, its dependency warnings are the CLI's ``warning:`` lines,
   and the CLI exits 0 at the dry-run seam. The CLI cases run ``-n ... cov
   clean``: a lab-bound leaf that takes no arguments, whose preamble runs
   every refusal before the seam stops it. The ``-n`` is load-bearing: a
   refusal the CLI MISSED reaches the seam and exits 0, where without it
   ``cov clean``'s own body would fail with exit 1 — the refusal's code — and
   hide the miss. The library side runs under its own ``asyncio.run``, so
   ``entry()`` never runs inside a live loop.
3. **Demotion parity.** For the two demotion layouts, the library's
   ``check_repos(...).demoted`` match the CLI's stderr ``warning:`` lines one
   for one, in order: the same repo spelling, and the same reason with
   ``exclude_projects x`` spelled ``--exclude-projects x`` and the lab list
   ``[lab1, lab2]`` compared separator and order alike.

Every CLI case runs under ``GITHUB_ACTIONS=true TERM=dumb FORCE_COLOR=`` (CI
renders rich at a fixed 80 columns), so multi-line messages are compared with
:func:`_squashed` or :func:`_flat` on both sides.

Verified red when written, one mutation at a time, each reverted after:

(i) dropping ``preferences=`` from the ``load_lab`` call in
``otto/session/lab.py``: lab parity's ``preferences`` layout failed on its
witness (``assert "'ssh'" == "'telnet'"``), past an equality that stayed
green, as predicted, because both sides lost the preferences together.

(ii) making ``check_repos``'s ``include`` branch demote instead of refuse:
refusal parity's ``broken-included`` case failed on both sides at once,
``assert (None, 0) == ('RepoLoadError', 1)``: the library did not raise, and
the CLI printed the demotion line and exited 0 at the seam.

(iii) printing ``d.repo.lower()`` in ``otto/cli/invoke.py``
``_echo_demotions``: demotion parity failed for both layouts
(``'broken_repo'`` against ``'Broken_Repo'``).

(iv) dropping ``_echo_demotions(e.demoted)`` before the frame in
``fail_loud_on_bootstrap_errors``: refusal parity's ``demoted-and-fatal``
case failed (``assert [] == [('Broken_Repo', 'not applicable to lab(s)
[lab1]')]``): the library carried the demotion, the CLI never printed it.
Moving that echo to after the frame failed the same case on the order
(``assert 421 < 343``: the demotion line landed after the frame).

(v) comparing the RAW repo name against the switches in
``otto.config.scope.active`` (``name = repo_name``): refusal parity's
``unmet-dependency-excluded`` case failed on both sides at once,
``assert ('DependencyRefusedError', 1) == (None, 0)``: ``-E Sibling_Repo``
normalises to ``sibling-repo`` and no longer matched ``Sibling_Repo``.

(vi) assigning instead of updating an option table in
``merge_host_preferences`` (``dest[key] = dict(val)``): lab parity's
``preferences`` layout failed on its witness (``port=2222`` gone from
alpha's ``ssh_options``: Beta's ``connect_timeout`` table replaced Acme's).

(vii) dropping ``variant="field"`` from the library side of the ``--field``
case: the equality failed (the library's lab carried ``fw-debug.bin``, the
CLI's ``fw-field.bin``). Dropping it AND the CLI's ``--field``: the equality
stayed green, as predicted, and the witness failed
(``fw=.../Acme/build/fw-debug.bin``).
"""

import ast
import asyncio
import dataclasses
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from otto import bootstrap as bs
from otto.config.repo import TOML_SETTINGS_PATH, Repo
from tests._fixtures.labdata import lab_data_dir, write_lab_json
from tests._fixtures.rootoptions import make_root_options
from tests._fixtures.sutrepo import make_sut_repo
from tests.unit.cli.conftest import _flat, _squashed

_CI_ENV = {"GITHUB_ACTIONS": "true", "TERM": "dumb", "FORCE_COLOR": ""}
"""CI's console: a forced terminal at a fixed 80 columns, no colour."""

_SOURCES = '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n'
"""One json ``[[lab.sources]]`` entry reading the repo's own ``lab/`` directory."""


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path, monkeypatch):
    """A fresh ``otto`` process's starting state, as far as the run can see it.

    No OTTO_* from the shell, an empty user home, logs under tmp_path, and CI's
    console.
    """
    for key in list(os.environ):
        if key.startswith("OTTO_"):
            monkeypatch.delenv(key)
    home = tmp_path / "otto-home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path / "xdir"))
    for key, value in _CI_ENV.items():
        monkeypatch.setenv(key, value)


def _install(monkeypatch, repos: "list[Any]", errors: "list[bs.BootstrapError] | None" = None):
    """Make *repos* and *errors* what ``bootstrap()`` returns for this test."""
    bs._reset()
    result = bs.BootstrapResult(
        env=None,
        repos=list(repos),
        errors=list(errors or []),
        warnings=[],
        ordered_repos=list(repos),  # as bootstrap() fills it, so the real preflight sees them
    )
    monkeypatch.setattr(bs, "_result", result)


def _host(element: str, ip: str, lab: str = "lab1", **fields: Any) -> dict:
    """One lab-json host record, a member of *lab*."""
    return {
        "ip": ip,
        "element": element,
        "creds": [{"login": "u", "password": "p"}],
        "labs": [lab],
        **fields,
    }


def _repo(
    root: Path,
    name: str,
    *,
    extra: str = "",
    hosts: "list[dict] | None" = None,
    files: "dict[str, str] | None" = None,
) -> Repo:
    """A real SUT repo; with *hosts*, it also declares a json source holding them."""
    settings = (_SOURCES + extra) if hosts is not None else extra
    sut = make_sut_repo(root / name, name=name, extra=settings, files=files)
    if hosts is not None:
        write_lab_json(sut / "lab" / "lab.json", hosts)
    return Repo(sut)


# ── 1. Lab parity ────────────────────────────────────────────────────────────

_PREFERENCE_FIELDS = [
    "term",
    "transfer",
    "impairer",
    "ssh_options",
    "sftp_options",
    "scp_options",
    "ftp_options",
    "nc_options",
    "telnet_options",
]
"""Where a host's merged ``[host_preferences]`` land: the factory resolves the
selections into the active ``term``/``transfer``/``impairer`` and folds the
option tables into the ``*_options`` defaults."""


@dataclasses.dataclass(frozen=True)
class LabFacts:
    """What a lab is, for the comparison: everything a layout can change."""

    name: str
    components: list[str]
    hosts: list[str]
    preferences: dict[str, dict[str, str]]
    addresses: dict[str, "str | None"]
    placeholders: list[str]
    products: dict[str, list[str]]
    """Per host, ``name=artifact`` for each product it carries, in order."""


def _attr(host: Any, name: str) -> Any:
    try:
        return getattr(host, name)
    except AttributeError:  # not every host kind carries every field (LocalHost has no `ip`)
        return None


def _facts(lab: Any) -> LabFacts:
    from otto.host.docker_host import DockerContainerHost

    return LabFacts(
        name=lab.name,
        components=list(lab.component_names),
        hosts=sorted(lab.hosts),
        preferences={
            hid: {name: repr(_attr(host, name)) for name in _PREFERENCE_FIELDS}
            for hid, host in sorted(lab.hosts.items())
        },
        addresses={hid: _attr(host, "ip") for hid, host in sorted(lab.hosts.items())},
        placeholders=sorted(
            hid
            for hid, host in lab.hosts.items()
            if isinstance(host, DockerContainerHost) and not host.container_id
        ),
        products={
            hid: [f"{p.name}={_attr(p, 'artifact')}" for p in _attr(host, "products") or []]
            for hid, host in sorted(lab.hosts.items())
        },
    )


@dataclasses.dataclass(frozen=True)
class Layout:
    """A matrix row: build the repos, name the lab, and say what must be true of it."""

    id: str
    build: "Callable[[Path], list[Repo]]"
    lab: str
    witness: "Callable[[LabFacts], None]"


def _two_sources(root: Path) -> "list[Repo]":
    return [
        _repo(root, "Acme", hosts=[_host("alpha", "10.0.0.1")]),
        _repo(root, "Beta", hosts=[_host("beta", "10.0.0.2")]),
    ]


def _two_sources_witness(facts: LabFacts) -> None:
    assert {"alpha", "beta"} <= set(facts.hosts)


def _override(root: Path) -> "list[Repo]":
    return [
        _repo(root, "Acme", hosts=[_host("alpha", "10.0.0.1")]),
        _repo(root, "Beta", hosts=[_host("alpha", "10.0.0.99")]),
    ]


def _override_witness(facts: LabFacts) -> None:
    assert facts.addresses["alpha"] == "10.0.0.99"  # the LATER repo's record


def _preferences(root: Path) -> "list[Repo]":
    """Both repos set the SAME option table under the SAME selector, each a different key.

    So the merge must update the table per key: a merge that replaced it would
    keep only Beta's key.
    """
    return [
        _repo(
            root,
            "Acme",
            hosts=[_host("alpha", "10.0.0.1")],
            extra=(
                '[host_preferences.".*"]\nterm = ["telnet", "ssh"]\n'
                '[host_preferences."alpha".ssh_options]\nport = 2222\n'
            ),
        ),
        _repo(
            root, "Beta", extra='[host_preferences."alpha".ssh_options]\nconnect_timeout = 7.5\n'
        ),
    ]


def _preferences_witness(facts: LabFacts) -> None:
    alpha = facts.preferences["alpha"]
    assert alpha["term"] == repr("telnet")  # Acme's selection list: its first choice
    assert "port=2222" in alpha["ssh_options"]  # Acme's key of the shared table
    assert "connect_timeout=7.5" in alpha["ssh_options"]  # Beta's key of the same table


def _two_labs(root: Path) -> "list[Repo]":
    return [
        _repo(
            root,
            "Acme",
            hosts=[_host("alpha", "10.0.0.1", lab="lab1"), _host("beta", "10.0.0.2", lab="lab2")],
        )
    ]


def _two_labs_witness(facts: LabFacts) -> None:
    assert (facts.name, facts.components) == ("lab1+lab2", ["lab1", "lab2"])
    assert {"alpha", "beta"} <= set(facts.hosts)


def _containers(root: Path) -> "list[Repo]":
    return [
        _repo(
            root,
            "Acme",
            hosts=[_host("dockhost", "10.0.0.1", docker_capable=True)],
            extra=(
                '\n[[docker.composes]]\nname = "core"\npath = "docker/compose.yml"\n'
                'services = ["api", "db"]\n'
            ),
            files={"docker/compose.yml": "services: {}\n"},
        )
    ]


def _containers_witness(facts: LabFacts) -> None:
    assert len(facts.placeholders) == 2
    assert all(hid.startswith("dockhost.") for hid in facts.placeholders)


def _inventory(root: Path) -> "list[Repo]":
    """The worked-example fixture: a lab entry that references the process inventory.

    The inventory is resolved for real, through ``OTTO_HOME``'s settings file,
    as ``test_open_context_loads_the_lab_with_the_process_inventory`` does.
    """
    fixture = lab_data_dir() / "tech1-inventory"
    repo = _repo(root, "Acme", extra=_SOURCES)
    (repo.sut_dir / "lab").mkdir()
    shutil.copy(fixture / "lab.json", repo.sut_dir / "lab" / "lab.json")
    user_settings = (
        '[inventory]\nbackend = "json"\n'
        f'path = "{fixture / "inventory.json"}"\n'
        'supplies = ["ip", "interfaces", "is_virtual", "site", "rack", '
        '"shelf", "board", "os_name"]\n'
        f'\n[creds]\nbackend = "json"\npath = "{fixture / "creds.json"}"\n'
    )
    home = Path(os.environ["OTTO_HOME"])
    (home / "settings.toml").write_text(user_settings)  # sutrepo-exempt: the user ~/.otto file
    return [repo]


def _inventory_witness(facts: LabFacts) -> None:
    assert facts.addresses["test1"] == "10.10.200.11"  # the inventory record's, not the lab file's


LAYOUTS = [
    Layout("two-sources", _two_sources, "lab1", _two_sources_witness),
    Layout("later-repo-overrides", _override, "lab1", _override_witness),
    Layout("preferences", _preferences, "lab1", _preferences_witness),
    Layout("plus-combined-labs", _two_labs, "lab1+lab2", _two_labs_witness),
    Layout("containers", _containers, "lab1", _containers_witness),
    Layout("inventory", _inventory, "unix", _inventory_witness),
]


def _variants(root: Path) -> "list[Repo]":
    """One product declared twice, once per variant.

    Both entries name their variant, so their order decides nothing in a
    working run. Debug goes first so that a broken variant filter, falling
    back to first match wins, picks debug, which the witness catches.
    """
    products = "".join(
        f'\n[[products]]\nname = "fw"\nkind = "shell"\nvariant = "{v}"\n'
        f'artifact = "build/fw-{v}.bin"\n'
        for v in ["debug", "field"]
    )
    return [_repo(root, "Acme", hosts=[_host("alpha", "10.0.0.1")], extra=products)]


def _field_witness(facts: LabFacts) -> None:
    [fw] = facts.products["alpha"]
    assert fw.startswith("fw=")
    assert fw.endswith("build/fw-field.bin"), fw  # the field entry, not fw-debug.bin


FIELD = Layout("field-variant", _variants, "lab1", _field_witness)
"""The ``--field`` row: run through ``entry()``, because the root callback sets the variant."""


def _cli_lab(lab: str) -> LabFacts:
    """The CLI's lab: the real ``--lab`` parse, then ``ensure_lab_context`` on what it stashes.

    A real Click context, closed as an invocation's root is, so the reset
    ``ensure_lab_context`` registers runs here too.
    """
    from typer.core import TyperGroup

    from otto.cli.invoke import ensure_lab_context
    from otto.cli.main import parse_lab_selection

    labs = parse_lab_selection([lab])  # the root callback's `--lab` callback
    with typer.Context(TyperGroup(name="otto")) as ctx:
        ctx.meta["_otto_root_options"] = make_root_options(labs=labs)
        return _facts(ensure_lab_context(ctx).lab)


async def _library_lab(lab: str, **kwargs: Any) -> LabFacts:
    import otto

    async with otto.open_context(lab=lab, **kwargs) as ctx:
        return _facts(ctx.lab)


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda layout: layout.id)
async def test_otto_lab_and_open_context_build_the_same_lab(layout, tmp_path, monkeypatch):
    _install(monkeypatch, layout.build(tmp_path))
    library = await _library_lab(layout.lab)
    cli = _cli_lab(layout.lab)
    assert cli == library
    layout.witness(library)


def test_otto_field_and_open_context_variant_field_build_the_same_lab(tmp_path, monkeypatch):
    """``otto --field --lab X`` and ``open_context(lab=X, variant="field")`` pick the same entry.

    The CLI side is the real ``entry()``, so the ``--field`` parse and the root
    callback's policy install are in the loop; its lab is read off the context
    ``ensure_lab_context`` installed, as Click's close resets it. Sync, with the
    library side under its own ``asyncio.run``, so ``entry()`` never runs inside
    a live loop.
    """
    from tests._fixtures.contexts_at_close import record_contexts_at_close

    _install(monkeypatch, FIELD.build(tmp_path))
    library = asyncio.run(_library_lab(FIELD.lab, variant="field"))
    # Installed after the library run: the recorder sees open_context's reset too.
    installed = record_contexts_at_close(monkeypatch)
    run = _otto(monkeypatch, ["--field", "-n", "--lab", FIELD.lab, "cov", "clean"])
    assert run.code == 0, run
    [cli] = [_facts(ctx.lab) for ctx in installed]
    assert cli == library
    FIELD.witness(library)


# ── 2. Refusal parity ────────────────────────────────────────────────────────

_BROKEN = "Broken_Repo"
"""Mixed case on purpose: a renderer that re-spells the repo name shows up."""

_FATAL = "Fatal_Repo"
"""A second broken repo, active, so one run carries a fatal error AND a demotion."""

_SIBLING = "Sibling_Repo"
"""A healthy repo whose requirement goes unmet; mixed case, so ``-E`` must normalise."""


@dataclasses.dataclass(frozen=True)
class World:
    """The repos a case installs as ``bootstrap()``'s result, and its load errors."""

    repos: "list[Any]"
    errors: "list[bs.BootstrapError]" = dataclasses.field(default_factory=list)


def _healthy(root: Path) -> Repo:
    """The repo that supplies ``lab1`` and ``lab2``, so a run that gets that far has a lab."""
    return _repo(
        root,
        "Healthy",
        hosts=[_host("alpha", "10.0.0.1", lab="lab1"), _host("beta", "10.0.0.2", lab="lab2")],
    )


def _broken(
    root: Path, name: str, *, lab_patterns: "list[str] | None" = None
) -> "tuple[Repo, bs.BootstrapError]":
    """A repo whose settings parsed (so its scope is known) but whose init module failed."""
    scope = (
        ""
        if lab_patterns is None
        else "[project]\nlab_patterns = [" + ", ".join(f'"{p}"' for p in lab_patterns) + "]\n"
    )
    repo = _repo(root, name, extra=scope)
    error = bs.BootstrapError(repo.sut_dir, "broken_init", ImportError("No module named 'missing'"))
    return repo, error


def _active_broken(root: Path) -> World:
    repo, error = _broken(root, _BROKEN)
    return World(repos=[_healthy(root), repo], errors=[error])


def _out_of_scope_broken(root: Path) -> World:
    repo, error = _broken(root, _BROKEN, lab_patterns=["elsewhere"])
    return World(repos=[_healthy(root), repo], errors=[error])


def _demoted_and_fatal(root: Path) -> World:
    """One out-of-scope broken repo (demoted) and one active broken repo (fatal)."""
    demoted, demoted_error = _broken(root, _BROKEN, lab_patterns=["elsewhere"])
    fatal, fatal_error = _broken(root, _FATAL)
    return World(repos=[_healthy(root), demoted, fatal], errors=[demoted_error, fatal_error])


def _unparsable(root: Path) -> World:
    """A ``settings.toml`` that does not parse: no ``Repo``, so no repo to attribute it to.

    The error is framed exactly as discovery frames it, from the real parse failure.
    """
    sut = make_sut_repo(root / "garbled", name="Garbled", extra="this is = = not toml")
    try:
        Repo(sut)
    except Exception as e:  # noqa: BLE001 — discovery contains ANY failure the same way
        error = bs.BootstrapError(sut, str(TOML_SETTINGS_PATH), e)
    else:  # pragma: no cover — the layout is wrong, not the product
        pytest.fail("the garbled settings.toml parsed")
    return World(repos=[_healthy(root)], errors=[error])


def _just_healthy(root: Path) -> World:
    return World(repos=[_healthy(root)])


def _with_sibling(root: Path) -> World:
    return World(repos=[_healthy(root), _repo(root, _SIBLING)])


_UNMET_REQUIREMENT = "acme-lib[extra]>=2"
"""Brackets on purpose: a renderer that lets rich read ``[extra]`` as markup deletes it."""


@dataclasses.dataclass(frozen=True)
class Case:
    """A refusal-parity row: the world, the switches, the library's verdict, the CLI's code."""

    id: str
    world: "Callable[[Path], World]"
    labs: list[str]
    raises: "type[Exception] | None"
    code: int
    include: list[str] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)
    fatal: list[str] = dataclasses.field(default_factory=list)
    """The repo directories a ``RepoLoadError`` must name, in order."""
    demoted: list[str] = dataclasses.field(default_factory=list)
    """The repos the library demotes, in order."""
    unmet: "str | None" = None
    """The repo whose requirement the preflight finds unmet."""


def _cases() -> "list[Case]":
    from otto.session import (
        DependencyRefusedError,
        LabBuildError,
        ProjectSelectionError,
        RepoLoadError,
    )

    two_labs = ["lab1", "lab2"]
    return [
        Case("broken-active", _active_broken, ["lab1"], RepoLoadError, 1, fatal=[_BROKEN]),
        Case(
            "broken-excluded",
            _active_broken,
            ["lab1"],
            None,
            0,
            exclude=[_BROKEN],
            demoted=[_BROKEN],
        ),
        Case("broken-out-of-scope", _out_of_scope_broken, two_labs, None, 0, demoted=[_BROKEN]),
        Case(
            "broken-included",
            _out_of_scope_broken,
            ["lab1"],
            RepoLoadError,
            1,
            include=[_BROKEN],
            fatal=[_BROKEN],
        ),
        Case(
            "demoted-and-fatal",
            _demoted_and_fatal,
            ["lab1"],
            RepoLoadError,
            1,
            fatal=[_FATAL],
            demoted=[_BROKEN],
        ),
        Case("unparsable-settings", _unparsable, ["lab1"], RepoLoadError, 1, fatal=["garbled"]),
        Case(
            "unmet-dependency",
            _just_healthy,
            ["lab1"],
            DependencyRefusedError,
            1,
            unmet="Healthy",
        ),
        Case(
            "unmet-dependency-excluded",
            _with_sibling,
            ["lab1"],
            None,
            0,
            exclude=[_SIBLING],
            unmet=_SIBLING,
        ),
        Case(
            "unknown-project",
            _just_healthy,
            ["lab1"],
            ProjectSelectionError,
            2,
            include=["healthyy"],
        ),
        Case("unknown-lab", _just_healthy, ["nosuch"], LabBuildError, 1),
        Case("missing-lab", _just_healthy, [], LabBuildError, 2),
    ]


CASES = _cases()


def _arm_unmet_dependency(monkeypatch: pytest.MonkeyPatch, repo_name: str) -> None:
    """The preflight finds *repo_name*'s requirement unmet — if it is among the repos it checks."""
    from otto.env.preflight import PreflightResult, Unsatisfied

    def preflight(repos, site_dirs=None):
        unmet = [
            Unsatisfied(repo=repo.name, requirement=_UNMET_REQUIREMENT, found="none")
            for repo in repos
            if repo.name == repo_name
        ]
        return PreflightResult(unsatisfied=unmet, warnings=[])

    monkeypatch.setattr("otto.env.preflight.preflight", preflight)


@dataclasses.dataclass(frozen=True)
class LibraryVerdict:
    """What ``open_context`` decided: the error it raised, or the warnings it reported."""

    error: "Exception | None"
    warnings: list[str]


async def _open_context(case: Case) -> LibraryVerdict:
    """Open the context the case describes; return what it raised, or its dependency warnings.

    The warnings are ``check_dependencies``' RETURN value on the context
    ``open_context`` installed (``open_context`` itself only logs them).
    """
    import otto
    from otto.session import check_dependencies

    try:
        async with otto.open_context(
            lab=case.labs, include_projects=case.include, exclude_projects=case.exclude
        ) as ctx:
            warnings = check_dependencies(ctx)
    except Exception as e:  # noqa: BLE001 — the verdict is asserted by the caller
        return LibraryVerdict(error=e, warnings=[])
    return LibraryVerdict(error=None, warnings=warnings)


@dataclasses.dataclass(frozen=True)
class CliRun:
    code: int
    stdout: str
    stderr: str
    output: str
    """Both streams interleaved in write order, as a terminal shows them."""


def _entry_command() -> typer.Typer:
    """A one-command app whose body IS ``otto``'s console-script entry.

    ``entry()`` holds the boundary frame that renders an ``OttoError`` escaping
    ``app()`` (the unknown lab's ``error: ...`` line), so a ``CliRunner`` on
    ``app`` alone would see the exception rather than what the user sees.
    Going through ``CliRunner.invoke`` (rather than its ``isolation()``) keeps
    the root conftest's live-log capture guard over the run.
    """
    wrapper = typer.Typer()

    @wrapper.command()
    def otto_entry() -> None:
        from otto.cli.main import entry

        entry()

    return wrapper


def _otto(monkeypatch: pytest.MonkeyPatch, argv: "list[str]") -> CliRun:
    """Run ``otto <argv>`` through the real entry point; its own argv is ``sys.argv``."""
    monkeypatch.setattr("sys.argv", ["otto", *argv])
    result = CliRunner().invoke(_entry_command(), [])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return CliRun(
        code=result.exit_code, stdout=result.stdout, stderr=result.stderr, output=result.output
    )


def _argv(case: Case) -> "list[str]":
    argv = ["-n"]
    for lab in case.labs:
        argv += ["--lab", lab]
    for name in case.include:
        argv += ["-I", name]
    for name in case.exclude:
        argv += ["-E", name]
    return [*argv, "cov", "clean"]


_DEMOTION = re.compile(
    r"repo (?P<repo>'[^']*') failed to load, but is inactive for this run "
    r"\((?P<why>.*)\) — continuing without it"
)
"""The demotion sentence both sides share; the CLI's carries ``warning: `` in front."""

_FLAG_SPELLING = {"exclude_projects ": "--exclude-projects "}
"""The one field name a demotion reason can carry, and its flag."""


def _in_flag_spelling(why: str) -> str:
    for field, flag in _FLAG_SPELLING.items():
        if why.startswith(field):
            return flag + why[len(field) :]
    return why


def _library_demotions(demoted: "list[Any]") -> "list[tuple[str, str]]":
    """``(repo, reason in flag spelling)`` for each of the library's ``DemotedRepo``."""
    out = []
    for d in demoted:
        match = _DEMOTION.fullmatch(d.message)
        assert match is not None, d.message
        out.append((d.repo, _in_flag_spelling(match["why"])))
    return out


def _cli_demotions(stderr: str) -> "list[tuple[str, str]]":
    """``(repo, reason)`` for each demotion ``warning:`` line the CLI printed."""
    return [
        (ast.literal_eval(match["repo"]), match["why"])
        for line in stderr.splitlines()
        if line.startswith("warning: ") and (match := _DEMOTION.fullmatch(line[len("warning: ") :]))
    ]


_LOAD_FRAME = "Cannot run commands while a repo fails to load (see warnings above)."


def _assert_cli_says(case: Case, verdict: Exception, run: CliRun) -> None:
    """The CLI printed the library's facts, in today's words."""
    from otto.session import (
        DependencyRefusedError,
        LabBuildError,
        ProjectSelectionError,
        RepoLoadError,
    )

    out = run.stdout + run.stderr
    if isinstance(verdict, RepoLoadError):
        # The payload first: the fatal repos and the demotions the error carries.
        assert [Path(error.sut_dir).name for error in verdict.errors] == case.fatal
        assert [d.repo for d in verdict.demoted] == case.demoted
        for error in verdict.errors:
            assert f"warning: {error}\n" in run.stderr
        assert _LOAD_FRAME in _flat(run.stdout)
        # Each carried demotion is the CLI's warning line, printed BEFORE the frame.
        assert _cli_demotions(run.stderr) == _library_demotions(verdict.demoted)
        frame_at = _flat(run.output).index(_LOAD_FRAME)
        for repo, why in _library_demotions(verdict.demoted):
            assert _flat(run.output).index(f"repo {repo!r} failed to load, but is") < frame_at
            assert why in _flat(run.output)[:frame_at]
    elif isinstance(verdict, DependencyRefusedError):
        [bad] = verdict.unsatisfied
        assert (bad.repo, bad.requirement) == (case.unmet, _UNMET_REQUIREMENT)
        assert _squashed(f"error: {verdict}") in _squashed(out)
        assert _squashed("fix: otto env sync") in _squashed(out)
        assert _squashed(f"or: uv pip install {bad.requirement!r}") in _squashed(out)
    elif isinstance(verdict, ProjectSelectionError):
        assert (verdict.kind, verdict.names) == ("unknown", case.include)
        assert _squashed(str(verdict)) in _squashed(out)
        assert f"no project {case.include[0]!r}" in _flat(out)
    elif isinstance(verdict, LabBuildError) and verdict.kind == "unknown_lab":
        # The boundary frame: a line opening `error: `, then the library's message
        # verbatim to the end of stdout. Squashed, because rich folds the message at
        # 80 columns and hard-breaks the long tmp_path it quotes mid-token.
        lines = run.stdout.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("error: ")), None)
        assert start is not None, run
        assert _squashed("\n".join(lines[start:])) == _squashed(f"error: {verdict}")
        assert run.stderr == ""
    elif isinstance(verdict, LabBuildError) and verdict.kind == "no_labs":
        assert verdict.field == "labs"
        assert run.stderr == "Error: Missing option '--lab' / '-l' (env var: 'OTTO_LAB').\n"
    else:  # pragma: no cover — a new refusal joined the table without its rendering check
        pytest.fail(f"no rendering check for {verdict!r}")


def _assert_cli_warns(case: Case, verdict: LibraryVerdict, run: CliRun) -> None:
    """A run that goes ahead: the library's dependency warnings are the CLI's lines."""
    if case.unmet is not None:
        inactive = [w for w in verdict.warnings if w.startswith(f"repo {case.unmet!r} requires")]
        assert len(inactive) == 1, verdict.warnings
        assert "is inactive for this run" in inactive[0]
    stderr_lines = run.stderr.splitlines()
    for warning in verdict.warnings:
        assert f"warning: {warning}" in stderr_lines, run


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_each_refusal_is_the_librarys_in_todays_words(case, tmp_path, monkeypatch):
    world = case.world(tmp_path)
    _install(monkeypatch, world.repos, world.errors)
    if case.unmet is not None:
        _arm_unmet_dependency(monkeypatch, case.unmet)
    verdict = asyncio.run(_open_context(case))
    run = _otto(monkeypatch, _argv(case))
    # Both sides in one comparison, so a divergence shows what EACH path decided.
    library_raised = type(verdict.error).__name__ if verdict.error is not None else None
    expected = case.raises.__name__ if case.raises is not None else None
    assert (library_raised, run.code) == (expected, case.code), (verdict, run)
    if verdict.error is not None:
        _assert_cli_says(case, verdict.error, run)
    else:
        _assert_cli_warns(case, verdict, run)


# ── 3. Demotion parity ───────────────────────────────────────────────────────

DEMOTIONS = [case for case in CASES if case.raises is None and case.demoted]


@pytest.mark.parametrize("case", DEMOTIONS, ids=lambda case: case.id)
def test_the_librarys_demotions_are_the_clis_warning_lines(case, tmp_path, monkeypatch):
    from otto.session import check_repos, select_projects

    world = case.world(tmp_path)
    _install(monkeypatch, world.repos, world.errors)
    result = bs.bootstrap()
    selection = select_projects(result.repos, case.include, case.exclude)
    demoted = check_repos(result, case.labs, selection).demoted
    assert [d.repo for d in demoted] == case.demoted
    library = _library_demotions(demoted)

    run = _otto(monkeypatch, _argv(case))
    assert run.code == 0, run
    assert _cli_demotions(run.stderr) == library
