"""``run_coverage_report`` refuses unusable inputs before reading anything."""

import pytest

from otto.coverage.errors import CoverageInputError
from otto.coverage.report_inputs import ReportInputs
from otto.coverage.reporter import run_coverage_report


@pytest.mark.asyncio
async def test_a_missing_run_directory_is_refused_by_field(tmp_path):
    missing = tmp_path / "nope" / "cov"
    with pytest.raises(CoverageInputError, match="output directory does not exist") as e:
        await run_coverage_report([missing], tmp_path / "out", ReportInputs())
    assert e.value.field == "cov_dirs"
    assert str(tmp_path / "nope") in str(e.value)


@pytest.mark.asyncio
async def test_a_run_dir_without_cov_is_not_refused(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    # Reaching past validation is enough: stub what follows it. reporter.py
    # imports prepare_destination lazily inside run_coverage_report, so the
    # patch target is its real home, otto.coverage.config, not the reporter
    # module (which never binds it at module scope).
    monkeypatch.setattr("otto.coverage.config.prepare_destination", lambda *a, **k: None)
    try:
        await run_coverage_report([run / "cov"], tmp_path / "out", ReportInputs())
    except CoverageInputError:
        pytest.fail("a run directory without cov/ must not be refused")
    except Exception:  # noqa: BLE001 — anything past validation is out of scope here
        pass


@pytest.mark.parametrize(
    ("specs", "match"),
    [
        (
            [("unit", None)],
            r"^Tier 'unit' requires a path \(only the 'system' tier may omit a path\)$",
        ),
        ([("system", None), ("system", None)], r"Duplicate tier name: 'system'"),
    ],
)
@pytest.mark.asyncio
async def test_bad_tier_specs_are_refused_by_field(tmp_path, specs, match):
    with pytest.raises(CoverageInputError, match=match) as e:
        await run_coverage_report([], tmp_path / "out", ReportInputs(), tier_specs=specs)
    assert e.value.field == "tier_specs"
