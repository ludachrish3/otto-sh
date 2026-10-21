"""A failed product keeps its host counters; a second collection captures it."""

from unittest.mock import MagicMock

import pytest

from otto.coverage import collect
from otto.coverage.capture import produce
from otto.coverage.errors import CaptureFailedError, CoverageCaptureError
from otto.coverage.reports import CleanReport
from otto.host import UnixHost
from otto.host.toolchain import Toolchain
from tests._fixtures.fake_project import fake_project
from tests._fixtures.gitrepo import TmpGitRepo


class _Lab:
    """In-memory host counters: (host, product) -> {relpath: bytes}."""

    def __init__(self, root):
        self.root = root
        self.broken = True
        self.counters = {
            ("h", "good"): {"x/good.c.gcda": b"adcg*33B\x01\x00\x00\x00"},
            ("h", "bad"): {"x/bad.c.gcda": b"adcg*33B\x01\x00\x00\x00"},
        }
        self.cleaned: list[list[produce.ProductKey]] = []


@pytest.fixture(autouse=True)
def _gcc13_system_gcov(monkeypatch):
    """Pin the system gcov to gcc 13, the major the fake counters carry."""
    monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", lambda gcov="gcov": 13)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    for prod in ("good", "bad"):
        n = root / "x" / f"{prod}.c.gcno"
        n.parent.mkdir(parents=True, exist_ok=True)
        n.write_bytes(b"oncg*33B\x01\x00\x00\x00")
    lab = _Lab(root)

    async def fetch_all(self):
        out = {}
        for (host, prod), files in lab.counters.items():
            if files:
                d = self.staging_root / host / prod
                for rel, data in files.items():
                    (d / rel).parent.mkdir(parents=True, exist_ok=True)
                    (d / rel).write_bytes(data)
                out[(host, prod)] = d
        return out

    async def clean(projects=None, *, host_ids=None, pairs=None):
        lab.cleaned.append(list(pairs or []))
        for k in pairs or []:
            lab.counters[(k.host, k.product)] = {}
        return CleanReport(hosts={})

    async def capture_pairs(self, pairs, staging, output, toolchain=None):
        if "bad" in str(output) and lab.broken:
            raise CaptureFailedError("lcov --capture failed:\ngeninfo: ERROR: boom")
        output.write_text("TN:\nend_of_record\n")
        return output

    class _Cap:
        def save(self, p):
            p.write_text("{}")

    monkeypatch.setattr("otto.coverage.fetcher.remote.GcdaFetcher.fetch_all", fetch_all)
    monkeypatch.setattr(collect, "clean_coverage", clean)
    monkeypatch.setattr("otto.coverage.merge.merger.LcovMerger.capture_pairs", capture_pairs)
    monkeypatch.setattr(produce, "build_capture", lambda **kw: _Cap())
    return lab


@pytest.fixture
def collect_kwargs(monkeypatch):
    """Build ``collect_coverage``'s keyword arguments for a one-host lab rooted at *root*.

    The root becomes a committed git checkout on first use (the commit is what
    the metadata records); the host carries no product definitions, so
    pairing goes by path alone and no artifact is read.
    """

    async def no_embedded(staging_root, pattern=None):
        return {}

    host = MagicMock(spec=UnixHost)
    host.id = "h"
    host.products = []
    host.toolchain = Toolchain()
    monkeypatch.setattr("otto.fleet.all_hosts", lambda pattern=None, **kw: iter([host]))
    monkeypatch.setattr("otto.coverage.fetcher.embedded.collect_embedded_coverage", no_embedded)

    def build(root):
        if not (root / ".git").exists():
            TmpGitRepo(root).commit("init")
        project = fake_project(root=root, settings={"coverage": {"hosts": "h"}})
        return {
            "projects": [project],
            "tier": None,
            "display_names": {"h": "h"},
            "clean_after_fetch": True,
        }

    return build


@pytest.mark.asyncio
async def test_failed_product_recovers_on_second_get(tmp_path, lab, collect_kwargs):
    with pytest.raises(CoverageCaptureError):
        await collect.collect_coverage(tmp_path / "run1/cov", **collect_kwargs(lab.root))
    assert lab.cleaned == [[produce.ProductKey("h", "good")]]
    assert lab.counters[("h", "bad")]  # still on the host
    assert not lab.counters[("h", "good")]

    lab.broken = False
    result = await collect.collect_coverage(tmp_path / "run2/cov", **collect_kwargs(lab.root))
    assert [k.product for k in result.produce.captured] == ["bad"]
    assert lab.cleaned[-1] == [produce.ProductKey("h", "bad")]
    assert not lab.counters[("h", "bad")]


@pytest.mark.asyncio
async def test_a_stale_leaf_in_a_reused_cov_dir_is_neither_captured_nor_cleaned(
    tmp_path, lab, collect_kwargs
):
    """Only the pairs this call fetched are produced, so only they can be cleaned.

    The stale leaf was collected at this same tier and commit (the metadata
    says so); one from another collection is refused outright (below).
    """
    from otto.coverage.capture.gitio import head_commit

    lab.counters[("h", "bad")] = {}
    kwargs = collect_kwargs(lab.root)
    cov_dir = tmp_path / "reused/cov"
    _write_meta(
        cov_dir,
        tier=collect._resolve_tier({"hosts": "h"}, None).name,
        base_commit=head_commit(lab.root),
    )
    stale = cov_dir / "other" / "app" / "x" / "stale.c.gcda"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"adcg*33B\x01\x00\x00\x00")
    (lab.root / "x" / "stale.c.gcno").write_bytes(b"oncg*33B\x01\x00\x00\x00")

    result = await collect.collect_coverage(cov_dir, **kwargs)

    assert result.produce.captured == [produce.ProductKey("h", "good")]
    assert result.captures_written == [cov_dir / "h" / "good" / "capture.json"]
    assert not (cov_dir / "other" / "app" / "capture.json").exists()
    assert lab.cleaned == [[produce.ProductKey("h", "good")]]


def _write_meta(cov_dir, **fields):
    import json

    meta = {"project_name": "p", "project_root": "/x", "tier": "e2e", "base_commit": "0" * 40}
    meta.update(fields)
    cov_dir.mkdir(parents=True, exist_ok=True)
    (cov_dir / ".otto_cov_meta.json").write_text(json.dumps(meta))


def _pending_leaf(cov_dir, host="h", product="old"):
    leaf = cov_dir / host / product / "x" / "old.c.gcda"
    leaf.parent.mkdir(parents=True)
    leaf.write_bytes(b"adcg*33B\x01\x00\x00\x00")
    return leaf


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", ["other-commit", "other-tier", "none"])
async def test_a_reused_dir_with_pending_counters_from_another_collection_is_refused(
    tmp_path, lab, collect_kwargs, meta
):
    """Pending counters keep the provenance their own collection recorded.

    Replacing the shared metadata would capture them later against this
    collection's commit or tier, so the reuse is refused before any host is
    fetched and before the metadata is touched.
    """
    from otto.coverage.capture.gitio import head_commit
    from otto.coverage.errors import CoverageInputError

    kwargs = collect_kwargs(lab.root)
    tier = collect._resolve_tier({"hosts": "h"}, None).name
    head = head_commit(lab.root)
    cov_dir = tmp_path / "reused/cov"
    _pending_leaf(cov_dir)
    if meta == "other-commit":
        _write_meta(cov_dir, tier=tier, base_commit="0" * 40)
    elif meta == "other-tier":
        _write_meta(cov_dir, tier="not-" + tier, base_commit=head)
    before = (cov_dir / ".otto_cov_meta.json").read_bytes() if meta != "none" else None

    with pytest.raises(CoverageInputError) as excinfo:
        await collect.collect_coverage(cov_dir, **kwargs)

    assert excinfo.value.field == "output_dir"
    assert "h/old" in str(excinfo.value)
    assert "fresh output directory" in str(excinfo.value)
    if meta != "none":
        assert "otto cov report" in str(excinfo.value)
    assert not (cov_dir / "h" / "good").exists()  # nothing was fetched
    if before is None:
        assert not (cov_dir / ".otto_cov_meta.json").exists()
    else:
        assert (cov_dir / ".otto_cov_meta.json").read_bytes() == before
    assert lab.cleaned == []


@pytest.mark.asyncio
async def test_a_reused_dir_whose_pending_counters_match_this_collection_is_reused(
    tmp_path, lab, collect_kwargs
):
    """Same tier and commit: the earlier counters keep their provenance, so reuse is fine."""
    from otto.coverage.capture.gitio import head_commit

    lab.counters[("h", "bad")] = {}
    kwargs = collect_kwargs(lab.root)
    cov_dir = tmp_path / "reused/cov"
    _pending_leaf(cov_dir)
    _write_meta(
        cov_dir,
        tier=collect._resolve_tier({"hosts": "h"}, None).name,
        base_commit=head_commit(lab.root),
    )

    result = await collect.collect_coverage(cov_dir, **kwargs)

    assert result.produce.captured == [produce.ProductKey("h", "good")]


@pytest.mark.asyncio
async def test_a_reused_dir_holding_only_captured_products_is_reused(tmp_path, lab, collect_kwargs):
    """A captured product carries its own commit in capture.json: nothing to protect."""
    lab.counters[("h", "bad")] = {}
    kwargs = collect_kwargs(lab.root)
    cov_dir = tmp_path / "reused/cov"
    leaf = _pending_leaf(cov_dir)
    (leaf.parent.parent / "capture.json").write_text("{}")
    _write_meta(cov_dir, base_commit="0" * 40)

    result = await collect.collect_coverage(cov_dir, **kwargs)

    assert result.produce.captured == [produce.ProductKey("h", "good")]
