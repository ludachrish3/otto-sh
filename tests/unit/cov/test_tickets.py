"""``[coverage.tickets]`` runtime resolution — commit-message ticket ids."""

import pytest

from otto.coverage.tickets import TicketConfigError, build_ticket_spec, load_ticket_spec


def test_extract_returns_whole_match_as_display_id():
    spec = build_ticket_spec(r"#(?P<num>[0-9]+)", "https://gh/x/issues/{num}")
    assert spec.extract("fix arp #1204") == ["#1204"]


def test_url_uses_named_group_not_whole_match():
    """GitHub's display id is `#1204` but its URL takes only `1204`."""
    spec = build_ticket_spec(r"#(?P<num>[0-9]+)", "https://gh/x/issues/{num}")
    assert spec.url_for("#1204") == "https://gh/x/issues/1204"


def test_whole_match_available_to_url_as_zero():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", "https://jira/browse/{0}")
    assert spec.url_for("PROJ-412") == "https://jira/browse/PROJ-412"


def test_multi_ticket_commit_yields_all_ids_deduped_in_order():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    ids = spec.extract("PROJ-412 and PROJ-388\n\nalso PROJ-412 again")
    assert ids == ["PROJ-412", "PROJ-388"]


def test_no_match_yields_empty_list():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    assert spec.extract("chore: bump deps") == []


def test_url_none_yields_none():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    assert spec.url_for("PROJ-412") is None


def test_bad_regex_fails_loud_at_build():
    with pytest.raises(TicketConfigError, match="not a valid regular expression"):
        build_ticket_spec(r"([A-Z]+", None)


def test_url_naming_unknown_group_fails_loud_at_build():
    """A template naming a group the pattern lacks must not become a render-time KeyError."""
    with pytest.raises(TicketConfigError, match="unknown group 'key'"):
        build_ticket_spec(r"#(?P<num>[0-9]+)", "https://x/{key}")


def test_load_returns_none_when_block_absent():
    assert load_ticket_spec({}) is None
    assert load_ticket_spec({"report": {"high": 90}}) is None


def test_load_builds_spec_from_raw_dict():
    spec = load_ticket_spec({"tickets": {"pattern": r"#(?P<n>[0-9]+)", "url": "u/{n}"}})
    assert spec is not None
    assert spec.extract("see #7") == ["#7"]


def test_load_without_pattern_fails_loud():
    with pytest.raises(TicketConfigError, match="requires a 'pattern'"):
        load_ticket_spec({"tickets": {"url": "u/{n}"}})
