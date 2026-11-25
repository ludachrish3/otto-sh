"""Render ``docs/cli/host/netcat-compatibility.md`` from the netcat bed table.

The page says which netcat implementations and which port and listener
strategies work where. Every cell is read from ``BED_NETCAT`` in
``tests/conformance/_nc_rungs.py``, the one table the netcat conformance
contract tests, so the page cannot say anything the gate does not check. The
rungs and their order come from the ladders in ``otto.host.transfer.nc_scripts``
through that module, never from a list here.

Run from the repo root::

    uv run python -m scripts.render_netcat_compatibility           # write the page
    uv run python -m scripts.render_netcat_compatibility --check   # report only

The page is committed. ``tests/unit/docs/test_netcat_compatibility_page.py``
fails when it differs from what the table generates.
"""

import argparse
import sys
from pathlib import Path

from tests._fixtures.paths import CHECKOUT_ROOT
from tests.conformance._nc_rungs import (
    BED_NETCAT,
    DOES_NOT_WORK,
    LADDERS,
    LISTENER,
    PIN_OPTION,
    PORT,
    TOOL_ABSENT,
    WORKS,
    BedNetcat,
    ListenerCheck,
)

PAGE_PATH = CHECKOUT_ROOT / "docs" / "cli" / "host" / "netcat-compatibility.md"
"""The committed page."""

ISSUE_URL = "https://github.com/ludachrish3/otto-sh/issues/"
"""Where a ``does-not-work`` cell's issue number points."""

_STATE_WORDS = {WORKS: "works", TOOL_ABSENT: "tool absent", DOES_NOT_WORK: "does not work"}
"""The words a listener check's state is shown as."""


def _cell(check: ListenerCheck) -> str:
    """One listener check's table cell: its state, and for a failure its reason and issue."""
    word = _STATE_WORDS[check.state]
    if check.state != DOES_NOT_WORK:
        return word
    issue = check.issue.lstrip("#")
    reason = check.reason.replace("|", "\\|")
    return f"{word}: {reason} ([#{issue}]({ISSUE_URL}{issue}))"


def _hosts(elements: list[str]) -> str:
    return ", ".join(f"`{element}`" for element in elements)


def _implementation_rows(table: dict[str, BedNetcat]) -> list[tuple[list[str], list[str]]]:
    """``(hosts, cells)`` per distinct implementation row, in the table's order.

    Hosts whose implementation, selecting name, route and every check's state
    agree share one row.
    """
    rows: dict[tuple[str, ...], tuple[list[str], list[str]]] = {}
    for element, host in table.items():
        for impl in host.implementations:
            checks = [_cell(impl.listener[rung]) for rung in LADDERS[LISTENER]]
            cells = [impl.package, f"`{impl.exec_name}`", host.route, *checks]
            hosts, _ = rows.setdefault(tuple(cells), ([], cells))
            hosts.append(element)
    return list(rows.values())


def _port_rows(table: dict[str, BedNetcat]) -> list[tuple[list[str], tuple[str, ...]]]:
    """``(hosts, strategies)`` per distinct set of port strategies, in the table's order."""
    rows: dict[tuple[str, ...], list[str]] = {}
    for element, host in table.items():
        rows.setdefault(host.port, []).append(element)
    return [(hosts, port) for port, hosts in rows.items()]


def _userland_rows(table: dict[str, BedNetcat]) -> list[tuple[list[str], str]]:
    """``(hosts, userland)`` per distinct userland, in the table's order."""
    rows: dict[str, list[str]] = {}
    for element, host in table.items():
        rows.setdefault(host.userland, []).append(element)
    return [(hosts, userland) for userland, hosts in rows.items()]


def _direct(table: dict[str, BedNetcat]) -> list[str]:
    """Return the packages some bed host runs on a direct route, in the table's order."""
    packages: dict[str, None] = {}
    for host in table.values():
        if host.route == "direct":
            packages.update({impl.package: None for impl in host.implementations})
    return list(packages)


def _line(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render_netcat_compatibility(table: dict[str, BedNetcat]) -> str:
    """Render the compatibility page from *table*.

    Args:
        table: Bed host element -> what its netcat transfer can run
            (``BED_NETCAT``).

    Returns:
        The page, ending in a newline.
    """
    listener_rungs = LADDERS[LISTENER]
    port_rungs = LADDERS[PORT]
    direct = _direct(table)
    sender = (
        [
            "(`<exec_name> <ip> <port> < file`) was run only with",
            f"{_join_words(direct)}. No bed host runs any other netcat on a direct route.",
        ]
        if direct
        else ["(`<exec_name> <ip> <port> < file`) was not run: no bed host is direct."]
    )
    if any(impl.package not in direct for host in table.values() for impl in host.implementations):
        sender += [
            "The other netcats were not tried in this form on the bed.",
            "That does not mean they fail.",
        ]
    lines = [
        "<!-- GENERATED FILE -- do not edit by hand.",
        "     scripts/render_netcat_compatibility.py renders this from BED_NETCAT in",
        "     tests/conformance/_nc_rungs.py. Run `make netcat-compatibility` to",
        "     regenerate it; tests/unit/docs/test_netcat_compatibility_page.py fails when",
        "     it is stale. -->",
        "",
        "# Netcat compatibility",
        "",
        "Which netcat to put in `nc_options.exec_name`, and which port and listener",
        "strategies work with it. Everything below comes from the test bed. The",
        "strategies themselves, and how to pin one, are on {doc}`netcat`.",
        "",
        '## What "works" means',
        "",
        "A listener check **works** with a netcat when otto moved a file with that",
        "check pinned, in the test bed's gate lane. **Tool absent** means the host",
        "does not have the tool the check runs, so pinning it fails. **Does not",
        "work** means the tool is there and the check fails with that netcat; the",
        "cell gives the reason and the issue that tracks it. A port strategy that a",
        "host can run was likewise used to find a port and move a file.",
        "",
        "A direct get runs neither ladder. The host sends the file to otto, so there",
        "is no port to find and no listener to check. The listener columns therefore",
        "describe a put, and a get through a hop. The sender form of a direct get",
        *sender,
        "",
        "Each row names the route it was run on. `direct` is a host otto reaches",
        "itself; `tunnelled` is a host behind a hop.",
        "",
        "## The bed hosts",
        "",
        "The host names below are the test bed's own. Match your host by its netcat",
        "implementation and its userland, not by a bed host's name.",
        "",
        _line(["Bed hosts", "What they are"]),
        _line(["---"] * 2),
        *(_line([_hosts(hosts), userland]) for hosts, userland in _userland_rows(table)),
        "",
        "## Netcat implementations",
        "",
        "Set `nc_options.exec_name` to the name in the second column. A row is shared",
        "by every bed host listed in it.",
        "",
        _line(["Netcat", "`exec_name`", "Bed hosts", "Route", *[f"`{r}`" for r in listener_rungs]]),
        _line(["---"] * (4 + len(listener_rungs))),
    ]
    for hosts, cells in _implementation_rows(table):
        package, exec_name, route, *checks = cells
        lines.append(_line([package, exec_name, _hosts(hosts), route, *checks]))
    lines += [
        "",
        f"The `{PIN_OPTION[LISTENER]}` option pins one of these checks.",
        "",
        "## Port strategies",
        "",
        "A port strategy never runs the netcat binary, so it depends on the host and",
        f'not on the implementation. `{PIN_OPTION[PORT]}` pins one. "Not available" means the',
        "host does not have the tool the strategy runs.",
        "",
        _line(["Bed hosts", *[f"`{r}`" for r in port_rungs]]),
        _line(["---"] * (1 + len(port_rungs))),
    ]
    for hosts, port in _port_rows(table):
        lines.append(
            _line([_hosts(hosts), *["works" if r in port else "not available" for r in port_rungs]])
        )
    return "\n".join(lines).rstrip() + "\n"


def _join_words(names: list[str]) -> str:
    """*names* as prose: ``a``, ``a or b``, ``a, b or c``."""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def main(argv: "list[str]") -> int:
    """Write the page, or with ``--check`` report whether the committed one is stale."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--page", type=Path, default=PAGE_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report a stale committed page and write nothing",
    )
    args = parser.parse_args(argv)
    text = render_netcat_compatibility(BED_NETCAT)
    if args.check:
        if not args.page.exists() or args.page.read_text(encoding="utf-8") != text:
            print(
                f"{args.page} is not what the table generates; "
                f"re-run `uv run python -m scripts.render_netcat_compatibility` and commit it",
                file=sys.stderr,
            )
            return 1
        print(f"{args.page}: up to date")
        return 0
    args.page.write_text(text, encoding="utf-8")
    print(f"{args.page}: rendered")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
