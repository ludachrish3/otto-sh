"""The dependency preflight, staged against fabricated dist-info directories.

Every case asserts BOTH arms -- satisfied is silent, unsatisfied names the
requirement and what was found -- so no guard can pass by refusing everything
or by accepting everything. That symmetry is the point: a preflight that always
refused would pass a one-armed suite and break every run.

Nothing here creates a venv, reaches an index, or imports a repo. Installed
packages are FABRICATED: a ``*.dist-info`` directory with a METADATA file is
all ``importlib.metadata`` needs, so a whole environment is three lines of
tmp_path.
"""

import subprocess
import sys
import textwrap
import types
from pathlib import Path

from otto.env.preflight import Unsatisfied, check_repo, preflight, read_project_table


def _install(site: Path, name: str, version: str, requires: "list[str] | None" = None) -> None:
    """Fabricate an installed distribution in *site*."""
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {r}" for r in (requires or [])]
    (info / "METADATA").write_text("\n".join(lines) + "\n")


def _repo(tmp_path: Path, name: str, deps: "list[str] | None", *, dynamic: bool = False):
    """A stand-in repo with a pyproject.

    Deliberately NOT a real ``Repo``: the evaluator reads only ``.name`` and
    ``.sut_dir``, and pinning that here is what keeps it free of
    ``otto.config`` -- and therefore safe to call from anywhere.
    """
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    # TOML LITERAL strings (single quotes). Requirements carry markers like
    # `python_version >= "3.0"`, and spelling them into a double-quoted TOML
    # string produces a file that does not parse -- which this module treats as
    # "no requirements", so every marker test would pass while checking
    # nothing. The self-check below is there because that failure is SILENT.
    body = (
        'dynamic = ["dependencies"]'
        if dynamic
        else ("dependencies = [" + ", ".join(f"'{d}'" for d in deps or []) + "]")
    )
    (root / "pyproject.toml").write_text(
        textwrap.dedent(f"""\
            [project]
            name = "{name}"
            version = "0.1.0"
            {body}
            """)
    )
    repo = types.SimpleNamespace(name=name, sut_dir=root)
    project = read_project_table(repo)
    assert project is not None, "fixture wrote a pyproject the evaluator cannot read"
    if not dynamic:
        assert project.get("dependencies", []) == list(deps or [])
    return repo


class TestSatisfaction:
    def test_a_satisfied_requirement_is_silent(self, tmp_path):
        site = tmp_path / "site"
        _install(site, "beetroot", "1.2.0")
        repo = _repo(tmp_path, "repo4", ["beetroot >= 0.1"])
        assert check_repo(repo, site_dirs=[site]) == []

    def test_an_absent_package_reports_found_none(self, tmp_path):
        site = tmp_path / "site"
        site.mkdir()
        repo = _repo(tmp_path, "repo4", ["beetroot >= 0.1"])
        assert check_repo(repo, site_dirs=[site]) == [
            Unsatisfied(repo="repo4", requirement="beetroot >= 0.1", found="none")
        ]

    def test_a_too_old_package_reports_the_version_it_found(self, tmp_path):
        """``found: none`` vs ``found: 2.12`` is the whole diagnostic value."""
        site = tmp_path / "site"
        _install(site, "paramiko", "2.12.0")
        repo = _repo(tmp_path, "repo4", ["paramiko >= 3"])
        [bad] = check_repo(repo, site_dirs=[site])
        assert bad.found == "2.12.0"

    def test_a_repo_with_no_pyproject_is_vacuously_satisfied(self, tmp_path):
        """repo1-repo3 are this shape, and they must keep working untouched."""
        root = tmp_path / "repo1"
        root.mkdir()
        repo = types.SimpleNamespace(name="repo1", sut_dir=root)
        assert check_repo(repo, site_dirs=[tmp_path / "site"]) == []

    def test_an_unreadable_pyproject_is_vacuously_satisfied(self, tmp_path):
        """The preflight is a gate on DEPENDENCIES, not a second config parser.

        A malformed pyproject already has an owner -- discovery frames it, and
        an installer will refuse it in its own words. Refusing here too would
        put otto's name on a syntax error it did not diagnose.
        """
        root = tmp_path / "repo4"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project\nname = ")
        repo = types.SimpleNamespace(name="repo4", sut_dir=root)
        assert check_repo(repo, site_dirs=[tmp_path / "site"]) == []

    def test_an_unparseable_installed_version_is_not_judged(self, tmp_path):
        """A preflight that CRASHES is worse than one that stays quiet.

        Installed metadata is written by whatever built the package, so its
        `Version` is not otto's to trust -- and `SpecifierSet.contains` raises
        on a non-PEP-440 one.
        """
        site = tmp_path / "site"
        _install(site, "beetroot", "not-a-version")
        repo = _repo(tmp_path, "repo4", ["beetroot >= 0.1"])
        assert check_repo(repo, site_dirs=[site]) == []

    def test_an_unparseable_requirement_is_skipped(self, tmp_path):
        """Same owner as an unreadable pyproject: the installer diagnoses it,
        and it does so better than a gate that only knows the string failed."""
        site = tmp_path / "site"
        site.mkdir()
        repo = _repo(tmp_path, "repo4", ["!!! not a requirement"])
        assert check_repo(repo, site_dirs=[site]) == []


class TestMarkers:
    def test_a_marker_that_excludes_this_interpreter_is_not_checked(self, tmp_path):
        site = tmp_path / "site"
        site.mkdir()
        repo = _repo(tmp_path, "repo4", ['beetroot >= 0.1; sys_platform == "nonesuch"'])
        assert check_repo(repo, site_dirs=[site]) == []

    def test_a_marker_that_includes_this_interpreter_is_checked(self, tmp_path):
        site = tmp_path / "site"
        site.mkdir()
        repo = _repo(tmp_path, "repo4", ['beetroot >= 0.1; python_version >= "3.0"'])
        assert len(check_repo(repo, site_dirs=[site])) == 1


class TestExtrasAndTransitives:
    def test_extras_are_not_followed(self, tmp_path):
        """Base dependencies only -- an extra's requirements are not ours.

        The extra MUST carry an unsatisfiable requirement, or this passes
        against an evaluator that follows extras enthusiastically and simply
        finds nothing to follow. `labjack-driver` is deliberately absent from
        the staged site.
        """
        site = tmp_path / "site"
        _install(
            site,
            "beetroot",
            "1.0.0",
            requires=['labjack-driver >= 1; extra == "labjack"'],
        )
        repo = _repo(tmp_path, "repo4", ["beetroot[labjack] >= 0.1"])
        assert check_repo(repo, site_dirs=[site]) == []

    def test_transitive_requirements_are_not_followed(self, tmp_path):
        """Transitive consistency is the installer's promise, not otto's."""
        site = tmp_path / "site"
        _install(site, "beetroot", "1.0.0", requires=["nonexistent-thing >= 9"])
        repo = _repo(tmp_path, "repo4", ["beetroot >= 0.1"])
        assert check_repo(repo, site_dirs=[site]) == []


class TestDynamicDependencies:
    def test_dynamic_and_installed_reads_requires_dist(self, tmp_path):
        """Installed-dist metadata FIRST: exact, and the only thing that can
        answer for a dynamic pyproject."""
        site = tmp_path / "site"
        _install(site, "repo4", "0.1.0", requires=["beetroot >= 0.1"])
        repo = _repo(tmp_path, "repo4", None, dynamic=True)
        [bad] = check_repo(repo, site_dirs=[site])
        assert bad.requirement.startswith("beetroot")

    def test_installed_metadata_beats_the_pyproject_it_disagrees_with(self, tmp_path):
        """The order is not cosmetic: an editable install whose pyproject has
        since gained a requirement reports what is INSTALLED, which is what the
        interpreter will actually import."""
        site = tmp_path / "site"
        _install(site, "repo4", "0.1.0", requires=["beetroot >= 0.1"])
        _install(site, "beetroot", "1.0.0")
        repo = _repo(tmp_path, "repo4", ["nowhere-at-all >= 9"])
        assert check_repo(repo, site_dirs=[site]) == []

    def test_dynamic_and_not_installed_warns_and_checks_nothing(self, tmp_path):
        site = tmp_path / "site"
        site.mkdir()
        repo = _repo(tmp_path, "repo4", None, dynamic=True)
        result = preflight([repo], site_dirs=[site])
        assert result.unsatisfied == []
        assert any("dynamic" in w and "repo4" in w for w in result.warnings)

    def test_a_checkable_repo_produces_no_warning(self, tmp_path):
        """The other arm: warnings are for what could not be ASKED, so a repo
        that answered must not produce one."""
        site = tmp_path / "site"
        site.mkdir()
        repo = _repo(tmp_path, "repo4", ["beetroot >= 0.1"])
        result = preflight([repo], site_dirs=[site])
        assert len(result.unsatisfied) == 1
        assert result.warnings == []


class TestImportFootprint:
    def test_importing_the_module_does_not_import_packaging(self):
        """F8: ``packaging`` is in ZERO import-budget snapshots, and no measured
        surface runs the CLI preamble -- so the snapshot diff cannot witness a
        module-scope import here. This asks the question directly.

        The rule it protects: every repo without a pyproject returns before the
        evaluation path, and every sample repo is that shape, so a lazy import
        keeps a real run's footprint where it was.
        """
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import otto.env.preflight, sys; print('packaging' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip() == "False"
