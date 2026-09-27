"""Render a :class:`~otto.tunnel.check.TunnelCheckReport` for the stdout table.

One section, built the way ``otto link check`` builds its own
(:func:`otto.link.check.link_sections`): a heading naming the whole path, a
run of subheadings (each hop's facts with its own proven range directly
under it, then a fingerprinted dest's, the ``bulk:`` sizes, what the sweep
found, and finally what was proven), then a table with one column per
requested protocol and one row per feature.

A completed report only: a refusal or a dry run never reaches here — the CLI
layer renders those the same way ``otto link check``'s CLI does (see
``otto.cli.link``'s ``_print_check_dry_run``/``refusal`` handling), because
neither one is a *result* with hops, columns or a proven line to lay out.

The table's rows are keyed by a protocol-neutral display label. Every row
name is identical across a report's columns except the bulk payload rows,
which carry their protocol's own size (``fwd 64 KiB`` for tcp, ``fwd 65000
B`` for udp); those two display as ``fwd bulk`` / ``rev bulk`` instead, and a
``bulk: …`` subheading says the per-protocol sizes once. The JSON report
(``report_to_json``) is untouched by this: it keeps each row's own
:attr:`~otto.check.FeatureResult.feature` name.
"""

from typing import TYPE_CHECKING

from ..check import CheckRow, CheckSection, FeatureResult, HostFingerprint
from ..check.render import proven_range_line
from . import _tunnel_rows as rows

if TYPE_CHECKING:
    from .check import TunnelCheckHop, TunnelCheckReport


def _display_label(protocol: str, feature: str) -> str:
    """Generalise *feature* to its protocol-neutral row label: the bulk rows, or itself."""
    bulk = rows.bulk_label(protocol)
    for direction in ("fwd", "rev"):
        if feature == f"{direction} {bulk}":
            return f"{direction} bulk"
    return feature


def _hop_facts_line(host_id: str, fp: HostFingerprint) -> str:
    """One hop's (or a fingerprinted dest's) fact line: tools, clock, launcher, platform."""
    clock = "yes" if fp.versions.get("epochrealtime") == "yes" else "no"
    facts = [
        f"socat {fp.versions.get('socat') or '?'}",
        f"bash {fp.versions.get('bash') or '?'}",
        f"clock {clock}",
        f"launcher {fp.versions.get('launcher') or '?'}",
        f"kernel {fp.kernel or '?'}",
        fp.isa or "?",
        fp.userland,
    ]
    return f"{host_id}  {' · '.join(facts)}"


RANGE_COMPONENTS = ["kernel", "isa", "userland", "socat", "bash", "launcher"]
"""The components each hop is labelled on against the proven range, in the order they print."""


def _hop_block(hop: "TunnelCheckHop") -> list[str]:
    """Build a hop's (or a dest's) subheading lines: its facts, then its own indented proven range.

    A dest the lab says has no shell was never fingerprinted, so it gets one
    line instead of two. The proven-range line sits directly under its facts
    line, indented to the column the facts line's first fact starts at
    (``len(host_id) + 2``, matching the facts line's own ``host_id  fact ·
    fact``), so it reads as a detail of that hop, not a peer subheading.
    """
    fp = hop.fingerprint
    if fp is None:
        return [f"{hop.host_id}: runs nothing of otto's — not fingerprinted"]
    lines = [_hop_facts_line(hop.host_id, fp)]
    if hop.range_labels:
        indent = " " * (len(hop.host_id) + 2)
        lines.append(f"{indent}{proven_range_line(fp, hop.range_labels, RANGE_COMPONENTS)}")
    return lines


def _bulk_line(protocols: list[str]) -> str:
    """Say what ``fwd``/``rev bulk`` measures per protocol, e.g. ``bulk: tcp 64 KiB``."""
    parts = [f"{protocol} {rows.bulk_label(protocol)}" for protocol in protocols]
    return f"bulk: {' · '.join(parts)}"


def _heading(report: "TunnelCheckReport") -> str:
    """Build the path with addresses, e.g. ``a 10.0.0.1 → b 10.0.0.2  :8080 (scratch 61000)``."""
    chain = " → ".join(f"{hop.host_id} {hop.address}" for hop in report.hops)
    dest = report.destination
    if dest is not None:
        chain += f" → dest {dest.host_id} {dest.address}"
    where = f"  :{report.port}"
    if report.scratch_port is not None:
        where += f" (scratch {report.scratch_port})"
    return chain + where


def _subheadings(report: "TunnelCheckReport") -> list[str]:
    """List each hop's block, the dest's, the ``bulk:`` line, the sweep, then what was proven."""
    dest = report.destination
    lines: list[str] = []
    for hop in report.hops:
        lines += _hop_block(hop)
    if dest is not None:
        lines += _hop_block(dest)
    lines.append(_bulk_line(report.protocols))
    for hop in report.hops:
        lines += hop.swept
    if dest is not None:
        lines += dest.swept
    lines.append(f"proven: {report.proven}")
    return lines


def tunnel_sections(report: "TunnelCheckReport") -> list[CheckSection]:
    """Build the one :class:`~otto.check.CheckSection` a completed *report* renders as.

    The columns are the requested protocols in report order; the rows are the
    union of every column's rows, first-seen order, keyed by their
    protocol-neutral label (:func:`_display_label`). A column with no row of
    that label renders ``n/a`` — :func:`~otto.check.render_sections` already
    does that for any ``None`` cell.
    """
    columns = [column.protocol for column in report.columns]
    order: list[str] = []
    per_column: list[dict[str, FeatureResult]] = []
    for column in report.columns:
        mapped: dict[str, FeatureResult] = {}
        for result in column.results:
            label = _display_label(column.protocol, result.feature)
            mapped[label] = result
            if label not in order:
                order.append(label)
        per_column.append(mapped)
    table_rows = [CheckRow(label, [mapped.get(label) for mapped in per_column]) for label in order]
    section = CheckSection(
        heading=_heading(report),
        subheadings=_subheadings(report),
        columns=columns,
        rows=table_rows,
        summary_name="path",
    )
    return [section]
