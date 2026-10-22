"""The Next steps block: last output, this shell vs future shells, exports only when needed.

`otto --install-completion` writes the completion script and appends its
`source` line to ~/.bashrc; nothing sources it for the current shell, so a
user who runs only the first step sees no completion and concludes it is
broken (spec 2026-08-27 lab-definition-v2 §12, spec 2026-10-03 init-library
§3.8). Every command must also reach the terminal whole: a folded
``export`` line pasted line by line runs and sets the wrong value.
"""

import os
from pathlib import Path

from rich.console import Console

from otto.cli.init import next_steps_panel

# Reuse the rendering suite's harness: same DispatchRunner wiring, and the
# same "scaffold a whole project first" helper.
from tests.unit.cli.test_init_render import _invoke, _scaffold_all

_EXPORTS_WHEN_INACTIVE = 3
"""This shell, ~/.bashrc and ~/.profile each get the export line."""


def _render(root: Path, roots: str, *, width: int = 300) -> str:
    """Print the block exactly as ``otto init`` does, on a *width*-column console."""
    console = Console(width=width, record=True, color_system=None)
    steps = next_steps_panel(root, roots=roots, width=width)
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
    _scaffold_all(root)
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
    assert text.count(f"export OTTOPATH={root}") == _EXPORTS_WHEN_INACTIVE
    # This shell and ~/.profile source the script; ~/.bashrc already does.
    assert text.count("source ~/.bash_completions/otto.sh") == 2


def test_exports_are_omitted_when_the_root_is_already_active(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    for roots in (str(root), f"/elsewhere{os.pathsep}{root}/"):
        text = _render(root, roots)
        assert "export OTTOPATH" not in text, roots
        assert "Add only" not in text
        assert text.count("source ~/.bash_completions/otto.sh") == 2


def test_an_entry_the_loader_would_not_find_still_gets_the_exports(tmp_path: Path) -> None:
    """``/elsewhere: <root>`` names ``" <root>"`` to the loader, so the root is not active yet."""
    root = tmp_path.resolve()
    text = _render(root, f"/elsewhere{os.pathsep} {root}")
    assert text.count(f"export OTTOPATH={root}") == _EXPORTS_WHEN_INACTIVE


def test_a_tilde_entry_names_the_root_the_loader_reads(tmp_path: Path, monkeypatch) -> None:
    """``OTTOPATH=~/proj`` loads ``$HOME/proj``, so init must not ask for a second export."""
    from otto.cli.init import _already_active

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    assert _already_active(root, "~/proj")
    assert "export OTTOPATH" not in _render(root, "~/proj")


def test_a_comma_list_still_gets_the_exports(tmp_path: Path) -> None:
    """A comma does not separate: ``/elsewhere,<root>`` is one path, not the root."""
    root = tmp_path.resolve()
    text = _render(root, f"/elsewhere,{root}")
    assert text.count(f"export OTTOPATH={root}") == _EXPORTS_WHEN_INACTIVE


def test_a_long_root_prints_every_export_line_whole_at_80_columns(tmp_path: Path) -> None:
    root = _long_root(tmp_path)
    assert len(str(root)) >= 70
    lines = [line.strip() for line in _render(root, "", width=80).splitlines()]
    assert lines.count(f"export OTTOPATH={root}") == _EXPORTS_WHEN_INACTIVE, lines
    assert "╭" not in "".join(lines)
    assert lines[0].startswith("Next steps ─")


def test_a_long_root_reaches_the_cli_output_whole(tmp_path: Path) -> None:
    root = _long_root(tmp_path)
    root.mkdir()
    result = _invoke(["--all", "--name", "widget", "--path", str(root)])
    assert result.exit_code == 0, result.output
    lines = [line.strip(" │") for line in result.output.splitlines()]
    assert lines.count(f"export OTTOPATH={root}") == _EXPORTS_WHEN_INACTIVE, result.output


def test_a_short_root_keeps_the_box_at_80_columns() -> None:
    text = _render(Path("/srv/acme"), "", width=80)
    assert text.startswith("╭─ Next steps ─")
    assert text.rstrip().endswith("╯")
    assert "│          export OTTOPATH=/srv/acme" in text


def test_every_export_line_is_whole_on_either_side_of_the_box_boundary() -> None:
    """At 80 columns the box holds a command of up to 66 characters (80 - 8 indent - 6 box)."""
    prefix = "export OTTOPATH="
    for length in range(63, 70):
        root = Path("/" + "a" * (length - len(prefix) - 1))
        text = _render(root, "", width=80)
        lines = [line.strip(" │") for line in text.splitlines()]
        assert lines.count(f"{prefix}{root}") == _EXPORTS_WHEN_INACTIVE, (length, text)
        assert text.startswith("╭") == (length <= 66), length


def _try_it(text: str) -> list[str]:
    """The commands under "3. Try it:", or ``[]`` when the block has no such step."""
    if "3. Try it:" not in text:
        return []
    tail = text.split("3. Try it:", 1)[1]
    return [line.strip(" │╰╯─") for line in tail.splitlines() if line.strip(" │╰╯─")]


def test_a_full_project_tries_the_example_lab_tests_and_instruction(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _scaffold_all(root)
    assert _try_it(_render(root, str(root))) == [
        "otto --lab example_lab --list-hosts",
        "otto test --list-tests",
        "otto --lab example_lab test TestExample",
        "otto --lab example_lab test test_example_function",
        "otto --lab example_lab run smoke",
    ]


def test_a_kmodcov_only_project_is_never_told_to_use_a_lab_it_does_not_have(
    tmp_path: Path, monkeypatch
) -> None:
    """``otto init --kmodcov`` on an empty directory scaffolds no lab and no tests (#675).

    Its Try-it step once printed `otto --lab example_lab ...` for a lab that
    does not exist; it now names what the project holds: the vendored library.
    """
    root = tmp_path.resolve()
    monkeypatch.chdir(root)
    result = _invoke(["--kmodcov", "--name", "widget", "--path", str(root)])
    assert result.exit_code in (0, 1), result.output
    assert not (root / "lab_data" / "lab.json").exists()
    steps = _try_it(_render(root, str(root)))
    assert not [line for line in steps if "example_lab" in line], steps
    assert "otto cov kmodcov check third_party/otto_kmodcov" in steps, steps


def test_no_try_it_step_for_a_root_with_nothing_to_try(tmp_path: Path) -> None:
    text = _render(tmp_path.resolve(), "")
    assert "3. Try it:" not in text
    assert "example_lab" not in text
