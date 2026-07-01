"""The docker no-output-dir set covers read-only subcommands only."""

from otto.cli.docker import _NO_OUTPUT_DIR_SUBCOMMANDS


def test_ps_is_no_output_dir() -> None:
    assert "ps" in _NO_OUTPUT_DIR_SUBCOMMANDS


def test_mutating_subcommands_create_dir() -> None:
    for sub in ("build", "up", "down"):
        assert sub not in _NO_OUTPUT_DIR_SUBCOMMANDS
