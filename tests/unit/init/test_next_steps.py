"""``otto.init.next_steps``: the Try-it commands are the library's answer; the CLI draws them."""

from pathlib import Path

import pytest

from otto.init.config import InitConfig
from otto.init.next_steps import try_it_commands
from otto.init.scaffolder import scaffold


def test_a_full_project_tries_the_example_lab_tests_and_instruction(tmp_path: Path) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["settings", "lab", "tests", "instructions"])
    assert try_it_commands(
        tmp_path, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=tmp_path
    ) == [
        "otto --lab example_lab --list-hosts",
        "otto test --list-tests",
        "otto --lab example_lab test TestExample",
        "otto --lab example_lab test test_example_function",
        "otto --lab example_lab run smoke",
    ]


def test_a_kmodcov_only_project_gets_only_the_library_check(tmp_path: Path) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["kmodcov"])
    assert try_it_commands(
        tmp_path, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=tmp_path
    ) == ["otto cov kmodcov check third_party/otto_kmodcov"]


def test_the_library_path_is_absolute_when_the_cwd_is_elsewhere(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    scaffold(InitConfig(root, "widget", "0.1.0"), ["kmodcov"])
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    assert try_it_commands(
        root, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=elsewhere
    ) == [f"otto cov kmodcov check {root / 'third_party' / 'otto_kmodcov'}"]


def test_an_empty_root_has_nothing_to_try(tmp_path: Path) -> None:
    assert (
        try_it_commands(
            tmp_path, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=tmp_path
        )
        == []
    )


def test_an_embedded_gcov_only_project_gets_only_the_patch_check(tmp_path: Path) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["embedded-gcov"])
    assert try_it_commands(
        tmp_path, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=tmp_path
    ) == ["otto cov embedded-gcov check third_party/embedded-gcov-patch"]


def test_both_checks_are_offered_when_both_areas_are_present(tmp_path: Path) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["kmodcov", "embedded-gcov"])
    assert try_it_commands(
        tmp_path, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=tmp_path
    ) == [
        "otto cov kmodcov check third_party/otto_kmodcov",
        "otto cov embedded-gcov check third_party/embedded-gcov-patch",
    ]


def test_the_patch_path_is_absolute_when_the_cwd_is_elsewhere(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    scaffold(InitConfig(root, "widget", "0.1.0"), ["embedded-gcov"])
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    assert try_it_commands(
        root, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=elsewhere
    ) == [f"otto cov embedded-gcov check {root / 'third_party' / 'embedded-gcov-patch'}"]


def test_a_custom_embedded_gcov_dir_is_the_one_checked(tmp_path: Path) -> None:
    scaffold(
        InitConfig(tmp_path, "widget", "0.1.0", embedded_gcov_dir="vendor/egcov"), ["embedded-gcov"]
    )
    assert try_it_commands(tmp_path, "third_party/otto_kmodcov", "vendor/egcov", cwd=tmp_path) == [
        "otto cov embedded-gcov check vendor/egcov"
    ]


def _example_cmds(cmds: list[str]) -> list[str]:
    return [c for c in cmds if "TestExample" in c or "test_example_function" in c]


def _tries(root: Path) -> list[str]:
    return try_it_commands(
        root, "third_party/otto_kmodcov", "third_party/embedded-gcov-patch", cwd=root
    )


def test_a_project_with_its_own_tests_is_not_told_to_run_template_names(tmp_path: Path) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["settings", "lab"])
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_math.py").write_text("class TestMathops: ...\n")
    cmds = _tries(tmp_path)
    assert "otto test --list-tests" in cmds
    assert not _example_cmds(cmds)


def _unreadable(path: Path) -> None:
    path.unlink()
    path.mkdir()  # a directory named test_example.py: globbed, not readable


@pytest.mark.parametrize(
    "body",
    [
        "def test_other(): pass\n",
        "class TestExampleOld: ...\ndef test_example_function_old(): ...\n",
        "# class TestExample:\n# def test_example_function(): ...\n",
        "def TestExample(): ...\nclass test_example_function: ...\n",
        "class TestExample(:\n",
        None,
    ],
    ids=["edited", "renamed", "commented-out", "swapped-kinds", "invalid-syntax", "unreadable"],
)
def test_an_example_file_without_the_exact_template_names_is_not_suggested(
    tmp_path: Path, body: str | None
) -> None:
    scaffold(InitConfig(tmp_path, "widget", "0.1.0"), ["settings", "lab", "tests", "instructions"])
    example = tmp_path / "tests" / "test_example.py"
    if body is None:
        _unreadable(example)
    else:
        example.write_text(body)
    cmds = _tries(tmp_path)
    assert "otto test --list-tests" in cmds
    assert not _example_cmds(cmds)
