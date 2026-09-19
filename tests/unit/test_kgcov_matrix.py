"""The kgcov compatibility matrix: the artifact agrees with the tree and with its schema."""

import datetime
import json
import re

import pytest
from jsonschema import Draft202012Validator

from scripts.render_kgcov_matrix import main as render_main
from scripts.render_kgcov_matrix import render
from tests._fixtures.kgcov_matrix import (
    BED,
    BUILD,
    CONTROL_SURFACE,
    CROSS_PROFILE,
    FORMAT,
    KGCOV_TESTS,
    SCHEMA_PATH,
    SURFACES,
    axes_mismatch,
    bed_profile_ids,
    build_matrix,
    discover_contracts,
    load_matrix,
    profiles,
    validation_errors,
    venue_of_contract,
)
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic


@pytest.fixture(scope="module")
def committed() -> dict:
    return load_matrix()


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_the_committed_matrix_validates_against_its_schema(committed):
    assert validation_errors(committed) == []
    assert committed["format"] == FORMAT


def test_the_row_axis_equals_the_discovered_contracts_both_ways(committed):
    discovered = discover_contracts()
    assert [s["contract"] for s in committed["surfaces"]] == [s.contract for s in SURFACES]
    assert set(discovered) == {s.contract for s in SURFACES}
    assert len(discovered) == len(SURFACES)


def test_exactly_one_bed_surface_is_the_control(committed):
    controls = [s for s in committed["surfaces"] if s["control"]]
    assert [s["id"] for s in controls] == [CONTROL_SURFACE.id]
    assert controls[0]["venue"] == BED


def test_the_column_axis_is_the_makefile_default_plus_the_cross_build(committed):
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"^KGCOV_TOOLCHAINS \?= (.+)$", makefile, re.MULTILINE)
    assert m, "the Makefile no longer declares KGCOV_TOOLCHAINS ?= …"
    expected = [*m.group(1).split(","), CROSS_PROFILE]
    assert [p["id"] for p in committed["profiles"]] == expected
    assert bed_profile_ids() == m.group(1).split(",")
    assert [p.id for p in profiles()] == expected
    venues = {p["id"]: p["venue"] for p in committed["profiles"]}
    assert venues[CROSS_PROFILE] == BUILD
    assert all(venues[p] == BED for p in bed_profile_ids())


def test_the_grid_pairs_every_row_with_every_column_of_its_venue_and_nothing_else(committed):
    venue_of_surface = {s["id"]: s["venue"] for s in committed["surfaces"]}
    venue_of_profile = {p["id"]: p["venue"] for p in committed["profiles"]}
    expected = {
        (s, p)
        for s, sv in venue_of_surface.items()
        for p, pv in venue_of_profile.items()
        if sv == pv
    }
    actual = {(s, p) for s, row in committed["cells"].items() for p in row}
    assert actual == expected


def test_the_committed_artifact_is_what_the_tree_would_generate(committed):
    assert build_matrix(committed) == committed
    assert axes_mismatch(committed) == []


def test_a_renamed_contract_is_a_row_mismatch(committed):
    stale = json.loads(json.dumps(committed))
    renamed = stale["surfaces"][0]["contract"] + "_renamed"
    stale["surfaces"][0]["contract"] = renamed
    problems = axes_mismatch(stale)
    assert problems
    assert any(renamed in p for p in problems), problems


def test_rebuilding_the_axes_keeps_every_verdict_and_mints_none(committed):
    """``build_matrix`` copies an existing verdict across and never writes one itself."""
    injected = json.loads(json.dumps(committed))
    cell = dict(_OK)
    injected["cells"]["parse-hits"][bed_profile_ids()[0]] = cell
    rebuilt = build_matrix(injected)
    assert rebuilt["cells"]["parse-hits"][bed_profile_ids()[0]] == cell

    fresh = build_matrix(None)
    statuses = {cell["status"] for row in fresh["cells"].values() for cell in row.values()}
    assert statuses == {"untested"}


def test_the_hand_written_row_venue_agrees_with_the_fixture_it_takes():
    assert all(venue_of_contract(s.contract) == s.venue for s in SURFACES)
    offenders = [
        (s.id, s.venue, venue_of_contract(s.contract))
        for s in SURFACES
        if venue_of_contract(s.contract) != s.venue
    ]
    assert offenders == []


def test_no_kgcov_contract_declares_its_fixture_via_usefixtures():
    """Discovery reads signatures; ``@pytest.mark.usefixtures`` would silently drop a row."""
    offenders = [path for path in KGCOV_TESTS if "usefixtures" in path.read_text(encoding="utf-8")]
    assert offenders == []


def test_every_measured_cell_carries_its_provenance(committed):
    for surface, row in committed["cells"].items():
        for profile, cell in row.items():
            if cell["status"] == "untested":
                assert cell == {"status": "untested"}, (surface, profile)
                continue
            for key in (
                "nodeid",
                "venue",
                "as_of",
                "outcome",
                "compiler_version",
                "kernel_release",
            ):
                assert key in cell, (surface, profile, key)
            assert "control" in cell, (surface, profile)
            if cell["venue"] == BED and cell["status"] == "measured-ok":
                assert cell["control"] == CONTROL_SURFACE.contract, (surface, profile)
            if cell["venue"] == BUILD:
                assert cell["control"] is None, (surface, profile)


def _cell(validator, committed, cell: dict) -> "list[str]":
    """Validation messages raised at or below one injected bed cell."""
    doc = json.loads(json.dumps(committed))
    surface, profile = "parse-hits", bed_profile_ids()[0]
    doc["cells"][surface][profile] = cell
    under = ("cells", surface, profile)
    return [
        e.message
        for e in validator.iter_errors(doc)
        if tuple(e.absolute_path)[: len(under)] == under
    ]


_OK = {
    "status": "measured-ok",
    "nodeid": "tests/e2e/cov/test_kgcov_toolchains_e2e.py::TestCoverage::test_parse_hits",
    "venue": "bed",
    "as_of": "2026-09-19",
    "outcome": "passed",
    "compiler_version": "12.3.0",
    "kernel_release": "6.8.0-86-generic",
    "control": (
        "tests/e2e/cov/test_kgcov_toolchains_e2e.py::TestBuild"
        "::test_a_demo_from_another_compiler_is_refused_at_load"
    ),
}


def test_a_fully_evidenced_measured_ok_cell_is_accepted(validator, committed):
    assert _cell(validator, committed, _OK) == []


def test_a_fully_evidenced_measured_broken_cell_is_accepted(validator, committed):
    broken = {
        **_OK,
        "status": "measured-broken",
        "outcome": "failed",
        "failure_summary": "call: boom",
    }
    assert _cell(validator, committed, broken) == []


@pytest.mark.parametrize(
    "missing", ["nodeid", "as_of", "outcome", "compiler_version", "kernel_release", "control"]
)
def test_measured_ok_missing_a_provenance_field_is_rejected(validator, committed, missing):
    cell = {k: v for k, v in _OK.items() if k != missing}
    messages = _cell(validator, committed, cell)
    assert any(missing in m for m in messages), messages


def test_measured_ok_whose_outcome_is_not_passed_is_rejected(validator, committed):
    messages = _cell(validator, committed, {**_OK, "outcome": "failed"})
    assert any("'passed' was expected" in m for m in messages), messages


def test_measured_ok_carrying_a_failure_summary_is_rejected(validator, committed):
    messages = _cell(validator, committed, {**_OK, "failure_summary": "call: boom"})
    assert any("failure_summary" in m for m in messages), messages


def test_measured_broken_without_a_failure_summary_is_rejected(validator, committed):
    broken = {**_OK, "status": "measured-broken", "outcome": "failed"}
    messages = _cell(validator, committed, broken)
    assert any("failure_summary" in m for m in messages), messages


def test_measured_broken_may_carry_a_passed_outcome_when_the_control_failed(validator, committed):
    # The collator writes a contract that passed under a failed control as
    # measured-broken with outcome "passed"; the schema must admit that.
    broken = {**_OK, "status": "measured-broken", "failure_summary": "control failed"}
    assert _cell(validator, committed, broken) == []


def test_untested_carrying_stale_evidence_is_rejected(validator, committed):
    messages = _cell(validator, committed, {"status": "untested", "as_of": "2026-09-19"})
    assert any("as_of" in m for m in messages), messages


def test_an_unknown_status_is_rejected(validator, committed):
    messages = _cell(validator, committed, {**_OK, "status": "not-observable"})
    assert any("not-observable" in m for m in messages), messages


def test_a_non_iso_date_is_rejected(validator, committed):
    messages = _cell(validator, committed, {**_OK, "as_of": "19/09/2026"})
    assert any("19/09/2026" in m for m in messages), messages


def test_a_null_control_on_a_bed_cell_is_a_schema_matter_for_the_collator_not_the_schema(
    validator, committed
):
    # The schema admits null (the build column needs it); the cross-reference
    # "a bed measured-ok names the control" is the guard above, not the schema.
    assert _cell(validator, committed, {**_OK, "control": None}) == []


def test_render_check_agrees_with_the_committed_artifact(capsys):
    assert render_main(["--check"]) == 0
    assert "axes" not in capsys.readouterr().err


def test_render_check_refuses_a_stale_axis(tmp_path, capsys):
    stale = load_matrix()
    stale["profiles"].append({"id": "gcc-99", "title": "gcc-99", "venue": BED})
    path = tmp_path / "m.json"
    path.write_text(json.dumps(stale))
    assert render_main(["--check", "--matrix", str(path)]) == 1
    assert "gcc-99" in capsys.readouterr().err


def test_the_page_names_every_row_and_column_and_the_three_states(committed):
    page = render(committed, rendered_on=datetime.date(2026, 9, 19))
    for s in committed["surfaces"]:
        assert s["title"] in page, s["id"]
    for p in committed["profiles"]:
        assert f"`{p['id']}`" in page, p["id"]
    for word in ("measured-ok", "measured-broken", "untested"):
        assert word in page
    assert "kgcov-matrix" in page.splitlines()[0]  # the MyST label
    assert "vendored table" in page


def test_a_measured_cell_renders_its_symbol_and_provenance(committed):
    doc = json.loads(json.dumps(committed))
    doc["cells"]["parse-hits"]["gcc-12"] = dict(_OK)
    doc["cells"]["refuses-other-compiler"]["gcc-12"] = {**_OK, "nodeid": CONTROL_SURFACE.contract}
    # A render date the cells do NOT carry: `_OK["as_of"]` is 2026-09-19, so a page
    # rendered that same day could not tell the two dates apart.
    page = render(doc, rendered_on=datetime.date(2026, 9, 20))
    assert "✅" in page
    assert "12.3.0" in page
    assert "6.8.0-86-generic" in page
    assert "2026-09-19" in page
    assert "2026-09-20" in page


def test_a_broken_cell_renders_its_symbol_and_summary(committed):
    doc = json.loads(json.dumps(committed))
    doc["cells"]["parse-hits"]["gcc-12"] = {
        **_OK,
        "status": "measured-broken",
        "outcome": "failed",
        "failure_summary": "call: E   assert 0 == 3",
    }
    page = render(doc, rendered_on=datetime.date(2026, 9, 19))
    assert "❌" in page
    assert "assert 0 == 3" in page


def test_main_writes_the_page_it_was_pointed_at(tmp_path):
    # BEHAVIOURAL, and the reason this file's renderer is exempt from the verdict-literal
    # scan in tests/unit/test_support_matrix.py: run the real `main` and require the
    # artifact it read to come back byte-identical. Only a run writes a verdict.
    matrix = tmp_path / "m.json"
    matrix.write_text(json.dumps(load_matrix(), indent=2) + "\n", encoding="utf-8")
    before = matrix.read_bytes()
    out = tmp_path / "kgcov-matrix.md"
    assert render_main(["--matrix", str(matrix), "--output", str(out)]) == 0
    assert matrix.read_bytes() == before, "the RENDERER wrote to the artifact"
    assert out.read_text(encoding="utf-8").startswith("(kgcov-matrix)=")


def test_the_kgcov_lane_folds_and_the_release_stage_gates():
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    # No re.DOTALL: `.*?` must not cross a newline, or a moved/emptied tab
    # block would let the match run on past it and capture a DIFFERENT
    # target's recipe body instead of failing loudly.
    kgcov = re.search(r"^kgcov:.*?\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert kgcov and "kgcov-matrix" in kgcov.group(1)  # noqa: PT018
    assert "if [ $$lane -ne 0 ]; then exit $$lane; fi; exit $$collate" in kgcov.group(1)
    stage = re.search(r"^release-kgcov-matrix:.*?\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert stage
    body = stage.group(1)
    assert "HEAD:schemas/kgcov_matrix.json" in body
    assert (  # noqa: PT018
        "scripts/check_matrix_downgrades.py --baseline" in body
        and "--candidate schemas/kgcov_matrix.json" in body
    )
    assert "chore(matrix): re-measure the kgcov matrix" in body
    assert "core.hooksPath=/dev/null" in body
    fold = re.search(r"^kgcov-matrix:.*?\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert fold and "scripts.collate_kgcov_matrix --write" in fold.group(1)  # noqa: PT018


def test_sphinx_srcs_names_the_kgcov_matrix_and_its_renderer():
    """`make docs` must re-trigger on a re-collated kgcov matrix, like any page edit.

    Mirrors the support matrix's own entries in `SPHINX_SRCS` — see the comment
    above them in the Makefile. Without both entries, `docs/_build/html/index.html`
    stays newer than a freshly re-measured artifact and `make docs` no-ops.
    """
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    srcs = re.search(r"^SPHINX_SRCS :=.*?\n((?:.*\\\n)*.*)", makefile, re.MULTILINE)
    assert srcs, "no SPHINX_SRCS assignment found in the Makefile (guard misparse?)"
    body = srcs.group(0)
    assert "schemas/kgcov_matrix.json" in body, (
        "SPHINX_SRCS does not list schemas/kgcov_matrix.json"
    )
    assert "scripts/render_kgcov_matrix.py" in body, (
        "SPHINX_SRCS does not list scripts/render_kgcov_matrix.py"
    )


def test_the_release_refreshes_the_kgcov_matrix_before_it_builds_the_docs():
    """The kgcov twin of test_support_matrix.py's same-named guard.

    `SPHINX_SRCS` lists `schemas/kgcov_matrix.json`, so `make docs` renders the
    kgcov-matrix page FROM that file. A release that re-measured AFTER building
    its docs would publish a page disagreeing with the artifact it commits in
    the same release.
    """
    body = (
        (PROJECT_ROOT / "Makefile")
        .read_text(encoding="utf-8")
        .split("\nrelease:", 1)[1]
        .split("\n\n", 1)[0]
    )
    stages = re.findall(r"\$\(MAKE\) ([a-z-]+)", body)
    assert "release-kgcov-matrix" in stages, (
        f"the release no longer refreshes the kgcov matrix; stages: {stages}"
    )
    assert stages.index("release-kgcov-matrix") < stages.index("docs"), (
        f"the release must refresh the kgcov matrix BEFORE building the docs that render it; "
        f"stages: {stages}"
    )


_CAVEAT = "stand on a control that did not pass"


def test_a_column_whose_control_did_not_pass_is_caveated_by_name(committed):
    """A hand run can leave measured-ok cells beside a control the same run broke.

    A run's verdicts bind only the cells it drew; the rest are copied forward.
    The page has to say so, or the column reads as evidence it no longer is.
    """
    doc = json.loads(json.dumps(committed))
    doc["cells"]["refuses-other-compiler"]["gcc-12"] = {
        **_OK,
        "nodeid": CONTROL_SURFACE.contract,
        "status": "measured-broken",
        "outcome": "failed",
        "failure_summary": "call: the demo loaded",
    }
    caveats = [
        line
        for line in render(doc, rendered_on=datetime.date(2026, 9, 19)).splitlines()
        if _CAVEAT in line
    ]
    assert len(caveats) == 1, caveats
    assert "`gcc-12`" in caveats[0], caveats[0]
    assert "make kgcov" in caveats[0], caveats[0]
    assert not any(f"`{other}`" in caveats[0] for other in ("gcc-9", "gcc-13", "clang")), caveats[0]


def test_a_column_whose_control_passed_is_not_caveated(committed):
    page = render(committed, rendered_on=datetime.date(2026, 9, 19))
    assert _CAVEAT not in page
