"""The verdict model: what one check said about one (protocol, port).

States are the spec's eight; the four in :data:`UNKNOWN_STATES` never reach
the drift table or the pin. Tiers rank by strength so two checks on the same
pair collapse to the one that knows more (:func:`merge_verdicts`).
"""

from dataclasses import dataclass
from typing import Literal

Kind = Literal["term", "transfer", "monitor"]
State = Literal[
    "supported",
    "login-failed",
    "service-mismatch",
    "closed",
    "listening",
    "timeout",
    "no-session",
    "not-checkable",
]
Tier = Literal["login", "session", "userland", "inventory", "dial"]

UNKNOWN_STATES: frozenset[str] = frozenset({"listening", "timeout", "no-session", "not-checkable"})
"""States that are questions, not answers: never drift, never pinned."""

ANSWERED_STATES: frozenset[str] = frozenset({"supported", "service-mismatch", "login-failed"})
"""States in which the service itself answered: a ``listening`` row never displaces one."""

TIER_STRENGTH: dict[str, int] = {"login": 4, "session": 3, "userland": 3, "inventory": 2, "dial": 1}

_KIND_ORDER: dict[str, int] = {"term": 0, "transfer": 1, "monitor": 2}

# Budgets. Module constants so tests inject them; the spec names every value.
SURVEY_BUDGET_S = 90.0
INVENTORY_TIMEOUT_S = 5.0
DIAL_TIMEOUT_S = 2.0
SWEEP_DIAL_TIMEOUT_S = 0.5
SWEEP_CONCURRENCY = 2
LOGIN_TIMEOUT_S = 10.0
SNMP_TIMEOUT_S = 2.0
SNMP_RETRIES = 1
SNMP_SYSUPTIME_OID = "1.3.6.1.2.1.1.3.0"


@dataclass(frozen=True, slots=True)
class ProtocolVerdict:
    """One row of the protocol table."""

    protocol: str
    kind: Kind
    port: int
    state: State
    tier: Tier
    vantage: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A (protocol, port) the survey will check; ``declared`` = from the host's options."""

    protocol: str
    kind: Kind
    port: int
    declared: bool


def _supersedes(new: ProtocolVerdict, held: ProtocolVerdict) -> bool:
    """Whether *new* replaces *held* for the same (protocol, port).

    Tier order decides between two verdicts that both answered. It does NOT
    decide when one of them is ``listening``: a bound socket is a question and
    an answered check is the answer, so the answer wins from whatever tier it
    came. The live case is the declared snmp port, where the inventory sees
    the socket (``listening``, tier ``inventory``) and the active sysUpTime
    GET answers (``supported``, tier ``dial``) -- ranking by tier alone would
    keep the question, and a verified agent could never be reported working,
    never reach ``supported`` and never produce a pin fragment.
    """
    if new.state == "listening" and held.state in ANSWERED_STATES:
        return False
    if held.state == "listening" and new.state in ANSWERED_STATES:
        return True
    return TIER_STRENGTH[new.tier] > TIER_STRENGTH[held.tier]


def merge_verdicts(verdicts: list[ProtocolVerdict]) -> list[ProtocolVerdict]:
    """One row per (protocol, port): the strongest verdict wins, a tie keeps the first."""
    best: dict[tuple[str, int], ProtocolVerdict] = {}
    for v in verdicts:
        key = (v.protocol, v.port)
        held = best.get(key)
        if held is None or _supersedes(v, held):
            best[key] = v
    return sorted(best.values(), key=lambda v: (_KIND_ORDER[v.kind], v.protocol, v.port))
