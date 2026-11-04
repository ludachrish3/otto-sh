"""``otto.console``'s literal-text decision: ``print_literal`` and ``literal_cell`` (#785)."""

from rich.console import Console
from rich.text import Text

from otto.console import literal_cell, print_literal
from tests._fixtures.literal_text import (
    HOSTILE,
    HOSTILE_TOKEN,
    assert_one_verbatim_line,
    rejoin_column,
)


def test_print_literal_prints_hostile_text_as_one_line(monkeypatch, capsys) -> None:
    monkeypatch.setenv("COLUMNS", "40")
    print_literal(HOSTILE)
    out = capsys.readouterr().out
    assert out.splitlines() == [HOSTILE]
    assert_one_verbatim_line(out, HOSTILE)


def test_a_styled_prefix_keeps_its_style_and_the_path_stays_literal() -> None:
    console = Console(record=True, width=40, color_system="truecolor", force_terminal=True)
    print_literal(Text.assemble(("Wrote schemas to ", "green"), "/tmp/[red]x"), console=console)
    assert console.export_text(clear=False).splitlines() == ["Wrote schemas to /tmp/[red]x"]
    styled = console.export_text(styles=True)
    assert "\x1b[32mWrote schemas to " in styled  # green prefix kept


def test_literal_cell_keeps_an_emoji_code() -> None:
    assert literal_cell(":smile:").plain == ":smile:"


def test_literal_cell_returns_a_text_unchanged() -> None:
    text = Text("x", style="cyan")
    assert literal_cell(text) is text


def test_a_literal_leaf_value_prints_verbatim(monkeypatch, capsys) -> None:
    """Preservation: ``otto host`` verbs' literal payloads already set all four flags."""
    from otto.cli.invoke import _print_value

    monkeypatch.setenv("COLUMNS", "40")
    _print_value(HOSTILE, literal=True)
    assert_one_verbatim_line(capsys.readouterr().out, HOSTILE)


# ── id_column: identifier cells are literal and fold ────────────────────────


def test_an_id_column_cell_is_literal_and_folds_instead_of_cutting() -> None:
    from rich import box
    from rich.table import Table

    from otto.console import id_column

    table = Table(box=box.ROUNDED)
    id_column(table, "id")
    table.add_row(literal_cell(HOSTILE_TOKEN))
    console = Console(width=60, record=True)
    console.print(table)
    out = console.export_text()
    assert "…" not in out
    assert rejoin_column(out, 0) == HOSTILE_TOKEN


def test_rich_fold_limits_are_pinned() -> None:
    """The two limits D3 states, pinned so a Rich upgrade that changes them is noticed.

    A space that lands on a fold boundary is dropped, and a double-width
    character cannot render in a one-cell column. Both hold at any width.
    """
    from rich.table import Table

    from otto.console import id_column

    def render(value: str, width: int) -> list[str]:
        table = Table(show_header=False, box=None, padding=0)
        id_column(table, "h", width=width)
        table.add_row(literal_cell(value))
        console = Console(width=20, record=True)
        console.print(table)
        return [line.rstrip() for line in console.export_text().splitlines()]

    assert render("ab cd", 2) == ["ab", "cd"]
    assert "漢" not in "".join(render("漢", 1))
