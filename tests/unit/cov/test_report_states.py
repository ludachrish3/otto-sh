"""The report walk's capture states: captured, failed, not yet captured.

A product dir holding ``capture.json`` is captured and loads; one holding
``capture.failed`` is listed once and never retried; one holding neither is
captured at report time with the tier and commit collection recorded.
"""

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from otto import layout
from otto.coverage.capture.gitio import head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.produce import ProduceResult, ProductCapture, ProductKey
from otto.coverage.errors import CoverageCaptureError, CoverageInputError
from otto.coverage.report_inputs import ReportInputs
from otto.coverage.reporter import run_coverage_report
from otto.coverage.tiers import load_tiers
from tests._fixtures.gitrepo import git_env

_TIERS = {"tiers": {"system": {"kind": "e2e", "precedence": 1}}}
_COMMIT = "c0ffee" + "0" * 34
_DETAIL = "geninfo: ERROR: boom"
_FAILURE = f"lcov --capture failed:\n{_DETAIL}\n"


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sut"
    root.mkdir()
    (root / "f.c").write_text("int a;\nint b;\nint c;\n")
    for args in (["init", "-q"], ["add", "f.c"], ["commit", "-qm", "init"]):
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, env=git_env(tmp_path)
        )
    return root


def _cov_dir(tmp_path: Path, project: Path, **overrides: object) -> Path:
    cov = tmp_path / "run" / "cov"
    cov.mkdir(parents=True)
    meta: dict[str, object] = {
        "project_name": "fw",
        "project_root": str(project),
        "toolchains": {},
        "source_roots": {},
        "tier": "system",
        "base_commit": _COMMIT,
        **overrides,
    }
    (cov / ".otto_cov_meta.json").write_text(
        json.dumps({k: v for k, v in meta.items() if v is not None})
    )
    return cov


def _inputs(project: Path) -> ReportInputs:
    return ReportInputs(project_root=project, tier_configs=load_tiers(_TIERS))


def _spy_produce(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def fake(cov_dir, **kwargs):
        calls.append({"cov_dir": cov_dir, **kwargs})
        return ProduceResult([])

    monkeypatch.setattr("otto.coverage.capture.produce.produce_captures", fake)
    return calls


def _at_recorded_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a non-git project's tree at :data:`_COMMIT`, the commit its metadata records."""
    monkeypatch.setattr("otto.coverage.capture.gitio.head_commit", lambda root: _COMMIT)


@pytest.mark.asyncio
async def test_pending_dir_is_captured_with_recorded_tier_and_commit(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    _at_recorded_commit(monkeypatch)
    cov = _cov_dir(tmp_path, project, tier="manual")
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
    calls = _spy_produce(monkeypatch)

    await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    (call,) = calls
    assert call["cov_dir"] == cov
    assert call["tier"] == "manual"
    assert call["base_commit"] == _COMMIT
    assert call["only"] == [ProductKey("h", "app")]
    assert call["project_root"] == project
    assert call["labs"] == ["fw"]


@pytest.mark.asyncio
async def test_every_produce_call_shares_one_object_list_cache(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    _at_recorded_commit(monkeypatch)
    covs = []
    for run in ("r1", "r2"):
        cov = _cov_dir(tmp_path / run, project)
        (cov / "h" / "app").mkdir(parents=True)
        (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
        covs.append(cov)
    calls = _spy_produce(monkeypatch)

    await run_coverage_report(covs, tmp_path / "report", _inputs(project))

    assert len(calls) == 2
    assert calls[0]["objects"] is not None
    assert calls[0]["objects"] is calls[1]["objects"]


@pytest.mark.asyncio
async def test_failed_dir_is_reported_once_not_retried(tmp_path, monkeypatch, caplog):
    project = tmp_path / "proj"
    project.mkdir()
    cov = _cov_dir(tmp_path, project)
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
    (cov / "h" / "app" / "capture.failed").write_text(_FAILURE)
    calls = _spy_produce(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    assert calls == []
    named = [r for r in caplog.records if r.levelno == logging.WARNING and "h/app" in r.message]
    assert [r.message for r in named] == [
        f"h/app: failed to capture; see {cov / 'h' / 'app' / 'capture.failed'}"
    ]
    assert not any(_DETAIL in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_report_time_failures_join_the_one_warning(tmp_path, monkeypatch, caplog):
    """A failure new in this report carries its detail, once; an earlier one only its path.

    ``otto cov report`` on a not-yet-captured dir is the first time that
    product's failure is seen, so the warning is its one diagnostic: the
    user must learn the cause (a rebuild, say) and the remedy without
    opening ``capture.failed``.
    """
    fresh = "h/tool: the product was rebuilt; re-run `otto test --cov` [twice]"
    project = tmp_path / "proj"
    project.mkdir()
    _at_recorded_commit(monkeypatch)
    cov = _cov_dir(tmp_path, project)
    for product in ("app", "tool"):
        (cov / "h" / product).mkdir(parents=True)
        (cov / "h" / product / "x.gcda").write_bytes(b"adcg")
    (cov / "h" / "app" / "capture.failed").write_text(_FAILURE)

    async def fake(cov_dir, **kwargs):
        (cov_dir / "h" / "tool" / "capture.failed").write_text(_FAILURE)
        failed = ProductCapture(ProductKey("h", "tool"), None, fresh)
        raise CoverageCaptureError(ProduceResult([failed]))

    monkeypatch.setattr("otto.coverage.capture.produce.produce_captures", fake)

    with caplog.at_level(logging.WARNING):
        store = await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    assert store is not None
    warnings = [r.message for r in caplog.records if "failed to capture" in r.message]
    reasons = ", ".join(str(cov / "h" / p / "capture.failed") for p in ("app", "tool"))
    # The detail is arbitrary lcov text, shown as written: the record does not
    # opt in to Rich markup, so its brackets need no escape.
    assert warnings == [f"h/app, h/tool: failed to capture; see {reasons}\n\n{fresh}"]
    assert _DETAIL not in warnings[0]


@pytest.mark.asyncio
async def test_report_refuses_dir_without_recorded_commit(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    cov = _cov_dir(tmp_path, project, base_commit=None)
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
    calls = _spy_produce(monkeypatch)

    with pytest.raises(CoverageInputError) as excinfo:
        await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    message = str(excinfo.value)
    assert excinfo.value.field == "cov_dirs"
    assert str(cov) in message
    assert "otto test --cov" in message
    assert "otto cov get" in message
    assert calls == []


@pytest.mark.asyncio
async def test_report_refuses_pending_dir_with_no_metadata(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    cov = tmp_path / "run" / "cov"
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
    calls = _spy_produce(monkeypatch)

    with pytest.raises(CoverageInputError) as excinfo:
        await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    assert str(cov) in str(excinfo.value)
    assert "otto cov get" in str(excinfo.value)
    assert calls == []


@pytest.mark.asyncio
async def test_run_dirs_need_a_project_root(tmp_path):
    cov = tmp_path / "run" / "cov"
    cov.mkdir(parents=True)
    with pytest.raises(CoverageInputError) as excinfo:
        await run_coverage_report([cov], tmp_path / "report", ReportInputs())
    assert excinfo.value.field == "cov_dirs"
    assert "git root" in str(excinfo.value)


@pytest.mark.asyncio
async def test_tracefile_only_report_needs_no_project_root(tmp_path):
    src = tmp_path / "f.c"
    src.write_text("int a;\n")
    info = tmp_path / "u.info"
    info.write_text(f"TN:\nSF:{src}\nDA:1,4\nend_of_record\n")

    store = await run_coverage_report(
        [], tmp_path / "report", ReportInputs(), tier_specs=[("unit", info)]
    )

    (frec,) = store.files()
    assert frec.lines[1].hits.for_tier("unit") == 4


@pytest.mark.asyncio
async def test_every_run_has_host_and_product(tmp_path):
    project = _git_repo(tmp_path)
    head = head_commit(project)
    cov = _cov_dir(tmp_path, project, base_commit=head)
    for host, product in (("h1", "app"), ("h2", "tool")):
        Capture(
            tier="system",
            product=product,
            board=host,
            base_commit=head,
            files={"f.c": CaptureFileCov(lines={1: 2})},
        ).save(cov / host / product / "capture.json")

    store = await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    assert {(r.host, r.product) for r in store.runs} == {("h1", "app"), ("h2", "tool")}
    assert all(r.host and r.product for r in store.runs)


@pytest.mark.asyncio
async def test_bare_tier_system_is_refused(tmp_path):
    with pytest.raises(CoverageInputError) as excinfo:
        await run_coverage_report(
            [], tmp_path / "report", ReportInputs(), tier_specs=[("system", None)]
        )
    assert excinfo.value.field == "tier_specs"
    assert "every --tier needs NAME=PATH" in str(excinfo.value)


@pytest.mark.asyncio
async def test_unit_harvest_uses_capture_dir(tmp_path, monkeypatch):
    from otto.coverage.merge import merger as merger_mod
    from tests.unit.cov._unit_lab import own_files, stub_unit_tools, unit_build

    stub_unit_tools(monkeypatch, tmp_path / "stub-bin")
    project = tmp_path / "proj"
    project.mkdir()
    own_files(monkeypatch, project, ["f.c"])
    hdir = tmp_path / "unit_build"
    unit_build(hdir, project, ["f.c"])
    seen: list[dict] = []

    async def fake_capture_dir(self, directory, output, toolchain=None, includes=None):
        seen.append({"directory": directory, "includes": includes})
        output.write_text(f"TN:\nSF:{project / 'f.c'}\nDA:1,1\nend_of_record\n")
        return output

    monkeypatch.setattr(merger_mod.LcovMerger, "capture_dir", fake_capture_dir)
    cov_config = {"tiers": {"unit": {"kind": "unit", "precedence": 1, "harvest_dirs": [str(hdir)]}}}

    await run_coverage_report(
        [],
        tmp_path / "report",
        ReportInputs(
            project_root=project, project_name="proj", tier_configs=load_tiers(cov_config)
        ),
    )

    assert seen == [{"directory": hdir, "includes": ["*/f.c"]}]


def test_an_unresolved_source_is_warned_once_by_the_resolver(tmp_path, caplog):
    from otto.coverage.merge.lcov_loader import LCOVLoader
    from otto.coverage.sources import SourceResolver
    from otto.coverage.store.model import CoverageStore

    project = tmp_path / "proj"
    (project / "other").mkdir(parents=True)
    # Named like the path but not ending with it: a candidate, so a WARNING.
    (project / "other" / "x.c").write_text("")
    record = "SF:/nowhere/x.c\nDA:1,1\nend_of_record\n"
    info = tmp_path / "t.info"
    info.write_text("TN:\n" + record + record)
    loader = LCOVLoader(CoverageStore(), SourceResolver(project))

    with caplog.at_level(logging.WARNING):
        loader.load(info, "unit")
        loader.load(info, "unit")

    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert "/nowhere/x.c" in warned[0].message


def test_post_run_coverage_shows_a_capture_failure_once(tmp_path, monkeypatch, caplog):
    """One `otto test --cov --cov-report`: collection names the failure, the report does not."""
    from otto.suite.run import RunOptions, _post_run_coverage

    project_root = tmp_path / "proj"
    project_root.mkdir()
    log_dir = tmp_path / "log"
    log_dir.mkdir()

    async def fake_collect(cov_dir, *, projects=None, **_kwargs):
        product = cov_dir / "h" / "app"
        product.mkdir(parents=True)
        (product / "x.gcda").write_bytes(b"adcg")
        (product / "capture.failed").write_text(_FAILURE)
        (cov_dir / ".otto_cov_meta.json").write_text(
            json.dumps(
                {
                    "project_name": "fw",
                    "project_root": str(project_root),
                    "tier": "system",
                    "base_commit": _COMMIT,
                }
            )
        )
        failed = ProductCapture(ProductKey("h", "app"), None, _FAILURE.rstrip("\n"))
        raise CoverageCaptureError(ProduceResult([failed]))

    monkeypatch.setattr("otto.coverage.collect.collect_coverage", fake_collect)
    project = MagicMock()
    project.root = project_root
    project.name = "fw"
    project.settings = {"coverage": _TIERS}

    with caplog.at_level(logging.INFO):
        asyncio.run(
            _post_run_coverage([project], log_dir, RunOptions(cov_clean=False, cov_report=True))
        )

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text.count(_DETAIL) == 1
    failed = layout.cov_dir(log_dir) / "h" / "app" / "capture.failed"
    assert f"h/app: failed to capture; see {failed}" in text
    assert (layout.cov_report_dir(log_dir) / "store.json").is_file()


@pytest.mark.asyncio
async def test_run_dirs_with_tier_tracefiles_are_refused_by_what_tier_does(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    cov = _cov_dir(tmp_path, project)
    info = tmp_path / "u.info"
    info.write_text("TN:\nend_of_record\n")
    for inputs in (ReportInputs(), _inputs(project)):
        with pytest.raises(CoverageInputError) as excinfo:
            await run_coverage_report(
                [cov], tmp_path / "report", inputs, tier_specs=[("unit", info)]
            )
        message = str(excinfo.value)
        assert excinfo.value.field == "tier_specs"
        assert "--tier NAME=PATH reports tracefiles alone" in message
        assert "cannot be combined with run directories" in message
        assert "[coverage]" not in message


@pytest.mark.asyncio
async def test_failed_products_warning_names_where_the_reasons_are(tmp_path, monkeypatch, caplog):
    project = tmp_path / "proj"
    project.mkdir()
    covs = []
    for run in ("r1", "r2"):
        cov = _cov_dir(tmp_path / run, project)
        (cov / "h" / "app").mkdir(parents=True)
        (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
        (cov / "h" / "app" / "capture.failed").write_text(_FAILURE)
        covs.append(cov)
    _spy_produce(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await run_coverage_report(covs, tmp_path / "report", _inputs(project))

    (warning,) = [r.message for r in caplog.records if "failed to capture" in r.message]
    for cov in covs:
        assert str(cov / "h" / "app" / "capture.failed") in warning


@pytest.mark.asyncio
async def test_an_empty_product_dir_has_nothing_to_capture(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    cov = tmp_path / "run" / "cov"  # no metadata: an empty dir must not need any
    (cov / "h" / "app").mkdir(parents=True)
    calls = _spy_produce(monkeypatch)

    await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    assert calls == []


@pytest.mark.asyncio
async def test_report_time_capture_is_loaded_into_the_report(tmp_path, monkeypatch):
    project = _git_repo(tmp_path)
    head = head_commit(project)
    cov = _cov_dir(tmp_path, project, base_commit=head)
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")

    async def fake(cov_dir, *, only, base_commit, tier, **_kwargs):
        captured = []
        for key in only:
            target = cov_dir / key.host / key.product / "capture.json"
            Capture(
                tier=tier,
                product=key.product,
                board=key.host,
                base_commit=base_commit,
                files={"f.c": CaptureFileCov(lines={2: 7})},
            ).save(target)
            captured.append(ProductCapture(key, target, None))
        return ProduceResult(captured)

    monkeypatch.setattr("otto.coverage.capture.produce.produce_captures", fake)

    store = await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    assert (cov / "h" / "app" / "capture.json").is_file()
    assert [(r.host, r.product) for r in store.runs] == [("h", "app")]
    (frec,) = store.files()
    assert frec.lines[2].hits.for_tier("system") == 7


@pytest.mark.asyncio
async def test_report_time_capture_at_another_commit_propagates(tmp_path):
    from otto.coverage.errors import CaptureCommitError

    project = _git_repo(tmp_path)
    cov = _cov_dir(tmp_path, project)  # recorded at _COMMIT, not the project's HEAD
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")

    with pytest.raises(CaptureCommitError) as excinfo:
        await run_coverage_report([cov], tmp_path / "report", _inputs(project))

    assert excinfo.value.taken_at == _COMMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", ["stale", "missing"])
async def test_input_refusals_leave_the_destination_untouched(tmp_path, monkeypatch, meta):
    project = tmp_path / "proj"
    project.mkdir()
    # "stale" lacks a current key; "missing" has no metadata for its pending product.
    cov = _cov_dir(tmp_path, project, tier=None) if meta == "stale" else tmp_path / "run" / "cov"
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
    report = tmp_path / "report"
    report.mkdir()
    (report / "sentinel").write_text("keep me")
    _spy_produce(monkeypatch)

    with pytest.raises(CoverageInputError):
        await run_coverage_report([cov], report, _inputs(project), overwrite=True)

    assert (report / "sentinel").read_text() == "keep me"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "captured"])
async def test_a_moved_commit_refusal_leaves_the_destination_untouched(
    tmp_path, monkeypatch, state
):
    """Counters (pending) or a capture.json (captured) from another commit are
    refused before ``--overwrite-dir`` clears the previous report."""
    from otto.coverage.errors import CaptureCommitError

    project = _git_repo(tmp_path)
    cov = _cov_dir(tmp_path, project)  # recorded at _COMMIT, not the project's HEAD
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")
    if state == "captured":
        Capture(
            tier="system",
            product="app",
            board="h",
            base_commit=_COMMIT,
            files={"f.c": CaptureFileCov(lines={2: 7})},
        ).save(cov / "h" / "app" / "capture.json")
    report = tmp_path / "report"
    report.mkdir()
    (report / "sentinel").write_text("keep me")
    calls = _spy_produce(monkeypatch)

    with pytest.raises(CaptureCommitError) as excinfo:
        await run_coverage_report([cov], report, _inputs(project), overwrite=True)

    assert excinfo.value.taken_at == _COMMIT
    assert (report / "sentinel").read_text() == "keep me"
    assert calls == []


def test_a_capture_duplicating_a_manual_one_is_not_refused_up_front(tmp_path):
    """A cov-dir copy of a committed manual capture is skipped at load time,
    never guarded, so the up-front check exempts it too."""
    from otto.coverage.capture.store_dir import write_manual_capture
    from otto.coverage.reporter import _refuse_moved_commits, _scan_run_inputs

    project = _git_repo(tmp_path)
    cov = _cov_dir(tmp_path, project)
    capture = Capture(
        tier="system",
        product="app",
        board="h",
        base_commit=_COMMIT,
        files={"f.c": CaptureFileCov(lines={2: 7})},
    )
    capture.save(cov / "h" / "app" / "capture.json")
    write_manual_capture(capture, project)

    _refuse_moved_commits(_scan_run_inputs([cov]), project)


def test_report_time_capture_at_another_commit_exits_1(tmp_path, caplog):
    from typer.testing import CliRunner

    from otto.cli.cov import cov_app

    project = _git_repo(tmp_path)
    cov = _cov_dir(tmp_path, project)
    (cov / "h" / "app").mkdir(parents=True)
    (cov / "h" / "app" / "x.gcda").write_bytes(b"adcg")

    with (
        patch("otto.coverage.report_inputs.resolve_report_inputs", return_value=_inputs(project)),
        caplog.at_level(logging.ERROR),
    ):
        result = CliRunner().invoke(
            cov_app, ["report", str(cov.parent), "--dir", str(tmp_path / "report")]
        )

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert any(f"taken at commit {_COMMIT[:12]}" in r.getMessage() for r in caplog.records)


def test_declared_products_keep_each_hosts_own_definition(monkeypatch):
    """Two hosts carrying different builds under one name: each key gets its host's."""
    from types import SimpleNamespace

    from otto.coverage.reporter import _declared_products

    on_h1 = SimpleNamespace(name="app")
    on_h2 = SimpleNamespace(name="app")
    hosts = {
        "h1": SimpleNamespace(products=[on_h1]),
        "h2": SimpleNamespace(products=[SimpleNamespace(name="lib"), on_h2]),
    }
    ctx = SimpleNamespace(lab=SimpleNamespace(hosts=hosts))
    monkeypatch.setattr("otto.run_context.try_get_context", lambda: ctx)

    keys = [ProductKey("h1", "app"), ProductKey("h2", "app"), ProductKey("gone", "app")]
    assert _declared_products(keys) == {keys[0]: on_h1, keys[1]: on_h2}
