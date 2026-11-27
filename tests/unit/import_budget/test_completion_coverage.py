"""Every TAB completion site maps to a gated, measured import-budget surface.

The site-to-surface map lives once, in ``scripts/import_budget.py``
(``SITE_PROFILES`` by kind, ``HANDOVER_SITES`` by site identity, next to
``SURFACES``); this module derives from it. A new completion site whose path no
surface measures fails here, naming the site. See "Adding a completion site"
in ``docs/architecture/subsystems/completion.md``.
"""

from typing import Any

import pytest

from tests._fixtures.budget_harness import load_harness
from tests._fixtures.completion_sites import (
    INPUT_HANDOVER_CLASSES,
    SITE_HANDOVER_CLASSES,
    CompletionSite,
    completion_sites,
    serialised_cli_tree,
)

_HARNESS = load_harness()

SiteKey = tuple[tuple[str, ...], str | None, str]


def _gated(key: str) -> bool:
    return any(s.key == key and not s.tracked for s in _HARNESS.SURFACES)


def _surface_for(
    site: CompletionSite, profiles: dict[str, str], handovers: dict[SiteKey, str]
) -> str | None:
    """The surface the maps send *site* to, or ``None`` when neither map has it."""
    if site.handover:
        return handovers.get(site.identity)
    return profiles.get(site.source["kind"])


def coverage_failures(
    sites: list[CompletionSite],
    profiles: dict[str, str],
    handovers: dict[SiteKey, str],
) -> list[str]:
    """Every way *sites* and the two maps disagree, one line per site, naming it."""
    failures: list[str] = []
    by_identity = {site.identity: site for site in sites}
    for site in sites:
        if site.handover and site.identity not in handovers:
            failures.append(f"{site.identity}: a handover site ({site.why}) not in HANDOVER_SITES")
        elif not site.handover and site.source["kind"] not in profiles:
            failures.append(
                f"{site.identity}: kind {site.source['kind']!r} has no SITE_PROFILES entry"
            )
    for identity in handovers:
        listed = by_identity.get(identity)
        if listed is None:
            failures.append(f"{identity}: listed in HANDOVER_SITES but no longer exists")
        elif not listed.handover:
            failures.append(f"{identity}: listed in HANDOVER_SITES but no longer a handover")
    return failures


# ── tree fixtures ──────────────────────────────────────────────────────────────


def _param(
    name: str,
    kind: str = "none",
    *,
    flags: tuple[str, ...] = (),
    nargs: int = 1,
    takes_value: bool = True,
    **source: Any,
) -> dict[str, Any]:
    return {
        "flags": list(flags),
        "name": name,
        "takes_value": takes_value,
        "multiple": False,
        "nargs": nargs,
        "sep": source.get("sep"),
        "source": {"kind": kind, **source},
    }


def _node(name: str, params=(), commands=None, *, scoped: bool = False) -> dict[str, Any]:
    node = {
        "name": name,
        "params": list(params),
        "commands": dict(commands or {}),
        "group": bool(commands) or scoped,
    }
    if scoped:
        node["scoped_by"] = "host_id"
    return node


def _tree(root_params=(), commands=None, host_classes=None) -> dict[str, Any]:
    tree = _node("otto", root_params, commands)
    tree["host_classes"] = dict(host_classes or {})
    return tree


def _host_tree(host_classes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A root with a scoped ``host`` group (no union verbs) and the given class views."""
    host = _node("host", [_param("host_id", "payload", key="hosts")], scoped=True)
    return _tree(commands={"host": host}, host_classes=host_classes)


# ── the enumeration ────────────────────────────────────────────────────────────


def test_enumeration_reaches_every_host_class():
    """A parameter that exists on one host class only is enumerated, with that class."""
    tree = _host_tree(
        {
            "plain": {"exec": _node("exec", [_param("user", "payload", flags=("--user",))])},
            "only": {
                "exec": _node(
                    "exec",
                    [
                        _param("user", "payload", flags=("--user",)),
                        _param("special", flags=("--special",)),
                    ],
                )
            },
        }
    )
    sites = completion_sites(tree)
    special = [s for s in sites if s.param == "special"]
    assert [(s.path, s.host_class) for s in special] == [(("host", "exec"), "only")]
    users = {(s.path, s.host_class) for s in sites if s.param == "user"}
    assert users == {(("host", "exec"), "plain"), (("host", "exec"), "only")}
    # the scoped group's own positional, outside any class
    assert [(s.path, s.host_class) for s in sites if s.param == "host_id"] == [(("host",), None)]


def test_enumeration_walks_a_scoped_groups_union_verbs():
    """The union menu (an id no class claims) is a view too: its verbs have no class."""
    host = _node(
        "host",
        [_param("host_id", "payload", key="hosts")],
        {"get": _node("get", [_param("src_files", "live", nargs=-1)])},
        scoped=True,
    )
    tree = _tree(commands={"host": host})
    assert [s.identity for s in completion_sites(tree) if s.param == "src_files"] == [
        (("host", "get"), None, "src_files")
    ]


def test_a_flag_is_not_a_site():
    flag = _param("debug", flags=("--debug",), takes_value=False)
    tree = _tree([flag, _param("x", flags=("-x",))])
    assert [s.param for s in completion_sites(tree)] == ["x"]


@pytest.mark.parametrize(
    ("param", "shape", "handover"),
    [
        pytest.param(_param("a", "payload", key="hosts"), "single", False, id="single"),
        pytest.param(_param("a", "tests", nargs=-1), "single", False, id="variadic"),
        pytest.param(_param("a", nargs=2), "multi", True, id="nargs-2"),
        pytest.param(
            _param("a", "payload", key="hosts", sep=",", live_past_sep=True),
            "past_sep",
            True,
            id="past-sep",
        ),
        pytest.param(_param("a", "payload", key="hosts", sep=","), "single", False, id="sep-only"),
        pytest.param(_param("a", "live"), "single", True, id="live"),
        pytest.param(
            _param("a", "tests", sep=",", live_past_sep=True),
            "single",
            False,
            id="past-sep-off-payload",
        ),
    ],
)
def test_shape_and_handover_follow_the_shims_rules(param, shape, handover):
    [site] = completion_sites(_tree([param]))
    assert (site.shape, site.handover) == (shape, handover)


def test_the_fixture_knows_every_handover_class_the_shim_produces():
    """Every class the differential may see is a site class ``why`` models, or an input one.

    ``EXPECTED_HANDOVER_REASONS`` is the shim differential's list of what the
    shim hands over on; a new class there fails here until
    ``tests/_fixtures/completion_sites.py`` places it, and a site class is
    then modelled by ``_shape`` and mapped by the coverage test.
    """
    from tests.unit.shim.test_differential import EXPECTED_HANDOVER_REASONS

    site = set(SITE_HANDOVER_CLASSES.values())
    assert site | INPUT_HANDOVER_CLASSES == EXPECTED_HANDOVER_REASONS
    assert not site & INPUT_HANDOVER_CLASSES


@pytest.mark.parametrize(
    ("param", "frag"),
    [
        pytest.param(_param("a", "live"), "", id="live"),
        pytest.param(
            _param("a", "payload", key="hosts", sep=",", live_past_sep=True), "x,", id="past-sep"
        ),
    ],
)
def test_each_site_class_is_the_reason_the_shim_gives(param, frag):
    """The shim's own hand-over on the site, bucketed as the differential buckets it."""
    from otto import _shim_complete as sc
    from tests.unit.shim.test_differential import _reason_class

    [site] = completion_sites(_tree([param]))
    with pytest.raises(sc.Handover) as raised:
        sc._source_values(param, frag, [], sc.Payloads({}))
    assert _reason_class(raised.value.reason) == SITE_HANDOVER_CLASSES[site.why]


def test_a_multi_value_site_is_the_shims_nargs_handover():
    """A ``multi`` site hands over, bucketed as the differential buckets it."""
    from otto import _shim_complete as sc
    from tests.unit.shim.test_differential import _reason_class

    [site] = completion_sites(_tree([_param("a", nargs=2, flags=("--a",))]))
    assert site.why == "multi"
    with pytest.raises(sc.Handover, match=r"^nargs=2 ") as raised:
        sc._reject_multi_value({"nargs": 2, "name": "a"})
    assert _reason_class(raised.value.reason) == SITE_HANDOVER_CLASSES[site.why]


# ── the maps against the real tree ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def real_sites() -> list[CompletionSite]:
    return completion_sites(serialised_cli_tree())


def test_site_identities_are_unique(real_sites):
    identities = [s.identity for s in real_sites]
    assert len(identities) == len(set(identities))


def test_every_site_maps_to_a_gated_surface(real_sites):
    unmapped, ungated = [], []
    for site in real_sites:
        key = _surface_for(site, _HARNESS.SITE_PROFILES, _HARNESS.HANDOVER_SITES)
        if key is None:
            unmapped.append(site.identity)
        elif not _gated(key):
            ungated.append((site.identity, key))
    assert unmapped == []
    assert ungated == []


def test_every_map_target_is_a_gated_surface():
    targets = {*_HARNESS.SITE_PROFILES.values(), *_HARNESS.HANDOVER_SITES.values()}
    assert sorted(t for t in targets if not _gated(t)) == []


def test_every_handover_site_is_listed(real_sites):
    missing = [
        f
        for f in coverage_failures(real_sites, _HARNESS.SITE_PROFILES, _HARNESS.HANDOVER_SITES)
        if "not in HANDOVER_SITES" in f
    ]
    assert missing == []


def test_no_listed_site_is_stale(real_sites):
    stale = [
        f
        for f in coverage_failures(real_sites, _HARNESS.SITE_PROFILES, _HARNESS.HANDOVER_SITES)
        if "listed in HANDOVER_SITES" in f
    ]
    assert stale == []


def test_the_real_tree_has_no_coverage_failure(real_sites):
    assert coverage_failures(real_sites, _HARNESS.SITE_PROFILES, _HARNESS.HANDOVER_SITES) == []


def test_every_measured_completion_surface_is_reached(real_sites):
    """A measured completion surface is some site's target, or a named variant of one."""
    reached = {
        _surface_for(site, _HARNESS.SITE_PROFILES, _HARNESS.HANDOVER_SITES) for site in real_sites
    }
    measured = {
        s.key for s in _HARNESS.SURFACES if s.key.startswith("completion_") and not s.tracked
    }
    variants = _HARNESS.VARIANT_SURFACES
    assert sorted(measured - reached - set(variants)) == []
    assert {v: t for v, t in variants.items() if t not in reached} == {}
    assert sorted(set(variants) - measured) == []
    assert sorted(set(variants) & reached) == [], "a variant a site reaches directly is stale"


# ── the coverage check fails where it must ─────────────────────────────────────


def _get_view(kind: str) -> dict[str, Any]:
    return {"get": _node("get", [_param("src_files", kind, nargs=-1)])}


def test_handover_identity_includes_host_class():
    """Two classes have a live ``get`` site; a map that lists one names the other."""
    tree = _host_tree({"alpha": _get_view("live"), "beta": _get_view("live")})
    listed: dict[SiteKey, str] = {
        (("host", "get"), "alpha", "src_files"): "completion_remote_path_hit"
    }
    failures = coverage_failures(completion_sites(tree), {"payload": "x"}, listed)
    assert failures == [
        "(('host', 'get'), 'beta', 'src_files'): a handover site (live) not in HANDOVER_SITES"
    ]


def test_an_unmapped_kind_fails():
    tree = _tree([_param("out", "echo", flags=("--out",))])
    failures = coverage_failures(completion_sites(tree), dict(_HARNESS.SITE_PROFILES), {})
    assert failures == ["((), None, 'out'): kind 'echo' has no SITE_PROFILES entry"]


def test_a_multi_value_site_fails_until_listed():
    tree = _tree([_param("pair", flags=("--pair",), nargs=2)])
    sites = completion_sites(tree)
    profiles = dict(_HARNESS.SITE_PROFILES)
    assert coverage_failures(sites, profiles, {}) == [
        "((), None, 'pair'): a handover site (multi) not in HANDOVER_SITES"
    ]
    assert coverage_failures(sites, profiles, {((), None, "pair"): "x"}) == []


def test_a_listed_site_that_is_gone_or_no_longer_a_handover_fails():
    tree = _host_tree({"alpha": _get_view("none")})
    listed: dict[SiteKey, str] = {
        (("host", "get"), "alpha", "src_files"): "completion_remote_path_hit",
        (("host", "put"), "alpha", "dest_dir"): "completion_remote_path_hit",
    }
    failures = coverage_failures(completion_sites(tree), {"none": "x", "payload": "x"}, listed)
    gone, stale = (("host", "put"), "alpha", "dest_dir"), (("host", "get"), "alpha", "src_files")
    assert failures == [
        f"{stale}: listed in HANDOVER_SITES but no longer a handover",
        f"{gone}: listed in HANDOVER_SITES but no longer exists",
    ]
