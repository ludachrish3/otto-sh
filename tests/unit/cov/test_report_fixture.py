"""The fixture report is consumed by the browser suite AND the docs-media
screenshot — it must render hermetically and deterministically.

Since Task 8's reporter swap the fixture renders the SPA (SpaRenderer), so
these pins read the emitted ``cov_data/`` JSON chunks directly rather than
Jinja-era HTML strings — the browser suite (``tests/e2e/cov/report_browser``)
is what pins the resulting DOM.
"""

import json

from tests._fixtures._report_fixture import build_fixture_report


def _index_payload(report_dir):
    text = (report_dir / "cov_data" / "index.js").read_text()
    return json.loads(text[len("window.__OTTO_COV__ = ") : -2])


def _file_chunk(report_dir, chunk_name):
    text = (report_dir / "cov_data" / "files" / f"{chunk_name}.js").read_text()
    return json.loads(text[len("window.__OTTO_COV_FILE__(") : -3])


def _find_file(node, name):
    for f in node["files"]:
        if f["name"] == name:
            return f
    for d in node["dirs"]:
        found = _find_file(d, name)
        if found is not None:
            return found
    return None


def test_fixture_report_renders(tmp_path):
    report_dir = build_fixture_report(tmp_path)
    assert (report_dir / "index.html").exists()
    assert (report_dir / "dist" / "covapp.js").exists()

    payload = _index_payload(report_dir)
    assert payload["project_name"] == "otto example product"
    assert payload["tier_order"] == ["system", "unit", "manual"]

    file_pages = list((report_dir / "cov_data" / "files").glob("*.js"))
    assert len(file_pages) == 2


def test_fixture_report_has_branch_pills(tmp_path):
    report_dir = build_fixture_report(tmp_path)
    payload = _index_payload(report_dir)
    main_node = _find_file(payload["tree"], "main.c")
    chunk = _file_chunk(report_dir, main_node["chunk"])

    # main.c line 4 (`if (a > 0 && b > 0)`) carries all three branch pill
    # states: taken, not-taken, unreachable.
    branches = chunk["lines"]["4"]["branches"]
    assert len(branches) == 3
    taken, not_taken, unreachable = branches
    assert taken["hits"] == {"system": 4, "unit": 8}
    assert taken["reachable"] == {"system": True, "unit": True}
    assert not_taken["hits"] == {}
    assert not_taken["reachable"] == {"system": True, "unit": True}
    assert unreachable["hits"] == {}
    assert unreachable["reachable"] == {"system": False, "unit": False}


def test_display_paths_are_short_and_deterministic(tmp_path):
    """The builder renders with prefix=base_dir — the screenshot and the
    browser pins both rely on the exact strings product/main.c|utils.c."""
    report_dir = build_fixture_report(tmp_path)
    payload = _index_payload(report_dir)
    main_node = _find_file(payload["tree"], "main.c")
    utils_node = _find_file(payload["tree"], "utils.c")
    assert main_node["path"] == "product/main.c"
    assert utils_node["path"] == "product/utils.c"

    index_text = (report_dir / "cov_data" / "index.js").read_text()
    assert str(tmp_path) not in index_text

    main_chunk = _file_chunk(report_dir, main_node["chunk"])
    assert main_chunk["path"] == "product/main.c"
    assert str(tmp_path) not in json.dumps(main_chunk)


class TestRunTable:
    """spec §10: two multi-host system runs, one unit run, and two manual
    runs (one fully revoked, one aging + dirty-remapped)."""

    def _runs(self, tmp_path):
        report_dir = build_fixture_report(tmp_path)
        payload = _index_payload(report_dir)
        return payload["runs"]

    def test_nightly_full_is_two_hosts_same_label(self, tmp_path):
        runs = self._runs(tmp_path)
        nightly = [r for r in runs if r["label"] == "nightly-full"]
        assert len(nightly) == 2
        assert {r["host"] for r in nightly} == {"router-a", "router-b"}
        assert all(r["tier"] == "system" for r in nightly)

    def test_unit_harvest_run(self, tmp_path):
        runs = self._runs(tmp_path)
        (unit_run,) = [r for r in runs if r["label"] == "unit harvest"]
        assert unit_run["tier"] == "unit"
        assert unit_run["host"] == "ci-01"

    def test_smoke_old_run_is_fully_revoked(self, tmp_path):
        report_dir = build_fixture_report(tmp_path)
        payload = _index_payload(report_dir)
        (smoke_run,) = [r for r in payload["runs"] if r["label"] == "smoke-old"]
        contrib = payload["run_contrib"][str(smoke_run["id"])]
        assert contrib == {"lines": 0, "revoked": 1, "files": []}

    def test_field_bring_up_run_is_aging_and_dirty_remapped(self, tmp_path):
        runs = self._runs(tmp_path)
        (field_run,) = [r for r in runs if r["label"] == "field bring-up"]
        assert field_run["tier"] == "manual"
        assert field_run["aging"] is True
        assert field_run["dirty_remap"] is True
        assert field_run["ticket"] == "FW-1188"
        assert field_run["tester"] == {"name": "M. Reyes"}
        # every run needs plausible board/host/labs/captured_at/base_commit
        for run in runs:
            assert run["board"]
            assert run["host"]
            assert run["labs"]
            assert run["captured_at"]
            assert run["base_commit"]


class TestRowStates:
    """Every row-precedence state (Global Constraints) is reachable from
    this one fixture, on main.c's file chunk."""

    def _main_chunk(self, tmp_path):
        report_dir = build_fixture_report(tmp_path)
        payload = _index_payload(report_dir)
        main_node = _find_file(payload["tree"], "main.c")
        return payload, _file_chunk(report_dir, main_node["chunk"])

    def test_covered_line_has_a_tier_hit(self, tmp_path):
        _, chunk = self._main_chunk(tmp_path)
        assert chunk["lines"]["4"]["hits"]["system"] == 4

    def test_stale_line_is_fully_revoked_no_hits(self, tmp_path):
        payload, chunk = self._main_chunk(tmp_path)
        line = chunk["lines"]["6"]
        assert line["state"] == "stale"
        assert line["hits"] == {}
        (smoke_run,) = [r for r in payload["runs"] if r["label"] == "smoke-old"]
        assert line["stale_run"] == [smoke_run["id"]]

    def test_aging_line_has_run_credit_but_no_tier_hit(self, tmp_path):
        """Row precedence puts a tier hit ahead of aging/stale — the aging
        line must have NO per-tier hits (else it would render as its
        tier's color, not aging), only a `run` credit."""
        payload, chunk = self._main_chunk(tmp_path)
        line = chunk["lines"]["9"]
        assert line["state"] == "aging"
        assert line["hits"] == {}
        (field_run,) = [r for r in payload["runs"] if r["label"] == "field bring-up"]
        assert line["run"] == {str(field_run["id"]): 3}

    def test_utils_c_has_one_excluded_line(self, tmp_path):
        report_dir = build_fixture_report(tmp_path)
        payload = _index_payload(report_dir)
        utils_node = _find_file(payload["tree"], "utils.c")
        chunk = _file_chunk(report_dir, utils_node["chunk"])
        assert chunk["excluded"] == [6]
        assert utils_node["stats"]["flags"]["excluded"] == 1


def test_line_pct_counts_pin_utils_c_below_main_c(tmp_path):
    """main.c now carries a stale + an aging line too (9 lines total, 7
    hit) — still higher than utils.c's 50%, preserving the numeric-sort
    ordering the browser suite pins (utils.c sorts first ascending)."""
    report_dir = build_fixture_report(tmp_path)
    payload = _index_payload(report_dir)
    main_node = _find_file(payload["tree"], "main.c")
    utils_node = _find_file(payload["tree"], "utils.c")
    assert main_node["stats"]["lines"] == {
        "total": 9,
        "hit": 7,
        "per_tier": {"system": 6, "unit": 4, "manual": 0},
    }
    assert utils_node["stats"]["lines"] == {
        "total": 2,
        "hit": 1,
        "per_tier": {"system": 0, "unit": 1, "manual": 0},
    }
