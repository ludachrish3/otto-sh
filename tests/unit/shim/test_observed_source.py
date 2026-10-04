"""The shim answers `observed` sites from the namespace, with the two TTLs, and never hands over."""

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


def test_containers_without_on_are_host_ids_then_names_then_ids():
    param = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "on"},
    }
    payloads = sc.Payloads(NAMES, None, _observed())
    assert sc._source_values(param, "", [], payloads, _res(), now=NOW) == [
        "dut1.integration.api",
        "east-i-api-1",
        "other-1",
        "3f9a",
        "aaaa",
    ]


def test_containers_with_on_are_that_hosts_only():
    param = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "on"},
    }
    payloads = sc.Payloads(NAMES, None, _observed())
    assert sc._source_values(param, "", [], payloads, _res({"on": "dut1"}), now=NOW) == [
        "east-i-api-1",
        "3f9a",
    ]
    assert sc._source_values(param, "", [], payloads, _res({"on": "dut2"}), now=NOW) == []


def test_an_expired_containers_entry_is_dropped_while_the_images_entry_lives():
    containers = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "on"},
    }
    tag = {"name": "tag", "source": {"kind": "observed", "key": "images", "by_option": "on"}}
    payloads = sc.Payloads(NAMES, None, _observed(containers_age=16 * 60, images_age=2 * 3600))
    assert sc._source_values(containers, "", [], payloads, _res({"on": "dut1"}), now=NOW) == []
    assert sc._source_values(tag, "", [], payloads, _res({"on": "dut1"}), now=NOW) == ["api:latest"]
    payloads = sc.Payloads(NAMES, None, _observed(images_age=25 * 3600))
    assert sc._source_values(tag, "", [], payloads, _res({"on": "dut1"}), now=NOW) == []


def test_the_ttl_boundary_mirrors_the_python_reader():
    """Exactly ``ttl`` old is fresh; one second more is not; a future stamp is fresh —
    the same rule as ``completion_cache._fresh_lists``, change both or neither."""
    containers = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "on"},
    }
    ttl = sc.DOCKER_OBSERVED_CONTAINERS_TTL_SECONDS
    res = _res({"on": "dut1"})

    def names(age):
        payloads = sc.Payloads(NAMES, None, _observed(containers_age=age))
        return sc._source_values(containers, "", [], payloads, res, now=NOW)

    assert names(ttl) == ["east-i-api-1", "3f9a"]
    assert names(ttl + 1) == []
    assert names(-60) == ["east-i-api-1", "3f9a"]


def test_tags_need_on():
    tag = {"name": "tag", "source": {"kind": "observed", "key": "images", "by_option": "on"}}
    assert (
        sc._source_values(tag, "", [], sc.Payloads(NAMES, None, _observed()), _res(), now=NOW) == []
    )


def test_a_missing_or_malformed_namespace_answers_the_declared_tier_only():
    param = {
        "name": "container",
        "source": {"kind": "observed", "key": "containers", "by_option": "on"},
    }
    for observed in (None, "junk", {"schema_version": 99, "hosts": {}}):
        payloads = sc.Payloads(NAMES, None, observed)
        assert sc._source_values(param, "", [], payloads, _res(), now=NOW) == [
            "dut1.integration.api"
        ]


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
                    {"name": "on", "flags": ["--on"], "takes_value": True, "nargs": 1},
                    {"name": "tail", "flags": ["--tail"], "takes_value": True, "nargs": 1},
                ],
            },
        },
    }
    res = sc.resolve(tree, ["logs", "--on", "dut1", "web-1", "--tail", "5"], {})
    assert res.option_values == {"on": "dut1", "tail": "5"}
    assert res.positional_words == ["web-1"]
