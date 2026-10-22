"""``otto.init.next_steps``: the Try-it commands are the library's answer; the CLI draws them."""

from pathlib import Path

from otto.init.config import InitConfig
from otto.init.next_steps import try_it_commands
from otto.init.scaffolder import scaffold


def test_a_full_project_tries_the_example_lab_tests_and_instruction(tmp_path: Path) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["settings", "lab", "tests", "instructions"])
    assert try_it_commands(tmp_path, "third_party/otto_kmodcov", cwd=tmp_path) == [
        "otto --lab example_lab --list-hosts",
        "otto test --list-tests",
        "otto --lab example_lab test TestExample",
        "otto --lab example_lab test test_example_function",
        "otto --lab example_lab run smoke",
    ]


def test_a_kmodcov_only_project_gets_only_the_library_check(tmp_path: Path) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["kmodcov"])
    assert try_it_commands(tmp_path, "third_party/otto_kmodcov", cwd=tmp_path) == [
        "otto cov kmodcov check third_party/otto_kmodcov"
    ]


def test_the_library_path_is_absolute_when_the_cwd_is_elsewhere(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    scaffold(InitConfig(root, "widget", "0.1.0"), ["kmodcov"])
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    assert try_it_commands(root, "third_party/otto_kmodcov", cwd=elsewhere) == [
        f"otto cov kmodcov check {root / 'third_party' / 'otto_kmodcov'}"
    ]


def test_an_empty_root_has_nothing_to_try(tmp_path: Path) -> None:
    assert try_it_commands(tmp_path, "third_party/otto_kmodcov", cwd=tmp_path) == []
