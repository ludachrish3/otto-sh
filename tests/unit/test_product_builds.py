"""Pins for tests/project3/product/product_builds.py: the one version-to-build-to-artifact map.

The suite (``tests/project3/tests/test_embedded_coverage.py``) and the bed reap
(``scripts/bed_reap.py``) both import it; the reap's loader is what is used here,
so the module under test is the one the reap rebuilds through.
"""

import sys
from pathlib import Path

import pytest

from scripts import bed_reap
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

builds = bed_reap._builds()

SETTINGS = {
    "products": [{"name": "cov_ext", "kind": "embedded"}, {"name": "unix_app", "kind": "unix"}],
    "coverage": {
        "embedded": {
            "build_dir": "/b/default",
            "builds": {"3.7": {"build_dir": "/b/v37"}, "4.4": {"build_dir": "/b/v44"}, "5.0": {}},
        }
    },
}


def test_the_reap_loads_the_module_the_project_declares_as_a_lib() -> None:
    assert Path(builds.__file__) == CHECKOUT_ROOT / "tests/project3/product/product_builds.py"
    text = (CHECKOUT_ROOT / "tests/project3/.otto/settings.toml").read_text()
    assert '\n    "product",\n' in text


@pytest.mark.parametrize(
    ("os_version", "build_dir"),
    [("3.7", "/b/v37"), ("4.4", "/b/v44"), ("5.0", "/b/default"), (None, "/b/default")],
)
def test_build_dir_for_prefers_the_versions_entry_then_the_single_dir(os_version, build_dir):
    assert builds.build_dir_for(SETTINGS, os_version) == build_dir


def test_build_dir_for_is_ottos_own_resolver_anchored_to_project3() -> None:
    """One reading of the table: tilde and project-relative values resolve as the collector's do."""
    from otto.coverage.collect import resolve_embedded_build_dir

    config = {
        "coverage": {
            "embedded": {"build_dir": "rel/dir", "builds": {"3.7": {"build_dir": "~/b37"}}}
        }
    }
    root = builds.PRODUCT_DIR.parent
    assert builds.build_dir_for(config, "3.7") == str(Path.home() / "b37")
    assert builds.build_dir_for(config, None) == str(root / "rel/dir")
    assert builds.build_dir_for(config, "4.4") == str(root / "rel/dir")
    embedded = config["coverage"]["embedded"]
    for version in ("3.7", None, "4.4"):
        assert builds.build_dir_for(config, version) == resolve_embedded_build_dir(
            embedded, version, root
        )
    assert resolve_embedded_build_dir({}, "3.7", root) is None
    (found,) = builds.declared_builds(
        {**SETTINGS, "coverage": {"embedded": {"builds": {"3.7": {"build_dir": "b37"}}}}}
    )
    assert found.build_dir == str(root / "b37")
    assert found.artifact == root / "b37/zephyr/cov_ext.stripped.llext"


def test_build_dir_for_without_any_dir_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="build_dir is not configured"):
        builds.build_dir_for({}, "3.7")


@pytest.mark.parametrize(("version", "zver"), [("3.7", "v3_7"), ("4.4", "v4_4"), (None, None)])
def test_zver_for(version, zver) -> None:
    assert builds.zver_for(version) == zver


def test_extension_path_and_build_command() -> None:
    assert builds.extension_path("/b/v37", "cov_ext") == Path(
        "/b/v37/zephyr/cov_ext.stripped.llext"
    )
    otto = Path("/venv/bin/otto")
    assert builds.build_command("/b/v37", "v3_7", otto) == (
        f"OTTO=/venv/bin/otto bash {builds.BUILD_SCRIPT} /b/v37 v3_7"
    )
    assert builds.build_command("/b/v37", None, otto).endswith(" /b/v37")
    assert Path(sys.executable).parent / "otto" == builds.OTTO_BIN


def test_declared_builds_is_one_entry_per_version_with_a_dir() -> None:
    found = builds.declared_builds(SETTINGS)
    assert [(b.os_version, b.build_dir, b.zver) for b in found] == [
        ("3.7", "/b/v37", "v3_7"),
        ("4.4", "/b/v44", "v4_4"),
    ]
    assert found[0].artifact == Path("/b/v37/zephyr/cov_ext.stripped.llext")


def test_declared_builds_needs_exactly_one_embedded_product() -> None:
    with pytest.raises(RuntimeError, match="expected one embedded product"):
        builds.declared_builds({"products": [], "coverage": SETTINGS["coverage"]})


def test_project3s_own_settings_declare_the_two_versions() -> None:
    found = builds.declared_builds()
    assert [b.os_version for b in found] == ["3.7", "4.4"]
    assert all(b.artifact.name.endswith(".stripped.llext") for b in found)


@pytest.mark.asyncio
async def test_build_extension_runs_the_script_locally_and_raises_on_a_red_build(
    monkeypatch, tmp_path
) -> None:
    from otto.result import CommandResult
    from otto.utils import Status

    seen = []

    class _Local:
        async def exec(self, cmd, timeout):
            seen.append((cmd, timeout))
            return CommandResult(status=Status.Failed, value="west: boom", command=cmd)

        async def close(self):
            seen.append("closed")

    from otto.host import local_host

    monkeypatch.setattr(local_host, "LocalHost", _Local)  # the defining module, not the package
    with pytest.raises(RuntimeError, match="west: boom"):
        await builds.build_extension(str(tmp_path), "v3_7")
    assert seen == [
        (builds.build_command(str(tmp_path), "v3_7", builds.OTTO_BIN), builds.BUILD_TIMEOUT_S),
        "closed",
    ]
