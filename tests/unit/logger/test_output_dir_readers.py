"""Every reader of a run's directory finds it where ``create_output_dir`` put it.

otto itself reads no run back by path; the readers are the e2e harness's
``output_dirs`` / ``assert_no_output_dir``, which every e2e test that checks
for (or against) a run directory goes through. Both sides take the command's
directory from :func:`otto.layout.command_output_dir`, and this pins that
they meet.
"""

import pytest

from otto.logger import management
from tests.e2e._otto_subprocess import assert_no_output_dir, output_dirs


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


@pytest.mark.parametrize(("command", "sub"), [("test", None), ("host", "exec")])
def test_the_e2e_reader_finds_the_run_the_writer_made(tmp_path, command, sub):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir(command, sub)
    assert output_dirs(tmp_path, command) == [out]
    assert output_dirs(tmp_path) == [out]
    with pytest.raises(AssertionError):
        assert_no_output_dir(tmp_path)


def test_the_e2e_reader_ignores_a_users_own_command_named_directory(tmp_path):
    # A project's own ./test holding an otto-pattern child is not a run.
    (tmp_path / "test" / "20200101_000000_000").mkdir(parents=True)
    assert output_dirs(tmp_path) == []
    assert_no_output_dir(tmp_path)


def test_the_e2e_reader_sees_a_run_of_any_command(tmp_path):
    # Not a hand-kept command list: everything under otto-output/ is otto's, so
    # a run of a command no list names (here `link up`) still registers.
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("link", "up")
    assert output_dirs(tmp_path) == [out]
    with pytest.raises(AssertionError):
        assert_no_output_dir(tmp_path)
