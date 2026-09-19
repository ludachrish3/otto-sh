"""The kgcov observation hook and collator, over synthetic items and records."""

import json
from types import SimpleNamespace

import pytest

from scripts.collate_kgcov_matrix import collate
from scripts.collate_kgcov_matrix import main as collate_main
from tests._fixtures.kgcov_matrix import (
    BED,
    BUILD,
    CONTROL_SURFACE,
    CROSS_PROFILE,
    SURFACES,
    build_matrix,
)
from tests.e2e.cov._kgcov_observation import (
    FORMAT,
    OBSERVATION,
    contract_of,
    profile_of_item,
    read_records,
    record_phase,
    run_id,
)
from tests.e2e.cov._repo5_build import TOOLCHAINS_KEY, Toolchain

pytestmark = pytest.mark.interpreter_agnostic

PARSE = "tests/e2e/cov/test_kgcov_toolchains_e2e.py::TestCoverage::test_parse_hits"
CROSS = "tests/e2e/cov/test_kgcov_cross_build.py::test_both_modules_are_x86_64_objects"


class _Config:
    def __init__(self):
        self.stash = {}


def _item(nodeid, *, config, params=None, fixturenames=()):
    callspec = SimpleNamespace(params=params) if params is not None else None
    item = SimpleNamespace(nodeid=nodeid, config=config, stash={}, fixturenames=list(fixturenames))
    if callspec is not None:
        item.callspec = callspec
    return item


def _report(when, *, failed=False, skipped=False, longrepr=""):
    return SimpleNamespace(
        when=when,
        failed=failed,
        skipped=skipped,
        passed=not failed and not skipped,
        longreprtext=longrepr,
    )


def _config_with(tc: Toolchain) -> _Config:
    config = _Config()
    config.stash[TOOLCHAINS_KEY] = {tc.name: tc}
    return config


GCC12 = Toolchain(
    "gcc-12", {"CC": "gcc-12"}, compiler_version="12.3.0", kernel_release="6.8.0-86-generic"
)


def test_contract_of_strips_the_parametrization():
    assert contract_of(f"{PARSE}[gcc-12]") == PARSE
    assert contract_of(CROSS) == CROSS


def test_a_bed_item_is_placed_by_its_built_with_param():
    item = _item(f"{PARSE}[gcc-12]", config=_Config(), params={"built_with": "gcc-12"})
    assert profile_of_item(item) == ("gcc-12", BED)


def test_a_build_item_is_placed_by_the_cross_build_fixture():
    item = _item(CROSS, config=_Config(), fixturenames=["cross_build", "request"])
    assert profile_of_item(item) == (CROSS_PROFILE, BUILD)


def test_an_item_with_neither_is_not_placed():
    item = _item("tests/e2e/cov/test_other.py::test_x", config=_Config(), fixturenames=["tmp_path"])
    assert profile_of_item(item) is None


def test_nothing_is_written_before_teardown(tmp_path):
    item = _item(f"{PARSE}[gcc-12]", config=_config_with(GCC12), params={"built_with": "gcc-12"})
    assert record_phase(item, _report("setup"), tmp_path) is None
    assert record_phase(item, _report("call"), tmp_path) is None
    assert not list(tmp_path.glob("*.json"))


def test_a_passing_item_writes_one_record_with_its_provenance(tmp_path):
    config = _config_with(GCC12)
    item = _item(f"{PARSE}[gcc-12]", config=config, params={"built_with": "gcc-12"})
    for when in ("setup", "call"):
        record_phase(item, _report(when), tmp_path)
    path = record_phase(item, _report("teardown"), tmp_path)
    record = json.loads(path.read_text())
    assert record == {
        "kind": OBSERVATION,
        "format": FORMAT,
        "nodeid": PARSE,
        "item": f"{PARSE}[gcc-12]",
        "profile": "gcc-12",
        "venue": BED,
        "outcome": "passed",
        "compiler_version": "12.3.0",
        "kernel_release": "6.8.0-86-generic",
        "as_of": record["as_of"],
        "run_id": run_id(config),
    }


def test_the_outcome_is_read_from_the_reports_not_the_body(tmp_path):
    item = _item(f"{PARSE}[gcc-12]", config=_config_with(GCC12), params={"built_with": "gcc-12"})
    record_phase(item, _report("setup"), tmp_path)
    record_phase(item, _report("call", failed=True, longrepr="x\nE   assert 1 == 2"), tmp_path)
    record = json.loads(record_phase(item, _report("teardown"), tmp_path).read_text())
    assert record["outcome"] == "failed"
    assert record["failure_summary"] == "call: E   assert 1 == 2"


def test_a_setup_error_is_an_error_not_a_failure(tmp_path):
    item = _item(f"{PARSE}[gcc-12]", config=_config_with(GCC12), params={"built_with": "gcc-12"})
    record_phase(item, _report("setup", failed=True, longrepr="E   boom"), tmp_path)
    record = json.loads(record_phase(item, _report("teardown"), tmp_path).read_text())
    assert record["outcome"] == "error"


def test_a_skip_is_its_own_outcome(tmp_path):
    item = _item(f"{PARSE}[gcc-12]", config=_config_with(GCC12), params={"built_with": "gcc-12"})
    record_phase(item, _report("setup", skipped=True), tmp_path)
    record = json.loads(record_phase(item, _report("teardown"), tmp_path).read_text())
    assert record["outcome"] == "skipped"


def test_an_item_whose_fixture_never_produced_a_toolchain_records_null_provenance(tmp_path):
    item = _item(f"{PARSE}[gcc-12]", config=_Config(), params={"built_with": "gcc-12"})
    record_phase(
        item, _report("setup", failed=True, longrepr="E   gcc-12 is not on PATH"), tmp_path
    )
    record = json.loads(record_phase(item, _report("teardown"), tmp_path).read_text())
    assert record["outcome"] == "error"
    assert record["compiler_version"] is None
    assert record["kernel_release"] is None


def test_rerunning_an_item_replaces_its_record(tmp_path):
    item = _item(f"{PARSE}[gcc-12]", config=_config_with(GCC12), params={"built_with": "gcc-12"})
    for _ in range(2):
        for when in ("setup", "call", "teardown"):
            record_phase(item, _report(when), tmp_path)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_the_first_write_of_a_session_clears_older_records(tmp_path):
    (tmp_path / "observation-stale.json").write_text("{}")
    item = _item(f"{PARSE}[gcc-12]", config=_config_with(GCC12), params={"built_with": "gcc-12"})
    for when in ("setup", "call", "teardown"):
        record_phase(item, _report(when), tmp_path)
    names = [p.name for p in tmp_path.glob("*.json")]
    assert "observation-stale.json" not in names
    assert len(names) == 1


def test_records_of_one_session_share_a_run_id():
    config = _config_with(GCC12)
    assert run_id(config) == run_id(config)
    assert run_id(config) != run_id(_config_with(GCC12))


def test_read_records_answers_every_record_sorted(tmp_path):
    for n in ("b", "a"):
        (tmp_path / f"observation-{n}.json").write_text(json.dumps({"n": n}))
    assert [r["n"] for r in read_records(tmp_path)] == ["a", "b"]
    assert read_records(tmp_path / "absent") == []


def test_every_surface_is_a_contract_the_hook_would_place():
    for surface in SURFACES:
        assert surface.venue in (BED, BUILD)
    assert CONTROL_SURFACE.venue == BED


def _item_for_surface(surface, *, config, profile="gcc-12"):
    if surface.venue == BED:
        return (
            _item(f"{surface.contract}[{profile}]", config=config, params={"built_with": profile}),
            profile,
        )
    return (
        _item(surface.contract, config=config, fixturenames=["cross_build", "request"]),
        CROSS_PROFILE,
    )


@pytest.mark.parametrize("surface", SURFACES, ids=[s.id for s in SURFACES])
def test_every_surface_places_in_its_own_declared_venue_and_writes(surface, tmp_path):
    item, profile = _item_for_surface(surface, config=_Config())
    assert profile_of_item(item) == (profile, surface.venue)
    path = None
    for when in ("setup", "call", "teardown"):
        path = record_phase(item, _report(when), tmp_path)
    assert path is not None
    record = json.loads(path.read_text())
    assert record["venue"] == surface.venue
    assert record["profile"] == profile


def test_a_nodeid_the_surface_table_does_not_name_writes_nothing(tmp_path):
    item = _item(
        "tests/e2e/cov/test_kgcov_toolchains_e2e.py::TestCoverage::test_no_such_contract[gcc-12]",
        config=_Config(),
        params={"built_with": "gcc-12"},
    )
    assert profile_of_item(item) is None
    for when in ("setup", "call", "teardown"):
        assert record_phase(item, _report(when), tmp_path) is None
    assert not list(tmp_path.glob("*.json"))


def test_a_bed_fixture_item_under_a_build_surfaces_nodeid_raises():
    build_surface = next(s for s in SURFACES if s.venue == BUILD)
    item = _item(
        f"{build_surface.contract}[gcc-12]", config=_Config(), params={"built_with": "gcc-12"}
    )
    with pytest.raises(RuntimeError, match=build_surface.contract):
        profile_of_item(item)


def test_two_different_items_in_one_session_both_survive(tmp_path):
    config = _config_with(GCC12)
    cross_surface = next(s for s in SURFACES if s.contract == CROSS)
    item_a = _item(f"{PARSE}[gcc-12]", config=config, params={"built_with": "gcc-12"})
    item_b, _ = _item_for_surface(cross_surface, config=config)
    for item in (item_a, item_b):
        for when in ("setup", "call", "teardown"):
            record_phase(item, _report(when), tmp_path)
    assert len(list(tmp_path.glob("*.json"))) == 2
    records = read_records(tmp_path)
    assert {r["item"] for r in records} == {f"{PARSE}[gcc-12]", CROSS}


def test_the_same_nodeid_under_two_profiles_both_survive(tmp_path):
    config = _config_with(GCC12)
    item_gcc12 = _item(f"{PARSE}[gcc-12]", config=config, params={"built_with": "gcc-12"})
    item_gcc13 = _item(f"{PARSE}[gcc-13]", config=config, params={"built_with": "gcc-13"})
    for item in (item_gcc12, item_gcc13):
        for when in ("setup", "call", "teardown"):
            record_phase(item, _report(when), tmp_path)
    records = read_records(tmp_path)
    assert len(records) == 2
    assert {r["profile"] for r in records} == {"gcc-12", "gcc-13"}


def test_an_xdist_worker_refuses_to_write_kgcov_observations(tmp_path):
    config = _config_with(GCC12)
    config.workerinput = {}
    item = _item(f"{PARSE}[gcc-12]", config=config, params={"built_with": "gcc-12"})
    record_phase(item, _report("setup"), tmp_path)
    record_phase(item, _report("call"), tmp_path)
    with pytest.raises(RuntimeError, match="-n0"):
        record_phase(item, _report("teardown"), tmp_path)


CONTROL = CONTROL_SURFACE.contract


def _record(nodeid, profile, outcome, *, run="run-1", venue=BED, version="12.3.0", summary=None):
    r = {
        "kind": OBSERVATION,
        "format": FORMAT,
        "nodeid": nodeid,
        "item": f"{nodeid}[{profile}]",
        "profile": profile,
        "venue": venue,
        "outcome": outcome,
        "compiler_version": version,
        "kernel_release": "6.8.0-86-generic",
        "as_of": "2026-09-19",
        "run_id": run,
    }
    if summary:
        r["failure_summary"] = summary
    return r


def _fresh():
    return build_matrix(None)


def test_a_passing_contract_with_a_passing_control_is_measured_ok():
    result = collate(
        _fresh(), [_record(PARSE, "gcc-12", "passed"), _record(CONTROL, "gcc-12", "passed")]
    )
    cell = result.matrix["cells"]["parse-hits"]["gcc-12"]
    assert cell == {
        "status": "measured-ok",
        "nodeid": PARSE,
        "venue": BED,
        "as_of": "2026-09-19",
        "outcome": "passed",
        "compiler_version": "12.3.0",
        "kernel_release": "6.8.0-86-generic",
        "control": CONTROL,
    }
    assert result.matrix["cells"]["refuses-other-compiler"]["gcc-12"]["status"] == "measured-ok"
    assert result.ok


def test_a_failing_contract_is_measured_broken_with_its_summary():
    records = [
        _record(PARSE, "gcc-12", "failed", summary="call: E   assert 0 == 3"),
        _record(CONTROL, "gcc-12", "passed"),
    ]
    cell = collate(_fresh(), records).matrix["cells"]["parse-hits"]["gcc-12"]
    assert cell["status"] == "measured-broken"
    assert cell["outcome"] == "failed"
    assert cell["failure_summary"] == "call: E   assert 0 == 3"


def test_an_error_is_measured_broken():
    records = [
        _record(PARSE, "gcc-12", "error", summary="setup: E   boom"),
        _record(CONTROL, "gcc-12", "passed"),
    ]
    assert (
        collate(_fresh(), records).matrix["cells"]["parse-hits"]["gcc-12"]["status"]
        == "measured-broken"
    )


def test_rule_1_a_record_naming_no_surface_or_column_is_discarded_with_its_reason():
    records = [
        _record(
            "tests/e2e/cov/test_kgcov_toolchains_e2e.py::TestCoverage::test_gone",
            "gcc-12",
            "passed",
        ),
        _record(PARSE, "gcc-99", "passed"),
        _record(PARSE, "default", "passed"),
    ]
    result = collate(_fresh(), records)
    assert result.kept == 0
    reasons = [d.reason for d in result.discarded]
    assert any("no surface" in r for r in reasons), reasons
    assert any("no column" in r for r in reasons), reasons
    assert result.matrix == _fresh()


def test_a_record_naming_a_real_column_in_the_wrong_venue_is_discarded_with_its_reason():
    # "gcc-12" is a real bed column; claiming it measures the build venue names
    # a column that exists, but not in that venue.
    result = collate(_fresh(), [_record(PARSE, "gcc-12", "passed", venue=BUILD)])
    assert result.kept == 0
    reasons = [d.reason for d in result.discarded]
    assert any("another venue" in r for r in reasons), reasons
    assert result.matrix == _fresh()


def test_a_record_whose_row_and_column_do_not_meet_is_discarded_not_crashed():
    # PARSE is a bed contract; CROSS_PROFILE x BUILD is a real column, self-
    # consistent, but the grid holds no `parse-hits` cell in the build venue
    # -- this must be an attributed discard, not a KeyError on the sparse grid.
    result = collate(
        _fresh(), [_record(PARSE, CROSS_PROFILE, "passed", venue=BUILD, version="13.3.0")]
    )
    assert result.kept == 0
    reasons = [d.reason for d in result.discarded]
    assert any("row and column do not meet" in r for r in reasons), reasons
    assert result.matrix == _fresh()


def test_rule_2_a_skip_moves_nothing_and_is_discarded_with_its_reason():
    result = collate(
        _fresh(), [_record(PARSE, "gcc-12", "skipped"), _record(CONTROL, "gcc-12", "passed")]
    )
    assert result.matrix["cells"]["parse-hits"]["gcc-12"] == {"status": "untested"}
    assert any("skip" in d.reason for d in result.discarded)


def test_rule_3_a_pass_under_a_failed_control_is_measured_broken_naming_the_control():
    records = [
        _record(PARSE, "gcc-12", "passed"),
        _record(CONTROL, "gcc-12", "failed", summary="call: E   loaded"),
    ]
    result = collate(_fresh(), records)
    cell = result.matrix["cells"]["parse-hits"]["gcc-12"]
    assert cell["status"] == "measured-broken"
    assert cell["outcome"] == "passed"
    assert CONTROL in cell["failure_summary"]
    assert "failed" in cell["failure_summary"]
    assert any("control" in line for line in result.refused_reasons)
    control_cell = result.matrix["cells"]["refuses-other-compiler"]["gcc-12"]
    assert control_cell["failure_summary"] == f"call: E   loaded; {CONTROL}: the control failed"


def test_rule_3_a_pass_with_no_control_record_at_all_is_measured_broken():
    result = collate(_fresh(), [_record(PARSE, "gcc-12", "passed")])
    assert result.matrix["cells"]["parse-hits"]["gcc-12"]["status"] == "measured-broken"


def test_rule_3_a_control_from_another_run_does_not_count():
    records = [
        _record(PARSE, "gcc-12", "passed", run="run-2"),
        _record(CONTROL, "gcc-12", "passed", run="run-1"),
    ]
    assert (
        collate(_fresh(), records).matrix["cells"]["parse-hits"]["gcc-12"]["status"]
        == "measured-broken"
    )


def test_rule_3_a_build_cell_needs_no_control():
    cell = collate(
        _fresh(), [_record(CROSS, CROSS_PROFILE, "passed", venue=BUILD, version="13.3.0")]
    ).matrix["cells"]["cross-x86_64-objects"][CROSS_PROFILE]
    assert cell["status"] == "measured-ok"
    assert cell["control"] is None


def test_rule_4_a_cell_the_run_did_not_draw_is_copied_unchanged():
    matrix = _fresh()
    old = {
        "status": "measured-ok",
        "nodeid": PARSE,
        "venue": BED,
        "as_of": "2026-01-01",
        "outcome": "passed",
        "compiler_version": "13.2.0",
        "kernel_release": "6.8.0-80-generic",
        "control": CONTROL,
    }
    matrix["cells"]["parse-hits"]["gcc-13"] = dict(old)
    result = collate(
        matrix, [_record(PARSE, "gcc-12", "passed"), _record(CONTROL, "gcc-12", "passed")]
    )
    assert result.matrix["cells"]["parse-hits"]["gcc-13"] == old


def test_rule_4_a_broken_cell_is_not_reset_by_a_run_that_did_not_draw_it():
    matrix = _fresh()
    matrix["cells"]["parse-hits"]["gcc-13"] = {
        "status": "measured-broken",
        "nodeid": PARSE,
        "venue": BED,
        "as_of": "2026-01-01",
        "outcome": "failed",
        "compiler_version": "13.2.0",
        "kernel_release": "6.8.0-80-generic",
        "control": CONTROL,
        "failure_summary": "x",
    }
    result = collate(matrix, [])
    assert result.matrix == matrix


def test_a_record_without_provenance_is_discarded_with_its_reason():
    record = _record(PARSE, "gcc-12", "error", version=None)
    record["kernel_release"] = None
    result = collate(_fresh(), [record, _record(CONTROL, "gcc-12", "passed")])
    assert result.matrix["cells"]["parse-hits"]["gcc-12"] == {"status": "untested"}
    assert any("provenance" in d.reason for d in result.discarded)


def test_a_record_of_another_format_is_discarded():
    record = _record(PARSE, "gcc-12", "passed")
    record["format"] = 2
    assert collate(_fresh(), [record]).kept == 0


def test_rule_5_the_collator_holds_no_version_control_vocabulary():
    from tests._fixtures.paths import PROJECT_ROOT

    module = PROJECT_ROOT / "scripts" / "collate_kgcov_matrix.py"
    source = module.read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "git" not in body.replace("git-ignored", "").replace("Git", "")


def test_main_reports_without_writing_and_writes_with_the_flag(tmp_path, capsys):
    records = tmp_path / "records"
    records.mkdir()
    for r in (_record(PARSE, "gcc-12", "passed"), _record(CONTROL, "gcc-12", "passed")):
        (records / f"{r['nodeid'].rsplit('::', 1)[1]}.json").write_text(json.dumps(r))
    matrix = tmp_path / "kgcov_matrix.json"
    matrix.write_text(json.dumps(_fresh()))
    assert collate_main(["--records", str(records), "--matrix", str(matrix)]) == 0
    assert json.loads(matrix.read_text())["cells"]["parse-hits"]["gcc-12"] == {"status": "untested"}
    assert "WOULD change" in capsys.readouterr().out
    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 0
    assert (
        json.loads(matrix.read_text())["cells"]["parse-hits"]["gcc-12"]["status"] == "measured-ok"
    )
    assert "artifact written" in capsys.readouterr().out


def test_main_exits_one_when_a_control_refused_a_verdict(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    (records / "a.json").write_text(json.dumps(_record(PARSE, "gcc-12", "passed")))
    matrix = tmp_path / "kgcov_matrix.json"
    matrix.write_text(json.dumps(_fresh()))
    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 1
    assert (
        json.loads(matrix.read_text())["cells"]["parse-hits"]["gcc-12"]["status"]
        == "measured-broken"
    )


def test_main_exits_two_on_an_artifact_that_fails_its_schema_after_collation(tmp_path):
    # gcc-13's cell is already malformed and this run's records never touch
    # it; gcc-12's cell DOES change (a real collation happens, so `--write`
    # has something to write). An implementation that writes before
    # validating would still corrupt the file here -- the byte-for-byte
    # comparison after catches that ordering bug that a records-free run
    # (nothing to write either way) cannot.
    records = tmp_path / "records"
    records.mkdir()
    for r in (_record(PARSE, "gcc-12", "passed"), _record(CONTROL, "gcc-12", "passed")):
        (records / f"{r['nodeid'].rsplit('::', 1)[1]}.json").write_text(json.dumps(r))
    matrix = tmp_path / "kgcov_matrix.json"
    doc = _fresh()
    doc["cells"]["parse-hits"]["gcc-13"] = {"status": "untested", "as_of": "2026-01-01"}
    matrix.write_text(json.dumps(doc))
    before = matrix.read_bytes()
    assert collate_main(["--records", str(records), "--matrix", str(matrix), "--write"]) == 2
    assert matrix.read_bytes() == before
