"""``Repo.creds_settings``: the raw ``[creds]`` table, a plain dict (spec 2026-09-06 §4)."""

from otto.config.repo import Repo
from tests._fixtures.sutrepo import make_sut_repo


def test_creds_settings_is_the_raw_table_or_empty(tmp_path):
    root = make_sut_repo(
        tmp_path / "r", extra='[creds]\nbackend = "json"\npath = "lab_data/creds.json"\n'
    )
    assert Repo(sut_dir=root).creds_settings == {"backend": "json", "path": "lab_data/creds.json"}
    bare = make_sut_repo(tmp_path / "bare")
    assert Repo(sut_dir=bare).creds_settings == {}
