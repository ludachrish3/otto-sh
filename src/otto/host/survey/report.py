"""The report: three Rich tables and a plain-text pin, in the spec's order.

Every cell and footnote is a ``Text`` so ``[ftp]`` in a cred identity is
printed, not parsed as markup. The pin stays plain text: copy-paste
integrity beats prettiness.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich import box
from rich.table import Table
from rich.text import Text

from .engine import dialling_terms

if TYPE_CHECKING:
    from ..host import BaseHost
    from .engine import Survey

_DEAD = frozenset({"login-failed", "service-mismatch", "closed"})
_COLUMNS = ["protocol", "kind", "port", "state", "tier", "vantage", "detail"]


@dataclass(frozen=True, slots=True)
class DriftRow:
    """One row of the drift table: a dead declared protocol, or a working one no menu names."""

    protocol: str
    kind: str
    drift: Literal["declared-but-dead", "working-but-undeclared"]
    detail: str


def protocol_table(survey: "Survey") -> Table:
    """Build the ``protocols`` table: one row per verdict, in survey order."""
    table = Table(title="protocols", box=box.ROUNDED)
    for name in _COLUMNS:
        table.add_column(name)
    for v in survey.verdicts:
        table.add_row(
            Text(v.protocol),
            Text(v.kind),
            Text(str(v.port) if v.port else "—"),
            Text(v.state),
            Text(v.tier),
            Text(v.vantage),
            Text(v.detail),
        )
    return table


def footnote_lines(survey: "Survey") -> list[Text]:
    """Build the footnote lines, each only when its source list is non-empty."""
    lines: list[Text] = []
    if survey.not_applicable:
        lines.append(Text(f"not applicable to this family: {', '.join(survey.not_applicable)}"))
    lines.extend(Text(f) for f in survey.footnotes)
    if survey.other_listeners:
        lines.append(Text(f"other listeners: {', '.join(survey.other_listeners)}"))
    return lines


def _kind_of(protocol: str) -> str:
    return "term" if protocol in dialling_terms() else "transfer"


def _declared_but_dead(declared: set[str], survey: "Survey") -> list[DriftRow]:
    """Protocols in the menus whose declared-port row answered dead."""
    rows: list[DriftRow] = []
    for proto in sorted(declared):
        port = survey.declared_ports.get(proto)
        row = next((v for v in survey.verdicts if v.protocol == proto and v.port == port), None)
        if row is not None and row.state in _DEAD:
            detail = f"{row.state} on declared port {port}"
            rows.append(DriftRow(proto, _kind_of(proto), "declared-but-dead", detail))
    return rows


def _working_but_undeclared(declared: set[str], survey: "Survey") -> list[DriftRow]:
    """Protocols with a ``supported`` verdict (any port) absent from the menus."""
    rows: list[DriftRow] = []
    supported_protocols = {v.protocol for v in survey.verdicts if v.state == "supported"}
    for proto in sorted(supported_protocols):
        if proto in declared or proto == "snmp":
            continue
        ports = sorted(
            {v.port for v in survey.verdicts if v.protocol == proto and v.state == "supported"}
        )
        detail = f"supported on {', '.join(map(str, ports))}"
        rows.append(DriftRow(proto, _kind_of(proto), "working-but-undeclared", detail))
    return rows


def drift_rows(
    host_terms: list[str], host_transfers: list[str], survey: "Survey"
) -> list[DriftRow]:
    """Compute the drift rows.

    Both kinds — declared-but-dead and working-but-undeclared — are merged
    and sorted together by ``(kind, protocol)``, not declared-but-dead first.
    """
    declared = set(host_terms) | set(host_transfers)
    rows = _declared_but_dead(declared, survey) + _working_but_undeclared(declared, survey)
    return sorted(rows, key=lambda r: (r.kind, r.protocol))


def drift_table(rows: list[DriftRow]) -> "Table | None":
    """Build the ``drift`` table, or ``None`` when there are no rows to show."""
    if not rows:
        return None
    table = Table(title="drift", box=box.ROUNDED)
    for name in ("protocol", "kind", "drift", "detail"):
        table.add_column(name)
    for r in rows:
        table.add_row(Text(r.protocol), Text(r.kind), Text(r.drift), Text(r.detail))
    return table


def menu_pin(host_terms: list[str], host_transfers: list[str], survey: "Survey") -> list[str]:
    """Build the plain-text pin lines: differing menu lists, then one options fragment per port.

    Only the menu lines the host's menus actually differ from are emitted.
    """
    supported = set(survey.supported)
    dialling = dialling_terms()
    terms = [n for n in dialling if n in supported]
    # A non-dialling term (e.g. console) never appears in `terms` -- it has
    # no port on the host's own address to survey -- so it is compared and
    # re-emitted separately from the dialling subset, never dropped from a
    # pin just because the survey has no way to confirm it by dialling.
    host_dialling_terms = [n for n in host_terms if n in dialling]
    host_nondialling_terms = [n for n in host_terms if n not in dialling]
    # Built from the supported verdict rows whose own `kind` is "transfer",
    # never from a name test over those rows: "console" is both a
    # non-dialling TERM (a unix console-term host carries a supported
    # own-session row named "console", kind "term") and a REGISTERED
    # TRANSFER backend (embedded-only), so `v.protocol in
    # TRANSFER_BACKENDS.names()` would read that term row as a transfer and
    # fire a false valid_transfers pin. `_kind_of` has the same flaw: it
    # reads "transfer" for anything that is not a DIALLING term. Each row's
    # own `kind` was set once, correctly, when its Candidate was built.
    transfers = sorted(
        {v.protocol for v in survey.verdicts if v.state == "supported" and v.kind == "transfer"}
    )
    lines: list[str] = []
    if set(terms) != set(host_dialling_terms):
        lines.append(f"valid_terms = {json.dumps(terms + host_nondialling_terms)}")
    if set(transfers) != set(host_transfers):
        lines.append(f"valid_transfers = {json.dumps(transfers)}")
    for proto in sorted(survey.working_ports):
        key = "snmp" if proto == "snmp" else f"{proto}_options"
        lines.append(f'"{key}": {json.dumps({"port": survey.working_ports[proto]})}')
    return lines


def survey_report(host: "BaseHost", survey: "Survey") -> "list[str | Table | Text]":
    """Lay out the protocol section of ``otto host <id> probe``, in the spec's order."""
    out: "list[str | Table | Text]" = [protocol_table(survey), *footnote_lines(survey)]
    terms = list(getattr(host, "valid_terms", []))
    transfers = list(getattr(host, "valid_transfers", []))
    drift = drift_table(drift_rows(terms, transfers, survey))
    if drift is not None:
        out.append(drift)
    pin = menu_pin(terms, transfers, survey)
    if pin:
        out.extend(["", *pin])
    return out
