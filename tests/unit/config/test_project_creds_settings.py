"""``Project.creds_settings``: the raw ``[creds]`` table, a plain dict (spec 2026-09-06 §4)."""

from otto.config.project import Project
from tests._fixtures.project_scaffold import make_project


def test_creds_settings_is_the_raw_table_or_empty(tmp_path):
    root = make_project(
        tmp_path / "r", extra='[creds]\nbackend = "json"\npath = "lab_data/creds.json"\n'
    )
    assert Project(root=root).creds_settings == {"backend": "json", "path": "lab_data/creds.json"}
    bare = make_project(tmp_path / "bare")
    assert Project(root=bare).creds_settings == {}
