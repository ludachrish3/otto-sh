"""prepare_run refuses a run that cannot save its files, before any host is touched."""

import os
import stat
from unittest.mock import MagicMock

import pytest

from otto.coverage.config import DestinationError
from otto.params import OptionsValidationError
from otto.suite.run import RunOptions, prepare_run
from tests._fixtures.bootstrap_seam import patch_bootstrap


def _project_with_tickets(monkeypatch, tickets: bool) -> None:
    cov = (
        {
            "tiers": {"system": {"kind": "e2e", "precedence": 1}},
            "tickets": {"pattern": r"[A-Z]{2,10}-[0-9]+"},
        }
        if tickets
        else {}
    )
    patch_bootstrap(monkeypatch, [MagicMock()])
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", lambda projects: cov)


def test_cov_dir_is_prepared(tmp_path):
    target = tmp_path / "e2e"
    prepare_run(RunOptions(cov_dir=target))
    assert (target / "cov").is_dir()


def test_non_empty_cov_dir_is_refused_in_field_terms(tmp_path):
    (tmp_path / "cov").mkdir()
    (tmp_path / "cov" / "stale").write_text("x")
    with pytest.raises(
        DestinationError, match=r"cov_dir target .*/cov is not empty.*set overwrite_cov_dir=True"
    ):
        prepare_run(RunOptions(cov_dir=tmp_path))


def test_overwrite_cov_dir_clears_only_the_cov_subtree(tmp_path):
    run = tmp_path / "e2e"
    (run / "cov").mkdir(parents=True)
    (run / "cov" / "stale").write_text("x")
    (run / "keep.txt").write_text("x")
    prepare_run(RunOptions(cov_dir=run, overwrite_cov_dir=True))
    assert list((run / "cov").iterdir()) == []
    assert (run / "keep.txt").read_text() == "x"


def test_a_non_empty_run_dir_with_no_cov_is_accepted(tmp_path):
    run = tmp_path / "e2e"
    run.mkdir()
    (run / "run.log").write_text("x")
    prepare_run(RunOptions(cov_dir=run))
    assert (run / "cov").is_dir()
    assert (run / "run.log").exists()


@pytest.mark.parametrize("alias", ["to-run-dir", "to-report-dir"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_a_symlinked_cov_leaf_is_refused_before_anything_is_cleared(tmp_path, alias, dry_run):
    run = tmp_path / "e2e"
    run.mkdir()
    if alias == "to-run-dir":
        planted = run / "keep.txt"
        (run / "cov").symlink_to(run)
        report = None
    else:
        (run / "report").mkdir()
        planted = run / "report" / "index.html"
        (run / "cov").symlink_to(run / "report")
        report = run / "report"
    planted.write_text("original")
    opts = RunOptions(
        cov_dir=run,
        overwrite_cov_dir=True,
        cov_report_dir=report,
        overwrite_cov_report_dir=True,
    )
    with pytest.raises(DestinationError, match=r"^cov_dir target .*/e2e/cov is a symlink") as exc:
        prepare_run(opts, dry_run=dry_run)
    assert exc.value.kind == "symlink"
    assert planted.read_text() == "original"
    assert (run / "cov").is_symlink()


def test_non_empty_cov_report_dir_is_refused_before_the_run(tmp_path):
    (tmp_path / "index.html").write_text("stale")
    with pytest.raises(DestinationError, match="cov_report_dir target"):
        prepare_run(RunOptions(cov_report_dir=tmp_path))


def test_dry_run_touches_nothing(tmp_path):
    target = tmp_path / "new" / "report"
    prepare_run(RunOptions(cov_report_dir=target), dry_run=True)
    assert not target.parent.exists()
    (tmp_path / "cov").mkdir()
    (tmp_path / "cov" / "stale").write_text("x")
    (tmp_path / "keep").write_text("x")
    with pytest.raises(DestinationError, match="is not empty"):
        prepare_run(RunOptions(cov_dir=tmp_path), dry_run=True)
    assert (tmp_path / "cov" / "stale").read_text() == "x"
    assert (tmp_path / "keep").read_text() == "x"


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
    _project_with_tickets(monkeypatch, tickets=False)
    with pytest.raises(
        OptionsValidationError, match=r"cov_tickets_json requires \[coverage.tickets\]"
    ):
        prepare_run(RunOptions(cov_tickets_json=tmp_path / "t.json"))


def test_tickets_json_with_a_tickets_table_passes(monkeypatch, tmp_path):
    _project_with_tickets(monkeypatch, tickets=True)
    prepare_run(RunOptions(cov_tickets_json=tmp_path / "t.json"))


def test_no_destinations_means_no_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "otto.bootstrap.bootstrap", lambda: pytest.fail("projects must not be read")
    )
    prepare_run(RunOptions())


def test_dry_run_with_overwrite_on_a_non_empty_cov_dir_passes_and_clears_nothing(tmp_path):
    (tmp_path / "cov").mkdir()
    (tmp_path / "cov" / "stale").write_text("x")
    (tmp_path / "keep").write_text("x")
    prepare_run(RunOptions(cov_dir=tmp_path, overwrite_cov_dir=True), dry_run=True)
    assert (tmp_path / "cov" / "stale").read_text() == "x"
    assert (tmp_path / "keep").read_text() == "x"


def test_a_bad_cov_dir_short_circuits_before_the_report_dir_is_touched(tmp_path):
    cov_dir = tmp_path / "run"
    (cov_dir / "cov").mkdir(parents=True)
    (cov_dir / "cov" / "stale").write_text("x")
    (cov_dir / "keep").write_text("x")
    report_dir = tmp_path / "report"
    with pytest.raises(DestinationError, match="cov_dir target"):
        prepare_run(RunOptions(cov_dir=cov_dir, cov_report_dir=report_dir))
    assert not report_dir.exists()
    assert (cov_dir / "keep").read_text() == "x"


def test_tickets_json_with_no_projects_is_refused(monkeypatch, tmp_path):
    patch_bootstrap(monkeypatch, [])
    with pytest.raises(
        OptionsValidationError, match=r"cov_tickets_json requires \[coverage.tickets\]"
    ):
        prepare_run(RunOptions(cov_tickets_json=tmp_path / "t.json"))
