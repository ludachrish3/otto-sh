"""Composite rules under v2: existence, element-wholesale, labs-entry wholesale (spec §6, §9)."""

import json
import logging

import pytest

from otto.config.lab import Lab
from otto.examples.lab_repository import ExampleLabRepository
from otto.labs import (
    CompositeLabRepository,
    HostSummary,
    LabNotFoundError,
    LabRepositoryError,
    LabSource,
)
from otto.labs.json_repository import JsonFileLabRepository

_CREDS = [{"login": "u", "password": "p"}]


def _h(element, ip, element_id=None, board=None):
    d = {"ip": ip, "element": element, "creds": _CREDS}
    if element_id is not None:
        d["element_id"] = element_id
    if board is not None:
        d["board"] = board
    return d


def _comp(*sources):
    return CompositeLabRepository(
        [
            LabSource(label=lbl, repository=ExampleLabRepository(labs=labs, resources=res))
            for lbl, labs, res in sources
        ]
    )


def test_element_replaced_wholesale_and_warns(caplog) -> None:
    comp = _comp(
        (
            "r/global",
            {"site": [_h("dut", "10.0.0.1", 3, "cpu"), _h("dut", "10.0.0.2", 3, "mgmt")]},
            None,
        ),
        ("r/local", {"site": [_h("dut", "10.9.9.9", 3, "cpu")]}, None),
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        lab = comp.load_lab("site")
    # The global element's mgmt board is gone with its element.
    assert set(lab.hosts) == {"dut3_cpu"}
    assert lab.hosts["dut3_cpu"].ip == "10.9.9.9"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("('dut', 3)" in m and "r/local" in m and "r/global" in m for m in msgs)


def test_finer_host_level_merge_is_not_what_happens() -> None:
    """Red-first guard for the decision: a host-granular merge would keep dut3_mgmt."""
    comp = _comp(
        ("a", {"site": [_h("dut", "10.0.0.1", 3, "cpu"), _h("dut", "10.0.0.2", 3, "mgmt")]}, None),
        ("b", {"site": [_h("dut", "10.9.9.9", 3, "cpu")]}, None),
    )
    assert "dut3_mgmt" not in comp.load_lab("site").hosts


def test_labs_entry_replaced_wholesale_by_declaring_source(caplog) -> None:
    """Spec §9 "labs entry collision across sources": later wins, warning names BOTH.

    The labels are deliberately distinctive and asserted as one ORDERED phrase
    in a single record. Two weaker forms were rejected:

    * ``"labs entry" in m and "'site'" in m`` says nothing about the sources,
      which is half of what the ledger row promises;
    * two separate ``in`` checks against short labels would be near-vacuous —
      with sources named ``a``/``b`` the literal string ``"labs entry"``
      already contains an ``a``, so the assertion passes on the format string
      alone, and even with distinct labels a per-label check cannot tell
      ``X overrides Y`` from ``Y overrides X``.

    The direction is the load-bearing half: this warning is the only place a
    user is told WHICH declaration is live, and the two arguments are adjacent
    ``%s``s in one format call, so transposing them is a one-character defect.
    """
    comp = _comp(
        ("r/global", {"site": [_h("x", "10.0.0.1")]}, {"site": {"old", "shared"}}),
        ("r/local", {"site": [_h("y", "10.0.0.2")]}, {"site": {"new"}}),
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        lab = comp.load_lab("site")
    assert lab.resources == {"new"}  # not unioned: 'shared' went with the replaced entry
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "labs entry" in m and "'site'" in m and "r/local overrides r/global" in m for m in msgs
    )


class _MembersOnly:
    """A source that MATCHES a name but declares nothing — the migration mistake."""

    def list_labs(self):
        return []

    def load_lab(self, name, preferences=None):
        from otto.host.factory import create_host_from_dict

        lab = Lab(name=name)
        lab.add_host(create_host_from_dict(_h("x", "10.0.0.1"), lab_name=name))
        return lab


def test_undeclared_lab_is_not_found_even_with_matching_members() -> None:
    comp = CompositeLabRepository([LabSource(label="m", repository=_MembersOnly())])
    with pytest.raises(LabNotFoundError, match="declare"):
        comp.load_lab("ghost")


def test_undeclared_but_matched_says_so_and_undeclared_unmatched_does_not() -> None:
    """The second sentence, and the load-before-decide ordering that buys it.

    ``load_lab`` deliberately loads every source BEFORE ruling on existence,
    purely so it can tell "nothing declares this" from "your elements match it
    but nothing declares it" — the actual mistake when a v1 file is migrated.
    Checking existence first would be cheaper and would lose that sentence, so
    both halves are pinned: present when members matched, ABSENT when they did
    not (otherwise the message would blame elements that do not exist).
    """
    matched = CompositeLabRepository([LabSource(label="m", repository=_MembersOnly())])
    with pytest.raises(LabNotFoundError) as matched_err:
        matched.load_lab("ghost")
    assert "not declared by any configured source (m)" in str(matched_err.value)
    assert "elements match it by pattern but nothing declares it" in str(matched_err.value)

    empty = _comp(("a", {"other": [_h("x", "10.0.0.1")]}, None))
    with pytest.raises(LabNotFoundError) as unmatched_err:
        empty.load_lab("ghost")
    assert "not declared by any configured source (a)" in str(unmatched_err.value)
    assert "elements match it" not in str(unmatched_err.value)


def test_declared_but_memberless_errors_naming_the_source() -> None:
    comp = _comp(("a", {"site": []}, {"site": set()}))
    with pytest.raises(LabRepositoryError, match=r"'site'.*declared.*no element"):
        comp.load_lab("site")


def test_host_id_clash_across_distinct_elements_after_merge_errors() -> None:
    comp = _comp(
        ("a", {"site": [_h("dut", "10.0.0.1", 3)]}, None),  # id dut3 from element ('dut', 3)
        ("b", {"site": [_h("dut3", "10.0.0.2")]}, None),  # id dut3 from element ('dut3', None)
    )
    with pytest.raises(LabRepositoryError, match=r"dut3.*\('dut', 3\).*\('dut3', None\)"):
        comp.load_lab("site")


def test_summaries_survive_a_backend_pattern_that_is_not_a_regex() -> None:
    """Completion must never raise into the user's TAB (spec §6.3).

    ``lab_patterns`` is open to any backend — the json one rejects an
    uncompilable pattern at parse, a custom one need not — and re-resolving it
    here is a live ``re.fullmatch``, so an unusable pattern must degrade to
    "no extra labs", never to a traceback in the shell.
    """

    class BadPattern:
        def list_labs(self):
            # Two declared labs, one of them NOT already in the summary's
            # ``labs`` — otherwise re-resolution short-circuits and the
            # unusable pattern is never compiled.
            return ["site", "other"]

        def load_lab(self, name, preferences=None):
            raise LabNotFoundError(name)

        def list_host_summaries(self):
            return [HostSummary(id="h", labs=["site"], lab_patterns=["site(["])]

    comp = CompositeLabRepository([LabSource(label="bad", repository=BadPattern())])
    (s,) = comp.list_host_summaries()
    assert s.labs == ["site"]  # the concrete membership survives; the pattern is skipped


def test_summaries_resolve_patterns_against_all_declared_labs(tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "lab.json").write_text(
        json.dumps(
            {
                "labs": {},
                "elements": [
                    {
                        "name": "g",
                        "labs": ["site.*"],
                        "hosts": [{"ip": "10.0.0.1", "creds": _CREDS}],
                    }
                ],
            }
        )
    )
    (b / "lab.json").write_text(json.dumps({"labs": {"site.b4": {}}, "elements": []}))
    comp = CompositeLabRepository(
        [
            LabSource(label="a", repository=JsonFileLabRepository([a])),
            LabSource(label="b", repository=JsonFileLabRepository([b])),
        ]
    )
    (s,) = comp.list_host_summaries()
    assert s.labs == ["site.b4"]
    assert set(comp.load_lab("site.b4").hosts) == {"g"}
