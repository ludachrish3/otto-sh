"""[inventory] compilation and the one-inventory-per-process resolution (spec §8)."""

import json
import re
from pathlib import Path

import pytest

from otto.inventory import (
    CredsOverlay,
    InventoryConstructionError,
    InventoryDeclaration,
    InventoryEnv,
    InventoryError,
    JsonInventory,
    JsonInventoryConfig,
    NetBoxInventoryConfig,
    build_inventory,
    build_inventory_from_declarations,
    compile_inventory,
    construct_inventory,
)
from otto.models.base import OttoModel
from otto.models.settings import CredsConfigSpec, InventoryConfigSpec, UserSettingsModel
from tests._fixtures.fake_repo import fake_repo


def _repo(tmp_path, name, table, creds=None):
    root = tmp_path / name
    root.mkdir()
    return fake_repo(name, sut_dir=root, settings={"inventory": table, "creds": creds or {}})


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
        InventoryConstructionError,
        match=r"'json' \(registered by .*; configured in origin\.toml\): parse failed: path",
    ):
        compile_inventory(
            InventoryConfigSpec(backend="json"), anchor_dir=tmp_path, origin="origin.toml"
        )
    with pytest.raises(InventoryConstructionError, match=r"parse failed: url: Extra inputs"):
        compile_inventory(
            InventoryConfigSpec(backend="json", path="i.json", url="x"),
            anchor_dir=tmp_path,
            origin="origin.toml",
        )


def test_json_supplies_must_be_a_list_of_field_names(tmp_path):
    with pytest.raises(
        InventoryConstructionError, match=r"configured in o\): parse failed: supplies"
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
    assert out.prepared.normalized["supplies"] == ("ip",)


def test_relative_paths_anchor_to_the_declaring_dir_and_tilde_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = InventoryConfigSpec(backend="json", path="lab/i.json")
    out = compile_inventory(cfg, anchor_dir=tmp_path / "repo", origin="o")
    assert out.prepared.config == JsonInventoryConfig(path=tmp_path / "repo" / "lab" / "i.json")
    assert out.creds is None
    assert out.cache_ttl.total_seconds() == 24 * 3600


def test_netbox_keys_are_parsed_by_its_own_model(tmp_path):
    cfg = InventoryConfigSpec(backend="netbox", url="https://nb", filter={"site": "a"})
    out = compile_inventory(cfg, anchor_dir=tmp_path, origin="o")
    assert out.prepared.config == NetBoxInventoryConfig(url="https://nb", filter={"site": "a"})


class _FakeConfig(OttoModel, frozen=True):
    url: str
    token_env: str | None = None


class _FakeBackend:
    """A third-party backend: records the configuration and env it was built from."""

    def __init__(self, c):
        self.env = c.env
        self.url = c.config.url
        self.token_env = c.config.token_env
        self.label = f"fake:{self.url}"
        self.supplies = frozenset({"ip"})

    def lookup(self, key):
        raise NotImplementedError

    def list_keys(self):
        return []

    def fingerprint(self):
        """A third-party backend that CAN report freshness — so it is never cached.

        A backend returning a string opts out of the cache by design, which is
        what the ``isinstance(inv, _FakeBackend)`` assertion below also pins.
        """
        return f"fake:{self.url}"


def test_a_third_party_backend_gets_its_config_and_env(tmp_path):
    """A registered backend is built by its factory from its parsed config and the env.

    Registered here rather than waited for, because an arm no test reaches is
    an arm no test can catch breaking.
    """
    from otto.inventory import register_inventory_backend

    register_inventory_backend("fake-test", config=_FakeConfig, factory=_FakeBackend)
    compiled = compile_inventory(
        InventoryConfigSpec(backend="fake-test", url="https://nb", token_env="NB_TOKEN"),
        anchor_dir=tmp_path,
        origin="o",
    )
    inv = construct_inventory(compiled)
    assert isinstance(inv, _FakeBackend)
    assert inv.env == InventoryEnv(tmp_path, "o")
    assert inv.url == "https://nb"
    assert inv.token_env == "NB_TOKEN"

    # A typo'd key fails the backend's config model, naming the settings file
    # and the backend, as the json backend's does.
    with pytest.raises(
        InventoryConstructionError,
        match=r"'fake-test' \(registered by .*; configured in r1/settings\.toml\): "
        r"parse failed: .*urll",
    ):
        compile_inventory(
            InventoryConfigSpec(backend="fake-test", urll="https://nb"),
            anchor_dir=tmp_path,
            origin="r1/settings.toml",
        )


class _CachedThirdParty:
    """A third-party backend otto WOULD wrap in the snapshot cache.

    ``fingerprint()`` returns ``None`` — the backend's own statement that it
    cannot report freshness, which is what lands it in the cache — and
    ``supplies`` is whatever the subclass says.
    """

    supplies = frozenset({"ip"})

    def __init__(self, url="https://cmdb"):
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


class _NoConfig(OttoModel, frozen=True):
    pass


@pytest.fixture
def registered_backend():
    """Register a backend class under a name for one test, built with no configuration.

    The root isolation fixture restores the table afterwards.
    """
    from otto.inventory import register_inventory_backend

    def _register(name, cls):
        register_inventory_backend(name, config=_NoConfig, factory=lambda c: cls(), overwrite=True)

    return _register


def _third_party(tmp_path, ttl):
    return compile_inventory(
        InventoryConfigSpec(backend="cmdb", cache_ttl=ttl),
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
        r"to a \[creds\] store",
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


def test_a_creds_store_over_a_cached_backend_is_not_the_refused_case(
    tmp_path, monkeypatch, registered_backend
):
    """ORDER: the check reads the INNER backend, before the overlay unions ``creds`` in.

    ``CredsOverlay`` is outermost and always claims ``creds``. Checking the
    constructed object rather than the backend would refuse the one
    configuration §9.4 recommends — creds in a ``[creds]`` store, records in a
    cached remote backend — and this is the test that would go red.
    """
    from otto.inventory import SnapshotCache

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    registered_backend("cmdb", _CachedThirdParty)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    inv = build_inventory_from_declarations(
        [
            InventoryDeclaration(
                origin="o",
                anchor_dir=tmp_path,
                table={"backend": "cmdb", "cache_ttl": "24h"},
                creds_table={"backend": "json", "path": str(creds)},
            )
        ],
        user_settings=None,
    )
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


def test_construct_wraps_a_creds_store_and_json_is_lazy(tmp_path):
    inv_path = _inventory_file(tmp_path)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    table = {"backend": "json", "path": str(inv_path), "supplies": ["ip"]}
    inv = build_inventory_from_declarations(
        [
            InventoryDeclaration(
                origin="o",
                anchor_dir=tmp_path,
                table=table,
                creds_table={"backend": "json", "path": str(creds)},
            )
        ],
        user_settings=None,
    )
    assert isinstance(inv, CredsOverlay)
    assert inv.supplies == frozenset({"ip", "creds"})
    assert inv.lookup("k").creds[0].login == "u"
    assert inv.store.label == f"json:{creds.resolve()}"


def test_no_creds_table_means_no_overlay(tmp_path):
    """§3 statement 2: without ``[creds]`` the backend's own records carry the creds."""
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


def test_a_missing_store_file_is_not_touched_until_lookup(tmp_path):
    """Construction does no I/O; the error, when it comes, names the store file.

    The filename deliberately carries regex metacharacters, so ``re.escape``
    below is load-bearing rather than decorative — an unescaped pattern reads
    ``s+`` and ``(1)`` as syntax and quietly stops pinning the filename.
    """
    inv_path = _inventory_file(tmp_path)
    absent = tmp_path / "absent+creds(1).json"
    inv = build_inventory_from_declarations(
        [
            InventoryDeclaration(
                origin="o",
                anchor_dir=tmp_path,
                table={"backend": "json", "path": str(inv_path)},
                creds_table={"backend": "json", "path": str(absent)},
            )
        ],
        user_settings=None,
    )  # no raise — nothing has been read yet
    assert isinstance(inv, CredsOverlay)
    # The path is regex-escaped AND resolved: construction resolves it, and an
    # unescaped tmp path would make the pattern's meaning accidental.
    with pytest.raises(InventoryError, match=rf"{re.escape(str(absent.resolve()))}: "):
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
    with pytest.raises(
        InventoryError, match=r"'nope' \(not registered; configured in o\): lookup failed"
    ):
        build_inventory_from_declarations(
            [InventoryDeclaration(origin="o", anchor_dir=tmp_path, table={"backend": "nope"})],
            user_settings=None,
        )
    with pytest.raises(InventoryError, match=r"o: \[inventory\] [\s\S]*backend\n\s+Field required"):
        build_inventory_from_declarations(
            [InventoryDeclaration(origin="o", anchor_dir=tmp_path, table={"path": "x"})],
            user_settings=None,
        )


def test_creds_file_in_the_inventory_table_points_at_the_creds_table(tmp_path):
    """Spec §4.4: extra='allow' would otherwise swallow the dead key as a backend kwarg."""
    with pytest.raises(
        InventoryError,
        match=r'creds_file has moved: declare \[creds\] backend = "json" / path = "<the same '
        r'path>" beside \[inventory\]',
    ):
        build_inventory_from_declarations(
            [
                InventoryDeclaration(
                    origin="o",
                    anchor_dir=tmp_path,
                    table={"backend": "json", "path": "i.json", "creds_file": "c.json"},
                )
            ],
            user_settings=None,
        )


def test_creds_without_an_inventory_is_an_error_naming_the_declaring_file(tmp_path):
    """Spec §4.3: a store nothing can look up must not silently do nothing."""
    with pytest.raises(
        InventoryError,
        match=r"r1/settings\.toml: \[creds\] is keyed by inventory key and no \[inventory\] is "
        r"declared in either settings file; declare one, or remove \[creds\]",
    ):
        build_inventory_from_declarations(
            [
                InventoryDeclaration(
                    origin="r1/settings.toml",
                    anchor_dir=tmp_path,
                    creds_table={"backend": "json", "path": "c.json"},
                )
            ],
            user_settings=None,
        )


def test_an_unknown_creds_backend_is_an_inventory_error(tmp_path):
    """A ``CredsError`` off the creds side must reach the caller as an ``InventoryError``."""
    inv_path = _inventory_file(tmp_path)
    with pytest.raises(
        InventoryError, match=r"creds backend 'nope' \(not registered; configured in o\)"
    ):
        build_inventory_from_declarations(
            [
                InventoryDeclaration(
                    origin="o",
                    anchor_dir=tmp_path,
                    table={"backend": "json", "path": str(inv_path)},
                    creds_table={"backend": "nope"},
                )
            ],
            user_settings=None,
        )


def test_a_creds_table_missing_backend_is_an_inventory_error(tmp_path):
    inv_path = _inventory_file(tmp_path)
    with pytest.raises(InventoryError, match=r"o: \[creds\] "):
        build_inventory_from_declarations(
            [
                InventoryDeclaration(
                    origin="o",
                    anchor_dir=tmp_path,
                    table={"backend": "json", "path": str(inv_path)},
                    creds_table={"path": "c.json"},
                )
            ],
            user_settings=None,
        )


def test_creds_resolve_independently_of_the_inventory(tmp_path):
    """Spec §4.2: a repo overriding only [inventory] still gets the user file's [creds]."""
    inv_path = _inventory_file(tmp_path)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    user = UserSettingsModel(creds=CredsConfigSpec(backend="json", path=str(creds)))
    inv = build_inventory_from_declarations(
        [
            InventoryDeclaration(
                origin="r1",
                anchor_dir=tmp_path,
                table={"backend": "json", "path": str(inv_path), "supplies": ["ip"]},
            )
        ],
        user_settings=user,
        user_settings_file=tmp_path / "settings.toml",
    )
    assert isinstance(inv, CredsOverlay)
    assert inv.lookup("k").creds[0].login == "u"
    # ...and the other way round: the repo declares only [creds], the user file the inventory.
    both = build_inventory_from_declarations(
        [
            InventoryDeclaration(
                origin="r1",
                anchor_dir=tmp_path,
                creds_table={"backend": "json", "path": str(creds)},
            )
        ],
        user_settings=UserSettingsModel(
            inventory=InventoryConfigSpec(backend="json", path=str(inv_path))
        ),
        user_settings_file=tmp_path / "settings.toml",
    )
    assert isinstance(both, CredsOverlay)


def test_creds_resolve_independently_across_two_repos(tmp_path):
    """Spec §4.2, repo vs. REPO: repo A's [inventory] and repo B's [creds] both resolve.

    ``test_creds_resolve_independently_of_the_inventory`` covers repo-vs-user
    -file only; each table's walk (``build_inventory_from_declarations`` /
    ``_resolve_creds``) considers a declaration only when it carries THAT
    table, so two repos splitting the tables between them is the same
    independence one level up — no user file involved at all.
    """
    inv_path = _inventory_file(tmp_path)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    inv = build_inventory_from_declarations(
        [
            InventoryDeclaration(
                origin="r1",
                anchor_dir=tmp_path,
                table={"backend": "json", "path": str(inv_path), "supplies": ["ip"]},
            ),
            InventoryDeclaration(
                origin="r2",
                anchor_dir=tmp_path,
                creds_table={"backend": "json", "path": str(creds)},
            ),
        ],
        user_settings=None,
    )
    assert isinstance(inv, CredsOverlay)
    assert inv.lookup("k").creds[0].login == "u"


def test_two_repos_must_agree_on_creds(tmp_path):
    inv_path = _inventory_file(tmp_path)
    table = {"backend": "json", "path": str(inv_path)}
    with pytest.raises(
        InventoryError,
        match=r"two active repos declare different \[creds\] tables: r1/settings\.toml and "
        r"r2/settings\.toml",
    ):
        build_inventory_from_declarations(
            [
                InventoryDeclaration(
                    origin="r1/settings.toml",
                    anchor_dir=tmp_path,
                    table=table,
                    creds_table={"backend": "json", "path": "a.json"},
                ),
                InventoryDeclaration(
                    origin="r2/settings.toml",
                    anchor_dir=tmp_path,
                    table=table,
                    creds_table={"backend": "json", "path": "b.json"},
                ),
            ],
            user_settings=None,
        )


def test_build_inventory_reads_a_repo_that_declares_only_creds(tmp_path):
    inv_path = _inventory_file(tmp_path)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"k": [{"login": "u", "password": "p"}]}))
    user_file = _user_file(tmp_path, f'[inventory]\nbackend = "json"\npath = "{inv_path}"\n')
    repos = [_repo(tmp_path, "r1", {}, creds={"backend": "json", "path": str(creds)})]
    inv = build_inventory(repos, user_settings_path=user_file)
    assert isinstance(inv, CredsOverlay)
    assert inv.store.label == f"json:{creds.resolve()}"


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
    with pytest.raises(
        InventoryError, match=rf"'nope' \(not registered; configured in {user_file}\)"
    ):
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


def test_a_creds_construction_failure_stays_a_construction_error(tmp_path):
    """Both creds sites keep the construction kind: compile (lookup) and build (factory)."""
    from otto.creds import CredsConstructionError, register_creds_backend
    from otto.inventory import InventoryConstructionError

    inv_path = _inventory_file(tmp_path)

    def _declare(creds_table):
        return [
            InventoryDeclaration(
                origin="o",
                anchor_dir=tmp_path,
                table={"backend": "json", "path": str(inv_path)},
                creds_table=creds_table,
            )
        ]

    with pytest.raises(InventoryConstructionError) as at_compile:
        build_inventory_from_declarations(_declare({"backend": "nope"}), user_settings=None)
    assert isinstance(at_compile.value, ValueError)
    assert isinstance(at_compile.value.__cause__, CredsConstructionError)

    class _NoKeys(OttoModel, frozen=True):
        pass

    def _boom(c):
        raise RuntimeError("vault down")

    register_creds_backend("boom-store", config=_NoKeys, factory=_boom)
    with pytest.raises(InventoryConstructionError, match="vault down") as at_build:
        build_inventory_from_declarations(_declare({"backend": "boom-store"}), user_settings=None)
    assert isinstance(at_build.value.__cause__, CredsConstructionError)
