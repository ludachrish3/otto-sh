"""The Next steps block: last output, this shell vs future shells, exports only when needed.

`otto --install-completion` writes the completion script and appends its
`source` line to ~/.bashrc; nothing sources it for the current shell, so a
user who runs only the first step sees no completion and concludes it is
broken (spec 2026-08-27 lab-definition-v2 §12, spec 2026-10-03 init-library
§3.8). Every command must also reach the terminal whole: a folded
``export`` line pasted line by line runs and sets the wrong value.
"""

from pathlib import Path

from rich.console import Console

from otto.cli.init import next_steps_panel

# Reuse the rendering suite's harness: same DispatchRunner wiring, and the
# same "scaffold a whole repo first" helper.
from tests.unit.cli.test_init_render import _invoke, _scaffold_all

_EXPORTS_WHEN_INACTIVE = 3
"""This shell, ~/.bashrc and ~/.profile each get the export line."""


def _render(root: Path, sut_dirs: str, *, width: int = 300) -> str:
    """Print the block exactly as ``otto init`` does, on a *width*-column console."""
    console = Console(width=width, record=True, color_system=None)
    steps = next_steps_panel(root, sut_dirs=sut_dirs, width=width)
    console.print(steps.renderable, soft_wrap=steps.soft_wrap)
    return console.export_text()


def _long_root(tmp_path: Path) -> Path:
    """A root of at least 70 characters: too long for a boxed export line at 80 columns."""
    base = tmp_path.resolve()
    return base / ("r" * max(1, 70 - len(str(base)) - 1))


def test_the_block_is_the_last_output(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    out = _invoke(["--all", "--path", str(tmp_path)]).output.rstrip()
    # Boxed or not (a long basetemp unboxes it), the last command closes the run.
    content = [line.strip(" │╰╯─") for line in out.splitlines()]
    assert [line for line in content if line][-1] == "otto --lab example_lab run smoke", out
    assert out.index("otto init —") < out.index("Next steps")


def test_both_shells_are_covered_and_completion_is_sourced_after_install(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    text = _render(root, "")
    for heading in (
        "1. Activate otto in this shell:",
        "2. Activate otto in future shells:",
        "~/.bashrc",
        "~/.profile",
        "3. Try it:",
    ):
        assert heading in text, heading
    assert text.index("otto --install-completion") < text.index(
        "source ~/.bash_completions/otto.sh"
    )
    assert "Never put `otto --install-completion` itself in a startup file" in text
    assert text.count(f"export OTTO_SUT_DIRS={root}") == _EXPORTS_WHEN_INACTIVE
    # This shell and ~/.profile source the script; ~/.bashrc already does.
    assert text.count("source ~/.bash_completions/otto.sh") == 2


def test_exports_are_omitted_when_the_root_is_already_active(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    for sut_dirs in (str(root), f"/elsewhere:{root}/", f"/elsewhere,{root}"):
        text = _render(root, sut_dirs)
        assert "export OTTO_SUT_DIRS" not in text, sut_dirs
        assert "Add only" not in text
        assert text.count("source ~/.bash_completions/otto.sh") == 2


def test_an_entry_the_loader_would_not_find_still_gets_the_exports(tmp_path: Path) -> None:
    """``/elsewhere, <root>`` names ``" <root>"`` to the loader, so the root is not active yet."""
    root = tmp_path.resolve()
    text = _render(root, f"/elsewhere, {root}")
    assert text.count(f"export OTTO_SUT_DIRS={root}") == _EXPORTS_WHEN_INACTIVE


def test_a_long_root_prints_every_export_line_whole_at_80_columns(tmp_path: Path) -> None:
    root = _long_root(tmp_path)
    assert len(str(root)) >= 70
    lines = [line.strip() for line in _render(root, "", width=80).splitlines()]
    assert lines.count(f"export OTTO_SUT_DIRS={root}") == _EXPORTS_WHEN_INACTIVE, lines
    assert "╭" not in "".join(lines)
    assert lines[0].startswith("Next steps ─")


def test_a_long_root_reaches_the_cli_output_whole(tmp_path: Path) -> None:
    root = _long_root(tmp_path)
    root.mkdir()
    result = _invoke(["--all", "--name", "widget", "--path", str(root)])
    assert result.exit_code == 0, result.output
    lines = [line.strip(" │") for line in result.output.splitlines()]
    assert lines.count(f"export OTTO_SUT_DIRS={root}") == _EXPORTS_WHEN_INACTIVE, result.output


def test_a_short_root_keeps_the_box_at_80_columns() -> None:
    text = _render(Path("/srv/acme"), "", width=80)
    assert text.startswith("╭─ Next steps ─")
    assert text.rstrip().endswith("╯")
    assert "│          export OTTO_SUT_DIRS=/srv/acme" in text


def test_every_export_line_is_whole_on_either_side_of_the_box_boundary() -> None:
    """At 80 columns the box holds a command of up to 66 characters (80 - 8 indent - 6 box)."""
    prefix = "export OTTO_SUT_DIRS="
    for length in range(63, 70):
        root = Path("/" + "a" * (length - len(prefix) - 1))
        text = _render(root, "", width=80)
        lines = [line.strip(" │") for line in text.splitlines()]
        assert lines.count(f"{prefix}{root}") == _EXPORTS_WHEN_INACTIVE, (length, text)
        assert text.startswith("╭") == (length <= 66), length
