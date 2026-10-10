"""The doctor's settings check IS the loader's compile (#497)."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import tomli

from otto.config.repo import Repo, compile_settings, validate_settings
from otto.host.os_profile import OS_PROFILES
from tests._fixtures.sutrepo import make_sut_repo


def _write(root: Path, body: str) -> Path:
    make_sut_repo(root, name="acme", version="1.0.0", extra=textwrap.dedent(body))
    return root / ".otto" / "settings.toml"


def test_a_json_lab_source_without_paths_passes_the_settings_compile(tmp_path: Path) -> None:
    """Only a source's envelope is a settings matter; its options are checked at preparation."""
    _write(tmp_path, '[[lab.sources]]\nbackend = "json"\n')
    assert validate_settings(tmp_path) == []


def test_a_typo_in_an_os_profile_default_is_checked_after_init(tmp_path: Path) -> None:
    """The settings compile checks a table's shape; what it means is checked after init."""
    from otto.host.os_profile import check_data_profiles

    _write(tmp_path, '[os_profiles.acme-os]\nbase = "unix"\nosTyp = "unix"\n')
    assert validate_settings(tmp_path) == []
    with pytest.raises(
        ValueError, match=r"\[os_profiles\.acme-os\] in repo .*unknown default field"
    ):
        check_data_profiles([Repo(sut_dir=tmp_path)])


def test_validating_registers_no_os_profile(tmp_path: Path) -> None:
    _write(tmp_path, '[os_profiles.acme-os-validated]\nbase = "unix"\n')
    before = sorted(OS_PROFILES.names())
    assert validate_settings(tmp_path) == []
    assert sorted(OS_PROFILES.names()) == before


def test_an_unknown_settings_key_renders_compactly(tmp_path: Path) -> None:
    _write(tmp_path, 'verzion = "s3cret-value"\n')
    (problem,) = validate_settings(tmp_path)
    assert "verzion" in problem
    assert "input_value" not in problem  # never str(ValidationError)
    assert "s3cret-value" not in problem


def test_settings_that_are_not_toml_fail_with_the_parse_error(tmp_path: Path) -> None:
    (tmp_path / ".otto").mkdir()
    (tmp_path / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: malformed TOML under test
        'name = "acme\n'
    )
    (problem,) = validate_settings(tmp_path)
    assert problem.startswith(f"{tmp_path / '.otto' / 'settings.toml'}: ")
    assert "Illegal character" in problem


def test_settings_that_are_not_utf8_fail_instead_of_raising(tmp_path: Path) -> None:
    (tmp_path / ".otto").mkdir()
    path = tmp_path / ".otto" / "settings.toml"
    path.write_bytes(b"\xff\xfe")  # sutrepo-exempt: non-UTF-8 settings under test
    (problem,) = validate_settings(tmp_path)
    assert problem.startswith(f"{path}: ")


def test_a_relative_root_is_reported_by_its_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".otto").mkdir()
    (tmp_path / ".otto" / "settings.toml").write_text(  # sutrepo-exempt: malformed TOML under test
        'name = "acme\n'
    )
    monkeypatch.chdir(tmp_path)
    (problem,) = validate_settings(Path())
    assert problem.startswith(f"{tmp_path / '.otto' / 'settings.toml'}: ")


def test_valid_settings_have_no_problems(tmp_path: Path) -> None:
    _write(tmp_path, 'libs = ["pylib"]\n[[lab.sources]]\nbackend = "json"\npaths = ["lab_data"]\n')
    assert validate_settings(tmp_path) == []


def test_compile_settings_matches_what_the_loader_assigns(
    tmp_path: Path,
) -> None:
    """The differential: every CompiledSettings field equals the Repo attribute of the same name."""
    _write(
        tmp_path,
        """
        libs = ["pylib"]
        tests = ["tests"]
        init = ["acme_init"]
        [[lab.sources]]
        backend = "json"
        paths = ["lab_data"]
        [project]
        lab_patterns = ["bench.*"]
        host_patterns = [".*"]
        [dependencies]
        optional = ["other >= 1.0"]
        [logging.levels]
        asyncssh = "DEBUG"
        [os_profiles.acme-os-parity]
        base = "unix"
        """,
    )
    compiled = compile_settings(
        tomli.loads((tmp_path / ".otto" / "settings.toml").read_text()), tmp_path
    )
    repo = Repo(sut_dir=tmp_path)
    for field in (
        "name",
        "version",
        "libs",
        "tests",
        "init",
        "declared_dependencies",
        "host_preferences",
        "logging_levels",
        "docker_settings",
        "monitor_settings",
        "env_backend",
        "declared_products",
        "declared_dev_tools",
    ):
        assert getattr(compiled, field) == getattr(repo, field), field
    assert [s.label for s in compiled.lab_sources] == [s.label for s in repo.lab_sources]
    assert compiled.project_scope is not None
    assert repo.project_scope is not None
    assert set(compiled.os_profiles) == set(repo.os_profiles) == {"acme-os-parity"}


def test_the_loader_reads_settings_as_utf8_under_an_ascii_locale(tmp_path: Path) -> None:
    """TOML is UTF-8 by spec; ``read_settings`` must not decode it in the locale encoding.

    ``LC_ALL=C PYTHONUTF8=0`` gives the child an ASCII locale encoding. With the
    bare ``open()`` that child died with ``UnicodeDecodeError`` on ``café``
    (proven red by reverting ``encoding="utf-8"``). The child prints the name as
    hex so the assertion never depends on the parent's stdout encoding.
    """
    otto_dir = tmp_path / ".otto"
    otto_dir.mkdir()
    settings = otto_dir / "settings.toml"
    settings.write_text(  # sutrepo-exempt: non-ASCII bytes the helper cannot write
        'name = "café"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    script = (
        "import sys; from pathlib import Path; from otto.config.repo import Repo; "
        "print(Repo(sut_dir=Path(sys.argv[1])).name.encode('utf-8').hex())"
    )
    out = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "OTTO_SUT_DIRS": ""},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert bytes.fromhex(out.strip()).decode() == "café"
