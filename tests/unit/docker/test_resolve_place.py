"""Every selected fragment lands on the one parent (spec §2)."""

from unittest.mock import MagicMock

import pytest

from otto.docker.resolve import (
    SelectedFragment,
    Selection,
    UseCaseResolutionError,
    place,
    resolve_fact_refs,
)
from otto.host.unix_host import UnixHost

from .test_resolve_select import _frag, _repo  # reuse the table builders


@pytest.fixture
def selection_two_repos() -> Selection:
    return Selection(
        use_case="integration",
        fragments=[
            SelectedFragment(_repo("a"), _frag()),
            SelectedFragment(_repo("b"), _frag()),
        ],
    )


@pytest.fixture
def parent_host() -> UnixHost:
    host = MagicMock(spec=UnixHost)
    host.id = "test3"
    host.docker_capable = True
    return host


def test_place_puts_every_fragment_on_the_parent(selection_two_repos, parent_host):
    placed = place(selection_two_repos, parent_host)
    assert list(placed) == [parent_host.id]
    assert placed[parent_host.id] == selection_two_repos.fragments


def test_place_hands_back_a_list_the_caller_owns(selection_two_repos, parent_host):
    placed = place(selection_two_repos, parent_host)
    assert placed[parent_host.id] is not selection_two_repos.fragments


def test_a_role_fact_ref_is_refused_with_the_replacement_named():
    with pytest.raises(
        UseCaseResolutionError, match=r"role facts are gone; use \$\{otto:parent\.<fact>\}"
    ):
        resolve_fact_refs({"DB": "${otto:role.db.addr}"}, {"parent": {"addr": "1.2.3.4"}})


def test_a_parent_fact_ref_still_resolves():
    facts = {"parent": {"id": "test3", "addr": "1.2.3.4"}}
    assert resolve_fact_refs({"DB": "${otto:parent.addr}"}, facts) == {"DB": "1.2.3.4"}
