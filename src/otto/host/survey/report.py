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

from ..transfer.nc_scripts import LADDER_OPTION, LISTENER_OPTION, PORT_OPTION
from .engine import dialling_terms

if TYPE_CHECKING:
    from ..host import BaseHost
    from .engine import NcFailedStrategy, NcLadderResult, Survey

_DEAD = frozenset({"login-failed", "service-mismatch", "closed"})
_COLUMNS = ["protocol", "kind", "port", "state", "tier", "vantage", "detail"]
_ID_COLUMNS = frozenset({"protocol", "vantage"})
"""The identifier columns of :data:`_COLUMNS`: a protocol name, and ``controller`` or a
hop's host id. They fold rather than end in "…" (``otto.console.id_column``)."""


@dataclass(frozen=True, slots=True)
class DriftRow:
    """One row of the drift table: a dead declared protocol, or a working one no menu names."""

    protocol: str
    kind: str
    drift: Literal["declared-but-dead", "working-but-undeclared"]
    detail: str


def protocol_table(survey: "Survey") -> Table:
    """Build the ``protocols`` table: one row per verdict, in survey order."""
    from ...console import id_column, literal_cell

    table = Table(title="protocols", box=box.ROUNDED)
    for name in _COLUMNS:
        if name in _ID_COLUMNS:
            id_column(table, name)
        else:
            table.add_column(name)
    for v in survey.verdicts:
        table.add_row(
            literal_cell(v.protocol),
            Text(v.kind),
            Text(str(v.port) if v.port else "—"),
            Text(v.state),
            Text(v.tier),
            literal_cell(v.vantage),
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


def terminal_mapping_lines(survey: "Survey") -> list[Text]:
    """One ``terminal mapping (<term>): <mapping>`` line per term a shell session opened on.

    What the handshake observed, and nothing more: never a cause, never a pin
    (see :mod:`otto.host.terminal_mapping`).
    """
    return [
        Text(f"terminal mapping ({term}): {mapping}")
        for term, mapping in survey.terminal_mappings.items()
    ]


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
    from ...console import id_column, literal_cell

    table = Table(title="drift", box=box.ROUNDED)
    id_column(table, "protocol")
    for name in ("kind", "drift", "detail"):
        table.add_column(name)
    for r in rows:
        table.add_row(literal_cell(r.protocol), Text(r.kind), Text(r.drift), Text(r.detail))
    return table


def _pinned_menu(declared: list[str], working: list[str], dead: set[str]) -> list[str]:
    """Return the menu to pin: the host's own order, less what is dead, plus what is new.

    The first entry of ``valid_terms`` and of ``valid_transfers`` is the
    default when no ``term``/``transfer`` field and no ``[host_preferences]``
    selector picks another, so a pin never moves an entry the host declared.
    A ``declared`` entry leaves the menu only on evidence: it is ``dead`` (its
    declared-port row answered dead) and the survey found it ``working``
    nowhere else. An entry in an unknown state, or with no verdict at all,
    stays in its place. A ``working`` entry the host did not declare is
    appended after them.
    """
    kept = [n for n in declared if n in working or n not in dead]
    return kept + [n for n in working if n not in declared]


def _dead_names(menu: list[str], survey: "Survey") -> set[str]:
    """Return the entries of ``menu`` the drift table reports as declared-but-dead."""
    return {r.protocol for r in _declared_but_dead(set(menu), survey)}


def menu_pin(host_terms: list[str], host_transfers: list[str], survey: "Survey") -> list[str]:
    """Build the plain-text pin lines: differing menu lists, then one options fragment per protocol.

    Only a menu the survey has evidence to change is emitted, and it keeps the
    host's own order (:func:`_pinned_menu`). A fragment holds the protocol's
    moved ``port``; the ``nc_options`` one also holds ``port_strategy`` and
    ``listener_check`` for each netcat strategy the survey measured.
    """
    supported = set(survey.supported)
    dialling = dialling_terms()
    terms = [n for n in dialling if n in supported]
    # A non-dialling term (e.g. console) never appears in `terms` -- it has
    # no port on the host's own address to survey -- so it is never read as
    # dead either: only the dialling subset of the host's menu is looked up
    # in the drift rows, and a non-dialling term is never dropped from a pin
    # just because the survey has no way to confirm it by dialling.
    host_dialling_terms = [n for n in host_terms if n in dialling]
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
    pinned_terms = _pinned_menu(host_terms, terms, _dead_names(host_dialling_terms, survey))
    pinned_transfers = _pinned_menu(host_transfers, transfers, _dead_names(host_transfers, survey))
    lines: list[str] = []
    if pinned_terms != host_terms:
        lines.append(f"valid_terms = {json.dumps(pinned_terms)}")
    if pinned_transfers != host_transfers:
        lines.append(f"valid_transfers = {json.dumps(pinned_transfers)}")
    fragments: dict[str, dict[str, object]] = {
        proto: {"port": port} for proto, port in survey.working_ports.items()
    }
    strategies = _measured_nc_strategies(survey)
    if strategies:
        fragments.setdefault("nc", {}).update(strategies)
    for proto in sorted(fragments):
        key = "snmp" if proto == "snmp" else f"{proto}_options"
        lines.append(f'"{key}": {json.dumps(fragments[proto])}')
    return lines


def _nc_ladders(survey: "Survey") -> "list[tuple[str, NcLadderResult]]":
    """Each netcat ladder's option name with what the survey found, in the option's order."""
    rungs = survey.nc_rungs
    if rungs is None:
        return []
    return [(PORT_OPTION, rungs.port), (LISTENER_OPTION, rungs.listener)]


def _measured_nc_strategies(survey: "Survey") -> dict[str, object]:
    """Return the ``nc_options`` keys a measured strategy pins; other states add none.

    None under ``--user``: the strategies were measured as that user, and the
    fragment applies to every login of the host, with no fall-through.
    """
    if survey.user is not None:
        return {}
    return {opt: got.rung for opt, got in _nc_ladders(survey) if got.state == "measured"}


def nc_strategy_lines(survey: "Survey") -> list[Text]:
    """One line per netcat strategy declared, not measured, or failed on the way.

    A measured strategy prints no line here: it is in the pin. Under
    ``--user`` it is not, so it prints its line, and one more says why it is
    not offered as a pin. Two ladders not measured for one reason share a line.
    When the probe could not confirm its own listener ended and no lifetime
    cap ends it, that line is last.
    """
    ladders = _nc_ladders(survey)
    unmeasured = [got for _opt, got in ladders if got.state == "unmeasured"]
    if ladders and len(unmeasured) == len(ladders) and len({u.note for u in unmeasured}) == 1:
        ladders = [(", ".join(opt for opt, _got in ladders), unmeasured[0])]
    lines: list[Text] = []
    measured_as = survey.user
    for opt, got in ladders:
        if got.state == "declared":
            said = f"{json.dumps(got.rung)} is declared in nc_options, so it was not measured"
            lines.append(Text(f"nc {opt}: {said}"))
        elif got.state == "unmeasured":
            lines.append(Text(f"nc {opt}: not measured: {got.note}"))
        elif measured_as is not None:
            said = f"{json.dumps(got.rung)} works when measured as {json.dumps(measured_as)}"
            lines.append(Text(f"nc {opt}: {said}"))
    if measured_as is not None and any(got.state == "measured" for _opt, got in ladders):
        lines.append(
            Text(
                f"nc: the strategies were measured as {json.dumps(measured_as)} and are not"
                " offered as a pin; run the probe without --user for one"
            )
        )
    rungs = survey.nc_rungs
    if rungs is not None:
        lines.extend(Text(_failed_strategy_line(f)) for f in rungs.failed)
        if rungs.listener_left:
            lines.append(Text(f"nc: {rungs.listener_left}"))
    return lines


def _failed_strategy_line(f: "NcFailedStrategy") -> str:
    """Return the one line that names a failed netcat strategy, its option and its reason."""
    option = LADDER_OPTION[f.ladder]
    return f"nc strategy {json.dumps(f.strategy)} failed for {option}: {f.reason}"


def survey_report(host: "BaseHost", survey: "Survey") -> "list[str | Table | Text]":
    """Lay out the protocol section of ``otto host <id> probe``, in the spec's order."""
    out: "list[str | Table | Text]" = [
        protocol_table(survey),
        *footnote_lines(survey),
        *nc_strategy_lines(survey),
        *terminal_mapping_lines(survey),
    ]
    terms = list(getattr(host, "valid_terms", []))
    transfers = list(getattr(host, "valid_transfers", []))
    drift = drift_table(drift_rows(terms, transfers, survey))
    if drift is not None:
        out.append(drift)
    pin = menu_pin(terms, transfers, survey)
    if pin:
        out.extend(["", *pin])
    return out
