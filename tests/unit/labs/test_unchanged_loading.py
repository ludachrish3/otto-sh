"""Today's configured loading, ``--list-labs``, the doctor and the NetBox snapshot, pinned.

Lab templating (#1176) adds named inventories and qualified references. A
project that uses neither must load, list and doctor exactly as it did
before, and a NetBox snapshot written before the upgrade must still be
served. This module pins those four behaviours as committed golden
artifacts, generated ONCE with the code that predates the change:

* ``tests/_fixtures/unchanged_loading.json``: every lab of five configured
  setups, built through :func:`otto.session.lab.build_lab`, projected host by
  host;
* ``tests/_fixtures/unchanged_list_labs.txt``: ``otto --list-labs`` for those
  setups and for the edge cases its panels handle today;
* ``tests/_fixtures/unchanged_doctor.json``: :func:`otto.init.doctor.check_project`
  for three two-project workspaces;
* ``tests/_fixtures/legacy_snapshot/``: a three-device NetBox snapshot and its
  meta under the filenames today's :class:`~otto.inventory.cache.SnapshotCache`
  chose, with ``recorded.json`` holding the ``[inventory]`` table that named
  them.

Generation and comparison run the SAME projection functions, so the only
thing a comparison can catch is a change in otto. ``python -m
tests.unit.labs.test_unchanged_loading`` rewrites the artifacts; doing so
after the change landed would pin the new behaviour as "unchanged", so a
red test here needs a maintainer ruling, never a regeneration.

Every artifact is portable across checkouts: the checkout root is written as
``<root>``, and nothing a test's temporary directory names is recorded.
"""

import dataclasses
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests

import otto
from otto.models.base import OttoModel
from otto.registry import FrozenMap
from tests._fixtures.env_guard import preserved_env_var
from tests._fixtures.fake_project import fake_project
from tests._fixtures.lab_projection import lab_projection, register_fixture_host_extensions
from tests._fixtures.labdata import json_lab_sources, lab_data_dir
from tests._fixtures.netbox_replay import LOGICAL_URL, RequestTrap, route_requests_to
from tests._fixtures.paths import GETTING_STARTED_INVENTORY_GOLDEN, TECH1_GOLDEN
from tests._fixtures.portable import portable

ROOT = Path(otto.__file__).resolve().parents[2]
"""The checkout root, written ``<root>`` in every artifact."""

FIXTURES = ROOT / "tests" / "_fixtures"
LOADING_GOLDEN = FIXTURES / "unchanged_loading.json"
LISTING_GOLDEN = FIXTURES / "unchanged_list_labs.txt"
DOCTOR_GOLDEN = FIXTURES / "unchanged_doctor.json"
LEGACY_SNAPSHOT = FIXTURES / "legacy_snapshot"
TWIN = ROOT / "docs" / "examples" / "getting-started-inventory"
TWIN_LAB = GETTING_STARTED_INVENTORY_GOLDEN
"""The twin's ``lab_data/lab.json`` as it was before its template replaced it."""
TECH1_INVENTORY = lab_data_dir() / "tech1-inventory"
TECH1_LEGACY = TECH1_GOLDEN
"""tech1's hand-written JSON lab file, as it was before its Jsonnet entry replaced it.

The bed's golden (:data:`tests._fixtures.paths.TECH1_GOLDEN`): an edit to the
bed updates it, and with it this setup's captured outputs."""

_COLUMNS = "160"
"""The console width ``--list-labs`` renders at, so its bytes do not depend on a terminal."""

_FETCHED_AT = datetime(2026, 11, 19, tzinfo=timezone.utc)
"""The clock the snapshot generator runs under, so its meta is byte-stable across runs."""

_EXAMPLE_BACKEND = "unchanged-example"
_PROFILED_BACKEND = "unchanged-profiled"
_UNREGISTERED = "unchanged-unregistered"

# --------------------------------------------------------------------------- #
# Environment: one otto home per scenario, registrations undone on exit.
# --------------------------------------------------------------------------- #


@contextmanager
def _env(**values: "str | None") -> Iterator[None]:
    """Set (or, for ``None``, unset) environment variables for the block, then restore them."""
    with ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(preserved_env_var(name, absent_during=value is None))
            if value is not None:
                os.environ[name] = value
        yield


@contextmanager
def _otto_home(home: Path, *, settings: "str | None" = None) -> Iterator[Path]:
    """Point ``OTTO_HOME`` at *home* (writing *settings* as its ``settings.toml``)."""
    from otto.config.user_settings import clear_user_settings_memo

    home.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        user_file = home / "settings.toml"
        user_file.write_text(settings)  # scaffold-exempt: the otto home's user file
    clear_user_settings_memo()
    try:
        with _env(OTTO_HOME=str(home)):
            yield home
    finally:
        clear_user_settings_memo()


@contextmanager
def _registered(name: str, *, config: type, factory: Callable[..., Any]) -> Iterator[None]:
    """Register lab-source backend *name* for the block, and unregister it after."""
    from otto.labs import register_lab_repository
    from otto.labs.registry import LAB_REPOSITORIES

    register_lab_repository(name, config=config, factory=factory)
    try:
        yield
    finally:
        LAB_REPOSITORIES.unregister(name=name)


@contextmanager
def _test_backends() -> Iterator[None]:
    """The two lab-source backends the scenarios select: the example one and a profiled one."""
    from otto.examples.lab_repository import ExampleLabSourceConfig, example_lab_source

    with (
        _registered(_EXAMPLE_BACKEND, config=ExampleLabSourceConfig, factory=example_lab_source),
        _registered(_PROFILED_BACKEND, config=_ProfiledConfig, factory=_profiled_source),
    ):
        yield


class _ProfiledConfig(OttoModel):
    """A lab source that takes no options."""


class _ProfiledRepository:
    """Lists ``needs-x`` only when the profiles it was built with hold profile ``x``."""

    def __init__(self, *, has_x: bool) -> None:
        self._labs = ["base", "needs-x"] if has_x else ["base"]

    def list_labs(self) -> list[str]:
        return list(self._labs)

    def load_lab(self, name: str, **_: Any) -> Any:
        from otto.labs import LabNotFoundError

        raise LabNotFoundError(f"{name!r} is listed only")


def _profiled_source(c: Any) -> _ProfiledRepository:
    return _ProfiledRepository(has_x="x" in c.env.profiles.profiles)


# --------------------------------------------------------------------------- #
# The configured projects.
# --------------------------------------------------------------------------- #


def _toml_tables(path: Path) -> dict[str, Any]:
    import tomli

    return tomli.loads(path.read_text())


def _json_project(name: str, root: Path, paths: list[Path], **settings: Any) -> Any:
    return fake_project(
        name,
        root=root,
        lab_sources=json_lab_sources(root, paths, name=name),
        settings=dict(settings),
    )


def _creds_user_file() -> str:
    """A user ``settings.toml`` holding ``tech1-inventory``'s ``[creds]`` table, path absolute.

    The split ``test_inventory_equivalence`` builds from ``user-settings.toml``,
    with ``[inventory]`` moved to the project: the user file keeps ``[creds]``.
    """
    creds = _toml_tables(TECH1_INVENTORY / "user-settings.toml")["creds"]
    path = (TECH1_INVENTORY / creds["path"]).resolve()
    return f"[creds]\nbackend = {json.dumps(creds['backend'])}\npath = {json.dumps(str(path))}\n"


def _example_source(root: Path) -> Any:
    from otto.labs.sources import PendingLabSource

    raw = {
        "labs": {
            "ex": [
                {
                    "name": "test1",
                    "metadata": {"role": "referenced"},
                    "hosts": [
                        {
                            "inventory": "test1",
                            "os_type": "unix",
                            "creds": [{"login": "vagrant"}, {"login": "test"}],
                        }
                    ],
                },
                {
                    "name": "inline1",
                    "hosts": [{"ip": "10.9.0.1", "creds": [{"login": "admin", "password": "a"}]}],
                },
            ]
        },
        "resources": {"ex": ["test1"]},
    }
    return PendingLabSource(
        backend=_EXAMPLE_BACKEND,
        label="example/example#1",
        raw=FrozenMap.freeze_json(value=raw),
        origin=str(root / ".otto" / "settings.toml"),
        project_root=root,
    )


@dataclasses.dataclass(frozen=True)
class _Setup:
    """One scenario: the selected projects, and what their process sees besides."""

    projects: list[Any]
    """The projects, in ``OTTOPATH`` order."""

    user_file: "str | None" = None
    """The otto home's ``settings.toml`` text; ``None`` for no user file."""

    test_backends: bool = False
    """Whether a project selects a lab-source backend this module registers."""


@contextmanager
def _entered(setup: _Setup, *, home: Path) -> Iterator[None]:
    """The process *setup* describes: its otto home at *home*, its backends registered.

    The backends are registered only where the setup selects them: an
    unregistered backend's message lists the registered ones, and must not
    list this module's.
    """
    with (
        _test_backends() if setup.test_backends else nullcontext(),
        _otto_home(home, settings=setup.user_file),
    ):
        yield


def _configurations() -> "dict[str, _Setup]":
    """Each configured setup, by name."""
    tech1_inventory_table = _toml_tables(TECH1_INVENTORY / "user-settings.toml")["inventory"]
    twin = _toml_tables(TWIN / ".otto" / "settings.toml")
    return {
        # tech1's lab file is a Jsonnet entry now; its frozen JSON copy is the
        # legacy fixture that stays loading unchanged.
        "tech1": _Setup([_json_project("tech1", lab_data_dir() / "tech1", [TECH1_LEGACY])]),
        "tech2": _Setup(
            [_json_project("tech2", lab_data_dir() / "tech2", [lab_data_dir() / "tech2"])]
        ),
        "tech1-inventory": _Setup(
            [
                _json_project(
                    "tech1-inventory",
                    TECH1_INVENTORY,
                    [TECH1_INVENTORY],
                    inventory=tech1_inventory_table,
                )
            ],
            user_file=_creds_user_file(),
        ),
        "getting-started-inventory": _Setup(
            [
                _json_project(
                    "gs",
                    TWIN,
                    [TWIN_LAB],
                    inventory=twin["inventory"],
                    creds=twin["creds"],
                )
            ]
        ),
        "example-source": _Setup(
            [
                fake_project(
                    "example",
                    root=TECH1_INVENTORY,
                    lab_sources=[_example_source(TECH1_INVENTORY)],
                    settings={"inventory": tech1_inventory_table},
                )
            ],
            user_file=_creds_user_file(),
            test_backends=True,
        ),
    }


# --------------------------------------------------------------------------- #
# Loading.
# --------------------------------------------------------------------------- #


def loading(workdir: Path) -> dict[str, Any]:
    """Build every lab of every configured setup through ``build_lab``; project each."""
    from otto.labs.sources import build_lab_sources
    from otto.session.lab import build_lab

    register_fixture_host_extensions()
    result: dict[str, Any] = {}
    for name, setup in _configurations().items():
        with _entered(setup, home=workdir / "homes" / name):
            labs = build_lab_sources(projects=setup.projects).list_labs()
            result[name] = {
                lab: lab_projection(build_lab(setup.projects, labs=[lab])) for lab in sorted(labs)
            }
    return result


# --------------------------------------------------------------------------- #
# --list-labs.
# --------------------------------------------------------------------------- #


def _listing_cases(workdir: Path) -> "dict[str, _Setup]":
    """Every ``--list-labs`` case, by name: the configured setups and today's edge cases."""
    from otto.labs.sources import PendingLabSource
    from otto.models.settings import OsProfileSpec
    from tests._fixtures.labdata import write_lab_json

    def pending(name: str, backend: str) -> Any:
        # A placeholder root: nothing here reads it, and the panel text is then
        # the same on every machine.
        root = Path("/nonexistent/otto-fake-project") / name
        return PendingLabSource(
            backend=backend,
            label=f"{name}/{backend}#1",
            raw=FrozenMap.freeze_json(value={}),
            origin=str(root / ".otto" / "settings.toml"),
            project_root=root,
        )

    tech2 = lab_data_dir() / "tech2"
    siblings = workdir / "siblings"
    write_lab_json(
        siblings / "good.json",
        [{"ip": "10.8.0.1", "element": "good1", "labs": ["good"], "creds": [{"login": "u"}]}],
    )
    (siblings / "broken.json").write_text('{"labs": {"broken": {}}, "elements": [')
    cases = dict(_configurations())
    cases["unregistered-inventory-inline-lab"] = _Setup(
        [_json_project("inline", tech2, [tech2], inventory={"backend": _UNREGISTERED})]
    )
    cases["unregistered-source-beside-a-good-one"] = _Setup(
        [
            _json_project("a", tech2, [tech2]),
            fake_project("b", lab_sources=[pending("b", _UNREGISTERED)]),
        ]
    )
    cases["no-lab-sources"] = _Setup([fake_project("bare")])
    # B declares profile ``x``; A's panel is built for A alone.
    cases["profile-declared-by-another-project"] = _Setup(
        [
            fake_project("a", lab_sources=[pending("a", _PROFILED_BACKEND)]),
            fake_project(
                "b",
                root=tech2,
                lab_sources=json_lab_sources(tech2, [tech2], name="b"),
                os_profiles={"x": OsProfileSpec.model_validate({"base": "unix"})},
            ),
        ],
        test_backends=True,
    )
    cases["malformed-sibling-file"] = _Setup(
        [_json_project("siblings", siblings, [siblings / "good.json", siblings / "broken.json"])]
    )
    cases["malformed-user-settings"] = _Setup(
        [_json_project("inline", tech2, [tech2])], user_file="[inventory\nthis is not toml"
    )
    return cases


def listing(workdir: Path) -> str:
    """``otto --list-labs`` for every listing case, each under a ``== <case> ==`` header."""
    from typer.testing import CliRunner

    from otto.cli.main import app

    register_fixture_host_extensions()
    chunks: list[str] = []
    with _env(COLUMNS=_COLUMNS, FORCE_COLOR=None, TTY_COMPATIBLE=None):
        for name, setup in _listing_cases(workdir).items():
            with (
                _entered(setup, home=workdir / "list-homes" / name),
                mock.patch("otto.declared.get_projects", return_value=setup.projects),
                # A fresh global console, so it reads COLUMNS rather than the
                # width an earlier test's console settled on.
                mock.patch("rich._console", None),
            ):
                result = CliRunner().invoke(app, ["--list-labs"])
            chunks.append(f"== {name} (exit {result.exit_code}) ==\n{result.output}")
    return "".join(chunks)


# --------------------------------------------------------------------------- #
# The doctor.
# --------------------------------------------------------------------------- #


def _write_project(
    root: Path,
    *,
    name: str,
    references: list[str],
    inventory: "dict[str, Any] | None" = None,
    creds: "dict[str, list[dict[str, str]]] | None" = None,
) -> None:
    """A project on disk: one json lab whose hosts reference *references*, bare.

    *inventory* and *creds*, when given, are the records of the project's own
    ``[inventory]`` and ``[creds]`` json files.
    """
    from tests._fixtures.project_scaffold import make_project

    tables = ['[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n']
    files: dict[str, str] = {}
    if inventory is not None:
        tables.append('[inventory]\nbackend = "json"\npath = "inventory.json"\nsupplies = ["ip"]\n')
        files["inventory.json"] = json.dumps(inventory, indent=2)
    if creds is not None:
        tables.append('[creds]\nbackend = "json"\npath = "creds.json"\n')
        files["creds.json"] = json.dumps(creds, indent=2)
    elements = [
        {
            "name": key,
            "labs": ["bench"],
            "hosts": [
                {"inventory": key, "os_type": "unix", "creds": [{"login": "u", "password": "p"}]}
            ],
        }
        for key in references
    ]
    files["lab/lab.json"] = json.dumps({"labs": {"bench": {}}, "elements": elements}, indent=2)
    make_project(root, name=name, version="0.1.0", extra="\n".join(tables), files=files)


_A_RECORDS = {"k1": {"ip": "10.7.0.1"}, "k2": {"ip": "10.7.0.2"}}


def _workspaces(workdir: Path) -> "dict[str, dict[str, Path]]":
    """The three two-project workspaces, written to disk; each project's root by its name."""
    spaces: dict[str, dict[str, Path]] = {}
    # (a) B declares nothing and references A's keys bare.
    a, b = workdir / "a" / "A", workdir / "a" / "B"
    _write_project(a, name="proj-a", references=["k1"], inventory=_A_RECORDS)
    _write_project(b, name="proj-b", references=["k2"])
    spaces["a-bare-references-without-inventory"] = {"A": a, "B": b}
    # (a') B declares only [creds].
    a, b = workdir / "a2" / "A", workdir / "a2" / "B"
    _write_project(a, name="proj-a", references=["k1"], inventory=_A_RECORDS)
    _write_project(
        b, name="proj-b", references=["k2"], creds={"k2": [{"login": "u", "password": "p"}]}
    )
    spaces["a2-creds-without-inventory"] = {"A": a, "B": b}
    # (b) A and B declare different [inventory] tables.
    a, b = workdir / "b" / "A", workdir / "b" / "B"
    _write_project(a, name="proj-a", references=["k1"], inventory=_A_RECORDS)
    _write_project(b, name="proj-b", references=["m1"], inventory={"m1": {"ip": "10.6.0.1"}})
    spaces["b-different-inventories"] = {"A": a, "B": b}
    return spaces


_DOCTOR_AREAS = frozenset({"settings", "lab"})
"""The areas an inventory declaration can move; the others are not this pin's business."""


def _report(root: Path) -> dict[str, Any]:
    from otto.init.doctor import check_project

    report = check_project(root)
    return {
        "ok": report.ok,
        "verdicts": {
            v.name: {"state": v.state, "problems": list(v.problems)}
            for v in report.verdicts
            if v.name in _DOCTOR_AREAS
        },
        "warnings": list(report.warnings),
        "inventory_label": report.inventory_label,
        "creds_label": report.creds_label,
    }


def doctor(workdir: Path) -> dict[str, Any]:
    """``check_project`` of each project of each workspace, made portable."""
    home = workdir / "doctor-home"
    result: dict[str, Any] = {}
    with _otto_home(home):
        for name, projects in _workspaces(workdir).items():
            roots = {f"<{label}>": root for label, root in projects.items()}
            roots |= {"<home>": home, "<root>": ROOT}
            text = json.dumps({label: _report(root) for label, root in projects.items()})
            result[name] = json.loads(portable(text, places=roots))
    return result


# --------------------------------------------------------------------------- #
# The NetBox snapshot.
# --------------------------------------------------------------------------- #

_LEGACY_TABLE = {"backend": "netbox", "url": LOGICAL_URL, "cache_ttl": "24h"}
"""The ``[inventory]`` table the legacy snapshot was written for, whole (its slug's input)."""


def _compiled_legacy(table: "dict[str, Any]") -> Any:
    from otto.inventory.config import compile_inventory
    from otto.models.settings import InventoryConfigSpec

    return compile_inventory(
        cfg=InventoryConfigSpec.model_validate(table),
        anchor_dir=ROOT,
        origin="tests/_fixtures/legacy_snapshot/recorded.json",
    )


def write_legacy_snapshot(workdir: Path) -> None:
    """Fetch three NetBox devices through today's cache and copy the pair it wrote."""
    from otto.inventory.cache import SnapshotCache
    from otto.inventory.config import construct_inventory
    from tests.unit.inventory.netbox_stub import TOKEN, NetBoxStub, device

    devices = [
        device(1, "nb1", ip="10.20.0.1/24"),
        device(2, "nb2", ip="10.20.0.2/24", rack="2", position=7.0),
        device(3, "nb3", ip="10.20.0.3/24", site="lab-b", platform="Debian"),
    ]
    home = workdir / "snapshot-home"
    with (
        _otto_home(home),
        _env(NETBOX_TOKEN=TOKEN),
        mock.patch("otto.inventory.cache._utc_now", return_value=_FETCHED_AT),
        NetBoxStub(devices, page_size=len(devices)) as stub,
        route_requests_to(stub=stub),
    ):
        inventory = construct_inventory(compiled=_compiled_legacy(_LEGACY_TABLE))
        assert isinstance(inventory, SnapshotCache), type(inventory)
        assert inventory.list_keys() == ["nb1", "nb2", "nb3"]
    assert stub.requests, "the generator must actually fetch"
    assert all(r.startswith("/api/dcim/devices/?") for r in stub.requests), stub.requests
    if LEGACY_SNAPSHOT.exists():
        shutil.rmtree(LEGACY_SNAPSHOT)
    LEGACY_SNAPSHOT.mkdir(parents=True)
    shutil.copyfile(inventory.snapshot_path, LEGACY_SNAPSHOT / inventory.snapshot_path.name)
    shutil.copyfile(inventory.meta_path, LEGACY_SNAPSHOT / inventory.meta_path.name)
    recorded = {
        "url": LOGICAL_URL,
        "inventory": _LEGACY_TABLE,
        "filenames": {"snapshot": inventory.snapshot_path.name, "meta": inventory.meta_path.name},
        "requests": stub.requests,
    }
    (LEGACY_SNAPSHOT / "recorded.json").write_text(json.dumps(recorded, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# The tests.
# --------------------------------------------------------------------------- #


def _no_temporary_path(text: str, tmp_path: Path) -> None:
    for spelled in {str(tmp_path), str(tmp_path.resolve())}:
        assert spelled not in text, f"{spelled} leaked into a recorded string"


def test_every_configured_lab_loads_as_before(tmp_path):
    got = loading(tmp_path)
    _no_temporary_path(json.dumps(got), tmp_path)
    assert got == json.loads(LOADING_GOLDEN.read_text())


def test_list_labs_prints_as_before(tmp_path):
    got = listing(tmp_path)
    _no_temporary_path(got, tmp_path)
    assert got.encode() == LISTING_GOLDEN.read_bytes()


def test_the_doctor_reports_as_before(tmp_path):
    got = doctor(tmp_path)
    _no_temporary_path(json.dumps(got), tmp_path)
    assert got == json.loads(DOCTOR_GOLDEN.read_text())


def test_the_profiled_backend_lists_needs_x_once_profile_x_is_selected(tmp_path):
    """The listing case means something: the backend does list ``needs-x`` with ``x`` in view."""
    from otto.labs.sources import build_lab_sources

    setup = _listing_cases(tmp_path)["profile-declared-by-another-project"]
    with _entered(setup, home=tmp_path / "home"):
        assert "needs-x" in build_lab_sources(projects=setup.projects).list_labs()


def test_the_legacy_snapshot_artifact_is_todays(tmp_path):
    from otto.inventory.config import construct_inventory
    from otto.inventory.snapshot import document_hash

    recorded = json.loads((LEGACY_SNAPSHOT / "recorded.json").read_text())
    names = recorded["filenames"]
    assert sorted(p.name for p in LEGACY_SNAPSHOT.iterdir()) == sorted(
        [names["snapshot"], names["meta"], "recorded.json"]
    )
    snapshot = json.loads((LEGACY_SNAPSHOT / names["snapshot"]).read_text())
    meta = json.loads((LEGACY_SNAPSHOT / names["meta"]).read_text())
    assert meta["sha256"] == document_hash(snapshot)
    # Every request the generator made was one devices list call (one page).
    assert recorded["requests"]
    assert all(r.startswith("/api/dcim/devices/?") for r in recorded["requests"])
    # The recorded table still names these files: construction does no I/O.
    with _otto_home(tmp_path / "home"):
        inventory = construct_inventory(compiled=_compiled_legacy(recorded["inventory"]))
    assert inventory.snapshot_path.name == names["snapshot"]
    assert inventory.meta_path.name == names["meta"]


def test_the_one_record_canonical_hash_is_pinned():
    from otto.inventory.snapshot import document_hash

    assert (
        document_hash({"a": {"ip": "10.0.0.1"}})
        == "24eff771f3beac7c1b8a24cdc7839288061c38edbc4061e67ec73b3eb7ecf969"
    )


def test_the_router_refuses_a_url_it_does_not_route():
    from tests.unit.inventory.netbox_stub import NetBoxStub

    with NetBoxStub([]) as stub, route_requests_to(stub=stub):
        with pytest.raises(
            AssertionError, match=r"refusing unrouted URL 'http://elsewhere\.invalid/x'"
        ):
            requests.Session().get("http://elsewhere.invalid/x")
        # Routed: the stub answered it (403, since no token was sent).
        assert requests.Session().get("http://netbox.invalid/api/nothing/").status_code == 403
    assert stub.requests == ["/api/nothing/"]


def test_the_request_trap_counts_and_refuses(monkeypatch):
    trap = RequestTrap(monkeypatch)
    with pytest.raises(requests.ConnectionError, match="RequestTrap"):
        requests.Session().get("http://netbox.invalid/api/dcim/devices/")
    assert (trap.attempts, trap.urls) == (1, ["http://netbox.invalid/api/dcim/devices/"])


def _generate() -> None:
    """Write every golden artifact from the code as it stands; see the module docstring."""
    with tempfile.TemporaryDirectory(prefix="otto-unchanged-") as scratch:
        workdir = Path(scratch)
        LOADING_GOLDEN.write_text(
            json.dumps(loading(workdir / "loading"), indent=2, sort_keys=True) + "\n"
        )
        LISTING_GOLDEN.write_bytes(listing(workdir / "listing").encode())
        DOCTOR_GOLDEN.write_text(
            json.dumps(doctor(workdir / "doctor"), indent=2, sort_keys=True) + "\n"
        )
        write_legacy_snapshot(workdir)
    sys.stdout.write("wrote the unchanged-loading artifacts\n")


if __name__ == "__main__":
    _generate()
