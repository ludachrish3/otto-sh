"""``[coverage]`` config resolution from the repo list.

Moved verbatim from ``otto.cli.test`` (library-extraction Task 14): these are
pure functions over the parsed ``.otto/settings.toml`` dict, with no CLI
dependency, so they belong in ``otto.coverage`` alongside the rest of the
coverage library.
"""

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from otto.config.coverage_settings import get_cov_config, get_cov_repo, has_cov_config
from otto.coverage.config import (
    DestinationError,
    DestinationErrorKind,
    check_destination,
    destination_message,
    prepare_destination,
)

_KW = {"field": "cov_dir", "remedy_field": "overwrite_cov_dir"}


@pytest.mark.parametrize(
    "kind",
    ["not_empty", "not_writable", "not_a_directory"],
)
def test_destination_message_matches_the_library_spelling_for_every_kind(
    kind: DestinationErrorKind,
) -> None:
    """``destination_message`` with the library's own spelling is byte-identical

    to the message :class:`DestinationError` builds itself — one template,
    used both ways.
    """
    path = Path("/tmp/cov")
    err = DestinationError(
        path,
        field="cov_dir",
        remedy_field="overwrite_cov_dir",
        kind=kind,
        reason="Permission denied",
    )
    message = destination_message(
        kind,
        path,
        subject="cov_dir",
        remedy="set overwrite_cov_dir=True",
        reason="Permission denied",
    )
    assert message == str(err)


class TestHasCovConfig:
    """Truth table over the keys that count as "coverage is configured"."""

    def test_empty_dict_is_false(self) -> None:
        assert has_cov_config({}) is False

    def test_unrelated_keys_are_false(self) -> None:
        assert has_cov_config({"something_else": True}) is False

    def test_embedded_is_true(self) -> None:
        assert has_cov_config({"embedded": {"build_dir": "build"}}) is True

    def test_tiers_is_true(self) -> None:
        assert has_cov_config({"tiers": {"unit": {"kind": "unit"}}}) is True

    def test_hosts_is_true(self) -> None:
        assert has_cov_config({"hosts": "zephyr37-llext"}) is True

    def test_falsy_values_are_false(self) -> None:
        """An empty/falsy value under a known key still counts as unconfigured."""
        assert has_cov_config({"embedded": {}, "tiers": {}, "hosts": ""}) is False


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
        repo.settings = {"coverage": {"hosts": "/remote"}}
        assert get_cov_repo([repo]) is repo

    def test_first_matching_repo_wins(self) -> None:
        unconfigured = MagicMock()
        unconfigured.settings = {}
        configured = MagicMock()
        configured.settings = {"coverage": {"hosts": "zephyr37-llext"}}
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
        cov = {"embedded": {}, "hosts": "zephyr37-llext"}
        repo = MagicMock()
        repo.settings = {"coverage": cov}
        assert get_cov_config([repo]) is cov


class TestPrepareDestination:
    def test_creates_a_missing_directory_and_leaves_it_empty(self, tmp_path):
        target = tmp_path / "new" / "cov"
        prepare_destination(target, overwrite=False, **_KW)
        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_accepts_an_existing_empty_directory(self, tmp_path):
        prepare_destination(tmp_path, overwrite=False, **_KW)
        assert list(tmp_path.iterdir()) == []

    def test_refuses_a_non_empty_directory_naming_the_field_and_the_remedy(self, tmp_path):
        (tmp_path / "stale.txt").write_text("x")
        with pytest.raises(DestinationError) as excinfo:
            prepare_destination(tmp_path, overwrite=False, **_KW)
        err = excinfo.value
        assert err.kind == "not_empty"
        assert err.field == "cov_dir"
        assert err.remedy_field == "overwrite_cov_dir"
        assert str(err) == (
            f"cov_dir target {tmp_path} is not empty; set overwrite_cov_dir=True to clear it."
        )
        assert (tmp_path / "stale.txt").exists()

    def test_overwrite_clears_files_directories_and_symlinks(self, tmp_path):
        (tmp_path / "f.txt").write_text("x")
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "inner.txt").write_text("y")
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (tmp_path / "link").symlink_to(outside)
        prepare_destination(tmp_path, overwrite=True, **_KW)
        assert list(tmp_path.iterdir()) == []
        assert (outside / "keep.txt").exists()

    def test_refuses_a_file_as_a_destination(self, tmp_path):
        target = tmp_path / "file"
        target.write_text("x")
        with pytest.raises(DestinationError, match="is not a directory") as excinfo:
            prepare_destination(target, overwrite=False, **_KW)
        assert excinfo.value.kind == "not_a_directory"

    def test_refuses_a_dangling_symlink_as_a_destination(self, tmp_path):
        """A dangling symlink target exists (as a symlink) but not as a
        directory: ``mkdir`` would otherwise fail on it with "File exists",
        surfacing as ``not_writable`` instead of the correct ``not_a_directory``."""
        target = tmp_path / "link"
        target.symlink_to(tmp_path / "missing")
        with pytest.raises(DestinationError, match="is not a directory") as excinfo:
            prepare_destination(target, overwrite=False, **_KW)
        assert excinfo.value.kind == "not_a_directory"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_refuses_a_directory_this_user_cannot_write(self, tmp_path):
        target = tmp_path / "ro"
        target.mkdir()
        target.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(DestinationError, match="cannot be written") as excinfo:
                prepare_destination(target, overwrite=False, **_KW)
            assert excinfo.value.kind == "not_writable"
        finally:
            target.chmod(stat.S_IRWXU)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_refuses_a_directory_this_user_cannot_read(self, tmp_path):
        """The emptiness pre-check's own ``iterdir()`` can raise ``PermissionError``;

        it must become :class:`DestinationError`, not escape as a raw ``OSError``.
        """
        target = tmp_path / "wx"
        target.mkdir()
        target.chmod(stat.S_IWUSR | stat.S_IXUSR)
        try:
            with pytest.raises(DestinationError, match="cannot be written") as excinfo:
                prepare_destination(target, overwrite=False, **_KW)
            assert excinfo.value.kind == "not_writable"
        finally:
            target.chmod(stat.S_IRWXU)


class TestCheckDestination:
    def test_touches_nothing_for_a_missing_directory(self, tmp_path):
        target = tmp_path / "new" / "cov"
        check_destination(target, overwrite=False, **_KW)
        assert not target.exists()
        assert not target.parent.exists()

    def test_refuses_a_non_empty_directory_without_touching_it(self, tmp_path):
        (tmp_path / "stale.txt").write_text("x")
        before = sorted(p.name for p in tmp_path.iterdir())
        with pytest.raises(DestinationError, match="is not empty"):
            check_destination(tmp_path, overwrite=False, **_KW)
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_overwrite_accepts_a_non_empty_directory_without_clearing_it(self, tmp_path):
        (tmp_path / "stale.txt").write_text("x")
        check_destination(tmp_path, overwrite=True, **_KW)
        assert (tmp_path / "stale.txt").exists()

    def test_refuses_a_file(self, tmp_path):
        target = tmp_path / "file"
        target.write_text("x")
        with pytest.raises(DestinationError, match="is not a directory"):
            check_destination(target, overwrite=False, **_KW)

    def test_refuses_a_dangling_symlink(self, tmp_path):
        target = tmp_path / "link"
        target.symlink_to(tmp_path / "missing")
        with pytest.raises(DestinationError, match="is not a directory") as excinfo:
            check_destination(target, overwrite=False, **_KW)
        assert excinfo.value.kind == "not_a_directory"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_refuses_an_unwritable_ancestor_of_a_missing_directory(self, tmp_path):
        parent = tmp_path / "ro"
        parent.mkdir()
        parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(DestinationError, match="cannot be written"):
                check_destination(parent / "cov", overwrite=False, **_KW)
        finally:
            parent.chmod(stat.S_IRWXU)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_refuses_a_target_three_levels_below_a_read_only_ancestor(self, tmp_path):
        """The ancestor walk keeps climbing past several missing levels."""
        parent = tmp_path / "ro"
        parent.mkdir()
        parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(DestinationError, match="cannot be written"):
                check_destination(parent / "a" / "b" / "c", overwrite=False, **_KW)
        finally:
            parent.chmod(stat.S_IRWXU)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_refuses_an_existing_directory_this_user_cannot_write(self, tmp_path):
        """The ``probe = path`` branch: an existing, readable, but unwritable directory."""
        target = tmp_path / "rx"
        target.mkdir()
        target.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(DestinationError, match="cannot be written") as excinfo:
                check_destination(target, overwrite=False, **_KW)
            assert excinfo.value.kind == "not_writable"
        finally:
            target.chmod(stat.S_IRWXU)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_refuses_a_directory_this_user_cannot_read(self, tmp_path):
        """``iterdir()`` on the emptiness check can raise ``PermissionError``, too."""
        target = tmp_path / "wx"
        target.mkdir()
        target.chmod(stat.S_IWUSR | stat.S_IXUSR)
        try:
            with pytest.raises(DestinationError, match="cannot be written") as excinfo:
                check_destination(target, overwrite=False, **_KW)
            assert excinfo.value.kind == "not_writable"
        finally:
            target.chmod(stat.S_IRWXU)
