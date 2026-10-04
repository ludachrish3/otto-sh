"""The shim answers `observed` sites from the namespace, with the two TTLs, and never hands over."""

import pytest

from otto import _shim_complete as sc
from otto.config import completion_cache as cc

NOW = 1_800_000_000.0


def _observed(containers_age=60, images_age=60):
    return {
        "schema_version": 1,
        "hosts": {
            "dut1": {
                "images": {
                    "observed_at": NOW - images_age,
                    "refs": ["api:latest"],
                    "ids": ["sha-a"],
                },
                "containers": {
                    "observed_at": NOW - containers_age,
                    "names": ["east-i-api-1"],
                    "ids": ["3f9a"],
                },
            },
            "dut9": {
                "containers": {"observed_at": NOW - 10, "names": ["other-1"], "ids": ["aaaa"]}
            },
        },
    }


NAMES = {
    "hosts": ["dut1", "dut2", "dut1.integration.api"],
    "docker_hosts": ["dut1"],
    "docker_default_parent_by_lab": {"east": "dut1"},
    "docker_services_by_use_case": {"integration": ["api", "db"]},
}


def _res(option_values=None, positional_words=None):
    res = sc.Resolution({"name": "logs", "group": False, "params": [], "commands": {}})
    res.option_values = dict(option_values or {})
    res.positional_words = list(positional_words or [])
    return res


def test_constants_match_the_python_side():
    assert sc.DOCKER_OBSERVED_KEY == cc.DOCKER_OBSERVED_KEY
    assert sc.DOCKER_OBSERVED_SCHEMA == cc.DOCKER_OBSERVED_SCHEMA_VERSION
    assert sc.DOCKER_OBSERVED_CONTAINERS_TTL_SECONDS == cc.DOCKER_OBSERVED_CONTAINERS_TTL_SECONDS
    assert sc.DOCKER_OBSERVED_IMAGES_TTL_SECONDS == cc.DOCKER_OBSERVED_IMAGES_TTL_SECONDS


def test_without_parent_the_default_parents_names_are_offered():
    containers = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "parent"},
    }
    payloads = sc.Payloads(NAMES, None, _observed())
    assert sc._source_values(containers, "", ["east"], payloads, _res(), now=NOW) == [
        "east-i-api-1",
        "3f9a",
    ]
    # No entry for west: the rule refused there.
    assert sc._source_values(containers, "", ["west"], payloads, _res(), now=NOW) == []
    # No lab selected: the map's only entry.
    assert sc._source_values(containers, "", [], payloads, _res(), now=NOW) == [
        "east-i-api-1",
        "3f9a",
    ]


def test_with_parent_that_hosts_names_are_offered_whatever_the_default():
    containers = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "parent"},
    }
    payloads = sc.Payloads(NAMES, None, _observed())
    assert sc._source_values(
        containers, "", ["east"], payloads, _res({"parent": "dut9"}), now=NOW
    ) == ["other-1", "aaaa"]
    assert (
        sc._source_values(containers, "", ["east"], payloads, _res({"parent": "dut2"}), now=NOW)
        == []
    )


def test_default_parent_mirrors_the_typer_rule():
    """Every selected lab resolves to one entry, else None; no labs: the only entry, else None."""
    names = {"docker_default_parent_by_lab": {"east": "dut1", "west": "dut3", "north": "dut1"}}
    assert sc._default_parent(names, ["east"]) == "dut1"
    assert sc._default_parent(names, ["east", "north"]) == "dut1"
    assert sc._default_parent(names, ["east", "west"]) is None
    assert sc._default_parent(names, ["south"]) is None
    # One selected lab resolves, another is absent (its rule refused): nothing offered.
    assert sc._default_parent(names, ["east", "south"]) is None
    assert sc._default_parent(names, ["south", "east"]) is None
    assert sc._default_parent(names, []) is None
    only = {"docker_default_parent_by_lab": {"east": "dut1"}}
    assert sc._default_parent(only, []) == "dut1"
    assert sc._default_parent(only, ["east", "south"]) is None
    assert sc._default_parent({"docker_default_parent_by_lab": ["x"]}, []) is None
    assert sc._default_parent({}, ["east"]) is None


# The mirror table: ``(by_lab, labs, expected)``. Twin of ``MIRROR_ROWS`` in
# tests/unit/cli/test_docker_default_parent_tab.py, which runs the SAME rows through Typer's
# ``_default_parent_for_tab`` -- keep the two verbatim (the "change both or neither" contract
# is this table). The real collector only writes ``dict[str, str]``, so the non-str rows are the
# only thing that pins the ``isinstance`` filter on each branch.
_AGREE = {"east": "dut1", "west": "dut3", "north": "dut1"}
MIRROR_ROWS = [
    pytest.param(_AGREE, ["east"], "dut1", id="one-lab-resolves"),
    pytest.param(_AGREE, ["east", "north"], "dut1", id="labs-agree"),
    pytest.param(_AGREE, ["east", "west"], None, id="labs-disagree"),
    pytest.param(_AGREE, ["south"], None, id="selected-lab-absent"),
    pytest.param(_AGREE, ["east", "south"], None, id="one-absent-after-one-resolves"),
    pytest.param(_AGREE, ["south", "east"], None, id="one-absent-before-one-resolves"),
    pytest.param(_AGREE, [], None, id="no-labs-two-str-entries"),
    pytest.param({"east": 5}, ["east"], None, id="selected-lab-non-str-int"),
    pytest.param({"east": None}, ["east"], None, id="selected-lab-non-str-none"),
    pytest.param({"east": 5, "west": 5}, ["east", "west"], None, id="labs-agree-on-a-non-str"),
    pytest.param({"east": "dut1"}, [], "dut1", id="no-labs-one-str-entry"),
    pytest.param({"east": "dut1", "west": 7}, [], "dut1", id="no-labs-non-str-entry-ignored"),
    pytest.param({"east": 5}, [], None, id="no-labs-only-a-non-str-entry"),
    pytest.param({"east": "dut1"}, ["east", "south"], None, id="single-entry-but-lab-absent"),
    pytest.param(["east"], [], None, id="non-dict-map-no-labs"),
    pytest.param(["east"], ["east"], None, id="non-dict-map-with-labs"),
    pytest.param("dut1", [], None, id="str-map"),
]


@pytest.mark.parametrize(("by_lab", "labs", "expected"), MIRROR_ROWS)
def test_default_parent_mirror_table(by_lab, labs, expected):
    """Every row's outcome; Typer's twin of this test runs the identical rows."""
    assert sc._default_parent({"docker_default_parent_by_lab": by_lab}, labs) == expected


def test_an_expired_containers_entry_is_dropped_while_the_images_entry_lives():
    containers = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "parent"},
    }
    tag = {"name": "tag", "source": {"kind": "observed", "key": "images", "by_option": "parent"}}
    payloads = sc.Payloads(NAMES, None, _observed(containers_age=16 * 60, images_age=2 * 3600))
    assert sc._source_values(containers, "", [], payloads, _res({"parent": "dut1"}), now=NOW) == []
    assert sc._source_values(tag, "", [], payloads, _res({"parent": "dut1"}), now=NOW) == [
        "api:latest"
    ]
    payloads = sc.Payloads(NAMES, None, _observed(images_age=25 * 3600))
    assert sc._source_values(tag, "", [], payloads, _res({"parent": "dut1"}), now=NOW) == []


def test_the_ttl_boundary_mirrors_the_python_reader():
    """Exactly ``ttl`` old is fresh; one second more is not; a future stamp is fresh —
    the same rule as ``completion_cache._fresh_lists``, change both or neither."""
    containers = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "parent"},
    }
    ttl = sc.DOCKER_OBSERVED_CONTAINERS_TTL_SECONDS
    res = _res({"parent": "dut1"})

    def names(age):
        payloads = sc.Payloads(NAMES, None, _observed(containers_age=age))
        return sc._source_values(containers, "", [], payloads, res, now=NOW)

    assert names(ttl) == ["east-i-api-1", "3f9a"]
    assert names(ttl + 1) == []
    assert names(-60) == ["east-i-api-1", "3f9a"]


def test_tags_without_a_parent_or_a_default_are_empty():
    tag = {"name": "tag", "source": {"kind": "observed", "key": "images", "by_option": "parent"}}
    bare = {"hosts": ["dut1"]}
    assert (
        sc._source_values(tag, "", [], sc.Payloads(bare, None, _observed()), _res(), now=NOW) == []
    )


def test_a_missing_or_malformed_namespace_answers_nothing():
    param = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "parent"},
    }
    for observed in (None, "junk", {"schema_version": 99, "hosts": {}}):
        payloads = sc.Payloads(NAMES, None, observed)
        assert sc._source_values(param, "", ["east"], payloads, _res(), now=NOW) == []


def test_services_scope_by_the_use_case_positional():
    param = {
        "name": "service",
        "source": {
            "kind": "payload",
            "key": "docker_services_by_use_case",
            "by_positional": "use_case",
            "sort": True,
        },
    }
    node = {
        "name": "up",
        "group": False,
        "commands": {},
        "params": [
            {"name": "use_case", "flags": [], "takes_value": True, "nargs": 1},
            {"name": "service", "flags": [], "takes_value": True, "nargs": -1},
        ],
    }
    res = sc.Resolution(node)
    res.positional_words = ["integration"]
    assert sc._source_values(param, "d", [], sc.Payloads(NAMES, None, None), res) == ["db"]
    res.positional_words = []
    assert sc._source_values(param, "", [], sc.Payloads(NAMES, None, None), res) == []


def test_resolve_records_option_values_and_positional_words():
    tree = {
        "name": "otto",
        "group": True,
        "params": [],
        "commands": {
            "logs": {
                "name": "logs",
                "group": False,
                "commands": {},
                "params": [
                    {"name": "container", "flags": [], "takes_value": True, "nargs": 1},
                    {"name": "parent", "flags": ["--parent"], "takes_value": True, "nargs": 1},
                    {"name": "tail", "flags": ["--tail"], "takes_value": True, "nargs": 1},
                ],
            },
        },
    }
    res = sc.resolve(tree, ["logs", "--parent", "dut1", "web-1", "--tail", "5"], {})
    assert res.option_values == {"parent": "dut1", "tail": "5"}
    assert res.positional_words == ["web-1"]
