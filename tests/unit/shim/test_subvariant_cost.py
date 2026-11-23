"""A sub-variant site costs exactly what its profile's measured representative costs.

The ``static``, ``none`` and ``markers`` sites answer from the payload or the
tables their validation already read, so each must do the file work of the TAB
the import budget measures for its profile (``completion_host_warm`` for the
``names`` profile, ``completion_test_warm`` for the ``tests`` one), and no more.
An in-process audit hook cannot hold that guarantee (CPython raises no audit
event for ``os.stat``, nor for an import of a loaded module), so each TAB runs
whole, in a fresh child, through the one recorder
(:mod:`tests._fixtures.recorded_tab`). See "What a TAB costs, and how it is
measured" in ``docs/architecture/subsystems/completion.md``.

The recorder counts ``os.stat`` only: a new ``os.lstat`` or ``pathlib`` stat on
a sub-variant's path passes here, and only the measured representatives, under
strace, count it.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._fixtures.budget_harness import load_harness
from tests._fixtures.completion_sites import (
    completion_nodes,
    completion_sites,
    serialised_cli_tree,
)
from tests._fixtures.generated_project import generate_project
from tests._fixtures.recorded_tab import (
    RecordedTab,
    assert_same_cost,
    prime_tab,
    record_cold_tab,
    record_warm_tab,
)

_HARNESS = load_harness()
SHIM_DENY = _HARNESS.SHIM_DENY
"""The shim deny roots, from the harness: the one list."""

NAMES_REPRESENTATIVE = _HARNESS.SITE_PROFILES["payload"]
"""The measured surface of the ``names`` profile, from the harness's one site map:
``static`` and ``none`` cost what it costs."""

TESTS_REPRESENTATIVE = _HARNESS.SITE_PROFILES["tests"]
"""The measured surface of the ``tests`` profile, from the harness's one site map:
``markers`` costs what it costs."""

NONE_SITE = ("otto --xdir ", "2")
"""A ``none`` site: ``--xdir`` takes a path, which ``completion_tree._source_for``
serialises as ``{"kind": "none"}``. The first ``none`` site of the shared
enumeration (:func:`completion_sites`; the root command's own options come
first), pinned by :func:`test_the_none_site_is_the_enumerations_first`."""

STATIC_SITE = ("otto --log-level ", "2")
"""A ``static`` site: ``--log-level``'s declared level names."""

MARKERS_SITE = ("otto test -m ", "3")
"""A ``markers`` site: ``otto test -m``'s marker expression."""


def _site(key: str) -> "tuple[str, str]":
    """The ``COMP_WORDS`` and ``COMP_CWORD`` of the harness surface *key*: its measured TAB."""
    extra = dict(_HARNESS.surface_by_key(key).env_extra)
    return extra["COMP_WORDS"], extra["COMP_CWORD"]


def _answer(key: str) -> set[str]:
    """What the harness surface *key* requires its TAB to answer."""
    return set(_HARNESS.surface_by_key(key).expect_answer)


@dataclass(frozen=True)
class WarmHome:
    """One generated project and one warm home every recording in this module shares."""

    root: Path
    env: dict[str, str]

    def tab(self, site: "tuple[str, str]", name: str, *, inject: str = "") -> RecordedTab:
        """Record a warm TAB at *site* (``COMP_WORDS``, ``COMP_CWORD``) from the shared state."""
        words, cword = site
        env = {**self.env, "COMP_WORDS": words, "COMP_CWORD": cword}
        return record_warm_tab(self.root, env, name, inject=inject)


@pytest.fixture(scope="module")
def warm(tmp_path_factory: pytest.TempPathFactory) -> WarmHome:
    """The generated project, seeded by a cold tests TAB and primed once.

    The project is the representatives' shape (``realistic``, with the
    templated lab ``completion_host_warm`` selects). The seed is
    ``completion_test_warm``'s: a cold ``otto test T<TAB>`` hands over, builds
    the cache and waits for the collect child's table, which leaves the
    ``tests`` marker. The priming TAB then leaves the ``names`` marker, so no
    recording is the one that creates it.
    """
    root = tmp_path_factory.mktemp("subvariant")
    project = generate_project(root, files=4, dirs=1, realistic=True, templated_lab=True)
    env = {
        **os.environ,
        "OTTOPATH": str(project),
        "OTTO_HOME": str(root / "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "_OTTO_COMPLETE": "complete_bash",
    }
    words, cword = _site(TESTS_REPRESENTATIVE)
    seed = record_cold_tab(root, {**env, "COMP_WORDS": words, "COMP_CWORD": cword}, "seed")
    assert _answer(TESTS_REPRESENTATIVE) <= set(seed.stdout.split()), "control: no tables"
    prime_tab({**env, "COMP_WORDS": "otto ", "COMP_CWORD": "1"})
    return WarmHome(root, env)


def _shim_only(tab: RecordedTab) -> None:
    """Assert no :data:`SHIM_DENY` root is in the child's ``sys.modules``."""
    denied = [m for m in tab.modules if _HARNESS.loaded_root(m, SHIM_DENY)]
    assert denied == [], f"the TAB imported {denied}, under a denied root {SHIM_DENY}"


def test_static_costs_what_payload_costs(warm):
    static = warm.tab(STATIC_SITE, "static")
    payload = warm.tab(_site(NAMES_REPRESENTATIVE), "payload")
    for tab in (static, payload):
        _shim_only(tab)
    assert {"DEBUG", "INFO", "ERROR"} <= set(static.stdout.split())
    assert _answer(NAMES_REPRESENTATIVE) <= set(payload.stdout.split())
    assert_same_cost(static, payload)


def test_each_sub_variant_maps_to_its_representative():
    """The site map sends each sub-variant kind to its representative's surface, so the
    cost comparisons below compare against the surface that measures those sites."""
    profiles = _HARNESS.SITE_PROFILES
    assert {profiles[k] for k in ("payload", "static", "none")} == {NAMES_REPRESENTATIVE}
    assert {profiles[k] for k in ("tests", "markers")} == {TESTS_REPRESENTATIVE}


def test_the_none_site_is_a_none_site_the_enumeration_yields():
    """``NONE_SITE`` is a TAB on a ``none`` site the enumeration yields, through one of its flags.

    Membership, not position: a new ``none`` option ahead of it changes nothing.
    """
    words, cword = NONE_SITE
    *line, flag = words.split()
    assert words.endswith(" ")
    assert cword == str(len(line) + 1)
    tree = serialised_cli_tree()
    nodes = {(e.path, e.host_class): e.node for e in completion_nodes(tree)}
    matches = [
        site
        for site in completion_sites(tree)
        if site.source["kind"] == "none"
        and ["otto", *site.path] == line
        and any(
            p["name"] == site.param and flag in p["flags"]
            for p in nodes[(site.path, site.host_class)]["params"]
        )
    ]
    assert matches, NONE_SITE


def test_none_costs_what_payload_costs_and_answers_nothing(warm):
    none = warm.tab(NONE_SITE, "none")
    payload = warm.tab(_site(NAMES_REPRESENTATIVE), "payload")
    for tab in (none, payload):
        _shim_only(tab)
    assert none.stdout.split() == []
    assert _answer(NAMES_REPRESENTATIVE) <= set(payload.stdout.split())
    assert_same_cost(none, payload)


def test_markers_costs_what_tests_costs(warm):
    markers = warm.tab(MARKERS_SITE, "markers")
    tests = warm.tab(_site(TESTS_REPRESENTATIVE), "tests")
    for tab in (markers, tests):
        _shim_only(tab)
        assert "subprocess" not in tab.modules, "the TAB started a collect child"
    assert "xfail" in markers.stdout.split(), "a marker pytest itself registers"
    assert _answer(TESTS_REPRESENTATIVE) <= set(tests.stdout.split())
    assert_same_cost(markers, tests)


def test_comparison_rejects_an_injected_stat(warm):
    clean = warm.tab(STATIC_SITE, "clean")
    assert_same_cost(clean, warm.tab(STATIC_SITE, "again"))  # control: a TAB matches itself
    injected = warm.tab(STATIC_SITE, "stat", inject="os.stat(os.devnull)")
    with pytest.raises(AssertionError, match="stats"):
        assert_same_cost(clean, injected)


def test_deny_check_rejects_an_injected_forbidden_import(warm):
    clean = warm.tab(STATIC_SITE, "clean")
    _shim_only(clean)  # control: the same TAB, uninjected, passes
    injected = warm.tab(STATIC_SITE, "typer", inject="import typer")
    with pytest.raises(AssertionError, match="typer"):
        _shim_only(injected)
