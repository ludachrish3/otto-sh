"""Project panel dependency-status subtitle (dependencies_summary)."""

from otto.config.dependencies import resolve_dependencies
from otto.config.project import Project
from tests._fixtures.project_scaffold import make_project


def _project(tmp_path, name, version="1.0.0", *, required=(), optional=()):
    req = ", ".join(f'"{e}"' for e in required)
    opt = ", ".join(f'"{e}"' for e in optional)
    root = make_project(
        tmp_path / name,
        name=name,
        version=version,
        extra=f"[dependencies]\nrequired = [{req}]\noptional = [{opt}]\n",
    )
    return Project(root=root)


def test_no_dependencies_no_summary_and_no_subtitle(tmp_path):
    project = _project(tmp_path, "solo")
    resolve_dependencies([project])
    assert project.dependencies_summary() is None
    assert project.get_lab_panel().subtitle is None


def test_summary_shows_each_status_shape(tmp_path):
    a = _project(
        tmp_path,
        "a",
        required=["b >= 1", "ghost"],
        optional=["m >= 2", "absentee"],
    )
    b = _project(tmp_path, "b", version="1.2.3")
    m = _project(tmp_path, "m", version="1.0.0")
    resolve_dependencies([a, b, m])
    plain = a.dependencies_summary().plain
    assert "✓ b 1.2.3" in plain
    assert "✗ ghost (missing)" in plain
    assert "⚠ m (found 1.0.0)" in plain
    assert "○ absentee (absent)" in plain


def test_subtitle_attached_to_lab_panel(tmp_path):
    a = _project(tmp_path, "a", required=["b"])
    b = _project(tmp_path, "b")
    resolve_dependencies([a, b])
    subtitle = a.get_lab_panel().subtitle
    assert subtitle is not None
    assert "✓ b 1.0.0" in subtitle.plain
