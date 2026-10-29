"""Unit tests for ``otto.coverage.collect`` — the composed collection workflow.

These exercise the single canonical fetch/metadata/capture implementation
(:func:`otto.coverage.collect.collect_coverage`), which calls the one clean
path (:func:`otto.coverage.collect.clean_coverage`, exercised directly in
``test_clean.py``) after a successful collection. Moved here (a library
extraction) from ``tests/unit/cli/test_test.py``'s coverage-collection suites
and adapted to the new fail-loud contract: ``collect_coverage`` never
swallows — the "collected nothing", ambiguous-tier, and non-git cases now
*raise*, and the never-fail-a-successful-run swallow policy lives one layer up
in :func:`otto.suite.run._post_run_coverage` (see ``TestPostRunSwallowPolicy``).
"""

import asyncio
import json
import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from otto.coverage.capture.produce import ProduceResult, ProductCapture
from otto.coverage.collect import CollectResult, collect_coverage
from otto.coverage.fetcher.failures import FetchFailure
from otto.coverage.reports import CleanReport
from otto.coverage.tree import ProductKey
from otto.host.element import Element
from otto.result import Result
from otto.utils import Status
from tests._fixtures.gitrepo import git_env


def _product(name: str, cov_dir: str | None = None, verdict: bool | None = True) -> MagicMock:
    """A product double for the instrumentation walk (mirrors ``test_fetcher``'s)."""
    p = MagicMock()
    p.name = name
    p.cov_dir = cov_dir
    p.instrumented = MagicMock(return_value=verdict)
    return p


def _empty_fetcher() -> MagicMock:
    """A ``GcdaFetcher`` double that fetches nothing."""
    fetcher = MagicMock()
    fetcher.fetch_all = AsyncMock(return_value={})
    return fetcher


@pytest.fixture(autouse=True)
def _gcc13_system_gcov(monkeypatch):
    """Pin the system gcov to gcc 13, the major the fake counters carry.

    The capture step resolves each product's gcov from its ``.gcda`` stamp;
    this keeps it off the real ``gcov --version`` probe and on the default tool.
    """
    monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", lambda gcov="gcov": 13)


@pytest.fixture(autouse=True)
def _stub_post_fetch_clean(monkeypatch):
    """Stub the post-fetch clean path for every test in this module.

    ``collect_coverage``'s own post-fetch ``clean_coverage`` call is exercised
    directly (and its real host walk tested) in ``test_clean.py``; these
    fetch/metadata/capture tests only need it to not reach out to real hosts.
    ``TestCleanAfterFetch`` overrides this with its own patch to inspect the
    call.
    """
    mock = AsyncMock(return_value=CleanReport(hosts={}))
    monkeypatch.setattr("otto.coverage.collect.clean_coverage", mock)
    return mock


def _counters(product_dir: Path, notes_root: Path) -> None:
    """Write one ``x.gcda`` under *product_dir* and its matching notes under *notes_root*."""
    product_dir.mkdir(parents=True, exist_ok=True)
    (product_dir / "x.gcda").write_bytes(b"adcg*33B\x01\x00\x00\x00")
    (notes_root / "x.gcno").write_bytes(b"oncg*33B\x01\x00\x00\x00")


HEAD = "c0ffee" * 6 + "abcd"
"""The commit a stubbed ``head_commit`` answers for a project root that is no git checkout."""


@pytest.fixture
def fake_head(monkeypatch):
    """Answer :data:`HEAD` for the collected-at commit, for tests whose root is no git checkout."""
    monkeypatch.setattr("otto.coverage.capture.gitio.head_commit", lambda root: HEAD)


@pytest.fixture
def project_root(tmp_path):
    """A real tmp_path git repository standing in for the project checkout."""
    import subprocess

    root = tmp_path / "sut"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            env=git_env(tmp_path),
        )

    git("init", "-q")
    (root / "f.c").write_text("int a;\nint b;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return root


# ── No [coverage] config → fail loud ─────────────────────────────────────────


class TestNoCoverageConfig:
    """A tree with no ``[coverage]`` section fails loud (ValueError)."""

    def test_no_coverage_config_raises_valueerror(self, tmp_path):
        project = MagicMock()
        project.settings = {}  # no [coverage] section
        with pytest.raises(ValueError, match=r"\[coverage\]"):
            asyncio.run(collect_coverage(tmp_path, projects=[project]))


class TestNamedExceptions:
    """The two ``collect_coverage`` fail-loud sites raise named ``ValueError``
    subclasses (:class:`~otto.config.coverage_settings.CoverageConfigError` and
    :class:`~otto.coverage.errors.NoCoverageDataError`) with the exact same
    message text as before — CLI ``except ValueError`` handling stays
    unmodified since both subclass ``ValueError``."""

    def test_no_coverage_config_raises_coverage_config_error(self, tmp_path):
        from otto.config.coverage_settings import CoverageConfigError, missing_cov_config_error

        project = MagicMock()
        project.root = tmp_path
        project.settings = {}
        with pytest.raises(CoverageConfigError) as excinfo:
            asyncio.run(collect_coverage(tmp_path, projects=[project]))
        assert isinstance(excinfo.value, ValueError)
        assert str(excinfo.value) == str(missing_cov_config_error([project]))
        assert str(tmp_path / ".otto" / "settings.toml") in str(excinfo.value)

    def test_no_gcda_data_raises_no_coverage_data_error(self, tmp_path):
        from otto.coverage.errors import NoCoverageDataError
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"

        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [_product("app", "/var/cov/app")]

        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(NoCoverageDataError) as excinfo,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))
        assert isinstance(excinfo.value, ValueError)
        assert str(excinfo.value) == (
            "no .gcda counters retrieved from any product (searched: test1:app:/var/cov/app)"
        )

    def test_errors_importable_from_coverage_package(self):
        from otto.config.coverage_settings import CoverageConfigError
        from otto.coverage import NoCoverageDataError

        assert issubclass(CoverageConfigError, ValueError)
        assert issubclass(NoCoverageDataError, ValueError)


# ── Fetch destination + fail-loud "collected nothing" ────────────────────────


class TestFetchStage:
    """The fetch constructs its GcdaFetcher at the given ``cov_dir`` (staging per
    ``(host, product)``), and a run that retrieves no ``.gcda`` from any product
    fails loud naming every ``host:product:cov_dir`` triple it searched."""

    def test_fetcher_uses_given_cov_dir_and_empty_fails_loud(self, tmp_path):
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"

        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [_product("app", "/var/cov/app")]

        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch(
                "otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()
            ) as fetcher_cls,
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(ValueError, match=r"no \.gcda counters retrieved from any product"),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        # The fetcher stages into the exact cov_dir it was handed (no override
        # logic — the caller resolves the destination now), carrying the
        # project-declared host selector so its own host walk matches ours.
        fetcher_cls.assert_called_once()
        assert fetcher_cls.call_args.args == (cov_dir,)
        assert fetcher_cls.call_args.kwargs["pattern"].pattern == ".*"

    def test_empty_message_names_every_product_searched(self, tmp_path):
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"

        h1 = MagicMock(spec=UnixHost)
        h1.id = "test1"
        h1.products = [_product("app", "/var/cov/app")]
        h2 = MagicMock(spec=UnixHost)
        h2.id = "test2"
        h2.products = [_product("agent")]

        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[h1, h2]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(
                ValueError, match=r"searched: test1:app:/var/cov/app, test2:agent:/tmp/agent"
            ),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

    def test_an_uninstrumented_product_is_not_searched(self, tmp_path):
        """Only instrumented products reach the message — the fetcher skips the rest."""
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"

        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [_product("app", "/var/cov/app"), _product("dbg", verdict=False)]

        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(ValueError, match=r"searched: test1:app:/var/cov/app\)") as excinfo,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))
        assert "dbg" not in str(excinfo.value)

    def test_the_runner_is_never_named_as_searched(self, tmp_path):
        """No stage looks at the local host — neither the network fetch nor the
        console dump — so the message must not claim it was searched."""
        from otto.host import UnixHost
        from otto.host.local_host import LocalHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"

        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [_product("app", "/var/cov/app")]
        runner = MagicMock(spec=LocalHost)
        runner.id = "local"
        runner.products = [_product("app", "/var/cov/app")]

        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host, runner]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(ValueError, match=r"no \.gcda counters") as excinfo,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))
        assert str(excinfo.value) == (
            "no .gcda counters retrieved from any product (searched: test1:app:/var/cov/app)"
        )


class TestProductsOnly:
    """The "nothing came back" message distinguishes *searched and found
    nothing* from *there was nothing to search*."""

    def test_no_data_error_names_every_host_product_cov_dir_triple(self, tmp_path):
        from otto.coverage.errors import NoCoverageDataError
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"
        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [_product("app", "/var/cov/app"), _product("agent")]
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(NoCoverageDataError) as excinfo,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))
        assert str(excinfo.value) == (
            "no .gcda counters retrieved from any product (searched: "
            "test1:app:/var/cov/app, test1:agent:/tmp/agent)"
        )

    def test_an_embedded_product_is_searched_over_the_console_not_a_path(self, tmp_path):
        """A board has no filesystem: its products dump over the serial console.

        Printing ``cov_dir_of`` there names ``/tmp/<product>``, a path that
        exists on no board — the reader goes looking for a directory instead of
        at the console transcript.
        """
        from otto.coverage.errors import NoCoverageDataError
        from otto.host.embedded_host import EmbeddedHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"
        board = MagicMock(spec=EmbeddedHost)
        board.id = "bench-1"
        board.products = [_product("blink")]
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[board]),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(NoCoverageDataError) as excinfo,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))
        assert str(excinfo.value) == (
            "no .gcda counters retrieved from any product (searched: bench-1:blink:console)"
        )
        assert "/tmp/blink" not in str(excinfo.value)

    def test_a_host_with_no_products_is_named_in_the_no_data_error(self, tmp_path):
        from otto.coverage.errors import NoCoverageDataError
        from otto.host import UnixHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.root = tmp_path
        project.name = "proj"
        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = []
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            pytest.raises(NoCoverageDataError) as excinfo,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))
        assert str(excinfo.value) == (
            "no .gcda counters retrieved from any product "
            "(no instrumented products on any host: test1)"
        )


# ── clean_after_fetch — post-fetch remote-clean toggle ───────────────────────


class TestCleanAfterFetch:
    """``clean_after_fetch`` gates whether ``collect_coverage`` calls the one
    clean path (:func:`~otto.coverage.collect.clean_coverage`) after
    production. Default ``True`` preserves ``otto test --cov`` semantics (zero
    the counters of exactly the pairs that captured, so a product that failed
    keeps its counters); ``False`` skips that internal clean so a caller
    (``otto cov get``) can own the post-fetch clean itself. Either way the
    fetch itself is unchanged."""

    APP = ProductKey("test1", "app")
    LIB = ProductKey("test1", "lib")

    def _run(self, cov_dir, produce_mock, clean_mock, *, clean_after_fetch=True):
        """Collect ``test1``'s two fetched products, ``produce_mock`` standing in for capture."""
        from otto.host import UnixHost

        project = MagicMock()
        project.root = cov_dir.parent
        project.name = "proj"

        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [_product("app", "/var/cov/app"), _product("lib", "/var/cov/lib")]

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_all = AsyncMock(
            return_value={
                ProductKey("test1", "app"): cov_dir / "test1" / "app",
                ProductKey("test1", "lib"): cov_dir / "test1" / "lib",
            }
        )
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher_instance),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            patch("otto.coverage.collect.clean_coverage", clean_mock),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch("otto.coverage.collect._write_metadata", new=AsyncMock()),
            patch("otto.coverage.capture.gitio.head_commit", return_value=HEAD),
            patch("otto.coverage.capture.produce.produce_captures", new=produce_mock),
        ):
            result = asyncio.run(
                collect_coverage(cov_dir, projects=[project], clean_after_fetch=clean_after_fetch)
            )
        return result, project

    def _outcome(self, cov_dir, *, lib_fails=False):
        """``test1/app`` captured; ``test1/lib`` failed when *lib_fails*, else left out."""
        app = ProductCapture(self.APP, cov_dir / "test1" / "app" / "capture.json", None)
        lib = ProductCapture(self.LIB, None, "lcov --capture failed")
        return ProduceResult([app, lib] if lib_fails else [app])

    def test_true_default_cleans_only_the_pairs_that_captured(self, tmp_path):
        cov_dir = tmp_path / "cov"
        clean_mock = AsyncMock(return_value=CleanReport(hosts={}))
        produce_mock = AsyncMock(return_value=self._outcome(cov_dir))
        result, project = self._run(cov_dir, produce_mock, clean_mock)  # default True
        # Scoped to the captured pair (test1/lib fetched but did not capture),
        # through the same project list collect_coverage itself resolved.
        clean_mock.assert_awaited_once_with([project], pairs=[self.APP])
        assert set(result.product_dirs) == {("test1", "app"), ("test1", "lib")}
        assert result.produce.captured == [self.APP]
        assert result.clean == CleanReport(hosts={})

    def test_nothing_captured_cleans_nothing(self, tmp_path):
        clean_mock = AsyncMock(return_value=CleanReport(hosts={}))
        produce_mock = AsyncMock(return_value=ProduceResult([]))
        result, _project = self._run(tmp_path / "cov", produce_mock, clean_mock)
        clean_mock.assert_not_awaited()
        assert result.clean is None

    def test_false_skips_the_internal_clean(self, tmp_path):
        cov_dir = tmp_path / "cov"
        clean_mock = AsyncMock(return_value=CleanReport(hosts={}))
        produce_mock = AsyncMock(return_value=self._outcome(cov_dir))
        result, _project = self._run(cov_dir, produce_mock, clean_mock, clean_after_fetch=False)
        clean_mock.assert_not_awaited()
        # The fetch still happened and its dirs are reported unchanged.
        assert set(result.product_dirs) == {("test1", "app"), ("test1", "lib")}
        assert result.clean is None

    def test_a_capture_failure_is_raised_after_the_captured_pairs_are_cleaned(self, tmp_path):
        from otto.coverage.errors import CoverageCaptureError

        cov_dir = tmp_path / "cov"
        report = CleanReport(hosts={"test1": {"app": Result(Status.Success)}})
        clean_mock = AsyncMock(return_value=report)
        failure = CoverageCaptureError(self._outcome(cov_dir, lib_fails=True))
        produce_mock = AsyncMock(side_effect=failure)
        with pytest.raises(CoverageCaptureError) as excinfo:
            self._run(cov_dir, produce_mock, clean_mock)
        assert excinfo.value is failure
        clean_mock.assert_awaited_once_with(ANY, pairs=[self.APP])
        # The clean's report rides on the error, for a caller that fails on it.
        assert excinfo.value.clean is report

    def test_a_clean_that_raises_does_not_hide_the_pending_capture_failure(self, tmp_path, caplog):
        from otto.coverage.errors import CoverageCaptureError, NoCoverageHostsError

        cov_dir = tmp_path / "cov"
        clean_mock = AsyncMock(side_effect=NoCoverageHostsError("nothing to clean"))
        failure = CoverageCaptureError(self._outcome(cov_dir, lib_fails=True))
        produce_mock = AsyncMock(side_effect=failure)
        with caplog.at_level("ERROR"), pytest.raises(NoCoverageHostsError):
            self._run(cov_dir, produce_mock, clean_mock)
        logged = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert len(logged) == 1
        assert "test1/lib" in logged[0]
        assert "lcov --capture failed" in logged[0]


# ── Embedded collection + metadata sidecar (moved from TestRunCoverageEmbedded)


@pytest.mark.usefixtures("fake_head")
class TestCollectEmbedded:
    """``collect_coverage`` collects embedded boards' products, and its
    ``.otto_cov_meta.json`` sidecar behaves exactly as the old coverage-metadata
    writer did. Where a ``[coverage]`` project resolves (so the capture tail would
    fire), ``produce_captures`` is stubbed — these tests pin metadata, not
    capture production."""

    def test_collects_embedded_when_only_embedded_configured(self, tmp_path):
        project = MagicMock()
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        staged = cov_dir / "zephyr37-fat" / "cov_ext"
        embedded_collect = AsyncMock(return_value={ProductKey("zephyr37-fat", "cov_ext"): staged})
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"embedded": {}}),
            patch("otto.fleet.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=None),
        ):
            result = asyncio.run(collect_coverage(cov_dir, projects=[project]))

        embedded_collect.assert_awaited_once()
        assert isinstance(result, CollectResult)
        assert result.product_dirs == {("zephyr37-fat", "cov_ext"): staged}
        # No [coverage] project → no captures produced.
        assert result.captures_written == []

    def test_unix_hop_host_not_treated_as_coverage_target(self, tmp_path):
        """A Unix SSH hop in the lab must not pollute the embedded meta.

        An embedded coverage lab must include the SSH hop (e.g. ``test4``
        fronting ``zephyr37_llext``) so the hop resolves — but the hop is
        infrastructure, not a coverage target, and emits no ``.gcda``. The meta
        must therefore (a) keep ``project_root`` = the embedded build dir (the hop must
        not flip it to the project dir, which breaks ``.gcno`` discovery and made
        ``geninfo`` skip the file on the real lab) and (b) carry only the embedded
        host's toolchain, not the hop's. Regression for the test4-hop report bug.
        """
        from otto.host import UnixHost
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build" / "cov_ext_app"
        build_dir.mkdir(parents=True)

        project = MagicMock()
        project.name = "project3"
        project.root = tmp_path / "project3"  # NOT what the meta project_root should resolve to

        hop = MagicMock(spec=UnixHost)
        hop.id = "test4"  # a Unix hop, produces no coverage
        hop.products = []

        zephyr37_llext = ZephyrHost(
            ip="192.0.2.33",
            element=Element("zephyr37_llext"),
            transfer="console",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )

        embedded_collect = AsyncMock(
            return_value={
                ProductKey("zephyr37-llext", "cov_ext"): cov_dir / "zephyr37-llext" / "cov_ext"
            }
        )
        cov_config = {"embedded": {"build_dir": str(build_dir)}}
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[hop, zephyr37_llext]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=_empty_fetcher()),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch(
                "otto.coverage.capture.produce.produce_captures",
                new=AsyncMock(return_value=ProduceResult([])),
            ),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["project_root"] == str(build_dir.resolve())
        # element "zephyr37_llext" slugs to the id "zephyr37-llext" (underscore -> hyphen).
        assert set(meta["toolchains"]) == {"zephyr37-llext"}
        assert "test4" not in meta["toolchains"]

    def test_embedded_toolchain_is_per_host(self, tmp_path):
        """Each embedded host's coverage toolchain comes from host.toolchain."""
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        host = ZephyrHost(
            ip="192.0.2.33",
            element=Element("zephyr37_llext"),
            transfer="console",
            toolchain=Toolchain(
                sysroot=Path("/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build"
        (build_dir / "zephyr").mkdir(parents=True)

        project = MagicMock()
        project.name = "project3"
        project.root = tmp_path / "project3"

        embedded_collect = AsyncMock(
            return_value={
                ProductKey("zephyr37-llext", "cov_ext"): cov_dir / "zephyr37-llext" / "cov_ext"
            }
        )
        cov_config = {"embedded": {"build_dir": str(build_dir)}}
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch(
                "otto.coverage.capture.produce.produce_captures",
                new=AsyncMock(return_value=ProduceResult([])),
            ),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        # element "zephyr37_llext" slugs to the id "zephyr37-llext" (underscore -> hyphen).
        entry = meta["toolchains"]["zephyr37-llext"]
        assert entry["gcov"] == "bin/arm-zephyr-eabi-gcov"
        assert entry["sysroot"] == "/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi"
        assert entry["lcov"] == "/usr/bin/lcov"

    def test_embedded_toolchain_falls_back_to_gcno_discovery(self, tmp_path):
        """A host left at the default Toolchain() resolves via .gcno discovery."""
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        host = ZephyrHost(ip="192.0.2.33", element=Element("zephyr37_llext"), transfer="console")
        # No toolchain configured -> default Toolchain() -> discovery fallback.
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build_dir = tmp_path / "build"
        build_dir.mkdir()

        project = MagicMock()
        project.name = "project3"
        project.root = tmp_path / "project3"

        embedded_collect = AsyncMock(
            return_value={
                ProductKey("zephyr37-llext", "cov_ext"): cov_dir / "zephyr37-llext" / "cov_ext"
            }
        )
        cov_config = {"embedded": {"build_dir": str(build_dir)}}

        discovered = Toolchain(
            sysroot=Path("/discovered"),
            gcov=Path("bin/x-gcov"),
            lcov=Path("/usr/bin/lcov"),
        )

        def _fake_discover(build_dir_arg):
            return discovered

        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch("otto.host.toolchain_discovery.discover_toolchain_from_gcno", new=_fake_discover),
            patch(
                "otto.coverage.capture.produce.produce_captures",
                new=AsyncMock(return_value=ProduceResult([])),
            ),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        # element "zephyr37_llext" slugs to the id "zephyr37-llext"; the collect result is
        # keyed by that id, so the host resolves and its toolchain is recorded.
        assert meta["toolchains"]["zephyr37-llext"]["gcov"] == "bin/x-gcov"
        assert meta["toolchains"]["zephyr37-llext"]["sysroot"] == "/discovered"

    def test_coverage_hosts_regex_passed_to_both_selectors(self, tmp_path):
        """``[coverage].hosts`` compiles to a regex handed to the Unix and
        embedded host selectors, so the collect-from set is project-declared
        rather than inferred from which hosts happened to emit ``.gcda``.
        """
        project = MagicMock()
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        all_hosts_mock = MagicMock(return_value=[])
        embedded_collect = AsyncMock(return_value={})
        with (
            patch(
                "otto.config.coverage_settings.get_cov_config",
                return_value={"hosts": "zephyr37_llext", "embedded": {}},
            ),
            patch("otto.fleet.all_hosts", new=all_hosts_mock),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=None),
            # No host produced .gcda → fail loud (the selectors still ran).
            pytest.raises(ValueError, match=r"no \.gcda counters"),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        unix_pat = all_hosts_mock.call_args.kwargs.get("pattern")
        assert unix_pat is not None
        assert unix_pat.search("zephyr37_llext")
        assert not unix_pat.search("test4")

        emb_pat = embedded_collect.await_args.kwargs.get("pattern")
        assert emb_pat is not None
        assert emb_pat.pattern == "zephyr37_llext"

    def test_unset_coverage_hosts_passes_no_pattern(self, tmp_path):
        """Unset ``[coverage].hosts`` → ``pattern=None`` (collect from all hosts)."""
        project = MagicMock()
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        all_hosts_mock = MagicMock(return_value=[])
        embedded_collect = AsyncMock(return_value={})
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"embedded": {}}),
            patch("otto.fleet.all_hosts", new=all_hosts_mock),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=None),
            pytest.raises(ValueError, match=r"no \.gcda counters"),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        assert all_hosts_mock.call_args.kwargs.get("pattern") is None
        assert embedded_collect.await_args.kwargs.get("pattern") is None

    def test_per_version_source_roots_recorded(self, tmp_path):
        """Two embedded hosts of different os_version each record their own build_dir
        as a per-host source root in the meta (multi-Zephyr-version coverage).
        """
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        build37 = tmp_path / "build" / "v3_7"
        build37.mkdir(parents=True)
        build44 = tmp_path / "build" / "v4_4"
        build44.mkdir(parents=True)

        project = MagicMock()
        project.name = "project3"
        project.root = tmp_path / "project3"

        zephyr37_fat = ZephyrHost(
            ip="192.0.2.33",
            element=Element("zephyr37_fat"),
            transfer="console",
            os_version="3.7",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk37/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )
        zephyr44_fat = ZephyrHost(
            ip="192.0.2.34",
            element=Element("zephyr44_fat"),
            transfer="console",
            os_version="4.4",
            toolchain=Toolchain(
                sysroot=Path("/opt/sdk44/gnu/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )

        embedded_collect = AsyncMock(
            return_value={
                ("zephyr37-fat", "cov_ext"): cov_dir / "zephyr37-fat" / "cov_ext",
                ("zephyr44-fat", "cov_ext"): cov_dir / "zephyr44-fat" / "cov_ext",
            }
        )
        cov_config = {
            "embedded": {
                "builds": {
                    "3.7": {"build_dir": str(build37)},
                    "4.4": {"build_dir": str(build44)},
                },
            },
        }
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[zephyr37_fat, zephyr44_fat]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch(
                "otto.coverage.capture.produce.produce_captures",
                new=AsyncMock(return_value=ProduceResult([])),
            ),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["source_roots"]["zephyr37-fat"] == str(build37.resolve())
        assert meta["source_roots"]["zephyr44-fat"] == str(build44.resolve())


@pytest.mark.usefixtures("fake_head")
class TestFetchedToolchainMetadata:
    """``.otto_cov_meta.json`` records a toolchain per *fetched* host only when
    one was configured: the reporter reads a host left at the default with
    the gcov its data's own stamp names, and recording the default would say
    "the runner's gcov" about counters another gcc, clang, or a container's
    image may have written."""

    @staticmethod
    def _collect(tmp_path, hosts):
        from otto.host import UnixHost
        from otto.host.docker_host import DockerContainerHost

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        project = MagicMock()
        project.name = "project1"
        project.root = tmp_path / "project1"

        doubles = []
        fetched = {}
        for host_id, kind, toolchain in hosts:
            host = MagicMock(spec=UnixHost if kind == "unix" else DockerContainerHost)
            host.id = host_id
            host.products = [_product("app", "/var/cov/app")]
            host.toolchain = toolchain
            doubles.append(host)
            fetched[ProductKey(host_id, "app")] = cov_dir / host_id / "app"

        fetcher = MagicMock()
        fetcher.fetch_all = AsyncMock(return_value=fetched)

        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=doubles),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch(
                "otto.coverage.capture.produce.produce_captures",
                new=AsyncMock(return_value=ProduceResult([])),
            ),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        return json.loads((cov_dir / ".otto_cov_meta.json").read_text())["toolchains"]

    def test_hosts_at_the_default_toolchain_get_no_entry(self, tmp_path):
        from otto.host.toolchain import Toolchain

        # A container answers `.toolchain` too — it inherits BaseHost's default
        # field — so the value rule covers it without a kind check.
        toolchains = self._collect(
            tmp_path,
            [("test1", "unix", Toolchain()), ("test3.project1.api", "container", Toolchain())],
        )

        assert toolchains == {}

    def test_a_configured_gcov_is_recorded(self, tmp_path):
        from otto.host.toolchain import Toolchain

        toolchains = self._collect(
            tmp_path,
            [
                ("test1", "unix", Toolchain(gcov=Path("/usr/bin/gcov-12"))),
                ("test2", "unix", Toolchain()),
            ],
        )

        assert toolchains == {
            "test1": {"sysroot": "/", "lcov": "usr/bin/lcov", "gcov": "/usr/bin/gcov-12"}
        }

    def test_extra_lcov_arguments_are_recorded_and_an_empty_list_is_not(self, tmp_path):
        """A deferred report builds its lcov commands from this metadata, so a
        host's extra lcov arguments have to be in it; an empty list would be
        noise in every other host's entry (issue #385)."""
        from otto.host.toolchain import Toolchain

        toolchains = self._collect(
            tmp_path,
            [
                ("test1", "unix", Toolchain(lcov_args=["--ignore-errors", "source"])),
                ("test2", "unix", Toolchain(gcov=Path("/usr/bin/gcov-12"))),
            ],
        )

        assert toolchains == {
            "test1": {
                "sysroot": "/",
                "lcov": "usr/bin/lcov",
                "gcov": "usr/bin/gcov",
                "lcov_args": ["--ignore-errors", "source"],
            },
            "test2": {"sysroot": "/", "lcov": "usr/bin/lcov", "gcov": "/usr/bin/gcov-12"},
        }

    def test_a_record_naming_only_an_lcov_is_recorded_as_it_was(self, tmp_path):
        """The reporter treats its gcov as silent; what was configured is kept."""
        from otto.host.toolchain import Toolchain

        toolchains = self._collect(
            tmp_path, [("test1", "unix", Toolchain(lcov=Path("/tmp/lcov-wrapper.sh")))]
        )

        assert toolchains == {
            "test1": {"sysroot": "/", "lcov": "/tmp/lcov-wrapper.sh", "gcov": "usr/bin/gcov"}
        }


@pytest.mark.usefixtures("fake_head")
class TestBuildDirPathAnchoring:
    """``[coverage.embedded] build_dir`` (and the per-version
    ``[coverage.embedded.builds.<ver>] build_dir``) is a passthrough
    ``dict[str, Any]`` value read raw from ``cov_config`` — it never passes
    through the pydantic model's ``ProjectPath``, so it must be anchored by hand
    at the point it's read to obey the documented path-resolution convention
    (docs/configuration/settings.md ``### Path resolution``): ``~`` expansion,
    then project-root anchoring for a still-relative value. Regression coverage
    for the settings-path-anchoring-phase2 gap.
    """

    @staticmethod
    def _zephyr_host(**kwargs):
        from otto.host.embedded_host import ZephyrHost
        from otto.host.toolchain import Toolchain

        kwargs.setdefault("ip", "192.0.2.33")
        kwargs.setdefault("transfer", "console")
        if isinstance(kwargs.get("element"), str):
            kwargs["element"] = Element(kwargs["element"])
        kwargs.setdefault(
            "toolchain",
            Toolchain(
                sysroot=Path("/opt/sdk/arm-zephyr-eabi"),
                gcov=Path("bin/arm-zephyr-eabi-gcov"),
                lcov=Path("/usr/bin/lcov"),
            ),
        )
        return ZephyrHost(**kwargs)

    def _collect(self, tmp_path, project, host, cov_config):
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        embedded_collect = AsyncMock(
            return_value={ProductKey(host.id, "cov_ext"): cov_dir / host.id / "cov_ext"}
        )
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch(
                "otto.coverage.capture.produce.produce_captures",
                new=AsyncMock(return_value=ProduceResult([])),
            ),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))
        return json.loads((cov_dir / ".otto_cov_meta.json").read_text())

    # ── single ``build_dir`` fallback ────────────────────────────────────────

    def test_relative_build_dir_anchors_to_project_root_not_cwd(self, tmp_path, monkeypatch):
        """A bare relative ``build_dir`` resolves under the project root
        (``cov_project.root``), never the process CWD.

        Without the fix, ``Path(build_dir).resolve()`` resolves relative to
        wherever ``otto cov`` happens to be invoked from.
        """
        project_root = tmp_path / "project3"
        (project_root / "build").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        project = MagicMock()
        project.name = "project3"
        project.root = project_root

        host = self._zephyr_host(element="zephyr37_llext")
        cov_config = {"embedded": {"build_dir": "build"}}

        meta = self._collect(tmp_path, project, host, cov_config)
        assert meta["project_root"] == str((project_root / "build").resolve())

    def test_tilde_build_dir_expands_via_home(self, tmp_path, monkeypatch):
        """A ``~``-rooted ``build_dir`` expands against ``HOME``, hermetically
        via a monkeypatched HOME so the test doesn't depend on the real
        user's home directory.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        build_dir = home / "covbuild"
        build_dir.mkdir()

        project = MagicMock()
        project.name = "project3"
        project.root = tmp_path / "project3"

        host = self._zephyr_host(element="zephyr37_llext")
        cov_config = {"embedded": {"build_dir": "~/covbuild"}}

        meta = self._collect(tmp_path, project, host, cov_config)
        assert meta["project_root"] == str(build_dir.resolve())

    # ── per-version ``builds.<ver>.build_dir`` ──────────────────────────────

    def test_per_version_relative_build_dir_anchors_to_project_root_not_cwd(
        self, tmp_path, monkeypatch
    ):
        """The per-version ``builds.<ver>.build_dir`` obeys the same
        anchoring rule as the single fallback: relative values resolve under
        the project root, not the process CWD.
        """
        project_root = tmp_path / "project3"
        (project_root / "build" / "v3_7").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        project = MagicMock()
        project.name = "project3"
        project.root = project_root

        host = self._zephyr_host(element="zephyr37_fat", os_version="3.7")
        cov_config = {"embedded": {"builds": {"3.7": {"build_dir": "build/v3_7"}}}}

        meta = self._collect(tmp_path, project, host, cov_config)
        assert meta["source_roots"]["zephyr37-fat"] == str(
            (project_root / "build" / "v3_7").resolve()
        )

    def test_per_version_tilde_build_dir_expands_via_home(self, tmp_path, monkeypatch):
        """The per-version ``builds.<ver>.build_dir`` expands a ``~`` prefix
        against ``HOME``, hermetically via a monkeypatched HOME.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        build_dir = home / "v3_7"
        build_dir.mkdir()

        project = MagicMock()
        project.name = "project3"
        project.root = tmp_path / "project3"

        host = self._zephyr_host(element="zephyr37_fat", os_version="3.7")
        cov_config = {"embedded": {"builds": {"3.7": {"build_dir": "~/v3_7"}}}}

        meta = self._collect(tmp_path, project, host, cov_config)
        assert meta["source_roots"]["zephyr37-fat"] == str(build_dir.resolve())


# ── Capture tail — fail loud (no swallowing inside collect_coverage) ──────────


class TestCaptureTail:
    """The post-collection capture.json tail runs against the resolved tier and
    returns the produced paths in :class:`CollectResult`. Unlike the old inline
    tail it never swallows — an ambiguous tier or a non-git sut *raise*."""

    def _collect(self, project, cov_dir, cov_config):
        """Drive collect_coverage with one embedded board's counters collected by the fake.

        The counters a test staged under ``cov_dir/board1`` are delivered by
        the mocked collector, as a real one would: left in place they would be
        a reused directory's uncaptured counters, which collection refuses.
        """
        held = cov_dir.parent / "held-board1"
        if (cov_dir / "board1").exists():
            (cov_dir / "board1").rename(held)

        async def deliver(staging_root, pattern=None, *, failures):
            if held.exists():
                held.rename(cov_dir / "board1")
            return {ProductKey("board1", "app"): cov_dir / "board1"}

        embedded_collect = AsyncMock(side_effect=deliver)
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
        ):
            return asyncio.run(collect_coverage(cov_dir, projects=[project]))

    def test_happy_path_writes_pinned_capture(self, tmp_path, project_root, monkeypatch):
        """A well-formed single-tier project leaves behind a real capture.json and
        returns it in CollectResult.captures_written."""
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        _counters(cov_dir / "board1" / "app", project_root)

        async def fake_capture(self, pairs, staging, output, toolchain=None):
            output.write_text(f"TN:\nSF:{project_root / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture_pairs", fake_capture)

        project = MagicMock()
        project.root = project_root
        project.name = "proj"

        # Non-empty (truthy) but no explicit [coverage.tiers] -> implicit
        # single "system" e2e tier.
        result = self._collect(project, cov_dir, {"tiers": {}})

        capture_path = cov_dir / "board1" / "app" / "capture.json"
        assert capture_path.is_file()
        assert json.loads(capture_path.read_text())["tier"] == "system"
        assert result.captures_written == [capture_path]

    def test_metadata_records_the_tier_and_the_commit_collected_at(
        self, tmp_path, project_root, monkeypatch
    ):
        """``.otto_cov_meta.json`` carries the resolved tier and the project's HEAD."""
        import subprocess

        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        _counters(cov_dir / "board1" / "app", project_root)

        async def fake_capture(self, pairs, staging, output, toolchain=None):
            output.write_text(f"TN:\nSF:{project_root / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture_pairs", fake_capture)

        project = MagicMock()
        project.root = project_root
        project.name = "proj"

        self._collect(project, cov_dir, {"tiers": {}})

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            env=git_env(tmp_path),
        ).stdout.strip()
        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
        assert meta["tier"] == "system"
        assert meta["base_commit"] == head
        capture = json.loads((cov_dir / "board1" / "app" / "capture.json").read_text())
        assert capture["base_commit"] == head

    def test_ambiguous_tiers_raise(self, tmp_path, project_root, monkeypatch):
        """Two e2e-kind tiers make ``resolve_get_tier`` raise ``ValueError`` —
        which now propagates out of collect_coverage (no swallow)."""
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        _counters(cov_dir / "board1" / "app", project_root)

        async def fake_capture(self, pairs, staging, output, toolchain=None):
            output.write_text(f"TN:\nSF:{project_root / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture_pairs", fake_capture)

        project = MagicMock()
        project.root = project_root
        project.name = "proj"

        cov_config = {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "nightly": {"kind": "e2e", "precedence": 2},
            }
        }

        with pytest.raises(ValueError, match=r"e2e-kind tiers"):
            self._collect(project, cov_dir, cov_config)

        assert not (cov_dir / "board1" / "app" / "capture.json").exists()

    def test_non_git_project_root_raises(self, tmp_path, monkeypatch):
        """A non-git project root now raises (GitUnavailableError, a RuntimeError)."""
        from otto.coverage.capture import produce as produce_mod
        from otto.coverage.capture.gitio import GitUnavailableError

        cov_dir = tmp_path / "cov"
        notgit = tmp_path / "notgit"
        notgit.mkdir()
        _counters(cov_dir / "board1" / "app", notgit)
        (notgit / "f.c").write_text("int a;\n")

        async def fake_capture(self, pairs, staging, output, toolchain=None):
            output.write_text(f"TN:\nSF:{notgit / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture_pairs", fake_capture)

        project = MagicMock()
        project.root = notgit
        project.name = "proj"

        with pytest.raises(GitUnavailableError):
            self._collect(project, cov_dir, {"tiers": {}})

    @pytest.mark.usefixtures("fake_head")
    def test_products_map_reuses_the_fetched_products_without_rescanning(self, tmp_path):
        """The produce step gets the products that yielded counters, never re-scanning
        an artifact for instrumentation: the fetch already decided that."""
        from otto.host import UnixHost

        project = MagicMock()
        project.root = tmp_path / "sut"
        project.name = "proj"
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        app, idle = _product("app", "/var/cov/app"), _product("idle", "/var/cov/idle")
        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [app, idle]
        fetcher = _empty_fetcher()
        fetcher.fetch_all = AsyncMock(
            return_value={ProductKey("test1", "app"): cov_dir / "test1" / "app"}
        )
        produce_mock = AsyncMock(return_value=ProduceResult([]))
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"tiers": {}}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            patch("otto.coverage.collect._write_metadata", new=AsyncMock()),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch("otto.coverage.capture.produce.produce_captures", new=produce_mock),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        assert produce_mock.await_args.kwargs["products"] == {ProductKey("test1", "app"): app}
        app.instrumented.assert_not_called()
        idle.instrumented.assert_not_called()

    @pytest.mark.usefixtures("fake_head")
    def test_same_named_products_on_two_hosts_keep_their_own_definitions(self, tmp_path):
        """Two hosts carrying different builds under one name each hand produce their own."""
        from otto.host import UnixHost

        project = MagicMock()
        project.root = tmp_path / "sut"
        project.name = "proj"
        cov_dir = tmp_path / "cov"
        hosts, apps = [], []
        for hid in ("test1", "test2"):
            app = _product("app", f"/var/cov/{hid}")
            host = MagicMock(spec=UnixHost)
            host.id = hid
            host.products = [app]
            hosts.append(host)
            apps.append(app)
        fetcher = _empty_fetcher()
        fetcher.fetch_all = AsyncMock(
            return_value={ProductKey(h.id, "app"): cov_dir / h.id / "app" for h in hosts}
        )
        produce_mock = AsyncMock(return_value=ProduceResult([]))
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"tiers": {}}),
            patch("otto.fleet.all_hosts", return_value=hosts),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            patch("otto.coverage.collect._write_metadata", new=AsyncMock()),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch("otto.coverage.capture.produce.produce_captures", new=produce_mock),
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        assert produce_mock.await_args.kwargs["products"] == {
            ProductKey("test1", "app"): apps[0],
            ProductKey("test2", "app"): apps[1],
        }

    @pytest.mark.usefixtures("fake_head")
    def test_annotations_passed_through_to_produce(self, tmp_path):
        """tier/ticket/note/tester/display_names thread through to produce_captures."""
        project = MagicMock()
        project.root = tmp_path / "sut"
        project.name = "proj"
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        produce_mock = AsyncMock(
            return_value=ProduceResult(
                [
                    ProductCapture(
                        ProductKey("board1", "app"), cov_dir / "board1" / "capture.json", None
                    )
                ]
            )
        )
        embedded_collect = AsyncMock(return_value={ProductKey("board1", "app"): cov_dir / "board1"})
        cov_config = {
            "tiers": {
                "manual": {"kind": "manual", "precedence": 1},
                "system": {"kind": "e2e", "precedence": 2},
            }
        }
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch("otto.coverage.capture.produce.produce_captures", new=produce_mock),
        ):
            result = asyncio.run(
                collect_coverage(
                    cov_dir,
                    projects=[project],
                    tier="manual",
                    ticket="JIRA-1",
                    note="hi",
                    tester={"name": "chris"},
                    display_names={"board1": "Board One"},
                )
            )

        kwargs = produce_mock.await_args.kwargs
        assert kwargs["tier"] == "manual"
        assert kwargs["ticket"] == "JIRA-1"
        assert kwargs["note"] == "hi"
        assert kwargs["tester"] == {"name": "chris"}
        assert kwargs["display_names"] == {"board1": "Board One"}
        assert result.captures_written == [cov_dir / "board1" / "capture.json"]


# ── Tier passthrough — a resolved TierConfig object skips re-resolution ──────


@pytest.mark.usefixtures("fake_head")
class TestTierPassthrough:
    """Passing an already-resolved :class:`~otto.coverage.tiers.TierConfig` as
    ``tier=`` is used directly — ``resolve_get_tier`` (and the ``load_tiers``
    call that feeds it) never runs. This is what lets ``otto cov get`` resolve
    the tier exactly once instead of twice (``get_coverage`` resolves; the old
    ``collect_coverage`` re-resolved from the name)."""

    def test_tierconfig_object_skips_resolve_get_tier(self, tmp_path):
        from otto.coverage.tiers import TierConfig

        project = MagicMock()
        project.root = tmp_path / "sut"
        project.name = "proj"
        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()

        tier_obj = TierConfig(name="manual", kind="manual", precedence=1, color="red")
        produce_mock = AsyncMock(
            return_value=ProduceResult(
                [
                    ProductCapture(
                        ProductKey("board1", "app"), cov_dir / "board1" / "capture.json", None
                    )
                ]
            )
        )
        embedded_collect = AsyncMock(return_value={ProductKey("board1", "app"): cov_dir / "board1"})
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"tiers": {}}),
            patch("otto.fleet.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch("otto.coverage.capture.produce.produce_captures", new=produce_mock),
            patch("otto.coverage.tiers.resolve_get_tier") as resolve_mock,
        ):
            result = asyncio.run(collect_coverage(cov_dir, projects=[project], tier=tier_obj))

        resolve_mock.assert_not_called()
        assert produce_mock.await_args.kwargs["tier"] == "manual"
        assert result.captures_written == [cov_dir / "board1" / "capture.json"]


# ── Swallow policy relocated to _post_run_coverage ───────────────────────────


class TestPostRunSwallowPolicy:
    """The never-fail-a-successful-run policy now lives in
    :func:`otto.suite.run._post_run_coverage`: a coverage-collection failure is
    logged and swallowed, leaving raw artifacts on disk. (Moved from the old
    coverage-collection swallow tests.)"""

    def _drive_post_run(self, project, cov_dir, cov_config):
        from otto.suite.run import RunOptions, _post_run_coverage

        embedded_collect = AsyncMock(return_value={ProductKey("board1", "app"): cov_dir / "board1"})
        opts = RunOptions(cov=True, cov_report=False, cov_dir=cov_dir)
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
            patch("otto.fleet.all_hosts", return_value=[]),
            patch("otto.coverage.fetcher.embedded.collect_embedded_coverage", new=embedded_collect),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
        ):
            asyncio.run(_post_run_coverage([project], cov_dir.parent / "log", opts))

    def test_ambiguous_tiers_do_not_fail_the_run(self, tmp_path, project_root, caplog):
        cov_dir = tmp_path / "cov"
        (cov_dir / "board1" / "app").mkdir(parents=True)
        (cov_dir / "board1" / "app" / "x.gcda").write_bytes(b"")

        project = MagicMock()
        project.root = project_root
        project.name = "proj"

        cov_config = {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "nightly": {"kind": "e2e", "precedence": 2},
            }
        }

        with caplog.at_level("WARNING"):
            self._drive_post_run(project, cov_dir, cov_config)

        assert not (cov_dir / "board1" / "app" / "capture.json").exists()
        assert any("Coverage collection failed" in rec.message for rec in caplog.records)

    def test_non_git_project_root_does_not_fail_the_run(self, tmp_path, monkeypatch, caplog):
        from otto.coverage.capture import produce as produce_mod

        cov_dir = tmp_path / "cov"
        notgit = tmp_path / "notgit"
        notgit.mkdir()
        _counters(cov_dir / "board1" / "app", notgit)
        (notgit / "f.c").write_text("int a;\n")

        async def fake_capture(self, pairs, staging, output, toolchain=None):
            output.write_text(f"TN:\nSF:{notgit / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture_pairs", fake_capture)

        project = MagicMock()
        project.root = notgit
        project.name = "proj"

        with caplog.at_level("WARNING"):
            self._drive_post_run(project, cov_dir, {"tiers": {}})

        assert not (cov_dir / "board1" / "app" / "capture.json").exists()
        assert any("Coverage collection failed" in rec.message for rec in caplog.records)


class TestContainerOnlyPattern:
    """A ``[coverage].hosts`` pattern naming only container hosts collects them.

    Unlike every test above, the embedded collector is NOT patched out: the
    real walk runs over the real lab, which is where a container-only
    selection was refused.
    """

    def test_the_containers_counters_are_collected(self, tmp_path, project_root, monkeypatch):
        from otto.context import OttoContext, reset_context, set_context
        from otto.coverage.capture import produce as produce_mod
        from otto.host.docker_host import DockerContainerHost
        from otto.host.toolchain import Toolchain
        from otto.labs.loader import Lab

        container = MagicMock(spec=DockerContainerHost)
        container.id = "test1.r1.api"
        container.name = "test1.r1.api"
        container.products = [_product("app", "/var/cov/app")]
        container.toolchain = Toolchain()
        lab = Lab(name="container_lab")
        lab.hosts[container.id] = container

        cov_dir = tmp_path / "cov"
        cov_dir.mkdir()
        staged = cov_dir / container.id / "app"

        async def fetch_all(*, failures):
            # The fetch delivers the container's one .gcda, as a real one would.
            _counters(staged, project_root)
            return {ProductKey(container.id, "app"): staged}

        async def fake_capture(self, pairs, staging, output, toolchain=None):
            output.write_text(f"TN:\nSF:{project_root / 'f.c'}\nDA:1,3\nend_of_record\n")
            return output

        monkeypatch.setattr(produce_mod.LcovMerger, "capture_pairs", fake_capture)

        fetcher = MagicMock()
        fetcher.fetch_all = AsyncMock(side_effect=fetch_all)
        project = MagicMock()
        project.root = project_root
        project.name = "proj"

        token = set_context(OttoContext(lab=lab))
        try:
            with (
                patch(
                    "otto.config.coverage_settings.get_cov_config",
                    return_value={"hosts": r".*\.r1\.api", "tiers": {}},
                ),
                patch("otto.config.coverage_settings.get_cov_project", return_value=project),
                patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher),
            ):
                result = asyncio.run(collect_coverage(cov_dir, projects=[project]))
        finally:
            reset_context(token)

        assert result.product_dirs == {(container.id, "app"): staged}
        capture_path = staged / "capture.json"
        assert result.captures_written == [capture_path]
        assert json.loads(capture_path.read_text())["tier"] == "system"


# ── A missing reader fails only the target that needs it ───────────────────

# a B11* version and stamp, as a little-endian file stores them
_B11 = b"*11B" + b"\x07\x00\x00\x00"


@contextmanager
def _two_targets(
    base: Path, project_root: Path, monkeypatch, broken: str
) -> Iterator[tuple[Path, MagicMock]]:
    """Stage a healthy gcc 13 product ``test1/app`` and a ``B11*`` target no llvm-cov reads.

    *broken* ``"gcno"``: the ``B11*`` target is embedded board ``board2/fw``, whose
    build directory collection reads for its notes' stamp; ``"gcda"``: it is
    product ``test1/lib``, resolved from its own ``.gcda`` only. Only git is on
    ``PATH``. Yields the ``(cov_dir, project)`` to collect.
    """
    from otto.coverage.capture import produce as produce_mod
    from otto.host import UnixHost
    from otto.host.toolchain import Toolchain

    git = shutil.which("git")
    assert git is not None
    bin_dir = base / "gitonly"
    bin_dir.mkdir(parents=True)
    (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(bin_dir))

    cov_dir = base / "cov"
    build = base / "build"
    build.mkdir()
    (build / "fw.gcno").write_bytes(b"oncg" + _B11)

    app, lib = _product("app", "/var/cov/app"), _product("lib", "/var/cov/lib")
    app.artifact = lib.artifact = None
    host = MagicMock(spec=UnixHost)
    host.id = "test1"
    host.products = [app, lib] if broken == "gcda" else [app]
    host.toolchain = Toolchain()

    async def fetch_all(*, failures):
        _counters(cov_dir / "test1" / "app", project_root)
        got = {ProductKey("test1", "app"): cov_dir / "test1" / "app"}
        if broken == "gcda":
            lib_dir = cov_dir / "test1" / "lib"
            lib_dir.mkdir(parents=True)
            (lib_dir / "fw.gcda").write_bytes(b"adcg" + _B11)
            got[ProductKey("test1", "lib")] = lib_dir
        return got

    async def embedded(staging_root, pattern=None, *, failures):
        if broken != "gcno":
            return {}
        board = cov_dir / "board2" / "fw"
        board.mkdir(parents=True)
        (board / "fw.gcda").write_bytes(b"adcg" + _B11)
        return {ProductKey("board2", "fw"): board}

    async def fake_capture(self, pairs, staging, output, toolchain=None):
        output.write_text(f"TN:\nSF:{project_root / 'f.c'}\nDA:1,3\nend_of_record\n")
        return output

    monkeypatch.setattr(produce_mod.LcovMerger, "capture_pairs", fake_capture)
    fetcher = MagicMock()
    fetcher.fetch_all = AsyncMock(side_effect=fetch_all)
    project = MagicMock()
    project.root = project_root
    project.name = "proj"
    cov_config = {"tiers": {}, "embedded": {"build_dir": str(build)}}
    with (
        patch("otto.config.coverage_settings.get_cov_config", return_value=cov_config),
        patch("otto.fleet.all_hosts", return_value=[host]),
        patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher),
        patch(
            "otto.coverage.fetcher.embedded.collect_embedded_coverage",
            new=AsyncMock(side_effect=embedded),
        ),
        patch("otto.config.coverage_settings.get_cov_project", return_value=project),
    ):
        yield cov_dir, project


_NO_LLVM_COV = (
    "has gcov stamp 'B11*', read by `llvm-cov`, but no `llvm-cov` is on PATH. Install it "
    "(`apt install llvm`), or name the gcov to use in the host's `toolchain.gcov`."
)


class TestAMissingReaderFailsOnlyItsTarget:
    """A reader the counters need that is not installed fails that product or board
    alone (its ``capture.failed`` names the reader); every other product captures.
    The notes an embedded build is read for at collection keep to that, as a
    product's own ``.gcda`` stamp does."""

    @pytest.mark.parametrize("broken", ["gcno", "gcda"])
    def test_the_target_without_its_reader_fails_and_the_rest_captures(
        self, tmp_path, project_root, monkeypatch, broken
    ):
        from otto.coverage.errors import CoverageCaptureError

        with (
            _two_targets(tmp_path, project_root, monkeypatch, broken) as (cov_dir, project),
            pytest.raises(CoverageCaptureError) as info,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        result = info.value.result
        assert result.captured == [ProductKey("test1", "app")]
        assert (cov_dir / "test1" / "app" / "capture.json").is_file()
        [failed] = result.failed
        host, product = ("board2", "fw") if broken == "gcno" else ("test1", "lib")
        assert failed.key == ProductKey(host, product)
        reason = f"Coverage data for product '{product}' on host '{host}' {_NO_LLVM_COV}"
        assert failed.failure == reason
        assert (cov_dir / host / product / "capture.failed").read_text() == f"{reason}\n"

    def test_otto_cov_get_reports_a_build_without_its_reader_as_it_does_a_product(
        self, tmp_path, project_root, monkeypatch
    ):
        """The exit status and message match, target names aside, on both paths."""
        from typer.testing import CliRunner

        from otto.cli import cov as cov_module
        from otto.cli.cov import cov_app

        seen: dict[str, tuple[int, str]] = {}
        for broken, label in (("gcno", "board2/fw"), ("gcda", "test1/lib")):
            base = tmp_path / broken
            with _two_targets(base, project_root, monkeypatch, broken) as (cov_dir, project):

                async def get_coverage(output_dir, *, _cov=cov_dir, _project=project, **_kw):
                    await collect_coverage(_cov, projects=[_project], clean_after_fetch=False)

                with (
                    patch("otto.coverage.get.get_coverage", new=get_coverage),
                    patch.object(cov_module.logger, "error") as logged,
                ):
                    result = CliRunner().invoke(cov_app, ["get"])
            host, product = label.split("/")
            message = (
                logged.call_args.args[0]
                .replace(label, "<target>")
                .replace(f"product '{product}' on host '{host}'", "<subject>")
            )
            seen[broken] = (result.exit_code, message)

        assert seen["gcno"] == seen["gcda"]
        exit_code, message = seen["gcda"]
        assert exit_code == 1
        assert message.endswith(f"<target>:\nCoverage data for <subject> {_NO_LLVM_COV}")


class TestAFailedFetchIsAFailedProduct:
    """A product whose counters could not be fetched fails like one that could not
    be captured: named in one ``CoverageCaptureError``, raised after the products
    that did capture were cleaned, its own counters left on the host."""

    APP = ProductKey("test1", "app")
    LOST = ProductKey("ctr", "wordcount")

    @contextmanager
    def _lab(self, cov_dir, produce_mock, clean_mock, *, app_fetched=True) -> Iterator[MagicMock]:
        """Patch a lab whose fetcher stages ``test1/app`` (unless *app_fetched* is
        false) and fails ``ctr/wordcount``; yields the project to collect."""
        from otto.host import UnixHost

        project = MagicMock()
        project.root = cov_dir.parent
        project.name = "proj"
        host = MagicMock(spec=UnixHost)
        host.id = "test1"
        host.products = [_product("app", "/var/cov/app")]

        async def fetch_all(*, failures):
            failures[self.LOST] = FetchFailure("bound to a different event loop")
            return {ProductKey("test1", "app"): cov_dir / "test1" / "app"} if app_fetched else {}

        fetcher = MagicMock()
        fetcher.fetch_all = AsyncMock(side_effect=fetch_all)
        with (
            patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
            patch("otto.fleet.all_hosts", return_value=[host]),
            patch("otto.coverage.fetcher.remote.GcdaFetcher", return_value=fetcher),
            patch(
                "otto.coverage.fetcher.embedded.collect_embedded_coverage",
                new=AsyncMock(return_value={}),
            ),
            patch("otto.coverage.collect.clean_coverage", clean_mock),
            patch("otto.config.coverage_settings.get_cov_project", return_value=project),
            patch("otto.coverage.collect._write_metadata", new=AsyncMock()),
            patch("otto.coverage.capture.gitio.head_commit", return_value=HEAD),
            patch("otto.coverage.capture.produce.produce_captures", new=produce_mock),
        ):
            yield project

    def _app_captured(self, cov_dir) -> AsyncMock:
        captured = ProductCapture(self.APP, cov_dir / "test1" / "app" / "capture.json", None)
        return AsyncMock(return_value=ProduceResult([captured]))

    @staticmethod
    def _app_cleaned() -> CleanReport:
        return CleanReport(hosts={"test1": {"app": Result(Status.Success)}})

    def test_it_is_raised_after_the_captured_products_are_cleaned(self, tmp_path):
        from otto.coverage.errors import CoverageCaptureError

        cov_dir = tmp_path / "cov"
        report = self._app_cleaned()
        clean_mock = AsyncMock(return_value=report)

        with (
            self._lab(cov_dir, self._app_captured(cov_dir), clean_mock) as project,
            pytest.raises(CoverageCaptureError) as info,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        clean_mock.assert_awaited_once_with(ANY, pairs=[self.APP])
        assert info.value.clean is report
        assert info.value.result.captured == [self.APP]
        [failed] = info.value.result.failed
        assert failed.key == self.LOST
        assert failed.failure == "bound to a different event loop"
        assert not failed.fetched
        assert "ctr/wordcount: fetch failed: bound to a different event loop" in str(info.value)
        assert "failed to fetch or capture" not in str(info.value)

    def test_it_joins_the_capture_failures_in_one_error(self, tmp_path):
        from otto.coverage.errors import CoverageCaptureError

        cov_dir = tmp_path / "cov"
        lib = ProductCapture(ProductKey("test1", "lib"), None, "lcov --capture failed")
        app = ProductCapture(self.APP, cov_dir / "test1" / "app" / "capture.json", None)
        produce_mock = AsyncMock(side_effect=CoverageCaptureError(ProduceResult([app, lib])))
        clean_mock = AsyncMock(return_value=self._app_cleaned())

        with (
            self._lab(cov_dir, produce_mock, clean_mock) as project,
            pytest.raises(CoverageCaptureError) as info,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        failed = {p.key for p in info.value.result.failed}
        assert failed == {ProductKey("test1", "lib"), self.LOST}
        assert "2 product(s) failed to fetch or capture;" in str(info.value)
        clean_mock.assert_awaited_once_with(ANY, pairs=[self.APP])

    def test_a_product_whose_remedy_is_a_declaration_is_not_told_to_rerun(self):
        from otto.coverage.errors import CoverageCaptureError

        undeclared = ProductCapture(
            ProductKey("board", "fw"),
            None,
            "it declares no dump function, so its counters cannot be dumped",
            fetched=False,
            remedy="declare the product's `dump_fn`",
        )
        lost = ProductCapture(self.LOST, None, "bound to a different event loop", fetched=False)

        message = str(CoverageCaptureError(ProduceResult([undeclared, lost])))

        assert message.startswith("2 product(s) could not be fetched; their counters stay on")
        assert (
            "board/fw: fetch failed: it declares no dump function, so its counters cannot be "
            "dumped; declare the product's `dump_fn`"
        ) in message
        assert message.endswith("Fix the cause and run `otto cov get` again for: ctr/wordcount.")

    def test_nothing_fetched_because_everything_failed_names_the_failure(self, tmp_path):
        """Not "no counters anywhere": the reason nothing came down is the failure."""
        from otto.coverage.errors import CoverageCaptureError

        cov_dir = tmp_path / "cov"
        produce_mock, clean_mock = AsyncMock(), AsyncMock()

        with (
            self._lab(cov_dir, produce_mock, clean_mock, app_fetched=False) as project,
            pytest.raises(CoverageCaptureError) as info,
        ):
            asyncio.run(collect_coverage(cov_dir, projects=[project]))

        assert [p.key for p in info.value.result.failed] == [self.LOST]
        produce_mock.assert_not_awaited()
        clean_mock.assert_not_awaited()

    def test_otto_cov_get_exits_1_naming_the_product(self, tmp_path):
        from typer.testing import CliRunner

        from otto.cli import cov as cov_module
        from otto.cli.cov import cov_app

        cov_dir = tmp_path / "cov"
        with self._lab(cov_dir, self._app_captured(cov_dir), AsyncMock()) as project:

            async def get_coverage(output_dir, **_kw):
                await collect_coverage(cov_dir, projects=[project], clean_after_fetch=False)

            with (
                patch("otto.coverage.get.get_coverage", new=get_coverage),
                patch.object(cov_module.logger, "error") as logged,
            ):
                result = CliRunner().invoke(cov_app, ["get"])

        assert result.exit_code == 1
        message = logged.call_args.args[0]
        assert "ctr/wordcount: fetch failed: bound to a different event loop" in message

    def test_otto_test_cov_warns_once_per_product_and_does_not_fail(self, tmp_path, caplog):
        """The run tail's rule for every collection failure: loud, never a failed run."""
        from otto.suite.run import RunOptions, _post_run_coverage

        cov_dir = tmp_path / "cov"
        opts = RunOptions(cov=True, cov_report=False, cov_dir=cov_dir)
        clean_mock = AsyncMock(return_value=self._app_cleaned())

        with (
            self._lab(cov_dir, self._app_captured(cov_dir), clean_mock) as project,
            caplog.at_level("WARNING"),
        ):
            asyncio.run(_post_run_coverage([project], tmp_path / "log", opts))

        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # One line per product not collected; nothing that says "failed to capture".
        assert warned == [
            "Coverage not collected: ctr/wordcount: fetch failed: bound to a different event loop"
        ]
        clean_mock.assert_awaited_once_with(ANY, pairs=[self.APP])
