"""The one catalog of files no template may read (lab templating, #1176).

The catalog holds every active project's settings file, the user settings
file, and every string of their inventory and creds tables, anchored and
resolved. It is built from raw tables, never prepared, so a declaration no
operation uses cannot fail one that is used; nothing is opened to build it.
"""

import builtins
import dataclasses
import inspect
import io
import json
import logging
import os
import threading
from pathlib import Path

import pytest

from otto.config.scope import TOML_SETTINGS_PATH
from otto.config.user_settings import load_user_settings, user_settings_path
from otto.inventory import (
    InventoryConstructionError,
    InventoryDeclaration,
    InventoryEnv,
    InventoryError,
    InventoryView,
    ProtectedPaths,
    below_creds_overlay,
    build_inventory_view,
    build_inventory_view_from_declarations,
    compile_inventory,
    deferred_inventory_view,
    inventory_declaration,
    register_inventory_backend,
)
from otto.inventory import config as inventory_config
from otto.inventory.config import (
    build_named_inventory_from_declarations,
    protected_paths,
    standalone_protection,
)
from otto.models.base import OttoModel
from otto.models.settings import InventoryConfigSpec, NamedInventorySpec, UserSettingsModel
from tests._fixtures.fake_project import fake_project
from tests._fixtures.named_inventory import FakeInventory
from tests._fixtures.paths import TESTS_ROOT
from tests._fixtures.project_scaffold import touch_settings

from ._render_helpers import record_opens

_SNAPSHOT = TESTS_ROOT / "unit" / "api_snapshot" / "public_api.txt"
_UNEXPANDABLE = "~nosuchuser-otto-protected/creds.json"
_EMPTY = ProtectedPaths(files=frozenset())


def _project(tmp_path: Path, name: str, **settings):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return fake_project(name, root=root, settings=settings)


def _settings_file(project) -> Path:
    return project.root / TOML_SETTINGS_PATH


def _records(path: Path) -> str:
    path.write_text(json.dumps({"k": {"ip": "10.0.0.1"}}))
    return str(path)


def _user_file(tmp_path: Path, body: str) -> Path:
    """Write *body* as a user settings file, passed to the catalog by path (its name is free)."""
    home = tmp_path / "user-home"
    home.mkdir(exist_ok=True)
    written = home / "user.toml"
    written.write_text(body)
    return written


def _declaration(tmp_path: Path, **tables) -> InventoryDeclaration:
    root = tmp_path / "decl"
    root.mkdir(exist_ok=True)
    return InventoryDeclaration(origin=str(root / TOML_SETTINGS_PATH), anchor_dir=root, **tables)


def _catalog(*, declarations=(), settings_files=(), user_settings=None, user_settings_file=None):
    return protected_paths(
        settings_files=list(settings_files),
        declarations=list(declarations),
        user_settings=user_settings,
        user_settings_file=user_settings_file,
    )


def test_catalog_holds_a_settings_only_projects_file(tmp_path):
    bare = _project(tmp_path, "bare")
    declaring = _project(tmp_path, "declaring", inventory={"backend": "json", "path": "inv.json"})
    absent = tmp_path / "absent.toml"
    for view in (
        build_inventory_view(projects=[bare, declaring], user_settings_path=absent),
        deferred_inventory_view(projects=[bare, declaring], user_settings_path=absent),
    ):
        catalog = view.protected()
        assert catalog.contains(_settings_file(bare))
        assert catalog.contains(_settings_file(declaring))
        assert catalog.contains(declaring.root / "inv.json")


def test_catalog_holds_the_default_user_settings_path():
    assert _catalog().contains(user_settings_path())
    assert deferred_inventory_view(projects=[]).protected().contains(user_settings_path())


def test_catalog_holds_user_file_inventory_and_creds_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    user_file = _user_file(
        tmp_path,
        '[inventory]\nbackend = "json"\npath = "inv.json"\n'
        '[creds]\nbackend = "json"\npath = "~/vault/creds.json"\n'
        '[[inventories]]\nname = "lab"\nbackend = "json"\npath = "named.json"\n'
        '[inventories.creds]\nbackend = "json"\npath = "/abs/lab-creds.json"\n',
    )
    catalog = deferred_inventory_view(projects=[], user_settings_path=user_file).protected()
    assert catalog.contains(user_file)
    assert catalog.contains(user_file.parent / "inv.json")
    assert catalog.contains(tmp_path / "home" / "vault" / "creds.json")
    assert catalog.contains(user_file.parent / "named.json")
    assert catalog.contains(Path("/abs/lab-creds.json"))


def test_catalog_holds_another_projects_creds_file(tmp_path):
    mine = _project(tmp_path, "mine")
    theirs = _project(
        tmp_path,
        "theirs",
        inventory={"backend": "json", "path": "inv.json"},
        creds={"backend": "json", "path": "secrets/creds.json"},
    )
    view = deferred_inventory_view(projects=[mine, theirs], user_settings_path=tmp_path / "no")
    assert (
        view.for_project(root=mine.root)
        .protected()
        .contains(theirs.root / "secrets" / "creds.json")
    )


def test_nested_third_party_path_is_protected(tmp_path):
    project = _project(
        tmp_path,
        "p",
        inventories=[
            {
                "name": "tp",
                "backend": "unregistered-third-party",
                "auth": {"files": [{"key": "keys/id.pem"}, "keys/other.pem"]},
            }
        ],
    )
    catalog = deferred_inventory_view(projects=[project], user_settings_path=tmp_path / "no")
    assert catalog.protected().contains(project.root / "keys" / "id.pem")
    assert catalog.protected().contains(project.root / "keys" / "other.pem")


def test_broken_unused_declaration_fails_nothing(tmp_path):
    project = _project(
        tmp_path,
        "p",
        inventory={"backend": "json", "path": _records(tmp_path / "inv.json")},
        inventories=[{"name": "broken", "backend": "no-such-backend", "path": "x.json"}],
    )
    view = deferred_inventory_view(projects=[project], user_settings_path=tmp_path / "no")
    assert view.protected().contains(project.root / "x.json")
    assert view.select_name(name="default").lookup("k").ip == "10.0.0.1"
    with pytest.raises(InventoryConstructionError, match="no-such-backend"):
        view.select_name(name="broken")


def test_deferred_view_reads_the_user_file_once(tmp_path, monkeypatch):
    user_file = _user_file(
        tmp_path,
        f'[[inventories]]\nname = "usr"\nbackend = "json"\n'
        f"path = {json.dumps(_records(tmp_path / 'usr.json'))}\n",
    )
    project = _project(
        tmp_path, "p", inventory={"backend": "json", "path": _records(tmp_path / "inv.json")}
    )
    reads: list[Path | None] = []
    real = inventory_config._read_user_settings

    def counting(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(inventory_config, "_read_user_settings", counting)
    view = deferred_inventory_view(projects=[project], user_settings_path=user_file)
    assert reads == []
    assert view.protected().contains(tmp_path / "usr.json")
    assert view.names() == ["default", "usr"]
    view.select_name(name="default")
    view.select_name(name="usr")
    view.protected()
    assert reads == [user_file]


def test_symlink_alias_resolves_into_the_catalog(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    real = vault / "creds.json"
    real.write_text("{}")
    project = _project(
        tmp_path,
        "p",
        inventory={"backend": "json", "path": "inv.json"},
        creds={"backend": "json", "path": "link.json"},
    )
    (project.root / "link.json").symlink_to(real)
    catalog = deferred_inventory_view(
        projects=[project], user_settings_path=tmp_path / "no"
    ).protected()
    assert real.resolve() in catalog.files
    assert catalog.contains(real)
    assert catalog.contains(project.root / "link.json")


class _CaptureConfig(OttoModel, frozen=True):
    path: str


class _EnvCapturing(FakeInventory):
    """An inventory that keeps the :class:`InventoryEnv` it was built with."""

    def __init__(self, env: InventoryEnv) -> None:
        super().__init__({})
        self.env = env

    def fingerprint(self) -> str:
        return "fixed"


def _register_capture() -> None:
    register_inventory_backend(
        "capture:env",
        config=_CaptureConfig,
        factory=lambda configured: _EnvCapturing(configured.env),
        snapshot_cache=False,
    )


def test_every_view_builder_sets_the_same_catalog(tmp_path):
    _register_capture()
    plain = _project(tmp_path, "plain")
    declaring = _project(
        tmp_path,
        "declaring",
        inventory={"backend": "capture:env", "path": "inv.json"},
        creds={"backend": "json", "path": "creds.json"},
    )
    user_file = _user_file(
        tmp_path, '[[inventories]]\nname = "usr"\nbackend = "capture:env"\npath = "usr.json"\n'
    )
    projects = [plain, declaring]
    declarations = [
        d
        for p in projects
        if (d := inventory_declaration(data=p.settings, root=p.root, origin=str(_settings_file(p))))
    ]
    views = [
        build_inventory_view(projects=projects, user_settings_path=user_file),
        build_inventory_view_from_declarations(
            declarations=declarations,
            user_settings=load_user_settings(path=user_file),
            user_settings_file=user_file,
            settings_files=[_settings_file(p) for p in projects],
        ),
        deferred_inventory_view(projects=projects, user_settings_path=user_file),
    ]
    expected = views[0].protected()
    assert expected.contains(_settings_file(plain))
    assert expected.contains(declaring.root / "creds.json")
    assert expected.contains(user_file.parent / "usr.json")
    for view in views:
        assert view.protected() == expected
        assert view.for_project(root=plain.root).protected() == expected
        assert view.with_refusal_hook(hook=None).protected() == expected
        for name in ("default", "usr"):
            env = below_creds_overlay(inventory=view.select_name(name=name)).env
            assert env.protected is not None
            assert env.protected() == expected
    assert InventoryView(inventories={}).protected() == inventory_config.NO_CATALOG


def test_catalog_construction_opens_no_file(tmp_path, monkeypatch):
    user_file = _user_file(tmp_path, '[creds]\nbackend = "json"\npath = "creds.json"\n')
    snapshot = load_user_settings(path=user_file)
    declaration = _declaration(
        tmp_path, table={"backend": "json", "path": "inv.json"}, creds_table={"path": "c.json"}
    )
    project = _project(tmp_path, "p", inventory={"backend": "json", "path": "inv.json"})
    view = deferred_inventory_view(projects=[project], user_settings_path=user_file)
    view.names()  # the user file is read here, by its one owner

    def refuse(*args, **kwargs):
        raise AssertionError(f"the catalog opened {args[:1]!r}")

    with monkeypatch.context() as m:
        for owner, name in ((builtins, "open"), (io, "open"), (os, "open")):
            m.setattr(owner, name, refuse)
        catalog = _catalog(
            declarations=[declaration],
            settings_files=[tmp_path / "s.toml"],
            user_settings=snapshot,
            user_settings_file=user_file,
        )
        deferred = view.protected()
    assert catalog.contains(user_file.parent / "creds.json")
    assert deferred.contains(user_file.parent / "creds.json")


def test_non_path_strings_are_skipped(tmp_path, caplog):
    values = ["https://netbox.example.com/api/", "^lab-[0-9]+$", "tok_9f8e7d6c5b4a3210"]
    declaration = _declaration(
        tmp_path,
        table={"backend": "netbox", "url": values[0], "filter": {"name__re": values[1]}},
        inventories=[{"name": "nb", "backend": "netbox", "token": values[2]}],
    )
    with caplog.at_level(logging.DEBUG):
        catalog = _catalog(declarations=[declaration], user_settings_file=tmp_path / "u.toml")
    assert catalog.contains(Path(declaration.origin))
    for record in caplog.records:
        for value in values:
            assert value not in record.getMessage()
    env = InventoryEnv(
        declaration.anchor_dir,
        declaration.origin,
        protected=lambda: catalog,
        own_table=tuple(inventory_config._strings(declaration.inventories)),
    )
    assert values[2] in env.own_table
    for shown in (repr(catalog), repr(env)):
        for value in values:
            assert value not in shown
    assert repr(catalog) == f"ProtectedPaths(<{len(catalog.files)} files>)"


def test_unexpandable_home_is_skipped(tmp_path):
    project = _project(
        tmp_path,
        "p",
        inventory={"backend": "json", "path": _records(tmp_path / "inv.json")},
        inventories=[{"name": "unused", "backend": "json", "path": _UNEXPANDABLE}],
    )
    view = deferred_inventory_view(projects=[project], user_settings_path=tmp_path / "no")
    catalog = view.protected()
    assert catalog.contains(_settings_file(project))
    assert not any("nosuchuser" in str(path) for path in catalog.files)
    # The unrelated render half is pinned by the render tests; here, the unrelated build.
    assert view.select_name(name="default").lookup("k").ip == "10.0.0.1"


def test_malformed_unused_path_value_is_skipped(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    declaration = _declaration(
        tmp_path,
        table={"backend": "json", "path": "good.json"},
        inventories=[
            {"name": "nul", "backend": "json", "path": "bad\x00name.json"},
            {"name": "loop", "backend": "json", "path": str(loop / "creds.json")},
        ],
    )
    catalog = _catalog(declarations=[declaration], user_settings_file=tmp_path / "u.toml")
    assert catalog.contains(declaration.anchor_dir / "good.json")
    assert not any("\x00" in str(path) for path in catalog.files)
    assert not catalog.contains(Path("bad\x00name.json"))


def _protected_file(tmp_path: Path) -> tuple[Path, ProtectedPaths]:
    settings = touch_settings(tmp_path / "proj")
    return settings, ProtectedPaths(files=frozenset({settings.resolve()}))


def test_a_hardlink_to_a_protected_file_is_contained(tmp_path):
    settings, catalog = _protected_file(tmp_path)
    alias = tmp_path / "alias.toml"
    os.link(settings, alias)
    other = tmp_path / "other.toml"
    other.write_text("")
    assert catalog.contains(alias)
    assert not catalog.contains(other)
    assert not catalog.contains(tmp_path / "missing.toml")


def test_an_open_files_stat_is_matched_by_identity(tmp_path):
    settings, catalog = _protected_file(tmp_path)
    alias = tmp_path / "alias.toml"
    os.link(settings, alias)
    other = tmp_path / "other.toml"
    other.write_text("")
    with alias.open("rb") as held, other.open("rb") as unrelated:
        assert catalog.contains_stat(os.fstat(held.fileno()))
        assert not catalog.contains_stat(os.fstat(unrelated.fileno()))


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.write_text("")
    try:
        return (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_a_case_variant_spelling_is_contained_on_a_case_insensitive_filesystem(tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip(f"the filesystem under {tmp_path} is case-sensitive: no case variant exists")
    settings, catalog = _protected_file(tmp_path)
    variant = settings.parent / "SETTINGS.toml"
    assert variant.resolve() not in catalog.files  # the spelling alone does not match
    assert catalog.contains(variant)


def test_identity_checks_stat_each_catalog_file_at_most_once(tmp_path, monkeypatch):
    settings, _ = _protected_file(tmp_path)
    other_protected = tmp_path / "creds.json"
    other_protected.write_text("{}")
    candidate = tmp_path / "unrelated.json"
    candidate.write_text("{}")
    stats: list[str] = []
    real_stat = os.stat

    def counting(path, *args, **kwargs):
        stats.append(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    declaration = _declaration(tmp_path, creds_table={"path": str(other_protected)})
    catalog = _catalog(
        declarations=[declaration],
        settings_files=[settings, tmp_path / "gone.toml"],
        user_settings_file=tmp_path / "u.toml",
    )
    # Counted from here, not from construction: building the catalog resolves
    # each path, and resolving stats (realpath lstats on every Python, and
    # Path.resolve(strict=False) also stats its result on 3.11 and 3.12). The
    # claim is that identity is computed lazily, once: a construction that
    # filled the identity cache would leave the counts below at 0.
    expected = [str(settings.resolve()), str(other_protected.resolve())]
    monkeypatch.setattr(os, "stat", counting)
    for _ in range(3):
        assert not catalog.contains(candidate)
    for file in catalog.files:
        assert stats.count(str(file)) <= 1, file
    for file in expected:
        assert stats.count(file) == 1, file


def test_catalog_identity_cache_stays_out_of_equality_hash_and_repr(tmp_path):
    settings, catalog = _protected_file(tmp_path)
    fresh = ProtectedPaths(files=catalog.files)
    assert catalog.contains(tmp_path / "proj" / ".otto" / ".." / ".otto" / "settings.toml")
    os.link(settings, tmp_path / "alias")
    assert catalog.contains(tmp_path / "alias")  # fills the identity cache
    assert catalog == fresh
    assert hash(catalog) == hash(fresh)
    assert repr(catalog) == repr(fresh) == "ProtectedPaths(<1 file>)"


def test_catalog_is_a_hashable_value_with_union(tmp_path):
    a = ProtectedPaths(files=frozenset({tmp_path / "a"}))
    b = ProtectedPaths(files=frozenset({tmp_path / "b"}))
    joined = a.union(b)
    assert joined == ProtectedPaths(files=frozenset({tmp_path / "a", tmp_path / "b"}))
    assert hash(joined) == hash(b.union(a))
    assert _EMPTY.union(a) == a


def test_no_catalog_is_the_catalogs_own_answer_and_survives_a_copy_and_a_pickle(tmp_path):
    """Whether a catalog may render is a field, not an identity: a copy refuses as the original."""
    import copy
    import pickle

    from otto.inventory.config import NO_CATALOG
    from otto.templating.errors import TemplatingError
    from otto.templating.imports import ImportPolicy

    assert NO_CATALOG != _EMPTY, "no catalog never equals an empty one"
    assert repr(NO_CATALOG) == "ProtectedPaths(<no catalog>)"
    assert NO_CATALOG.union(_EMPTY).refusal == NO_CATALOG.refusal
    for clone in (
        copy.copy(NO_CATALOG),
        copy.deepcopy(NO_CATALOG),
        pickle.loads(pickle.dumps(NO_CATALOG)),  # noqa: S301 — round trip of otto's own sentinel
    ):
        assert clone is not NO_CATALOG
        assert clone == NO_CATALOG
        with pytest.raises(TemplatingError, match="no protected-file catalog"):
            ImportPolicy(roots=(tmp_path,), protected=clone)
    ImportPolicy(roots=(tmp_path,), protected=copy.deepcopy(_EMPTY))  # a real catalog renders


def test_an_unreadable_user_file_gives_a_catalog_that_renders_nothing(tmp_path):
    """No partial catalog stands in for the complete one, and the refusal quotes no value."""
    from otto.templating.errors import TemplatingError
    from otto.templating.imports import ImportPolicy

    user_file = tmp_path / "settings.toml"
    # invalid: [lab] is a project table
    user_file.write_text(  # scaffold-exempt: the unreadable USER file is the subject
        '[creds]\nbackend = "json"\npath = "creds.json"\n\n[lab]\nname = "s3cret"\n'
    )
    view = deferred_inventory_view(
        projects=[_single_flight_project(tmp_path)], user_settings_path=user_file
    )
    catalog = view.protected()
    assert catalog.refusal == (
        f"no protected-file catalog: the user settings file {user_file} cannot be read, so the "
        "files it names cannot be protected; no template is rendered until it reads"
    )
    assert catalog.contains(user_file), "the files it does know stay protected"
    with pytest.raises(TemplatingError) as refused:
        ImportPolicy(roots=(tmp_path,), protected=catalog)
    assert "s3cret" not in str(refused.value)
    assert "Extra inputs" not in str(refused.value)  # the read error is never quoted


def test_public_compile_inventory_call_shape_unchanged(tmp_path):
    compiled = compile_inventory(
        cfg=InventoryConfigSpec(backend="json", path="inv.json"),
        anchor_dir=tmp_path,
        origin=str(tmp_path / "settings.toml"),
    )
    env = compiled.prepared.env
    assert env.protected is None
    assert "inv.json" in env.own_table
    assert env == InventoryEnv(tmp_path, str(tmp_path / "settings.toml"))
    parameters = inspect.signature(compile_inventory).parameters
    assert list(parameters) == ["cfg", "anchor_dir", "origin"]
    assert {p.kind for p in parameters.values()} == {inspect.Parameter.KEYWORD_ONLY}
    line = "call\totto.inventory:compile_inventory\tsync\tKO:cfg:- KO:anchor_dir:- KO:origin:-"
    assert line in _SNAPSHOT.read_text().splitlines()


def _vms_entry() -> NamedInventorySpec:
    return NamedInventorySpec(
        name="vms",
        backend="json",
        path="inv.json",
        creds={"backend": "json", "path": "nested/creds.json"},
    )


def test_standalone_policy_protects_the_origin_and_its_own_table_paths(tmp_path):
    settings = touch_settings(tmp_path)
    compiled = compile_inventory(cfg=_vms_entry(), anchor_dir=tmp_path, origin=str(settings))
    env = compiled.prepared.env
    assert env.settings_file == settings.resolve()
    catalog = standalone_protection(env)
    assert catalog.contains(settings)
    assert catalog.contains(tmp_path / "inv.json")
    assert catalog.contains(tmp_path / "nested" / "creds.json")


def test_a_relative_origin_is_resolved_at_compile_time(tmp_path, monkeypatch):
    settings = touch_settings(tmp_path)
    monkeypatch.chdir(tmp_path)
    env = compile_inventory(cfg=_vms_entry(), anchor_dir=tmp_path, origin=str(TOML_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path.parent)
    assert env.prepared.env.settings_file == settings.resolve()
    assert standalone_protection(env.prepared.env).contains(settings)


def test_a_public_compile_whose_origin_is_a_label_protects_its_table_alone(tmp_path):
    label = f"{tmp_path / 'settings.toml'} [[inventories]] 'vms'"
    env = compile_inventory(cfg=_vms_entry(), anchor_dir=tmp_path, origin=label).prepared.env
    assert env.settings_file is None
    catalog = standalone_protection(env)
    assert catalog.contains(tmp_path / "inv.json")
    assert not catalog.contains(tmp_path / "settings.toml")
    assert not any("[[inventories]]" in str(path) for path in catalog.files)


def test_a_named_entry_compiled_outside_a_view_protects_its_settings_file(tmp_path):
    _register_capture()
    project = _project(
        tmp_path,
        "p",
        inventories=[{"name": "vms", "backend": "capture:env", "path": "inv.json"}],
    )
    settings = _settings_file(project)
    declaration = inventory_declaration(
        data=project.settings, root=project.root, origin=str(settings)
    )
    assert declaration is not None
    inventory = build_named_inventory_from_declarations(
        name="vms", declarations=[declaration], user_settings=None
    )
    assert isinstance(inventory, _EnvCapturing)
    env = inventory.env
    assert env.protected is None
    assert env.origin != str(settings)  # the entry's label, not a path
    assert env.settings_file == settings
    catalog = standalone_protection(env)
    assert catalog.contains(settings)
    assert catalog.contains(project.root / "inv.json")


def test_standalone_policy_skips_non_path_strings(tmp_path):
    origin = tmp_path / "settings.toml"
    env = InventoryEnv(
        tmp_path, str(origin), own_table=(_UNEXPANDABLE, "bad\x00.json", "", "ok.json")
    )
    catalog = standalone_protection(env)
    assert catalog.files == frozenset({(tmp_path / "ok.json").resolve()})
    with_file = dataclasses.replace(env, settings_file=origin)
    assert standalone_protection(with_file).files == catalog.files | {origin.resolve()}


class _HeldRead:
    """Stand in for the user-file read: the first call blocks until released.

    The catalog's lock is wrapped so the test knows, without sleeping, that a
    second caller is waiting on it while the first read is still running.
    """

    def __init__(self, result: "UserSettingsModel | InventoryError") -> None:
        self.result = result
        self.reads = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.second_waiting = threading.Event()

    def read(self, path):
        del path
        self.reads += 1
        self.entered.set()
        assert self.release.wait(timeout=30)
        if isinstance(self.result, InventoryError):
            raise self.result
        return self.result

    def watch(self, catalog) -> None:
        lock = catalog._lock
        held = self
        attempts = []

        class _Watched:
            def __enter__(self):
                attempts.append(1)
                if len(attempts) == 2:
                    held.second_waiting.set()
                return lock.__enter__()

            def __exit__(self, *exc):
                return lock.__exit__(*exc)

        catalog._lock = _Watched()


def _run_concurrently(view, held: _HeldRead, first, second) -> list[object]:
    """Start *first* (the cold read), then *second* once it waits; return both outcomes."""
    outcomes: list[object] = [None, None]

    def call(index, fn):
        try:
            outcomes[index] = fn(view)
        except Exception as e:  # noqa: BLE001 - the outcome under test
            outcomes[index] = e

    threads = [
        threading.Thread(target=call, args=(0, first)),
        threading.Thread(target=call, args=(1, second)),
    ]
    threads[0].start()
    assert held.entered.wait(timeout=30)
    threads[1].start()
    assert held.second_waiting.wait(timeout=30)
    held.release.set()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return outcomes


def _single_flight_project(tmp_path: Path):
    return _project(
        tmp_path,
        "p",
        inventories=[{"name": "proj", "backend": "json", "path": _records(tmp_path / "p.json")}],
    )


def test_concurrent_cold_user_settings_read_is_single_flight(tmp_path, monkeypatch):
    user_dir = tmp_path / "user-home"
    user_dir.mkdir()
    user_file = user_dir / "settings.toml"
    snapshot = UserSettingsModel.model_validate(
        {
            "inventories": [
                {
                    "name": "usr",
                    "backend": "json",
                    "path": "usr.json",
                    "creds": {"backend": "json", "path": "usr-creds.json"},
                }
            ]
        }
    )
    creds = user_dir / "usr-creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "s3cret"}]}))
    held = _HeldRead(snapshot)
    monkeypatch.setattr(inventory_config, "_read_user_settings", held.read)
    # A project template whose import root holds the user file's creds: only the
    # complete snapshot's catalog refuses it.
    entry = tmp_path / "p" / "tmpl.jsonnet"
    entry.parent.mkdir(exist_ok=True)
    entry.write_text("{ k: { ip: importstr 'usr-creds.json' } }")
    templated = {"name": "tmpl", "backend": "json", "path": str(entry)}
    templated["import_paths"] = [str(user_dir)]
    proj = {"name": "proj", "backend": "json", "path": _records(tmp_path / "p.json")}
    project = _project(tmp_path, "p", inventories=[proj, templated])
    view = deferred_inventory_view(projects=[project], user_settings_path=user_file)
    held.watch(view.inventories)

    def build(view):
        return view.select_name(name="proj"), view.names()

    def protect_then_build(view):
        return view.protected(), view.select_name(name="tmpl"), build(view)

    first, second = _run_concurrently(view, held, build, protect_then_build)
    assert held.reads == 1
    assert isinstance(first, tuple), first
    assert isinstance(second, tuple), second
    assert first[1] == ["proj", "tmpl", "usr"]
    catalog, template, (_, names) = second
    assert names == ["proj", "tmpl", "usr"]
    assert catalog.contains(creds)
    assert view.protected() is catalog
    opened = record_opens(monkeypatch)
    with pytest.raises(InventoryError, match=r"refused \(a protected file\)") as caught:
        template.list_keys()
    assert not [p for p in opened() if p.name == creds.name]
    assert "s3cret" not in str(caught.value)


def test_user_file_read_error_is_single_flight_too(tmp_path, monkeypatch):
    error = InventoryError("user settings: broken")
    held = _HeldRead(error)
    monkeypatch.setattr(inventory_config, "_read_user_settings", held.read)
    user_file = tmp_path / "settings.toml"
    view = deferred_inventory_view(
        projects=[_single_flight_project(tmp_path)], user_settings_path=user_file
    )
    held.watch(view.inventories)

    def build(view):
        return view.select_name(name="proj")

    first, second = _run_concurrently(view, held, build, build)
    assert held.reads == 1
    assert first is error
    assert second is error
    assert view.protected().contains(user_file)
