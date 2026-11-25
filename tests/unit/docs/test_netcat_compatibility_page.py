"""The netcat compatibility page says what the bed table says, and nothing else.

The page is written whole by ``scripts/render_netcat_compatibility.py`` from
``BED_NETCAT`` and committed, so a reader of the repository sees it without
building the docs. These tests pin the committed page to the table and the
renderer's cells to the table's rows.
"""

import dataclasses

import pytest

from scripts.render_netcat_compatibility import (
    ISSUE_URL,
    PAGE_PATH,
    main,
    render_netcat_compatibility,
)
from tests.conformance._nc_rungs import (
    BED_NETCAT,
    DOES_NOT_WORK,
    LADDERS,
    LISTENER,
    NC_IMPLEMENTATION_CELLS,
    PORT,
    BedNetcat,
    ListenerCheck,
    NcImplementation,
    implementation,
)

pytestmark = pytest.mark.interpreter_agnostic

_STATE_WORD = {"works": "works", "tool-absent": "tool absent", "does-not-work": "does not work"}


def _rows(page: str, heading: str) -> list[list[str]]:
    """The cells of each body row of the table under *heading*."""
    section = page.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    table = [line for line in section.splitlines() if line.startswith("|")]
    return [[c.strip() for c in line.strip("|").split(" | ")] for line in table[2:]]


def _failing_table() -> dict[str, BedNetcat]:
    """A table whose one netcat has a listener check that does not work."""
    broken = ListenerCheck(
        state=DOES_NOT_WORK,
        reason="it counts two listeners | for one socket pair",
        opens_with="shared",
        issue="#1304",
    )
    ok = BED_NETCAT["ubu2404"].implementations[0]
    listener = {**ok.listener, "netstat": broken}
    impl = NcImplementation("ncat", "/usr/bin/ncat", "ncat (Nmap)", listener)
    return {"ubu2404": dataclasses.replace(BED_NETCAT["ubu2404"], implementations=(impl,))}


def test_every_bed_host_is_named_with_what_it_is():
    """Each bed host appears once in the legend, beside its userland from the table."""
    rows = _rows(render_netcat_compatibility(BED_NETCAT), "The bed hosts")
    seen = {host.strip("`"): userland for hosts, userland in rows for host in hosts.split(", ")}
    assert seen == {element: row.userland for element, row in BED_NETCAT.items()}
    assert seen["bb1161"] == "BusyBox 1.16.1 guest"
    assert seen["ubu2404"] == "Ubuntu 24.04 guest"


def test_the_legend_tells_the_reader_to_match_by_netcat_and_userland():
    page = render_netcat_compatibility(BED_NETCAT)
    legend = page.split("## The bed hosts\n", 1)[1].split("\n## ", 1)[0]
    assert "Match your host by its netcat" in legend
    assert "not by a bed host's name" in legend


def test_the_committed_page_is_what_the_table_generates():
    assert PAGE_PATH.read_text(encoding="utf-8") == render_netcat_compatibility(BED_NETCAT), (
        f"{PAGE_PATH} is stale; re-run "
        f"`uv run python -m scripts.render_netcat_compatibility` and commit the page"
    )


def test_every_implementation_and_every_listener_check_has_a_cell():
    page = render_netcat_compatibility(BED_NETCAT)
    rows = _rows(page, "Netcat implementations")
    header = next(
        line
        for line in page.split("## Netcat implementations\n", 1)[1].splitlines()
        if line.startswith("| Netcat")
    )
    assert [c.strip(" `") for c in header.strip("|").split(" | ")[4:]] == list(LADDERS[LISTENER])
    for element, exec_name in NC_IMPLEMENTATION_CELLS:
        impl = implementation(element, exec_name)
        matches = [
            row
            for row in rows
            if row[0] == impl.package
            and row[1] == f"`{exec_name}`"
            and f"`{element}`" in row[2].split(", ")
        ]
        assert len(matches) == 1, f"{element}:{exec_name} has {len(matches)} rows"
        cells = matches[0][4:]
        assert cells == [_STATE_WORD[impl.listener[rung].state] for rung in LADDERS[LISTENER]], (
            f"{element}:{exec_name}"
        )


def test_every_port_strategy_is_in_a_row_for_every_host():
    page = render_netcat_compatibility(BED_NETCAT)
    rows = _rows(page, "Port strategies")
    for element, host in BED_NETCAT.items():
        (row,) = [r for r in rows if f"`{element}`" in r[0].split(", ")]
        shown = [rung for rung, cell in zip(LADDERS[PORT], row[1:], strict=True) if cell == "works"]
        assert tuple(shown) == host.port, element


def test_a_does_not_work_cell_shows_its_reason_and_issue():
    page = render_netcat_compatibility(_failing_table())
    (row,) = _rows(page, "Netcat implementations")
    cell = row[4 + LADDERS[LISTENER].index("netstat")]
    assert cell == (
        f"does not work: it counts two listeners \\| for one socket pair ([#1304]({ISSUE_URL}1304))"
    )
    assert row[4] == "works"


def test_the_page_states_each_rows_route_and_what_works_means():
    page = render_netcat_compatibility(BED_NETCAT)
    for row in _rows(page, "Netcat implementations"):
        assert row[3] in {"direct", "tunnelled"}
    routes = {
        (impl.package, host.route) for host in BED_NETCAT.values() for impl in host.implementations
    }
    shown = {(row[0], row[3]) for row in _rows(page, "Netcat implementations")}
    assert shown == routes
    assert "otto moved a file with that" in page
    assert "A direct get runs neither ladder" in page
    # The sender form ran only where a bed host is direct, and the page says which netcat.
    assert "was run only with\nnetcat-openbsd." in page
    assert "not tried in this form on the bed.\nThat does not mean they fail." in page
    assert "That does not mean they fail." not in render_netcat_compatibility(
        {k: v for k, v in BED_NETCAT.items() if v.route == "direct"}
    )
    assert "ncat" not in page.split("was run only with")[1].split(".")[0]


def test_check_reports_a_stale_page_and_writes_nothing(tmp_path, capsys):
    page = tmp_path / "page.md"
    assert main(["--page", str(page), "--check"]) == 1
    assert not page.exists()
    assert main(["--page", str(page)]) == 0
    assert page.read_text(encoding="utf-8") == render_netcat_compatibility(BED_NETCAT)
    assert main(["--page", str(page), "--check"]) == 0
    page.write_text("stale\n", encoding="utf-8")
    assert main(["--page", str(page), "--check"]) == 1
    assert page.read_text(encoding="utf-8") == "stale\n"
    assert "netcat_compatibility" in capsys.readouterr().err
