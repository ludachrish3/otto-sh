"""Fact refs + env assembly (spec §6). Pure — the source mapping is passed in."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from otto.config.lab import Lab
from otto.docker import resolve as resolve_mod
from otto.docker.resolve import (
    SelectedFragment,
    Selection,
    UseCaseResolutionError,
    assemble_env,
    build_facts,
    resolve_fact_refs,
)
from otto.host.unix_host import UnixHost

from .test_resolve_select import _frag, _repo


@pytest.fixture(autouse=True)
def _admit_all_scopes():
    """No `[project]` scope narrows anything.

    build_facts calls the real scope_for_repo/repo_targets; without this,
    scope_for_repo would reach for otto's config bootstrap, which unit tests
    don't have. Individual tests override this with a nested patch to
    exercise real scoping.
    """
    with patch.object(resolve_mod, "scope_for_repo", return_value=None):
        yield


def _facts(**over):
    base = {
        "use_case": "integration",
        "compose_project": "otto-integration-u",
        "parent": {"id": "test3", "addr": "10.10.200.13"},
        "hosts": {"test3": {"addr": "10.10.200.13"}, "test1": {"addr": "10.10.200.11"}},
        "files": {"core": "/repo/docker/compose.yml"},
        "scratch_dir": "/tmp/scratch",
    }
    base.update(over)
    return base


def _host(hid, ip, *, capable=True):
    h = MagicMock(spec=UnixHost)
    h.id = hid
    h.ip = ip
    h.docker_capable = capable
    return h


def test_fact_refs_resolve():
    env = {
        "PEER": "${otto:host.test1.addr}",
        "PARENT": "${otto:parent.addr}",
        "UC": "${otto:use_case}",
        "PROJ": "${otto:compose_project}",
        "MIXED": "http://${otto:parent.addr}:8080/api",
    }
    out = resolve_fact_refs(env, _facts())
    assert out["PEER"] == "10.10.200.11"
    assert out["PARENT"] == "10.10.200.13"
    assert out["UC"] == "integration"
    assert out["PROJ"] == "otto-integration-u"
    assert out["MIXED"] == "http://10.10.200.13:8080/api"


def test_non_otto_refs_pass_through_untouched():
    out = resolve_fact_refs({"V": "${PRODUCT_VAR:-x}", "W": "$plain"}, _facts())
    assert out == {"V": "${PRODUCT_VAR:-x}", "W": "$plain"}


def test_unknown_fact_ref_is_refused_naming_the_ref():
    with pytest.raises(UseCaseResolutionError, match=r"host\.nope\.addr"):
        resolve_fact_refs({"X": "${otto:host.nope.addr}"}, _facts())


@pytest.mark.parametrize("ref", ["role.edge.addr", "role.edge.host_id", "role.edge", "role.x.y.z"])
def test_every_role_fact_ref_is_refused_naming_the_parent_replacement(ref):
    """A role ref of any shape names its replacement; none falls to "unknown"."""
    with pytest.raises(UseCaseResolutionError) as e:
        resolve_fact_refs({"X": f"${{otto:{ref}}}"}, _facts())
    msg = str(e.value)
    assert f"${{otto:{ref}}}" in msg
    assert "role facts are gone; use ${otto:parent.<fact>}" in msg
    assert "a use-case deploys on one parent" in msg
    assert "unknown fact ref" not in msg


@pytest.mark.parametrize(
    "ref",
    [
        "bogus",  # unknown namespace
        "parent",  # wrong arity (missing attr)
        "parent.name",  # wrong attr
        "host.t3.id",  # wrong attr ('id' isn't exposed, only 'addr')
        "USE_CASE",  # case-sensitive miss
        "use_case.x",  # wrong arity (extra segment)
    ],
)
def test_off_namespace_fact_refs_are_refused(ref):
    """Every shape that falls off the end of the if-chain (no KeyError raised).

    Distinct from test_unknown_fact_ref_is_refused_naming_the_ref, which enters
    the `host` arm and reaches the same raise via `except KeyError: pass` —
    these never even attempt a facts[...] lookup, so they exercise the OTHER
    route into the raise.
    """
    with pytest.raises(UseCaseResolutionError, match="unknown fact ref"):
        resolve_fact_refs({"X": f"${{otto:{ref}}}"}, _facts())


@pytest.mark.parametrize("value", ["${otto:}", "${otto:parent.addr"])
def test_malformed_otto_ref_is_refused(value):
    """An empty or unterminated otto ref ships nothing verbatim — it refuses."""
    with pytest.raises(UseCaseResolutionError, match="malformed otto fact ref"):
        resolve_fact_refs({"X": value}, _facts())


def test_assemble_env_merges_fragments_then_pass_env():
    frags = [
        SelectedFragment(_repo("a"), _frag(env={"A": "1", "SHARED": "from-a"})),
        SelectedFragment(
            _repo("b"), _frag(env={"SHARED": "from-b"}, pass_env=("EDGE_TAG", "MISSING"))
        ),
    ]
    got = assemble_env(frags, _facts(), pass_env_source={"EDGE_TAG": "v9"})
    assert got.env == {"A": "1", "SHARED": "from-b", "EDGE_TAG": "v9"}
    assert got.missing_pass_env == ["MISSING"]


def test_assemble_env_pass_env_overrides_fragment_env_on_same_name():
    """Channel 1b (pass_env) wins over channel 1a (fragment env) — order matters."""
    frags = [
        SelectedFragment(
            _repo("a"), _frag(env={"EDGE_TAG": "fragment-default"}, pass_env=("EDGE_TAG",))
        ),
    ]
    got = assemble_env(frags, _facts(), pass_env_source={"EDGE_TAG": "from-shell"})
    assert got.env == {"EDGE_TAG": "from-shell"}


def test_assemble_env_dedupes_missing_pass_env():
    frags = [
        SelectedFragment(_repo("a"), _frag(pass_env=("MISSING",))),
        SelectedFragment(_repo("b"), _frag(pass_env=("MISSING",))),
    ]
    got = assemble_env(frags, _facts(), pass_env_source={})
    assert got.missing_pass_env == ["MISSING"]


def test_build_facts_shape():
    parent = _host("test3", "10.10.200.13")
    other = _host("test1", "10.10.200.11")
    lab = MagicMock(spec=Lab)
    lab.hosts = {"test3": parent, "test1": other}
    sf = SelectedFragment(_repo("a"), _frag())
    sel = Selection(use_case="integration", fragments=[sf])
    facts = build_facts(
        sel,
        lab,
        compose_project="otto-integration-u",
        parent_id="test3",
        files={"core": "/x/compose.yml"},
        scratch_dir="/tmp/s",
    )
    assert facts["parent"] == {"id": "test3", "addr": "10.10.200.13"}
    assert facts["hosts"]["test1"] == {"addr": "10.10.200.11"}
    assert facts["files"] == {"core": "/x/compose.yml"}
    assert "roles" not in facts, "a use-case is one parent: facts carry no role table"


def test_build_facts_includes_in_scope_hosts_that_cannot_run_containers():
    """A non-docker in-scope host IS in the namespace; a non-unix host is not.

    Spec §7 defines ``hosts`` as the owning repo's scoped universe with no
    capability qualifier, and the motivating case is the one a reader tries
    first: a bench DUT that will never run a container, whose address a
    deployed service needs. The parent rule's ``docker_capable`` clause answers a
    different question ("where can this stack RUN") and must not leak into
    this one -- a `${otto:host.dut1.addr}` that refused would make the
    namespace useless for exactly the thing it exists for.

    ``UnixHost`` still gates: a host otto models with no ``ip`` at all has no
    address to publish, and ``_addr`` refuses rather than fabricates one.
    """
    edge = _host("test3", "10.10.200.13")
    dut = _host("dut1", "10.10.200.19", capable=False)
    not_unix = MagicMock()  # no spec=UnixHost -> isinstance() fails
    not_unix.id = "other"
    lab = MagicMock(spec=Lab)
    lab.hosts = {"test3": edge, "dut1": dut, "other": not_unix}
    sf = SelectedFragment(_repo("a"), _frag())
    sel = Selection(use_case="integration", fragments=[sf])
    facts = build_facts(
        sel,
        lab,
        compose_project="p",
        parent_id="test3",
        files={},
        scratch_dir="/tmp/s",
    )
    assert set(facts["hosts"]) == {"test3", "dut1"}
    assert facts["hosts"]["dut1"]["addr"] == "10.10.200.19", (
        "a host that cannot run a container still publishes its address"
    )


def test_build_facts_hosts_scoped_to_union_of_participating_repos():
    """facts["hosts"] is the UNION of every participating repo's scoped universe.

    Not lab-wide: a host outside every participating repo's scope must not
    appear (spec §7: "the owning repo's scoped universe").
    """
    t1 = _host("test1", "10.10.200.11")
    t3 = _host("test3", "10.10.200.13")
    t9 = _host("test9", "10.10.200.19")
    lab = MagicMock(spec=Lab)
    lab.hosts = {"test1": t1, "test3": t3, "test9": t9}
    sf_a = SelectedFragment(_repo("a"), _frag())
    sf_b = SelectedFragment(_repo("b"), _frag())
    sel = Selection(use_case="integration", fragments=[sf_a, sf_b])
    with (
        patch.object(resolve_mod, "scope_for_repo", side_effect=lambda name: name),
        patch.object(
            resolve_mod,
            "repo_targets",
            side_effect=lambda scope, lab_name, hid: (
                (scope, hid) in {("a", "test3"), ("b", "test1")}
            ),
        ),
    ):
        facts = build_facts(
            sel,
            lab,
            compose_project="p",
            parent_id="test3",
            files={},
            scratch_dir="/tmp/s",
        )
    assert set(facts["hosts"]) == {"test1", "test3"}


def test_build_facts_parent_names_unknown_host_refused():
    lab = MagicMock(spec=Lab)
    lab.hosts = {"test3": _host("test3", "10.10.200.13")}
    sf = SelectedFragment(_repo("a"), _frag())
    sel = Selection(use_case="integration", fragments=[sf])
    with pytest.raises(UseCaseResolutionError, match="is not in the active lab") as e:
        build_facts(
            sel,
            lab,
            compose_project="p",
            parent_id="ghost",
            files={},
            scratch_dir="/tmp/s",
        )
    assert "'ghost'" in str(e.value)


def test_build_facts_host_with_no_address_refused():
    lab = MagicMock(spec=Lab)
    lab.hosts = {"test9": _host("test9", "")}
    sf = SelectedFragment(_repo("a"), _frag())
    sel = Selection(use_case="integration", fragments=[sf])
    with pytest.raises(UseCaseResolutionError, match="has no configured address") as e:
        build_facts(
            sel,
            lab,
            compose_project="p",
            parent_id="test9",
            files={},
            scratch_dir="/tmp/s",
        )
    assert "'test9'" in str(e.value)
