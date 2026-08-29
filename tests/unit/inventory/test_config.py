"""[inventory] compilation and the one-inventory-per-process resolution (spec §8)."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.inventory import (
    CredsOverlay,
    InventoryDeclaration,
    InventoryError,
    JsonInventory,
    build_inventory,
    build_inventory_from_declarations,
    compile_inventory,
    construct_inventory,
)
from otto.models.settings import InventoryConfigSpec, UserSettingsModel


def _repo(tmp_path, name, table):
    root = tmp_path / name
    root.mkdir()
    return SimpleNamespace(sut_dir=root, inventory_settings=table)


def _inventory_file(dir_: Path, name="inventory.json"):
    p = dir_ / name
    p.write_text(json.dumps({"k": {"ip": "10.0.0.1"}}))
    return p


def _user_file(dir_: Path, body: str) -> Path:
    """Write a USER-level ``settings.toml`` — not a SUT repo's.

    ``make_sut_repo`` writes ``<root>/.otto/settings.toml`` inside a project;
    this is the per-user file that happens to share a basename. One writer, so
    the scaffold-policy exemption is stated once.
    """
    settings_file = dir_ / "settings.toml"
    settings_file.write_text(body)  # sutrepo-exempt: user-level ~/.otto file, not a SUT repo
    return settings_file


def test_json_requires_path_and_refuses_unknown_keys(tmp_path):
    with pytest.raises(
        InventoryError,
        match=r"origin\.toml: \[inventory\] backend 'json' requires a 'path' string",
    ):
        compile_inventory(
            InventoryConfigSpec(backend="json"), anchor_dir=tmp_path, origin="origin.toml"
        )
    with pytest.raises(InventoryError, match=r"unknown key\(s\) for the json backend: \['url'\]"):
        compile_inventory(
            InventoryConfigSpec(backend="json", path="i.json", url="x"),
            anchor_dir=tmp_path,
            origin="origin.toml",
        )


def test_json_supplies_must_be_a_list_of_field_names(tmp_path):
    with pytest.raises(
        InventoryError,
        match=r"o: \[inventory\] 'supplies' must be a list of record field names",
    ):
        compile_inventory(
            InventoryConfigSpec(backend="json", path="i.json", supplies="ip"),
            anchor_dir=tmp_path,
            origin="o",
        )
    out = compile_inventory(
        InventoryConfigSpec(backend="json", path="i.json", supplies=["ip"]),
        anchor_dir=tmp_path,
        origin="o",
    )
    assert out.kwargs["supplies"] == ["ip"]


def test_relative_paths_anchor_to_the_declaring_dir_and_tilde_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = InventoryConfigSpec(backend="json", path="lab/i.json", creds_file="~/creds.json")
    out = compile_inventory(cfg, anchor_dir=tmp_path / "repo", origin="o")
    assert out.kwargs["path"] == tmp_path / "repo" / "lab" / "i.json"
    assert out.creds_file == tmp_path / "creds.json"
    assert out.cache_ttl.total_seconds() == 24 * 3600


def test_other_backends_keep_their_kwargs_verbatim(tmp_path):
    cfg = InventoryConfigSpec(backend="netbox", url="https://nb", filter={"site": "a"})
    out = compile_inventory(cfg, anchor_dir=tmp_path, origin="o")
    assert out.kwargs == {"url": "https://nb", "filter": {"site": "a"}}


class _FakeBackend:
    """Stand-in for the netbox backend (Task 8): records what it was constructed with."""

    def __init__(self, *, repo_dir, url, token_env=None):
        self.repo_dir = repo_dir
        self.url = url
        self.token_env = token_env
        self.label = f"fake:{url}"
        self.supplies = frozenset({"ip"})

    def fingerprint(self):
        """A third-party backend that CAN report freshness — so it is never cached.

        Required by the protocol (spec §10) and read at construction since the
        snapshot cache landed: a backend returning a string opts out of the
        cache by design, which is what the ``isinstance(inv, _FakeBackend)``
        assertion below now also pins.
        """
        return f"fake:{self.url}"


def test_a_non_json_backend_gets_repo_dir_plus_its_own_kwargs(tmp_path):
    """The other construction arm: kwargs verbatim, plus ``repo_dir`` for anchoring.

    Registered here rather than waited for, because an arm no test reaches is
    an arm no test can catch breaking.
    """
    from otto.inventory import register_inventory_backend
    from otto.inventory.registry import INVENTORY_BACKENDS

    register_inventory_backend("fake-test", _FakeBackend)
    try:
        compiled = compile_inventory(
            InventoryConfigSpec(backend="fake-test", url="https://nb", token_env="NB_TOKEN"),
            anchor_dir=tmp_path,
            origin="o",
        )
        inv = construct_inventory(compiled)
        assert isinstance(inv, _FakeBackend)
        assert inv.repo_dir == tmp_path
        assert inv.url == "https://nb"
        assert inv.token_env == "NB_TOKEN"

        # A typo'd kwarg is the backend's own TypeError; it must reach the user
        # naming the settings file and the backend, as the json arm's does.
        bad = compile_inventory(
            InventoryConfigSpec(backend="fake-test", urll="https://nb"),
            anchor_dir=tmp_path,
            origin="r1/settings.toml",
        )
        with pytest.raises(
            InventoryError,
            match=r"r1/settings\.toml: \[inventory\] backend 'fake-test': "
            r".*unexpected keyword argument 'urll'",
        ):
            construct_inventory(bad)
    finally:
        INVENTORY_BACKENDS.unregister("fake-test")


class _CachedThirdParty:
    """A third-party backend otto WOULD wrap in the snapshot cache.

    ``fingerprint()`` returns ``None`` — the backend's own statement that it
    cannot report freshness, which is what lands it in the cache — and
    ``supplies`` is whatever the subclass says.
    """

    supplies = frozenset({"ip"})

    def __init__(self, repo_dir=None, *, url="https://cmdb"):
        self.repo_dir = repo_dir
        self.url = url
        self.label = f"cmdb:{url}"

    def lookup(self, key):
        raise InventoryError(f"{self.label}: nothing here")

    def list_keys(self):
        return []

    def fingerprint(self):
        return None


class _SuppliesCreds(_CachedThirdParty):
    """…and its own records carry credentials, which a snapshot cannot."""

    supplies = frozenset({"ip", "creds"})


@pytest.fixture
def registered_backend():
    """Register a backend class under a name for one test; unregister it after.

    ``overwrite=True`` so a re-run inside one session cannot fail on a
    leftover, and the teardown runs whatever the test did — the registry is
    process-global, and a name left behind changes what the NEXT test resolves.
    """
    from otto.inventory import register_inventory_backend
    from otto.inventory.registry import INVENTORY_BACKENDS

    names: list[str] = []

    def _register(name, cls):
        register_inventory_backend(name, cls, overwrite=True)
        names.append(name)

    try:
        yield _register
    finally:
        for name in names:
            INVENTORY_BACKENDS.unregister(name)


def _third_party(tmp_path, ttl, *, creds_file=None):
    return compile_inventory(
        InventoryConfigSpec(backend="cmdb", cache_ttl=ttl, creds_file=creds_file),
        anchor_dir=tmp_path,
        origin="o",
    )


def test_a_backend_that_supplies_creds_is_never_snapshot_cached(
    tmp_path, monkeypatch, registered_backend
):
    """§9.4 and §9.5 collide, silently: the snapshot drops ``creds`` by construction.

    Without this refusal the first process answers off the wire WITH creds and
    every process inside the TTL answers from the snapshot WITHOUT them — the
    referenced unix host then fails validation ("creds: List should have at
    least 1 item"), flip-flopping with the TTL and never naming the cache.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    registered_backend("cmdb", _SuppliesCreds)
    with pytest.raises(
        InventoryError,
        match=r"o: backend 'cmdb' supplies 'creds', which a snapshot cannot carry; "
        r"set cache_ttl = \"0\" for this backend, or have the backend leave creds "
        r"to creds_file",
    ):
        construct_inventory(_third_party(tmp_path, "24h"))


def test_the_same_backend_constructs_with_caching_turned_off(
    tmp_path, monkeypatch, registered_backend
):
    """The remedy the message names has to work, or the refusal is a dead end."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    registered_backend("cmdb", _SuppliesCreds)
    inv = construct_inventory(_third_party(tmp_path, "0"))
    assert isinstance(inv, _SuppliesCreds)  # not wrapped: nothing to lose across a snapshot


def test_a_creds_file_over_a_cached_backend_is_not_the_refused_case(
    tmp_path, monkeypatch, registered_backend
):
    """ORDER: the check reads the INNER backend, before the overlay unions ``creds`` in.

    ``CredsOverlay`` is outermost and always claims ``creds``. Checking the
    constructed object rather than the backend would refuse the one
    configuration §9.4 recommends — creds in ``creds_file``, records in a
    cached remote backend — and this is the test that would go red.
    """
    from otto.inventory import SnapshotCache

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    registered_backend("cmdb", _CachedThirdParty)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    inv = construct_inventory(_third_party(tmp_path, "24h", creds_file=str(creds)))
    assert isinstance(inv, CredsOverlay)
    assert "creds" in inv.supplies  # the overlay claims it — and is still cached beneath
    assert isinstance(inv.inner, SnapshotCache)


def test_same_as_ignores_origin_but_not_the_anchored_kwargs_or_the_ttl(tmp_path):
    cfg = InventoryConfigSpec(backend="json", path="i.json")
    a = compile_inventory(cfg, anchor_dir=tmp_path, origin="r1")
    b = compile_inventory(cfg, anchor_dir=tmp_path, origin="r2")
    c = compile_inventory(cfg, anchor_dir=tmp_path / "elsewhere", origin="r3")
    assert a.same_as(b)
    assert not a.same_as(c)
    # R13: cache_ttl is behaviour, not decoration — "0" means never cache.
    never = compile_inventory(
        InventoryConfigSpec(backend="json", path="i.json", cache_ttl="0"),
        anchor_dir=tmp_path,
        origin="r4",
    )
    weekly = compile_inventory(
        InventoryConfigSpec(backend="json", path="i.json", cache_ttl="7d"),
        anchor_dir=tmp_path,
        origin="r5",
    )
    assert not never.same_as(weekly)
    assert not a.same_as(never)  # the "24h" default differs from "0" too


def test_two_repos_differing_only_in_cache_ttl_are_a_conflict(tmp_path):
    """R13: otherwise declaration order silently decides whether the process caches."""
    inv_path = _inventory_file(tmp_path)
    table = {"backend": "json", "path": str(inv_path)}
    declarations = [
        InventoryDeclaration(
            origin="r1/settings.toml", anchor_dir=tmp_path, table={**table, "cache_ttl": "0"}
        ),
        InventoryDeclaration(
            origin="r2/settings.toml", anchor_dir=tmp_path, table={**table, "cache_ttl": "7d"}
        ),
    ]
    with pytest.raises(
        InventoryError,
        match=r"two active repos declare different \[inventory\] tables: "
        r"r1/settings\.toml and r2/settings\.toml",
    ):
        build_inventory_from_declarations(declarations, user_settings=None)
    # Anti-vacuity: identical TTLs are not a conflict.
    agreeing = [
        InventoryDeclaration(
            origin=d.origin, anchor_dir=tmp_path, table={**table, "cache_ttl": "7d"}
        )
        for d in declarations
    ]
    assert isinstance(
        build_inventory_from_declarations(agreeing, user_settings=None), JsonInventory
    )


def test_construct_wraps_creds_file_and_json_is_lazy(tmp_path):
    inv_path = _inventory_file(tmp_path)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    table = {
        "backend": "json",
        "path": str(inv_path),
        "creds_file": str(creds),
        "supplies": ["ip"],
    }
    inv = build_inventory_from_declarations(
        [InventoryDeclaration(origin="o", anchor_dir=tmp_path, table=table)], user_settings=None
    )
    assert isinstance(inv, CredsOverlay)
    assert inv.supplies == frozenset({"ip", "creds"})
    assert inv.lookup("k").creds[0].login == "u"


def test_no_creds_file_means_no_overlay(tmp_path):
    """§9.4: without ``creds_file`` the backend's own records carry the creds."""
    inv_path = _inventory_file(tmp_path)
    inv = build_inventory_from_declarations(
        [
            InventoryDeclaration(
                origin="o", anchor_dir=tmp_path, table={"backend": "json", "path": str(inv_path)}
            )
        ],
        user_settings=None,
    )
    assert isinstance(inv, JsonInventory)  # NOT wrapped
    assert "creds" in inv.supplies  # the record's own, straight from the file


def test_a_missing_creds_file_is_not_touched_until_lookup(tmp_path):
    """Construction does no I/O; the error, when it comes, names the creds file.

    The filename deliberately carries regex metacharacters, so ``re.escape``
    below is load-bearing rather than decorative — an unescaped pattern reads
    ``s+`` and ``(1)`` as syntax and quietly stops pinning the filename.
    """
    inv_path = _inventory_file(tmp_path)
    absent = tmp_path / "absent+creds(1).json"
    compiled = compile_inventory(
        InventoryConfigSpec(backend="json", path=str(inv_path), creds_file=str(absent)),
        anchor_dir=tmp_path,
        origin="o",
    )
    inv = construct_inventory(compiled)  # no raise — nothing has been read yet
    assert isinstance(inv, CredsOverlay)
    # The path is regex-escaped AND resolved: construct_inventory resolves it,
    # and an unescaped tmp path would make the pattern's meaning accidental.
    with pytest.raises(InventoryError, match=rf"creds_file {re.escape(str(absent.resolve()))}: "):
        inv.lookup("k")


def test_construct_resolves_the_json_path_for_the_fingerprint(tmp_path):
    """§9.1: the fingerprint is the RESOLVED path, and resolving is config's job."""
    inv_path = _inventory_file(tmp_path)
    compiled = compile_inventory(
        InventoryConfigSpec(backend="json", path="./nested/../inventory.json"),
        anchor_dir=tmp_path,
        origin="o",
    )
    inv = construct_inventory(compiled)
    assert inv.path == inv_path.resolve()
    fingerprint = inv.fingerprint()
    assert fingerprint is not None
    assert ".." not in fingerprint


def test_project_override_wins_over_the_user_file(tmp_path):
    repo_inv = _inventory_file(tmp_path, "repo.json")
    user_inv = _inventory_file(tmp_path, "user.json")
    repo = _repo(tmp_path, "r1", {"backend": "json", "path": str(repo_inv)})
    user = UserSettingsModel(inventory=InventoryConfigSpec(backend="json", path=str(user_inv)))
    inv = build_inventory_from_declarations(
        [InventoryDeclaration(origin="r1", anchor_dir=repo.sut_dir, table=repo.inventory_settings)],
        user_settings=user,
    )
    assert isinstance(inv, JsonInventory)
    assert inv.path == repo_inv.resolve()


def test_user_file_when_no_repo_declares_and_none_when_nobody_does(tmp_path):
    user_inv = _inventory_file(tmp_path, "user.json")
    user = UserSettingsModel(inventory=InventoryConfigSpec(backend="json", path=str(user_inv)))
    inv = build_inventory_from_declarations([], user_settings=user)
    assert isinstance(inv, JsonInventory)
    assert inv.path == user_inv.resolve()
    assert build_inventory_from_declarations([], user_settings=UserSettingsModel()) is None
    assert build_inventory_from_declarations([], user_settings=None) is None


def test_two_repos_must_agree(tmp_path):
    a = _inventory_file(tmp_path, "a.json")
    b = _inventory_file(tmp_path, "b.json")
    same = [
        InventoryDeclaration(
            origin="r1/settings.toml",
            anchor_dir=tmp_path,
            table={"backend": "json", "path": str(a)},
        ),
        InventoryDeclaration(
            origin="r2/settings.toml",
            anchor_dir=tmp_path,
            table={"backend": "json", "path": str(a)},
        ),
    ]
    assert isinstance(build_inventory_from_declarations(same, user_settings=None), JsonInventory)
    different = [
        same[0],
        InventoryDeclaration(
            origin="r2/settings.toml",
            anchor_dir=tmp_path,
            table={"backend": "json", "path": str(b)},
        ),
    ]
    with pytest.raises(
        InventoryError,
        match=r"two active repos declare different \[inventory\] tables: "
        r"r1/settings\.toml and r2/settings\.toml",
    ):
        build_inventory_from_declarations(different, user_settings=None)


def test_unknown_backend_and_bad_table_are_inventory_errors(tmp_path):
    with pytest.raises(InventoryError, match=r"o: Unknown inventory backend"):
        build_inventory_from_declarations(
            [InventoryDeclaration(origin="o", anchor_dir=tmp_path, table={"backend": "nope"})],
            user_settings=None,
        )
    with pytest.raises(InventoryError, match=r"o: \[inventory\] [\s\S]*backend\n\s+Field required"):
        build_inventory_from_declarations(
            [InventoryDeclaration(origin="o", anchor_dir=tmp_path, table={"creds_file": "x"})],
            user_settings=None,
        )


def test_build_inventory_reads_repos_and_the_user_file(tmp_path):
    inv_path = _inventory_file(tmp_path)
    user_file = _user_file(tmp_path, f'[inventory]\nbackend = "json"\npath = "{inv_path}"\n')
    repos = [_repo(tmp_path, "r1", {})]
    inv = build_inventory(repos, user_settings_path=user_file)
    assert isinstance(inv, JsonInventory)
    assert inv.path == inv_path.resolve()
    assert build_inventory(repos, user_settings_path=tmp_path / "absent.toml") is None


def test_build_inventory_prefers_a_declaring_repo_and_anchors_to_its_root(tmp_path):
    """The origin is pinned by the test below, which reaches it through an error."""
    repos = [_repo(tmp_path, "r1", {"backend": "json", "path": "repo.json"})]
    # The repo's own root is the anchor, so a relative path resolves there.
    (repos[0].sut_dir / "repo.json").write_text(json.dumps({"k": {"ip": "10.0.0.2"}}))
    inv = build_inventory(repos, user_settings_path=tmp_path / "absent.toml")
    assert isinstance(inv, JsonInventory)
    assert inv.path == (repos[0].sut_dir / "repo.json").resolve()
    assert inv.lookup("k").ip == "10.0.0.2"


def test_build_inventory_names_each_repos_settings_file_as_its_origin(tmp_path):
    """The origin is ``<sut_dir>/.otto/settings.toml``, spelled by ``TOML_SETTINGS_PATH``.

    Pinned through the two-repos-disagree error, which is the only place the
    origin reaches a user. The test that merely *promised* this in its name
    never checked it — mutating the origin to ``str(repo.sut_dir)`` stayed
    green.
    """
    from otto.config.repo import TOML_SETTINGS_PATH

    a = _inventory_file(tmp_path, "a.json")
    b = _inventory_file(tmp_path, "b.json")
    repos = [
        _repo(tmp_path, "r1", {"backend": "json", "path": str(a)}),
        _repo(tmp_path, "r2", {"backend": "json", "path": str(b)}),
    ]
    expected = [re.escape(str(r.sut_dir / TOML_SETTINGS_PATH)) for r in repos]
    with pytest.raises(
        InventoryError,
        match=r"two active repos declare different \[inventory\] tables: "
        rf"{expected[0]} and {expected[1]}; a process has exactly one inventory",
    ):
        build_inventory(repos, user_settings_path=tmp_path / "absent.toml")
    # Anti-vacuity: the path the pattern demands is the one that exists on disk
    # for a real repo, not just a string this test made up.
    assert str(TOML_SETTINGS_PATH) == ".otto/settings.toml"


def test_build_inventory_turns_a_broken_user_file_into_an_inventory_error(tmp_path):
    user_file = _user_file(tmp_path, "[inventory\n")
    with pytest.raises(InventoryError, match=r"settings\.toml: "):
        build_inventory([], user_settings_path=user_file)


def test_build_inventory_names_the_user_file_as_the_origin(tmp_path):
    user_file = _user_file(tmp_path, '[inventory]\nbackend = "nope"\n')
    with pytest.raises(InventoryError, match=rf"{user_file}: Unknown inventory backend"):
        build_inventory([], user_settings_path=user_file)


def test_build_inventory_over_a_real_repo_reads_the_inventory_table(tmp_path):
    """End-to-end through ``Repo.inventory_settings``, not a stand-in namespace.

    A ``SimpleNamespace`` repo cannot tell us the property exists, is named
    ``inventory``, and returns the raw sub-dict — that is exactly the seam
    this test covers.
    """
    from otto.config.repo import Repo
    from tests._fixtures.sutrepo import make_sut_repo

    sut = make_sut_repo(
        tmp_path / "p", name="p", extra='[inventory]\nbackend = "json"\npath = "lab/i.json"\n'
    )
    (sut / "lab").mkdir()
    (sut / "lab" / "i.json").write_text(json.dumps({"k": {"ip": "10.0.0.9"}}))
    repo = Repo(sut_dir=sut)
    assert repo.inventory_settings == {"backend": "json", "path": "lab/i.json"}
    inv = build_inventory([repo], user_settings_path=tmp_path / "absent.toml")
    assert isinstance(inv, JsonInventory)
    assert inv.path == (sut / "lab" / "i.json").resolve()
    assert inv.lookup("k").ip == "10.0.0.9"


def test_a_repo_without_an_inventory_table_declares_nothing(tmp_path):
    from otto.config.repo import Repo
    from tests._fixtures.sutrepo import make_sut_repo

    repo = Repo(sut_dir=make_sut_repo(tmp_path / "p", name="p"))
    assert repo.inventory_settings == {}
    assert build_inventory([repo], user_settings_path=tmp_path / "absent.toml") is None


def test_a_user_file_relative_path_anchors_to_the_user_file_directory(tmp_path):
    user_dir = tmp_path / "home"
    user_dir.mkdir()
    inv_path = _inventory_file(user_dir)
    user_file = _user_file(user_dir, '[inventory]\nbackend = "json"\npath = "inventory.json"\n')
    inv = build_inventory([], user_settings_path=user_file)
    assert isinstance(inv, JsonInventory)
    assert inv.path == inv_path.resolve()
