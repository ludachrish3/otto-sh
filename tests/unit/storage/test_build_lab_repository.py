"""Unit tests for the host-source backend factory (build_lab_repository)."""

import pytest

from otto.storage import (
    JsonFileLabRepository,
    LabRepositoryError,
    build_lab_repository,
    register_lab_repository,
)
from otto.storage.registry import LAB_REPOSITORIES


class TestJsonDefault:
    def test_missing_backend_defaults_to_json(self, tmp_path):
        repo = build_lab_repository({}, tmp_path, search_paths=[tmp_path])
        assert isinstance(repo, JsonFileLabRepository)
        assert repo.search_paths == [tmp_path]

    def test_explicit_json_receives_search_paths(self, tmp_path):
        p1 = tmp_path / "a"
        p2 = tmp_path / "b"
        repo = build_lab_repository({"backend": "json"}, tmp_path, search_paths=[p1, p2])
        assert isinstance(repo, JsonFileLabRepository)
        assert repo.search_paths == [p1, p2]

    def test_json_without_search_paths_is_empty(self, tmp_path):
        repo = build_lab_repository({"backend": "json"}, tmp_path)
        assert isinstance(repo, JsonFileLabRepository)
        assert repo.search_paths == []


class TestCustomBackend:
    def test_registered_backend_receives_repo_dir_and_kwargs(self, tmp_path):
        class FakeRepo:
            def __init__(self, repo_dir, url=None):
                self.repo_dir = repo_dir
                self.url = url

            def load_lab(self, name, preferences=None):
                raise NotImplementedError

            def list_labs(self):
                return []

        register_lab_repository("fake-build-test", FakeRepo)
        try:
            repo = build_lab_repository(
                {"backend": "fake-build-test", "fake-build-test": {"url": "https://x"}},
                tmp_path,
                search_paths=[tmp_path],
            )
            assert isinstance(repo, FakeRepo)
            assert repo.repo_dir == tmp_path
            assert repo.url == "https://x"
        finally:
            LAB_REPOSITORIES.unregister("fake-build-test")

    def test_registered_backend_without_kwargs(self, tmp_path):
        class BareRepo:
            def __init__(self, repo_dir):
                self.repo_dir = repo_dir

            def load_lab(self, name, preferences=None):
                raise NotImplementedError

            def list_labs(self):
                return []

        register_lab_repository("bare-build-test", BareRepo)
        try:
            repo = build_lab_repository({"backend": "bare-build-test"}, tmp_path)
            assert isinstance(repo, BareRepo)
            assert repo.repo_dir == tmp_path
        finally:
            LAB_REPOSITORIES.unregister("bare-build-test")


class TestBuiltinBypassFix:
    def test_reregistering_json_takes_effect(self, tmp_path):
        """build_lab_repository resolves "json" through the registry, not a
        hardcoded JsonFileLabRepository construction — re-registering "json"
        (overwrite=True) must be honored, same constructor contract (search_paths=).
        """

        class ReplacementJsonRepo:
            def __init__(self, search_paths=None):
                self.search_paths = list(search_paths or [])

            def load_lab(self, name, preferences=None):
                raise NotImplementedError

            def list_labs(self):
                return []

        register_lab_repository("json", ReplacementJsonRepo, overwrite=True)
        try:
            repo = build_lab_repository({}, tmp_path, search_paths=[tmp_path])
            assert isinstance(repo, ReplacementJsonRepo)
            assert repo.search_paths == [tmp_path]
        finally:
            register_lab_repository("json", JsonFileLabRepository, overwrite=True)


class TestErrors:
    def test_unknown_backend_raises_lab_repository_error(self, tmp_path):
        with pytest.raises(LabRepositoryError, match="Unknown lab repository backend"):
            build_lab_repository({"backend": "does-not-exist"}, tmp_path)

    def test_malformed_envelope_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match=r"Invalid \[lab\] settings"):
            build_lab_repository({"backend": 3}, tmp_path)
