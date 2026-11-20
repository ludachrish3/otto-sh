"""The report: structural table asserts, footnotes only with content, the pin only-and-differing."""

import json

from rich.table import Table
from rich.text import Text

from otto.host.survey.engine import Survey
from otto.host.survey.report import (
    DriftRow,
    drift_rows,
    drift_table,
    footnote_lines,
    menu_pin,
    protocol_table,
)
from otto.host.survey.verdict import UNKNOWN_STATES, ProtocolVerdict


def _v(protocol, port, state, *, kind="term", tier="login", detail=""):
    return ProtocolVerdict(
        protocol=protocol,
        kind=kind,
        port=port,
        state=state,
        tier=tier,
        vantage="controller",
        detail=detail,
    )


def _cells(table: Table, column: int) -> list[str]:
    """Structural read of the rendered rows."""
    return [str(c) for c in table.columns[column]._cells]


def _survey(**kw):
    s = Survey()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_dead_states_are_disjoint_from_unknown_states():
    """`_DEAD` (declared-but-dead) must never overlap `UNKNOWN_STATES` (never drift)."""
    from otto.host.survey.report import _DEAD

    assert not (_DEAD & UNKNOWN_STATES)


def test_protocol_table_has_the_seven_columns_and_one_row_per_verdict():
    s = _survey(
        verdicts=[
            _v("ssh", 22, "closed", tier="dial"),
            _v("ssh", 2222, "supported", detail="login 'admin [ssh]': session opened"),
        ]
    )
    t = protocol_table(s)
    assert [c.header for c in t.columns] == [
        "protocol",
        "kind",
        "port",
        "state",
        "tier",
        "vantage",
        "detail",
    ]
    assert _cells(t, 2) == ["22", "2222"]
    assert _cells(t, 3) == ["closed", "supported"]
    assert t.box is not None
    assert t.box.__class__.__name__ == "Box"


def test_cells_are_text_so_brackets_survive():
    """Mutation: add_row with str cells and rich eats `[ssh]` as markup."""
    t = protocol_table(
        _survey(
            verdicts=[
                _v("ssh", 22, "login-failed", detail="login 'admin [ssh]': permission denied")
            ]
        )
    )
    cell = t.columns[6]._cells[0]
    assert isinstance(cell, Text)
    assert "[ssh]" in cell.plain


def test_footnotes_appear_only_with_content_and_in_order():
    assert footnote_lines(_survey()) == []
    s = _survey(
        not_applicable=["console", "tftp"],
        footnotes=["3 loopback-only listeners not shown"],
        other_listeners=["2323/tcp busybox"],
    )
    assert [t.plain for t in footnote_lines(s)] == [
        "not applicable to this family: console, tftp",
        "3 loopback-only listeners not shown",
        "other listeners: 2323/tcp busybox",
    ]


def test_drift_declared_but_dead_uses_the_declared_port_only_and_excludes_unknowns():
    """Mutation: count a `timeout` as dead and a slow host drifts."""
    s = _survey(
        verdicts=[
            _v("ssh", 22, "closed", tier="dial"),
            _v("ssh", 2222, "supported"),
            _v("telnet", 23, "timeout", tier="dial"),
            _v("ftp", 21, "supported", kind="transfer"),
        ],
        declared_ports={"ssh": 22, "telnet": 23, "ftp": 21},
    )
    rows = drift_rows(["ssh", "telnet"], ["scp"], s)
    assert rows == [
        DriftRow(
            protocol="ssh",
            kind="term",
            drift="declared-but-dead",
            detail="closed on declared port 22",
        ),
        DriftRow(
            protocol="ftp",
            kind="transfer",
            drift="working-but-undeclared",
            detail="supported on 21",
        ),
    ]
    assert drift_table([]) is None
    assert [c.header for c in drift_table(rows).columns] == ["protocol", "kind", "drift", "detail"]


def test_the_pin_drops_a_dead_term_and_names_each_moved_port():
    s = _survey(
        verdicts=[
            _v("ssh", 2222, "supported"),
            _v("telnet", 23, "login-failed"),
            _v("scp", 2222, "supported", kind="transfer"),
            _v("snmp", 1161, "supported", kind="monitor"),
        ],
        declared_ports={"ssh": 22, "telnet": 23, "scp": 22, "snmp": 161},
        working_ports={"ssh": 2222, "snmp": 1161},
        supported=["scp", "snmp", "ssh"],
    )
    lines = menu_pin(["ssh", "telnet"], ["scp"], s)
    assert lines == [
        'valid_terms = ["ssh"]',
        '"snmp": {"port": 1161}',
        '"ssh_options": {"port": 2222}',
    ]
    assert json.loads(lines[1].split(": ", 1)[1]) == {"port": 1161}


def test_no_pin_when_the_menus_and_ports_already_match():
    s = _survey(
        verdicts=[_v("ssh", 22, "supported")], declared_ports={"ssh": 22}, supported=["ssh"]
    )
    assert menu_pin(["ssh"], [], s) == []


def test_a_non_dialling_own_term_never_triggers_a_valid_terms_pin_by_itself():
    """console can never appear in the dialling subset the survey builds -- it has no port

    to dial -- so a pin built from the dialled terms alone always differed
    from the host's menu and pinned a removal. A non-dialling term is never
    read as dead, so it stays in the pinned menu and the menu is unchanged.
    """
    s = _survey(
        verdicts=[_v("ssh", 1, "supported"), _v("scp", 1, "supported", kind="transfer")],
        supported=["ssh", "scp"],
    )
    assert menu_pin(["ssh", "console"], ["scp"], s) == []


def test_a_menu_whose_only_non_dialling_entry_is_console_gets_no_valid_terms_pin():
    s = _survey(verdicts=[_v("telnet", 1, "supported")], supported=["telnet"])
    assert menu_pin(["telnet", "console"], [], s) == []


def test_valid_transfers_reads_each_rows_own_kind_not_supported_membership():
    """A stray ``console`` in ``survey.supported`` -- as the pre-fix resolver left it --

    must not read as a transfer just because ``console`` also happens to be
    a registered (embedded-only) TRANSFER backend name. ``menu_pin`` decides
    kind from each row's own ``kind``, never from ``TRANSFER_BACKENDS`` names
    intersected with ``supported``.
    """
    s = _survey(
        verdicts=[
            _v("console", 0, "supported", kind="term"),
            _v("scp", 22, "supported", kind="transfer"),
        ],
        supported=["console", "scp"],
    )
    assert menu_pin(["console"], ["scp"], s) == []


def test_a_valid_terms_pin_keeps_the_hosts_non_dialling_term():
    """A real change on the dialling side (telnet newly supported) still pins --

    and the pinned list keeps ``console`` rather than silently dropping it.
    The newly found term goes last: the host's own entries keep their places.
    """
    s = _survey(
        verdicts=[_v("ssh", 1, "supported"), _v("telnet", 1, "supported")],
        supported=["ssh", "telnet"],
    )
    assert menu_pin(["ssh", "console"], [], s) == ['valid_terms = ["ssh", "console", "telnet"]']


def test_a_valid_terms_pin_keeps_a_non_dialling_term_first():
    """The first menu entry is the host's default term, so a pin must not move it.

    A host declaring ``["console", "ssh"]`` used to get ``["ssh", "telnet",
    "console"]``: pasting that pin changed the default term from console to ssh.
    """
    s = _survey(
        verdicts=[_v("ssh", 1, "supported"), _v("telnet", 1, "supported")],
        supported=["ssh", "telnet"],
    )
    assert menu_pin(["console", "ssh"], [], s) == ['valid_terms = ["console", "ssh", "telnet"]']


def test_a_valid_terms_pin_appends_a_newly_found_term_after_the_declared_ones():
    """A dialling default is protected too: ssh found on a telnet-only host goes after telnet."""
    s = _survey(
        verdicts=[_v("ssh", 1, "supported"), _v("telnet", 1, "supported")],
        supported=["ssh", "telnet"],
    )
    assert menu_pin(["telnet"], [], s) == ['valid_terms = ["telnet", "ssh"]']


def test_a_valid_terms_pin_drops_a_dead_term_and_keeps_the_rest_in_order():
    s = _survey(
        verdicts=[_v("ssh", 22, "login-failed"), _v("telnet", 23, "supported")],
        declared_ports={"ssh": 22, "telnet": 23},
        supported=["telnet"],
    )
    assert menu_pin(["telnet", "ssh", "console"], [], s) == ['valid_terms = ["telnet", "console"]']


def test_a_declared_term_in_an_unknown_state_is_never_pinned_away():
    """An unknown is a question, not an answer: it never feeds the pin.

    telnet timing out on a host declaring ``["telnet", "ssh"]`` used to pin
    ``["ssh"]``: no drift row, yet pasting the pin dropped telnet and made
    ssh the default.
    """
    for state in sorted(UNKNOWN_STATES):
        s = _survey(
            verdicts=[_v("telnet", 23, state, tier="dial"), _v("ssh", 22, "supported")],
            declared_ports={"ssh": 22, "telnet": 23},
            supported=["ssh"],
        )
        assert drift_rows(["telnet", "ssh"], [], s) == []
        assert menu_pin(["telnet", "ssh"], [], s) == [], state


def test_a_term_found_beside_an_unknown_declared_one_is_appended_after_it():
    s = _survey(
        verdicts=[_v("telnet", 23, "timeout", tier="dial"), _v("ssh", 22, "supported")],
        declared_ports={"ssh": 22, "telnet": 23},
        supported=["ssh"],
    )
    assert menu_pin(["telnet"], [], s) == ['valid_terms = ["telnet", "ssh"]']


def test_a_declared_transfer_the_survey_could_not_judge_is_never_pinned_away():
    """``not-checkable`` and no verdict at all are both "no evidence": the entry stays."""
    s = _survey(
        verdicts=[
            _v("ssh", 22, "supported"),
            _v("scp", 22, "not-checkable", kind="transfer"),
            _v("sftp", 22, "supported", kind="transfer"),
        ],
        declared_ports={"ssh": 22, "scp": 22, "sftp": 22, "nc": 9000},
        supported=["sftp", "ssh"],
    )
    assert menu_pin(["ssh"], ["scp", "nc", "sftp"], s) == []
    s.verdicts.append(_v("shell", 22, "supported", kind="transfer"))
    assert menu_pin(["ssh"], ["scp", "nc", "sftp"], s) == [
        'valid_transfers = ["scp", "nc", "sftp", "shell"]'
    ]


def test_a_term_dead_on_its_declared_port_but_working_on_another_stays_in_the_menu():
    """The drift table calls it declared-but-dead; the pin moves its port, never drops it."""
    s = _survey(
        verdicts=[
            _v("telnet", 23, "supported"),
            _v("ssh", 22, "closed", tier="dial"),
            _v("ssh", 2222, "supported"),
        ],
        declared_ports={"ssh": 22, "telnet": 23},
        working_ports={"ssh": 2222},
        supported=["ssh", "telnet"],
    )
    assert menu_pin(["telnet", "ssh"], [], s) == ['"ssh_options": {"port": 2222}']


def test_a_valid_transfers_pin_keeps_the_hosts_order_and_appends_what_is_new():
    """``valid_transfers`` has the same first-entry-is-default rule as ``valid_terms``.

    The pin used to be sorted by name: ftp found on a host declaring
    ``["sftp", "scp"]`` pinned ``["ftp", "scp", "sftp"]`` and made ftp the default.
    """
    s = _survey(
        verdicts=[
            _v("ssh", 22, "supported"),
            _v("sftp", 22, "supported", kind="transfer"),
            _v("scp", 22, "supported", kind="transfer"),
            _v("ftp", 21, "supported", kind="transfer"),
        ],
        supported=["ftp", "scp", "sftp", "ssh"],
    )
    assert menu_pin(["ssh"], ["sftp", "scp"], s) == ['valid_transfers = ["sftp", "scp", "ftp"]']


def test_a_valid_transfers_pin_drops_a_dead_transfer_and_keeps_the_rest_in_order():
    s = _survey(
        verdicts=[
            _v("ssh", 22, "supported"),
            _v("sftp", 22, "supported", kind="transfer"),
            _v("scp", 22, "login-failed", kind="transfer"),
            _v("ftp", 21, "supported", kind="transfer"),
        ],
        declared_ports={"ssh": 22, "sftp": 22, "scp": 22, "ftp": 21},
        supported=["ftp", "sftp", "ssh"],
    )
    assert menu_pin(["ssh"], ["sftp", "scp", "ftp"], s) == ['valid_transfers = ["sftp", "ftp"]']


def test_terminal_mapping_lines_name_each_term_and_what_was_observed():
    from otto.host.survey.report import survey_report, terminal_mapping_lines

    s = _survey(terminal_mappings={"ssh": "onlcr", "console": "unmeasured"})
    assert [str(t) for t in terminal_mapping_lines(s)] == [
        "terminal mapping (ssh): onlcr",
        "terminal mapping (console): unmeasured",
    ]
    rendered = [str(x) for x in survey_report(object(), s) if isinstance(x, Text)]
    assert "terminal mapping (ssh): onlcr" in rendered


def test_no_session_means_no_terminal_mapping_line():
    from otto.host.survey.report import terminal_mapping_lines

    assert terminal_mapping_lines(_survey()) == []
