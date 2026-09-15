"""The verdict model: one row per (protocol, port), strongest tier wins."""

import dataclasses

import pytest

from otto.host.survey.verdict import (
    TIER_STRENGTH,
    UNKNOWN_STATES,
    ProtocolVerdict,
    merge_verdicts,
)


def _v(protocol="ssh", port=22, state="closed", tier="dial", kind="term", detail=""):
    return ProtocolVerdict(
        protocol=protocol,
        kind=kind,
        port=port,
        state=state,
        tier=tier,
        vantage="controller",
        detail=detail,
    )


def test_two_tiers_on_one_pair_collapse_to_the_strongest():
    """Mutation: keep the last instead of the strongest and a dial `closed` outranks a login."""
    rows = merge_verdicts([_v(state="supported", tier="login"), _v(state="closed", tier="dial")])
    assert rows == [_v(state="supported", tier="login")]
    rows = merge_verdicts([_v(state="closed", tier="dial"), _v(state="supported", tier="login")])
    assert rows == [_v(state="supported", tier="login")]


def test_a_tie_keeps_the_first_verdict():
    rows = merge_verdicts([_v(detail="first"), _v(detail="second")])
    assert rows == [_v(detail="first")]


def test_different_ports_are_different_rows_in_kind_protocol_port_order():
    rows = merge_verdicts(
        [
            _v(protocol="scp", kind="transfer", port=22, state="supported", tier="session"),
            _v(port=2222, state="supported", tier="login"),
            _v(port=22),
            _v(protocol="snmp", kind="monitor", port=161, state="timeout", tier="dial"),
        ]
    )
    assert [(r.kind, r.protocol, r.port) for r in rows] == [
        ("term", "ssh", 22),
        ("term", "ssh", 2222),
        ("transfer", "scp", 22),
        ("monitor", "snmp", 161),
    ]


def test_the_unknown_states_are_exactly_the_four_the_spec_names():
    assert frozenset({"listening", "timeout", "no-session", "not-checkable"}) == UNKNOWN_STATES


def test_login_outranks_session_outranks_inventory_outranks_dial():
    assert (
        TIER_STRENGTH["login"]
        > TIER_STRENGTH["session"]
        == TIER_STRENGTH["userland"]
        > TIER_STRENGTH["inventory"]
        > TIER_STRENGTH["dial"]
    )


def test_verdicts_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _v().state = "supported"  # type: ignore[misc]


def test_listening_never_outranks_an_answered_verdict_whatever_the_tier():
    """A bound socket is a question; an answered check is the answer, even from a weaker tier.

    The declared snmp port is the live case: the inventory sees the socket
    (``listening``, tier ``inventory``) and the active GET answers
    (``supported``, tier ``dial``). Tier order alone would keep the question
    and the working agent could never be reported, never reach ``supported``
    and never produce a pin fragment.

    Mutation: rank by tier alone and the ``listening`` row wins both ways.
    """
    listening = _v(protocol="snmp", port=161, state="listening", tier="inventory", kind="monitor")
    answered = _v(protocol="snmp", port=161, state="supported", tier="dial", kind="monitor")
    assert merge_verdicts([listening, answered]) == [answered]
    assert merge_verdicts([answered, listening]) == [answered]


@pytest.mark.parametrize("state", ["service-mismatch", "login-failed"])
def test_listening_loses_to_every_answered_state_not_only_supported(state):
    listening = _v(state="listening", tier="inventory")
    answered = _v(state=state, tier="dial")
    assert merge_verdicts([listening, answered]) == [answered]
    assert merge_verdicts([answered, listening]) == [answered]


def test_tier_order_still_decides_between_two_answered_verdicts():
    """Mutation: let the later row win and the inventory's `closed` beats the login's answer."""
    weak = _v(state="closed", tier="inventory")
    strong = _v(state="supported", tier="login")
    assert merge_verdicts([weak, strong]) == [strong]
    assert merge_verdicts([strong, weak]) == [strong]
