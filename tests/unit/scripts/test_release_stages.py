"""Pins for scripts/release_stages.py — the one map of which CI job proves which release leg."""

import json

import scripts.local_gate_status as lgs
from scripts import release_stages as rs
from tests._fixtures.paths import CHECKOUT_ROOT


def test_every_ci_job_the_map_names_is_a_required_context() -> None:
    required = json.loads((CHECKOUT_ROOT / lgs.REQUIRED_CONTEXTS_FILE).read_text())
    named = {n for names in rs.EVIDENCE.values() for n in names if n != rs.STAMP}
    assert named - set(required) == set()


def test_skip_lines_name_the_leg_the_evidence_and_the_run() -> None:
    lines = rs.skip_lines("https://r/1")
    assert len(lines) == len(rs.EVIDENCE)
    check_python = "skipped Makefile:check-python: proved by lint-python, typecheck-python"
    assert f"{check_python} in https://r/1" in lines
    assert "skipped Makefile:coverage-ts: proved by the coverage stamp" in lines


def test_the_quality_gates_page_carries_the_rendered_table() -> None:
    page = (CHECKOUT_ROOT / "docs/architecture/quality-gates.md").read_text()
    assert rs.render_table() in page
