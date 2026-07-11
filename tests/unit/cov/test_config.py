"""``[coverage]`` config resolution from the repo list.

Moved verbatim from ``otto.cli.test`` (library-extraction Task 14): these are
pure functions over the parsed ``.otto/settings.toml`` dict, with no CLI
dependency, so they belong in ``otto.coverage`` alongside the rest of the
coverage library.
"""

from unittest.mock import MagicMock

import pytest

from otto.coverage.config import (
    get_cov_config,
    get_cov_repo,
    has_cov_config,
    prepare_empty_dir,
)


class TestHasCovConfig:
    """Truth table over the keys that count as "coverage is configured"."""

    def test_empty_dict_is_false(self) -> None:
        assert has_cov_config({}) is False

    def test_unrelated_keys_are_false(self) -> None:
        assert has_cov_config({"something_else": True}) is False

    def test_gcda_remote_dir_is_true(self) -> None:
        assert has_cov_config({"gcda_remote_dir": "/remote"}) is True

    def test_embedded_is_true(self) -> None:
        assert has_cov_config({"embedded": {"extension": "cov_ext"}}) is True

    def test_tiers_is_true(self) -> None:
        assert has_cov_config({"tiers": {"unit": {"kind": "unit"}}}) is True

    def test_hosts_is_true(self) -> None:
        assert has_cov_config({"hosts": "sprout_cov"}) is True

    def test_falsy_values_are_false(self) -> None:
        """An empty/falsy value under a known key still counts as unconfigured."""
        assert (
            has_cov_config({"gcda_remote_dir": "", "embedded": {}, "tiers": {}, "hosts": ""})
            is False
        )


class TestGetCovRepo:
    """First repo carrying a non-empty ``[coverage]`` section wins."""

    def test_no_repos_returns_none(self) -> None:
        assert get_cov_repo([]) is None

    def test_no_repo_has_coverage_returns_none(self) -> None:
        repo = MagicMock()
        repo.settings = {}
        assert get_cov_repo([repo]) is None

    def test_repo_with_coverage_section_found(self) -> None:
        repo = MagicMock()
        repo.settings = {"coverage": {"gcda_remote_dir": "/remote"}}
        assert get_cov_repo([repo]) is repo

    def test_first_matching_repo_wins(self) -> None:
        unconfigured = MagicMock()
        unconfigured.settings = {}
        configured = MagicMock()
        configured.settings = {"coverage": {"hosts": "sprout_cov"}}
        also_configured = MagicMock()
        also_configured.settings = {"coverage": {"hosts": "other"}}
        assert get_cov_repo([unconfigured, configured, also_configured]) is configured

    def test_empty_coverage_section_does_not_match(self) -> None:
        """A ``[coverage]`` table present but with none of the recognized keys
        set is treated the same as no section at all."""
        repo = MagicMock()
        repo.settings = {"coverage": {}}
        assert get_cov_repo([repo]) is None


class TestGetCovConfig:
    """Extracts the ``[coverage]`` dict from the first matching repo."""

    def test_no_repos_returns_empty_dict(self) -> None:
        assert get_cov_config([]) == {}

    def test_no_matching_repo_returns_empty_dict(self) -> None:
        repo = MagicMock()
        repo.settings = {}
        assert get_cov_config([repo]) == {}

    def test_returns_matching_repo_coverage_dict(self) -> None:
        cov = {"gcda_remote_dir": "/remote", "hosts": "sprout_cov"}
        repo = MagicMock()
        repo.settings = {"coverage": cov}
        assert get_cov_config([repo]) is cov


class TestPrepareEmptyDir:
    """The typer-free empty/overwrite gate: raises ValueError, never typer.BadParameter."""

    def test_creates_missing_dir(self, tmp_path) -> None:
        target = tmp_path / "fresh"
        prepare_empty_dir(target, overwrite=False, flag_name="--cov-dir")
        assert target.is_dir()

    def test_existing_empty_dir_is_ok(self, tmp_path) -> None:
        target = tmp_path / "empty"
        target.mkdir()
        prepare_empty_dir(target, overwrite=False, flag_name="--cov-dir")
        assert target.is_dir()

    def test_nonempty_without_overwrite_raises_value_error(self, tmp_path) -> None:
        """A non-empty target must raise a plain ValueError — no typer coupling here."""
        import typer

        target = tmp_path / "used"
        target.mkdir()
        (target / "stale.txt").write_text("stale")
        with pytest.raises(ValueError, match="is not empty") as excinfo:
            prepare_empty_dir(target, overwrite=False, flag_name="--cov-dir")
        # It is a plain ValueError, not typer's BadParameter subclass.
        assert not isinstance(excinfo.value, typer.BadParameter)
        # Message names the flag and the derived overwrite flag (unchanged text).
        assert "--cov-dir target" in str(excinfo.value)
        assert "--overwrite-cov-dir" in str(excinfo.value)
        # Refused to touch the stale contents.
        assert (target / "stale.txt").exists()

    def test_overwrite_clears_contents(self, tmp_path) -> None:
        target = tmp_path / "clear"
        target.mkdir()
        (target / "stale.txt").write_text("stale")
        (target / "sub").mkdir()
        (target / "sub" / "nested.txt").write_text("more")
        prepare_empty_dir(target, overwrite=True, flag_name="--cov-dir")
        assert list(target.iterdir()) == []
