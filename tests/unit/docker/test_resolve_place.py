"""Placement tables (spec §5): knob precedence, scope, loud ambiguity."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from otto.config.lab import Lab
from otto.docker import resolve as resolve_mod
from otto.docker.resolve import (
    SelectedFragment,
    Selection,
    UseCaseResolutionError,
    resolve_placement,
)
from otto.host.unix_host import UnixHost

from .test_resolve_select import _frag, _repo  # reuse the table builders


def _host(hid, *, roles=(), capable=True, source_lab="unix"):
    h = MagicMock(spec=UnixHost)
    h.id = hid
    h.roles = list(roles)
    h.docker_capable = capable
    h.source_lab = source_lab
    return h


def _lab(*hosts):
    lab = MagicMock(spec=Lab)
    lab.name = "unix"
    lab.hosts = {h.id: h for h in hosts}
    return lab


def _selection(*frags_with_repos):
    return Selection(
        use_case="integration",
        fragments=[SelectedFragment(r, f) for r, f in frags_with_repos],
    )


@pytest.fixture(autouse=True)
def _admit_all_scopes():
    with patch.object(resolve_mod, "scope_for_repo", return_value=None):
        yield


def test_on_collapses_every_fragment():
    lab = _lab(_host("test1"), _host("test3", roles=["edge"]))
    sel = _selection((_repo("a"), _frag(role="edge")), (_repo("b"), _frag()))
    assert list(resolve_placement(sel, lab, on="test1")) == ["test1"]


def test_placement_pin_beats_role_match():
    lab = _lab(_host("test1", roles=["edge"]), _host("test3", roles=["edge"]))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": "test3"})))
    assert list(resolve_placement(sel, lab)) == ["test3"]


def test_lab_qualified_pin():
    lab = _lab(_host("test3", source_lab="unix"))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": "unix:test3"})))
    assert list(resolve_placement(sel, lab)) == ["test3"]


def test_lab_qualified_pin_wrong_lab_refused():
    lab = _lab(_host("test3", source_lab="unix"))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": "alt:test3"})))
    with pytest.raises(UseCaseResolutionError, match="is lab-qualified but") as e:
        resolve_placement(sel, lab)
    assert "alt" in str(e.value)


def test_unique_role_match():
    lab = _lab(_host("test1"), _host("test3", roles=["edge"]))
    sel = _selection((_repo("a"), _frag(role="edge")))
    assert list(resolve_placement(sel, lab)) == ["test3"]


def test_ambiguous_role_lists_candidates_and_knobs():
    lab = _lab(_host("test1", roles=["edge"]), _host("test3", roles=["edge"]))
    sel = _selection((_repo("a"), _frag(role="edge")))
    with pytest.raises(UseCaseResolutionError, match="is ambiguous — carried by") as e:
        resolve_placement(sel, lab)
    msg = str(e.value)
    assert "test1" in msg
    assert "test3" in msg
    assert "--on" in msg
    assert "placement" in msg


def test_role_zero_carriers_refused():
    """Role declared, but not one in-scope host carries it (resolve.py raise #1)."""
    lab = _lab(_host("test1"), _host("test3"))
    sel = _selection((_repo("a"), _frag(role="edge")))
    with pytest.raises(UseCaseResolutionError, match="carries role") as e:
        resolve_placement(sel, lab)
    assert "'edge'" in str(e.value)


def test_no_role_single_capable_host_fallback():
    lab = _lab(_host("test3"), _host("dut", capable=False))
    sel = _selection((_repo("a"), _frag()))
    assert list(resolve_placement(sel, lab)) == ["test3"]


def test_no_role_multiple_capable_hosts_refused():
    lab = _lab(_host("test1"), _host("test3"))
    sel = _selection((_repo("a"), _frag()))
    with pytest.raises(UseCaseResolutionError, match="declares no role and the repo's scope holds"):
        resolve_placement(sel, lab)


def test_scope_narrows_role_candidates():
    lab = _lab(_host("test1", roles=["edge"]), _host("test3", roles=["edge"]))
    sel = _selection((_repo("a"), _frag(role="edge")))
    with patch.object(
        resolve_mod,
        "repo_targets",
        side_effect=lambda scope, lab_name, hid: hid == "test3",
    ):
        assert list(resolve_placement(sel, lab)) == ["test3"]


def test_fragments_group_by_resolved_host():
    lab = _lab(_host("test1", roles=["builder"]), _host("test3", roles=["edge"]))
    sel = _selection(
        (_repo("a"), _frag(role="edge")),
        (_repo("b"), _frag(role="builder")),
        (_repo("c"), _frag(role="edge")),
    )
    placed = resolve_placement(sel, lab)
    assert [f.repo.name for f in placed["test3"]] == ["a", "c"]
    assert [f.repo.name for f in placed["test1"]] == ["b"]


def test_unqualified_pin_names_unknown_host_refused():
    """A bare (unqualified) pin has no other-lab excuse: not found is a typo."""
    lab = _lab(_host("test1"))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": "ghost"})))
    with pytest.raises(UseCaseResolutionError, match="names no host in the active lab"):
        resolve_placement(sel, lab)


def test_pin_names_non_capable_host_refused():
    lab = _lab(_host("test3", capable=False))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": "test3"})))
    with pytest.raises(UseCaseResolutionError, match="must name a docker-capable unix host"):
        resolve_placement(sel, lab)


def test_placement_key_mismatched_role_refused():
    """A placement dict carrying a key that isn't this fragment's role can never
    fire for this fragment in any lab (the role is fixed) — that's a config
    typo, not a legitimate multi-lab knob, so it is refused loudly."""
    lab = _lab(_host("test3", roles=["edge"]))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edgee": "test3"})))
    with pytest.raises(UseCaseResolutionError, match="can never apply to this fragment"):
        resolve_placement(sel, lab)


def test_placement_without_role_refused():
    """A placement table with no role at all can never be consulted, in any lab."""
    lab = _lab(_host("test3"))
    sel = _selection((_repo("a"), _frag(role=None, placement={"edge": "test3"})))
    with pytest.raises(UseCaseResolutionError, match="can never apply to this fragment"):
        resolve_placement(sel, lab)


def test_degenerate_pin_empty_lab_component_refused():
    lab = _lab(_host("test3"))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": ":test3"})))
    with pytest.raises(UseCaseResolutionError, match="needs both parts non-empty"):
        resolve_placement(sel, lab)


def test_degenerate_pin_empty_host_component_refused():
    lab = _lab(_host("test3"))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": "unix:"})))
    with pytest.raises(UseCaseResolutionError, match="needs both parts non-empty"):
        resolve_placement(sel, lab)


def test_pin_addressed_to_inactive_lab_falls_through_to_role_match():
    """A lab-qualified pin naming a lab that is simply not the active one (and
    whose host cannot be found here either) is legitimate multi-lab config
    addressed elsewhere — it stays silently inert, not a refusal, and
    resolution falls through to the next knob (role match)."""
    lab = _lab(_host("test1"), _host("test3", roles=["edge"]))
    sel = _selection((_repo("a"), _frag(role="edge", placement={"edge": "otherlab:ghost"})))
    assert list(resolve_placement(sel, lab)) == ["test3"]
