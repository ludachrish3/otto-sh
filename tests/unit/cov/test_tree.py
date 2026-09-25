"""The shared ``cov/<host>/<product>/`` walk."""

from pathlib import Path

import pytest

from otto.config.coverage_settings import CoverageConfigError
from otto.coverage.tree import iter_product_dirs


def test_empty_cov_dir_yields_nothing(tmp_path: Path) -> None:
    cov = tmp_path / "cov"
    cov.mkdir()
    assert iter_product_dirs(cov) == []


def test_missing_cov_dir_yields_nothing(tmp_path: Path) -> None:
    assert iter_product_dirs(tmp_path / "cov") == []


def test_two_hosts_two_products_sorted(tmp_path: Path) -> None:
    cov = tmp_path / "cov"
    for host in ("h2", "h1"):
        for product in ("gateway", "app"):
            (cov / host / product).mkdir(parents=True)

    assert iter_product_dirs(cov) == [
        ("h1", "app", cov / "h1" / "app"),
        ("h1", "gateway", cov / "h1" / "gateway"),
        ("h2", "app", cov / "h2" / "app"),
        ("h2", "gateway", cov / "h2" / "gateway"),
    ]


def test_product_dirs_are_not_filtered_on_content(tmp_path: Path) -> None:
    # No .gcda filtering here — each caller decides what interests it.
    cov = tmp_path / "cov"
    (cov / "h1" / "app").mkdir(parents=True)
    (cov / "h1" / "agent").mkdir()
    (cov / "h1" / "app" / "capture.json").write_text("{}")

    assert iter_product_dirs(cov) == [
        ("h1", "agent", cov / "h1" / "agent"),
        ("h1", "app", cov / "h1" / "app"),
    ]


def test_stray_file_at_host_level_is_ignored(tmp_path: Path) -> None:
    cov = tmp_path / "cov"
    (cov / "h1" / "app").mkdir(parents=True)
    (cov / ".otto_cov_meta.json").write_text("{}")

    assert iter_product_dirs(cov) == [("h1", "app", cov / "h1" / "app")]


def test_stray_file_at_product_level_is_ignored(tmp_path: Path) -> None:
    cov = tmp_path / "cov"
    (cov / "h1" / "app").mkdir(parents=True)
    (cov / "h1" / "notes.txt").write_text("x")

    assert iter_product_dirs(cov) == [("h1", "app", cov / "h1" / "app")]


def test_gcda_directly_under_a_host_dir_is_refused(tmp_path: Path) -> None:
    cov = tmp_path / "cov"
    (cov / "h1").mkdir(parents=True)
    (cov / "h1" / "x.gcda").write_bytes(b"")

    with pytest.raises(CoverageConfigError, match=r"cov/<host>/<product>/") as excinfo:
        iter_product_dirs(cov)
    assert str(cov / "h1") in str(excinfo.value)


def test_capture_directly_under_a_host_dir_is_refused(tmp_path: Path) -> None:
    cov = tmp_path / "cov"
    (cov / "h1").mkdir(parents=True)
    (cov / "h1" / "capture.json").write_text("{}")

    with pytest.raises(CoverageConfigError, match=r"cov/<host>/<product>/") as excinfo:
        iter_product_dirs(cov)
    assert str(cov / "h1") in str(excinfo.value)
