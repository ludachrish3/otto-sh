"""prepare_run refuses a run that cannot save its files, before any host is touched."""

import os
import stat
from unittest.mock import MagicMock

import pytest

from otto.coverage.config import DestinationError
from otto.params import OptionsValidationError
from otto.suite.run import RunOptions, prepare_run


def _repo_with_tickets(monkeypatch, tickets: bool) -> None:
    cov = (
        {
            "tiers": {"system": {"kind": "e2e", "precedence": 1}},
            "tickets": {"pattern": r"[A-Z]{2,10}-[0-9]+"},
        }
        if tickets
        else {}
    )
    monkeypatch.setattr("otto.config.get_repos", lambda: [MagicMock()])
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", lambda repos: cov)


def test_cov_dir_is_prepared(tmp_path):
    target = tmp_path / "cov"
    prepare_run(RunOptions(cov_dir=target))
    assert target.is_dir()


def test_non_empty_cov_dir_is_refused_in_field_terms(tmp_path):
    (tmp_path / "stale").write_text("x")
    with pytest.raises(DestinationError, match=r"cov_dir target .* set overwrite_cov_dir=True"):
        prepare_run(RunOptions(cov_dir=tmp_path))


def test_overwrite_cov_dir_clears_it(tmp_path):
    (tmp_path / "stale").write_text("x")
    prepare_run(RunOptions(cov_dir=tmp_path, overwrite_cov_dir=True))
    assert list(tmp_path.iterdir()) == []


def test_non_empty_cov_report_dir_is_refused_before_the_run(tmp_path):
    (tmp_path / "index.html").write_text("stale")
    with pytest.raises(DestinationError, match="cov_report_dir target"):
        prepare_run(RunOptions(cov_report_dir=tmp_path))


def test_dry_run_touches_nothing(tmp_path):
    target = tmp_path / "new" / "report"
    prepare_run(RunOptions(cov_report_dir=target), dry_run=True)
    assert not target.parent.exists()
    (tmp_path / "stale").write_text("x")
    with pytest.raises(DestinationError, match="is not empty"):
        prepare_run(RunOptions(cov_dir=tmp_path), dry_run=True)
    assert (tmp_path / "stale").read_text() == "x"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_read_only_report_dir_is_refused_as_unwritable(tmp_path):
    target = tmp_path / "ro"
    target.mkdir()
    target.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(DestinationError, match="cannot be written"):
            prepare_run(RunOptions(cov_report_dir=target))
    finally:
        target.chmod(stat.S_IRWXU)


def test_tickets_json_without_a_tickets_table_is_refused(monkeypatch, tmp_path):
    _repo_with_tickets(monkeypatch, tickets=False)
    with pytest.raises(
        OptionsValidationError, match=r"cov_tickets_json requires \[coverage.tickets\]"
    ):
        prepare_run(RunOptions(cov_tickets_json=tmp_path / "t.json"))


def test_tickets_json_with_a_tickets_table_passes(monkeypatch, tmp_path):
    _repo_with_tickets(monkeypatch, tickets=True)
    prepare_run(RunOptions(cov_tickets_json=tmp_path / "t.json"))


def test_no_destinations_means_no_work(tmp_path, monkeypatch):
    monkeypatch.setattr("otto.config.get_repos", lambda: pytest.fail("repos must not be read"))
    prepare_run(RunOptions())


def test_dry_run_with_overwrite_on_a_non_empty_cov_dir_passes_and_clears_nothing(tmp_path):
    (tmp_path / "stale").write_text("x")
    prepare_run(RunOptions(cov_dir=tmp_path, overwrite_cov_dir=True), dry_run=True)
    assert (tmp_path / "stale").read_text() == "x"


def test_a_bad_cov_dir_short_circuits_before_the_report_dir_is_touched(tmp_path):
    cov_dir = tmp_path / "cov"
    cov_dir.mkdir()
    (cov_dir / "stale").write_text("x")
    report_dir = tmp_path / "report"
    with pytest.raises(DestinationError, match="cov_dir target"):
        prepare_run(RunOptions(cov_dir=cov_dir, cov_report_dir=report_dir))
    assert not report_dir.exists()


def test_tickets_json_with_no_repos_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr("otto.config.get_repos", list)
    with pytest.raises(
        OptionsValidationError, match=r"cov_tickets_json requires \[coverage.tickets\]"
    ):
        prepare_run(RunOptions(cov_tickets_json=tmp_path / "t.json"))
