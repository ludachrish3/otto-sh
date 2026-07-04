import sys
import textwrap
from pathlib import Path

import pytest

from otto.configmodule.repo import Repo
from tests._fixtures.mockrepo import MockRepo

mock_repo: MockRepo = None
tests_root = Path(__file__).parent.parent.parent


def _write_repo(tmp_path: Path, settings_body: str) -> Path:
    """Materialize a minimal SUT repo at *tmp_path* with the given TOML body
    appended after the required ``name`` / ``version`` fields.
    """
    otto_dir = tmp_path / ".otto"
    otto_dir.mkdir(parents=True)
    base = textwrap.dedent("""
        name = "tmp_repo"
        version = "1.0.0"
    """).strip()
    (otto_dir / "settings.toml").write_text(f"{base}\n{settings_body}\n")
    return tmp_path


def _repo_with_settings(tmp_path: Path, settings_body: str) -> "Repo":
    """Materialize a minimal SUT repo and return the parsed Repo.

    Accepts raw TOML (including ``name``/``version``) — no base prepended.
    """
    import textwrap as _textwrap

    otto_dir = tmp_path / ".otto"
    otto_dir.mkdir(parents=True, exist_ok=True)
    (otto_dir / "settings.toml").write_text(_textwrap.dedent(settings_body))
    return Repo(sut_dir=tmp_path)


@pytest.fixture(autouse=False)
def default_mock_repo():

    global mock_repo  # noqa: PLW0603 — module-level singleton/cache

    mock_repo = MockRepo(tests_root / "repo1")


def test_repo_config_location(default_mock_repo):

    repo_settings_file = mock_repo.sut_dir / ".otto" / "settings.toml"
    assert repo_settings_file.exists()


def test_repo_commit_name(default_mock_repo):

    assert mock_repo.commit_name == f"{mock_repo.commit} ({mock_repo.description})"


def test_repo_settings_tests_sut_dir_variable(default_mock_repo):

    assert mock_repo.tests == [mock_repo.sut_dir / "tests"]


def test_repo_settings_init_sut_dir_variable(default_mock_repo):

    assert mock_repo.init == ["repo1_instructions", "custom_hosts", "repo1_monitor_uptime"]


def test_bootstrap_registers_repo1_instructions_and_suites(monkeypatch):
    """``bootstrap()`` is the granular replacement for the deleted
    ``Repo.apply_settings()`` / ``apply_repo_settings()``: per repo it adds libs
    to ``sys.path``, imports init modules, and imports test files — which
    together register repo1's instructions/suites into the shared
    ``INSTRUCTIONS``/``SUITES`` registries (module-level, process-wide).

    Isolation: Python's import cache couples this test to any earlier test in
    the same worker that imported repo1's modules — the cached modules make
    bootstrap's imports no-ops, the decorators never re-run, and the delta
    assertions see "sets are equal" (deterministically reproducible by running
    this test twice in one process). So: park any repo1-originated registry
    entries, evict the cached modules, and restore both afterwards.
    """
    from otto import bootstrap as bs
    from otto.cli.run import INSTRUCTIONS
    from otto.suite.register import SUITES

    repo1 = tests_root / "repo1"
    pylib = str(repo1 / "pylib")

    # Remove any prior entries so the precondition holds even if another
    # test (or a previous run in the same worker) already appended it.
    while pylib in sys.path:
        sys.path.remove(pylib)

    assert pylib not in sys.path

    def _park(registry, origin_prefix: str) -> dict:
        parked = {}
        for name in list(registry.names()):
            origin = registry.origin(name)
            if origin.startswith(origin_prefix):
                parked[name] = (registry.get(name), origin)
                registry.unregister(name)
        return parked

    def _park_repo1_suites() -> dict:
        # Two origin flavors both mean "repo1's suite world" (mirrors
        # test_import_and_register.py's clean_registry): suites re-registered
        # by an in-process `pytest.main([suite_file])` run (run_suite's
        # mechanism) carry pytest's own module name as origin (e.g.
        # "test_device") but keep repo1's file, while a bootstrap() of ANY
        # repo carries the `_otto_suite_*` auto-scan origin but may carry a
        # foreign file (another checkout's repo1 — an entry a repo1-file-only
        # park would miss, colliding with this test's own imports as
        # "already registered"). Park on either signal.
        parked = {}
        for name in list(SUITES.names()):
            entry = SUITES.get(name)
            origin = SUITES.origin(name)
            if origin.startswith("_otto_suite_") or Path(entry.file).is_relative_to(repo1):
                parked[name] = (entry, origin)
                SUITES.unregister(name)
        return parked

    parked_instructions = _park(INSTRUCTIONS, "repo1_instructions")
    parked_suites = _park_repo1_suites()
    evicted = {
        m: sys.modules.pop(m)
        for m in list(sys.modules)
        if m.startswith(("repo1_instructions", "_otto_suite_"))
    }

    before_instructions = set(INSTRUCTIONS.names())
    before_suites = set(SUITES.names())

    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo1))
    bs._reset()
    try:
        result = bs.bootstrap()
        assert result.errors == []

        assert pylib in sys.path
        assert set(INSTRUCTIONS.names()) > before_instructions
        assert set(SUITES.names()) > before_suites
    finally:
        bs._reset()
        # Restore the exact pre-test world: sys.path, this test's
        # registrations out, the parked entries and cached modules back in.
        while pylib in sys.path:
            sys.path.remove(pylib)
        for name in set(INSTRUCTIONS.names()) - before_instructions:
            INSTRUCTIONS.unregister(name)
        for name in set(SUITES.names()) - before_suites:
            SUITES.unregister(name)
        for mod in [m for m in sys.modules if m.startswith(("repo1_instructions", "_otto_suite_"))]:
            sys.modules.pop(mod, None)
        sys.modules.update(evicted)
        for registry, parked in ((INSTRUCTIONS, parked_instructions), (SUITES, parked_suites)):
            for name, (obj, origin) in parked.items():
                registry.register(name, obj, overwrite=True, origin=origin)


def test_product_log_prefixes_init_libs_and_explicit_capture(tmp_path):
    # A libs dir containing a real package (has __init__.py); its immediate
    # child package name becomes a capture prefix.
    libs_dir = tmp_path / "pylib"
    pkg = libs_dir / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    # A non-package child (no __init__.py) must NOT be picked up.
    (libs_dir / "loose.py").write_text("")

    sut = _write_repo(
        tmp_path,
        textwrap.dedent("""
        libs = ["${sut_dir}/pylib"]
        init = ["my_instructions.commands", "custom_hosts"]

        [logging]
        capture = ["thirdparty_lib"]
    """),
    )
    repo = Repo(sut_dir=sut)
    prefixes = repo.product_log_prefixes()

    # init roots: first dotted segment of each init entry
    assert "my_instructions" in prefixes
    assert "custom_hosts" in prefixes
    # immediate sub-package of a libs dir
    assert "mypkg" in prefixes
    # explicit [logging] capture entry
    assert "thirdparty_lib" in prefixes
    # a loose (non-package) module is not a prefix
    assert "loose" not in prefixes


def test_logging_capture_defaults_empty(tmp_path):
    sut = _write_repo(tmp_path, "")
    repo = Repo(sut_dir=sut)
    assert repo.logging_capture == []


# TODO: Test various settings fields and the recording of arbitrary additional data


class TestValidLabsParsing:
    """Tests for ``valid_labs`` parsing in ``Repo.parse_settings``.

    ``valid_labs`` lets a repo declare which labs it supports (e.g. an embedded
    product that only works in an embedded lab). Parsing stores the declared
    list; an unset key yields an empty list. Enforcement (rejecting a selected
    lab not in the list, and treating an empty list as "must declare") is a
    separate, deferred step — parsing must not silently treat unset as
    allow-all.
    """

    def test_absent_yields_empty_list(self, tmp_path):
        sut = _write_repo(tmp_path, "")
        repo = Repo(sut_dir=sut)
        assert repo.valid_labs == []

    def test_declared_labs_parsed_in_order(self, tmp_path):
        sut = _write_repo(tmp_path, 'valid_labs = ["embedded", "veggies"]')
        repo = Repo(sut_dir=sut)
        assert repo.valid_labs == ["embedded", "veggies"]


def test_repo_parses_unified_host_preferences(tmp_path):
    repo = _repo_with_settings(
        tmp_path,
        """
        name = "p"
        version = "1.0.0"
        [host_preferences.".*"]
        term = ["telnet"]
        ssh_options = { connect_timeout = 5.0 }
    """,
    )
    assert repo.host_preferences[".*"]["term"] == ["telnet"]
    assert repo.host_preferences[".*"]["ssh_options"] == {"connect_timeout": 5.0}
    assert not hasattr(repo, "host_defaults")


class TestHostPreferencesParsing:
    """Tests for unified ``[host_preferences]`` parsing in ``Repo.parse_settings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path):
        sut = _write_repo(tmp_path, "")
        repo = Repo(sut_dir=sut)
        assert repo.host_preferences == {}

    def test_selections_and_option_tables_parsed(self, tmp_path):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*"]
            term = ["telnet"]

            [host_preferences.".*".ssh_options]
            port = 2222
            connect_timeout = 5.0
        """),
        )
        repo = Repo(sut_dir=sut)
        assert repo.host_preferences[".*"]["term"] == ["telnet"]
        assert repo.host_preferences[".*"]["ssh_options"] == {
            "port": 2222,
            "connect_timeout": 5.0,
        }

    def test_legacy_host_defaults_rejected(self, tmp_path):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_defaults.ssh_options]
            port = 2222
        """),
        )
        with pytest.raises(ValueError, match=r"\[host_defaults\] was removed"):
            Repo(sut_dir=sut)

    def test_unknown_preference_key_raises(self, tmp_path):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*"]
            bogus_options = { x = 1 }
        """),
        )
        with pytest.raises(ValueError, match="unknown"):
            Repo(sut_dir=sut)

    def test_sutdir_expansion_in_host_preferences(self, tmp_path):
        """``${sut_dir}`` is expanded inside ``[host_preferences]`` strings, like
        every other repo settings table.
        """
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*".ssh_options]
            known_hosts = "${sut_dir}/known_hosts"
        """),
        )
        repo = Repo(sut_dir=sut)
        assert repo.host_preferences[".*"]["ssh_options"]["known_hosts"] == f"{sut}/known_hosts"


@pytest.fixture
def restore_profiles():
    """Snapshot/restore the global os-profile registry around a test, since
    ``Repo.parse_settings`` registers data profiles into module-global state.
    """
    from otto.host import os_profile

    saved = dict(os_profile.OS_PROFILES._entries)
    saved_origins = dict(os_profile.OS_PROFILES._origins)
    try:
        yield
    finally:
        os_profile.OS_PROFILES._entries.clear()
        os_profile.OS_PROFILES._entries.update(saved)
        os_profile.OS_PROFILES._origins.clear()
        os_profile.OS_PROFILES._origins.update(saved_origins)


class TestOsProfilesParsing:
    """Tests for ``[os_profiles]`` parsing in ``Repo.parse_settings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path, restore_profiles):
        sut = _write_repo(tmp_path, "")
        repo = Repo(sut_dir=sut)
        assert repo.os_profiles == {}

    def test_profile_parsed_and_registered(self, tmp_path, restore_profiles):
        from otto.host.os_profile import build_os_profile

        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.zephyr-3_7]
            base = "embedded"
            os_name = "Zephyr"
            os_version = "3.7"
            command_frame = "zephyr"
            filesystem = "fat-ram"
            max_filename_len = 32
        """),
        )
        repo = Repo(sut_dir=sut)
        assert "zephyr-3_7" in repo.os_profiles
        # Registered globally so lab data can select it by name.
        prof = build_os_profile("zephyr-3_7")
        assert prof.base == "embedded"
        assert prof.defaults["os_version"] == "3.7"
        assert prof.defaults["max_filename_len"] == 32
        # The ``base`` key is consumed, not kept as a default field.
        assert "base" not in prof.defaults

    def test_missing_base_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            os_name = "Zephyr"
        """),
        )
        # pydantic.ValidationError (a ValueError subclass) now fires for the
        # missing required 'base' field; the error location names the field.
        with pytest.raises(ValueError, match=r"os_profiles\.broken\.base"):
            Repo(sut_dir=sut)

    def test_invalid_base_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            base = "windows"
        """),
        )
        # _register_os_profiles wraps register_os_profile's rejection of an
        # unregistered base host class.
        with pytest.raises(ValueError, match="base must name a registered host class"):
            Repo(sut_dir=sut)

    def test_unknown_default_field_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            base = "unix"
            osTyp = "unix"
        """),
        )
        with pytest.raises(ValueError, match="unknown default field"):
            Repo(sut_dir=sut)

    def test_sutdir_expansion_in_profile_default(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.nix]
            base = "unix"

            [os_profiles.nix.ssh_options]
            known_hosts = "${sut_dir}/known_hosts"
        """),
        )
        repo = Repo(sut_dir=sut)
        prof = repo.os_profiles["nix"]
        assert prof.defaults["ssh_options"]["known_hosts"] == f"{sut}/known_hosts"


class TestCollectTestsHardening:
    def _make_repo(self, tmp_path, test_body="def test_ok():\n    assert True\n"):
        from otto.configmodule.repo import Repo

        sut = tmp_path / "sut"
        (sut / ".otto").mkdir(parents=True)
        (sut / ".otto" / "settings.toml").write_text(
            'name = "sut"\nversion = "1.0.0"\ntests = ["${sut_dir}/tests"]\n'
        )
        (sut / "tests").mkdir()
        (sut / "tests" / "test_a.py").write_text(test_body)
        return Repo(sut_dir=sut)

    def test_collects_with_fileno_dependent_conftest_and_pytest_asyncio(self, tmp_path):
        import pytest_asyncio  # noqa: F401 — reproduce the parent-import precondition (bug A)

        repo = self._make_repo(tmp_path)
        # A conftest that needs a real stdout fd (reproduces bug B under StringIO).
        (repo.sut_dir / "tests" / "conftest.py").write_text(
            "import faulthandler, signal, sys\n"
            "def pytest_configure(config):\n"
            "    faulthandler.register(signal.SIGUSR1, file=sys.stderr)\n"
        )
        items = repo.collect_tests()
        assert len(items) == 1
        assert items[0].name == "test_ok"

    def test_collection_failure_is_logged_not_silent(self, tmp_path, caplog):
        import logging

        repo = self._make_repo(tmp_path)
        # A conftest that raises at collection time -> pytest INTERNAL/usage error.
        (repo.sut_dir / "tests" / "conftest.py").write_text("raise RuntimeError('boom')\n")
        with caplog.at_level(logging.ERROR):
            repo.collect_tests()
        assert any("collection failed" in r.message.lower() for r in caplog.records)

    def test_markers_and_tests_selectors_narrow_results(self, tmp_path):
        body = (
            "import pytest\n"
            "def test_keep():\n    assert True\n"
            "@pytest.mark.slow\ndef test_slow():\n    assert True\n"
        )
        repo = self._make_repo(tmp_path, test_body=body)
        (repo.sut_dir / "tests" / "conftest.py").write_text(
            "def pytest_configure(config):\n    config.addinivalue_line('markers','slow: x')\n"
        )
        all_names = {t.name for t in repo.collect_tests()}
        slow_names = {t.name for t in repo.collect_tests(markers="slow")}
        kw_names = {t.name for t in repo.collect_tests(tests="test_keep")}
        assert {"test_keep", "test_slow"} <= all_names
        assert slow_names == {"test_slow"}
        assert kw_names == {"test_keep"}

    def test_unknown_suite_logs_warning(self, tmp_path, caplog):
        import logging

        repo = self._make_repo(tmp_path)
        with caplog.at_level(logging.WARNING):
            repo.collect_tests(suite="no_such_suite")
        assert any(
            "no_such_suite" in r.message and "not found in the registry" in r.message
            for r in caplog.records
        )


class TestOsProfilesIntegration:
    """End-to-end: the repo1 fixture's ``[os_profiles]`` tables flow through
    settings parse → registry → factory, including a data-defined profile that
    references a *code-registered* command frame.
    """

    def test_repo1_profile_resolves_code_registered_frame(self, restore_profiles):
        import sys

        from otto.host.embedded_filesystem import FatRamFileSystem
        from otto.host.embedded_host import EmbeddedHost
        from otto.storage.factory import create_host_from_dict

        # Constructing the repo parses settings, registering the data profiles.
        repo = MockRepo(tests_root / "repo1")
        assert {"zephyr-3.7", "zephyr-2.7", "zephyr-4.4"} <= set(repo.os_profiles)

        # Importing the init modules registers the `zephyr-inline` frame the
        # 2.7 profile names — this runs *after* parse, mirroring bootstrap order.
        pylib = str(repo.sut_dir / "pylib")
        added = pylib not in sys.path
        repo.add_libs_to_pythonpath()
        try:
            repo.import_init_modules()

            # A host need only declare its identity + filesystem; the profile
            # supplies the rest (the copy-paste this feature eliminates).
            host = create_host_from_dict(
                {
                    "ip": "192.0.2.13",
                    "element": "sprout27demo",
                    "os_type": "zephyr-2.7",
                    "filesystem": "fat-ram",
                }
            )
        finally:
            if added:
                while pylib in sys.path:
                    sys.path.remove(pylib)

        assert isinstance(host, EmbeddedHost)
        assert host.os_type == "zephyr-2.7"  # the profile selector is recorded
        assert host.os_name == "Zephyr"
        assert host.os_version == "2.7"
        assert host.max_filename_len == 32
        # The data profile resolved a frame that only code registered:
        assert type(host.command_frame).__name__ == "ZephyrInlineRetcodeFrame"
        # filesystem stays per-host:
        assert isinstance(host.filesystem, FatRamFileSystem)
