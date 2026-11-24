"""Every sentence or table that states a netcat ladder's order agrees with the table.

The table is ``PORT_LADDER`` / ``LISTENER_LADDER`` in ``nc_scripts``. Each
statement is listed here as (file, regex, ladder, table, names). The regex's group
``text`` is the part read: for a table, its rows (the first column is read); for
a sentence, the whole sentence. A statement that is not found fails the test: a
reworded sentence is re-listed here, never dropped.
"""

import re
from typing import NamedTuple

import pytest

from otto.host.transfer.nc_scripts import CONTROL_VALUES, LISTENER_LADDER, PORT_LADDER
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

RUNG = re.compile(r"\b(ss|netstat|python|proc)\b")
ROW = re.compile(r"^\| ``(\w+)``", re.MULTILINE)


class Statement(NamedTuple):
    file: str
    pattern: str
    ladder: str
    table: bool = False
    names: tuple[str, ...] | None = None  # the rungs a partial statement names, exactly


STATEMENTS = [
    Statement(
        "docs/cli/host/netcat.md",
        r"\*\*Port-finding strategies\*\*.*?\n\n(?P<text>(?:\|[^\n]*\n)+)",
        "port",
        table=True,
    ),
    Statement("docs/cli/host/netcat.md", r"cascade order is: (?P<text>[^\n]*)", "port"),
    Statement(
        "docs/cli/host/netcat.md",
        r"\*\*Listener-check strategies\*\*.*?\n\n(?P<text>(?:\|[^\n]*\n)+)",
        "listener",
        table=True,
    ),
    Statement("docs/cli/host/netcat.md", r"(?P<text>Probe for [^\n]*)", "listener"),
    Statement(
        "docs/configuration/host-options.md",
        r"(?P<text>the ss/netstat/proc scans start)",
        "port",
        names=("ss", "netstat", "proc"),
    ),
    Statement(
        "src/otto/host/transfer/base.py",
        r"in order \((?P<text>ss →\s+netstat →\s+python →\s+proc)\)",
        "port",
    ),
    Statement(
        "src/otto/host/transfer/base.py",
        r"(?P<text>probe for ss, then netstat, falling back to proc)",
        "listener",
    ),
    Statement(
        "src/otto/host/options.py",
        r"(?P<text>The ss/netstat/proc port-finding)",
        "port",
        names=("ss", "netstat", "proc"),
    ),
    Statement(
        "src/otto/host/options.py",
        r"(?P<text>probes ss → netstat → python → proc)",
        "port",
    ),
    Statement(
        "src/otto/host/options.py",
        r"(?P<text>probes ss → netstat → proc)\.",
        "listener",
    ),
]

LADDERS = {"port": PORT_LADDER, "listener": LISTENER_LADDER}


@pytest.mark.parametrize("statement", STATEMENTS, ids=lambda s: f"{s.file}:{s.pattern[:40]}")
def test_the_docs_state_the_table_order(statement: Statement) -> None:
    source = (CHECKOUT_ROOT / statement.file).read_text(encoding="utf-8")
    found = re.search(statement.pattern, source, re.DOTALL)
    assert found, f"{statement.file} no longer holds a statement matching {statement.pattern!r}"
    text = found.group("text")
    table_order = [rung.name for rung in LADDERS[statement.ladder]]
    if statement.table:
        first, *rest = CONTROL_VALUES
        assert ROW.findall(text) == [first, *table_order, *rest]
        return
    said = RUNG.findall(text)
    if statement.names is None:
        assert said == table_order
    else:
        assert said == list(statement.names)
        assert said == [name for name in table_order if name in said]
