"""The renderer's static assets now include a built JS bundle (make web).

A dist-less checkout must still render a usable (static) report, but say
loudly that interactivity is missing — silence here would look like a bug
in the report, not a build-step omission.
"""

from pathlib import Path

from otto.coverage.renderer import html_renderer
from otto.coverage.renderer.html_renderer import HtmlRenderer
from otto.coverage.store.model import CoverageStore


def _render_empty(tmp_path: Path) -> Path:
    out = tmp_path / "report"
    HtmlRenderer(out).render(CoverageStore(tier_order=["system"]))
    return out


def test_index_references_built_bundle(tmp_path):
    """The template must load the vite-built bundle, not the deleted report.js."""
    out = _render_empty(tmp_path)
    index = (out / "index.html").read_text()
    assert "static/dist/covreport.js" in index
    assert "static/report.js" not in index


def test_missing_dist_warns_and_still_renders(tmp_path, monkeypatch, caplog):
    """No dist (checkout without `make web`): render succeeds, warning names the fix."""
    bare_static = tmp_path / "static_src"
    bare_static.mkdir()
    (bare_static / "report.css").write_text("body {}")
    monkeypatch.setattr(html_renderer, "STATIC_DIR", bare_static)

    with caplog.at_level("WARNING"):
        out = _render_empty(tmp_path)

    assert (out / "index.html").exists()
    assert any("make web" in r.message for r in caplog.records)


def test_present_dist_copies_bundle_and_does_not_warn(tmp_path, monkeypatch, caplog):
    fake_static = tmp_path / "static_src"
    (fake_static / "dist").mkdir(parents=True)
    (fake_static / "report.css").write_text("body {}")
    (fake_static / "dist" / "covreport.js").write_text("// bundle")
    monkeypatch.setattr(html_renderer, "STATIC_DIR", fake_static)

    with caplog.at_level("WARNING"):
        out = _render_empty(tmp_path)

    assert (out / "static" / "dist" / "covreport.js").read_text() == "// bundle"
    assert not [r for r in caplog.records if "make web" in r.message]
