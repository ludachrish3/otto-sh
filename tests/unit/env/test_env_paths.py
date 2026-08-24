"""Where the env lives, and where its metadata lives with it."""

import json

from otto.config.home import workspace_home
from otto.env import EnvMeta, env_path, meta_path, read_meta, write_meta


class TestEnvPath:
    def test_it_is_env_under_the_workspace_home(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        assert env_path([repo]) == workspace_home([repo]) / "env"

    def test_it_never_creates_anything(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("OTTO_SUT_DIRS", raising=False)
        env_path()
        assert not (tmp_path / "home").exists()


class TestMeta:
    def test_metadata_lives_inside_the_env_so_force_takes_it(self, tmp_path):
        """F6: recorded beside env/ would survive --force and pin the old backend."""
        env = tmp_path / "env"
        assert meta_path(env).parent == env

    def test_round_trip(self, tmp_path):
        env = tmp_path / "env"
        env.mkdir()
        write_meta(env, EnvMeta(backend="uv", otto_version="9.9.9"))
        got = read_meta(env)
        assert got == EnvMeta(backend="uv", otto_version="9.9.9")

    def test_absent_metadata_reads_as_none_not_an_error(self, tmp_path):
        env = tmp_path / "env"
        env.mkdir()
        assert read_meta(env) is None

    def test_corrupt_metadata_reads_as_none_rather_than_crashing(self, tmp_path):
        """A half-written env must be recoverable by ``create --force``, and
        ``--force`` cannot run if merely READING the env raises.
        """
        env = tmp_path / "env"
        env.mkdir()
        meta_path(env).write_text("{not json")
        assert read_meta(env) is None

    def test_metadata_missing_a_key_reads_as_none(self, tmp_path):
        env = tmp_path / "env"
        env.mkdir()
        meta_path(env).write_text(json.dumps({"backend": "uv"}))
        assert read_meta(env) is None
