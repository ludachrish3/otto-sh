"""Discovery lives in ``otto.config.project``; ``otto.bootstrap`` re-exports its public name.

Spec 2026-10-11 placement-and-phases §3.9: discovery parses the environment and the
projects' settings, which is ``otto.config``'s work (spec
``2026-10-06-repo-and-scope-inputs-design`` S-3), so ``discover``, ``DiscoveryResult``,
the cache, ``get_env`` and ``discovered_teardown_deadline`` are defined in
``otto.config.project``. The root re-exports ``get_env``, its one public name, the
same object, lazily: importing the root loads no project loader. Discovery never
composes and runs no user code.
"""

import subprocess
import sys

import pytest

import otto.config.project as config_project
from otto import bootstrap
from tests._fixtures.project_scaffold import make_project


def test_the_root_re_exports_discovery_s_own_object():
    assert bootstrap.get_env is config_project.get_env
    assert "get_env" in dir(bootstrap)


def test_only_get_env_is_public_at_the_root():
    assert "get_env" in bootstrap.__all__
    assert {"discover", "DiscoveryResult", "discovered_teardown_deadline"}.isdisjoint(
        bootstrap.__all__
    )
    assert not hasattr(bootstrap, "discover")
    assert not hasattr(bootstrap, "DiscoveryResult")
    assert not hasattr(bootstrap, "discovered_teardown_deadline")


def test_importing_the_root_loads_no_project_loader_until_a_discovery_name_is_read():
    probe = (
        "import sys, otto.bootstrap as bs; "
        "before = 'otto.config.project' in sys.modules; bs.get_env; "
        "print(before, 'otto.config.project' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["False", "True"]


def test_discovery_imports_no_init_module_and_composes_nothing(tmp_path, monkeypatch):
    project = make_project(
        tmp_path / "proj",
        name="proj",
        extra='libs = ["lib"]\ninit = ["discovery_home_marker"]\n',
        files={"lib/discovery_home_marker.py": "MARKED = True\n"},
    )
    monkeypatch.setenv("OTTOPATH", str(project))
    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "4.5")
    bootstrap.invalidate()
    assert [r.name for r in config_project.discover().projects] == ["proj"]
    assert config_project.get_env().teardown_deadline == 4.5
    assert config_project.discovered_teardown_deadline() == 4.5
    assert not bootstrap.is_bootstrapped()
    assert "discovery_home_marker" not in sys.modules


def test_a_failed_discovery_leaves_the_deadline_unread(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTOPATH", str(tmp_path / "no-such-project"))
    bootstrap.invalidate()
    with pytest.raises(FileNotFoundError):
        config_project.discover()
    assert config_project.discovered_teardown_deadline() is None


def test_clear_discovery_drops_the_cache_and_invalidate_calls_it(tmp_path, monkeypatch):
    first = make_project(tmp_path / "first", name="first")
    monkeypatch.setenv("OTTOPATH", str(first))
    config_project.clear_discovery()
    found = config_project.discover()
    assert config_project.discover() is found
    config_project.clear_discovery()
    assert config_project.discover() is not found
    again = config_project.discover()
    bootstrap.invalidate()
    assert config_project._discovery.result is None
    assert config_project.discover() is not again


def test_the_root_reads_discovery_off_its_owner_when_it_composes(tmp_path, monkeypatch):
    seen: list[str] = []
    real = config_project.discover

    def _spy():
        seen.append("discover")
        return real()

    monkeypatch.setenv("OTTOPATH", str(make_project(tmp_path / "proj", name="proj")))
    monkeypatch.setattr("otto.config.project.discover", _spy)
    bootstrap.invalidate()
    assert [r.name for r in bootstrap.bootstrap().projects] == ["proj"]
    assert seen == ["discover"]
