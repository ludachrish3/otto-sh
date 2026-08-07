"""Pins for the served-bundle filter and its configure-time drift guard.

The drift guard exists because the in-fixture zero-match guard is armed only
under ``make dashboard`` (OTTO_TS_COVERAGE is make-only by design), so CI's
nox-driven dashboard matrix never executed it (review 2026-08-06 §5.4, gate
G13). These tests are the guard's falsifiability proof: the drifted-layout
cases below are the exact regression the guard was built for, observed red
against a synthetic dist rather than trusted on faith.
"""

from pathlib import Path

from tests._fixtures._ts_bundle_filter import bundle_filter_drift_reason, bundle_url_matches


class TestBundleUrlMatches:
    def test_current_dashboard_shape_matches(self):
        assert bundle_url_matches("file:///repo/web/dist/assets/index-B3xk9a2f.js")
        assert bundle_url_matches("http://127.0.0.1:8321/assets/index-B3xk9a2f.js")

    def test_current_covapp_shape_matches(self):
        assert bundle_url_matches("file:///repo/web/dist-covapp/covapp.js")
        assert bundle_url_matches("http://127.0.0.1:8321/covapp.js")

    def test_drifted_layouts_do_not_match(self):
        # A vite outDir/assetsDir change is the regression this predicate exists
        # to catch — both halves of it must read as drift, not silently pass.
        assert not bundle_url_matches("file:///repo/web/dist/js/index-B3xk9a2f.js")
        assert not bundle_url_matches("file:///repo/web/dist-covapp/covapp2.js")
        assert not bundle_url_matches("file:///repo/web/dist/index.html")


class TestBundleFilterDriftReason:
    def _dist(self, tmp_path: Path, *relpaths: str) -> Path:
        dist = tmp_path / "dist"
        for rel in relpaths:
            p = dist / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// bundle")
        return dist

    def test_matching_layout_is_clean(self, tmp_path: Path):
        dist = self._dist(tmp_path, "assets/index-B3xk9a2f.js", "index.html")
        assert bundle_filter_drift_reason(dist, "monitor dashboard") is None

    def test_covapp_layout_is_clean(self, tmp_path: Path):
        dist = self._dist(tmp_path, "covapp.js", "index.html")
        assert bundle_filter_drift_reason(dist, "coverage report") is None

    def test_drifted_layout_is_named(self, tmp_path: Path):
        # Positive control: the guard observed red. A renamed assets dir is the
        # drift scenario from the review; the reason must name the app and the
        # offending bundles so the fix-it path is one read.
        dist = self._dist(tmp_path, "js/index-B3xk9a2f.js")
        reason = bundle_filter_drift_reason(dist, "monitor dashboard")
        assert reason is not None
        assert "monitor dashboard" in reason
        assert "js/index-B3xk9a2f.js" in reason

    def test_empty_dist_defers_to_the_stale_gate(self, tmp_path: Path):
        # A missing/empty dist is already a loud pytest.exit from the existing
        # missing/stale gates; reporting it as filter drift would misdirect.
        dist = tmp_path / "dist"
        dist.mkdir()
        assert bundle_filter_drift_reason(dist, "monitor dashboard") is None

    def test_an_ancestor_named_assets_cannot_forge_a_match(self, tmp_path: Path):
        # Review catch: the predicate is a URL check, and against an ABSOLUTE
        # path any ancestor directory named "assets" satisfied it — a checkout
        # under such a path silently disabled the guard. Matching the
        # dist-relative path pins that closed.
        dist = tmp_path / "assets" / "workspace" / "dist"
        (dist / "js").mkdir(parents=True)
        (dist / "js" / "index-B3xk9a2f.js").write_text("// bundle")
        assert bundle_filter_drift_reason(dist, "monitor dashboard") is not None

    def test_module_flavoured_bundles_are_not_invisible(self, tmp_path: Path):
        # A rename that also switches emission to .mjs must read as drift, not
        # as "no JS here" (which would silently defer to the stale gate).
        dist = self._dist(tmp_path, "js/index-B3xk9a2f.mjs")
        assert bundle_filter_drift_reason(dist, "monitor dashboard") is not None
