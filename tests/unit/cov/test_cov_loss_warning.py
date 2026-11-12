"""A ``cov_dir`` the pre-run reset left in place and gone at the fetch is a WARNING (#777 §3).

Through the real functions: the pre-run clean (``_pre_run_cov_clean``) and
the post-run collection (``_post_run_coverage``) over a host double that
runs real ``sh`` on tmp dirs (:mod:`tests._fixtures.shell_host`). Every
"silent" case asserts on the loss warning alone (:func:`_loss_warnings`):
with nothing fetched, the collection also logs its own, separate
"Coverage collection failed" WARNING, which is not this feature's.
"""

import logging
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.coverage.reports import CleanReport
from otto.result import CommandResult, Result
from otto.suite.run import RunOptions, _post_run_coverage, _pre_run_cov_clean
from otto.utils import Status
from tests._fixtures.shell_host import ShellHost, coverage_shell_host, install_shell_lab

_LOSS = "existed at run start and is gone"


def _loss_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and _LOSS in r.getMessage()]


def _project() -> MagicMock:
    project = MagicMock()
    project.name = "sut"
    project.settings = {"coverage": {"hosts": ".*"}}
    return project


@pytest.fixture
def cov_lab(tmp_path, monkeypatch):
    """One host ``t1`` with one instrumented product ``app``, its cov_dir under tmp."""
    cov_dir = tmp_path / "remote" / "app-cov"
    host = coverage_shell_host("t1", {"app": cov_dir})
    install_shell_lab(monkeypatch, host)
    return host, cov_dir


async def _pair(tmp_path: Path, *, cov_clean=True, remove_after_reset=True, cov_dir=None):
    """One invocation's pre-run clean, then (optionally) the dir removed, then the collection."""
    opts = RunOptions(cov=True, cov_clean=cov_clean)
    projects = [_project()]
    pre_clean = await _pre_run_cov_clean(projects, opts)
    if remove_after_reset and cov_dir is not None:
        shutil.rmtree(cov_dir, ignore_errors=True)
    log_dir = tmp_path / "log"
    log_dir.mkdir(exist_ok=True)
    await _post_run_coverage(projects, log_dir, opts, pre_clean)
    return pre_clean


def _kept(cov_dir: Path) -> None:
    """Make *cov_dir* hold content the reset never removes, so the reset keeps it."""
    cov_dir.mkdir(parents=True)
    (cov_dir / "notes.txt").write_text("user data")


@pytest.mark.asyncio
async def test_a_dir_present_at_reset_and_gone_at_fetch_warns(tmp_path, cov_lab, caplog):
    _host, cov_dir = cov_lab
    _kept(cov_dir)
    with caplog.at_level(logging.DEBUG):
        pre_clean = await _pair(tmp_path, cov_dir=cov_dir)
    assert pre_clean is not None
    assert pre_clean.present_at_reset == frozenset({("t1", "app")})
    [record] = _loss_warnings(caplog)
    assert record.getMessage() == (
        f"cov_dir {cov_dir} on t1 for app existed at run start and is gone: its coverage "
        "is missing from this run (was it removed before collection?)"
    )


@pytest.mark.asyncio
async def test_a_dir_the_reset_emptied_and_removed_stays_debug(tmp_path, cov_lab, caplog):
    # The reset's own removal of an emptied cov_dir is never a loss: the
    # product simply wrote nothing this run (#722).
    _host, cov_dir = cov_lab
    (cov_dir / "sub").mkdir(parents=True)
    (cov_dir / "sub" / "a.gcda").write_bytes(b"")
    with caplog.at_level(logging.DEBUG):
        pre_clean = await _pair(tmp_path, remove_after_reset=False, cov_dir=cov_dir)
    assert not cov_dir.exists()
    assert pre_clean is not None
    assert pre_clean.ok
    assert pre_clean.present_at_reset == frozenset()
    assert _loss_warnings(caplog) == []
    assert any("No cov_dir for app on t1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_dir_absent_at_reset_stays_debug(tmp_path, cov_lab, caplog):
    _host, cov_dir = cov_lab
    with caplog.at_level(logging.DEBUG):
        pre_clean = await _pair(tmp_path, cov_dir=cov_dir)
    assert pre_clean is not None
    assert pre_clean.present_at_reset == frozenset()
    assert _loss_warnings(caplog) == []
    assert any("No cov_dir for app on t1" in r.getMessage() for r in caplog.records)


class _FailingProbeHost(ShellHost):
    """A shell host whose presence probe exits non-zero; every other command runs."""

    async def exec(self, cmd, **kwargs):
        if "otto-cov-presence" in cmd:
            self.calls.append(("exec", cmd))
            return CommandResult(status=Status.Error, value="boom", command=cmd, retcode=2)
        return await super().exec(cmd, **kwargs)


@pytest.mark.asyncio
async def test_a_probe_error_gives_no_evidence_and_stays_debug(tmp_path, monkeypatch, caplog):
    cov_dir = tmp_path / "remote" / "app-cov"
    _kept(cov_dir)
    host = coverage_shell_host("t1", {"app": cov_dir})
    failing = _FailingProbeHost(host_id="t1")
    failing.products = host.products
    install_shell_lab(monkeypatch, failing)
    with caplog.at_level(logging.DEBUG):
        pre_clean = await _pair(tmp_path, cov_dir=cov_dir)
    assert pre_clean is not None
    assert pre_clean.ok
    assert pre_clean.present_at_reset == frozenset()
    assert _loss_warnings(caplog) == []


@pytest.mark.asyncio
async def test_no_cov_clean_has_no_evidence_and_stays_debug(tmp_path, cov_lab, caplog):
    host, cov_dir = cov_lab
    cov_dir.mkdir(parents=True)
    with caplog.at_level(logging.DEBUG):
        pre_clean = await _pair(tmp_path, cov_clean=False, cov_dir=cov_dir)
    assert pre_clean is None
    assert host.calls  # the fetch still ran
    assert _loss_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_standalone_collection_has_no_evidence_and_stays_debug(tmp_path, cov_lab, caplog):
    from otto.coverage.collect import collect_coverage
    from otto.coverage.errors import NoCoverageDataError

    _host, cov_dir = cov_lab
    assert not cov_dir.exists()  # whatever happened to it, `otto cov get` has no evidence
    with caplog.at_level(logging.DEBUG), pytest.raises(NoCoverageDataError):
        await collect_coverage(tmp_path / "out", projects=[_project()])
    assert _loss_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_second_invocation_inherits_no_evidence(tmp_path, cov_lab, caplog):
    _host, cov_dir = cov_lab
    _kept(cov_dir)
    with caplog.at_level(logging.DEBUG):
        first = await _pair(tmp_path, cov_dir=cov_dir)
        second = await _pair(tmp_path, cov_dir=cov_dir)
    assert first is not None
    assert first.present_at_reset == frozenset({("t1", "app")})
    assert second is not None
    assert second.present_at_reset == frozenset()
    assert len(_loss_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_the_post_fetch_reset_neither_probes_nor_replaces_the_evidence(tmp_path):
    """The clean after a capture never probes, and the fetcher keeps the pre-run evidence."""
    from otto.coverage.capture.produce import ProduceResult, ProductCapture
    from otto.coverage.collect import collect_coverage
    from otto.coverage.tree import ProductKey
    from otto.host import UnixHost

    evidence = frozenset({("test1", "app")})
    cov_dir = tmp_path / "cov"
    project = MagicMock()
    project.root = tmp_path
    project.name = "proj"
    host = MagicMock(spec=UnixHost)
    host.id = "test1"
    app = MagicMock()
    app.name = "app"
    app.instrumented = MagicMock(return_value=True)
    host.products = [app]
    key = ProductKey("test1", "app")
    fetcher = MagicMock()
    fetcher.fetch_all = AsyncMock(return_value={key: cov_dir / "test1" / "app"})
    captured = ProduceResult([ProductCapture(key, cov_dir / "test1" / "app" / "c.json", None)])
    clean = AsyncMock(
        return_value=CleanReport(hosts={"test1": {"app": Result(status=Status.Success)}})
    )
    with (
        patch("otto.config.coverage_settings.get_cov_config", return_value={"hosts": ".*"}),
        patch("otto.fleet.all_hosts", return_value=[host]),
        patch("otto.coverage.fetcher.remote._GcdaFetcher", return_value=fetcher) as fetcher_cls,
        patch(
            "otto.coverage.fetcher.embedded.collect_embedded_coverage",
            new=AsyncMock(return_value={}),
        ),
        patch("otto.coverage.collect.clean_coverage", clean),
        patch("otto.config.coverage_settings.get_cov_project", return_value=project),
        patch("otto.coverage.collect._write_metadata", new=AsyncMock()),
        patch("otto.coverage.capture.gitio.head_commit", return_value="0" * 40),
        patch(
            "otto.coverage.capture.produce.produce_captures", new=AsyncMock(return_value=captured)
        ),
    ):
        await collect_coverage(cov_dir, projects=[project], present_at_reset=evidence)
    assert fetcher_cls.call_args.kwargs["present_at_reset"] == evidence
    clean.assert_awaited_once()
    assert clean.call_args.kwargs.get("record_presence", False) is False
