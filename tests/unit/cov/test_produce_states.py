"""produce_captures over several products: each captures or fails on its own."""

import json

import pytest

from otto.coverage.capture import produce
from otto.coverage.errors import CaptureCommitError, CaptureFailedError, CoverageCaptureError
from otto.host.errors import CoverageToolMissingError

_DATA = b"adcg*33B\x01\x00\x00\x00"  # gcc 13, the stubbed system major
_NOTES = b"oncg*33B\x01\x00\x00\x00"


class _FakeCapture:
    def save(self, path):
        path.write_text("{}")


def _setup(tmp_path, names=("good", "bad")):
    cov = tmp_path / "cov"
    for prod in names:
        d = cov / "h" / prod / "x"
        d.mkdir(parents=True)
        (d / f"{prod}.c.gcda").write_bytes(_DATA)
        n = tmp_path / "proj/x" / f"{prod}.c.gcno"
        n.parent.mkdir(parents=True, exist_ok=True)
        n.write_bytes(_NOTES)
    (cov / ".otto_cov_meta.json").write_text(
        json.dumps(
            {
                "project_name": "p",
                "project_root": str(tmp_path / "proj"),
                "toolchains": {},
                "source_roots": {},
                "tier": "system",
                "base_commit": "abc",
            }
        )
    )
    return cov, tmp_path / "proj"


@pytest.fixture(autouse=True)
def no_system_gcov_probe(monkeypatch):
    """Keep toolchain resolution off the real ``gcov --version`` subprocess."""
    monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", lambda gcov="gcov": 13)


@pytest.fixture
def fake_capture(monkeypatch):
    calls = []

    async def capture_pairs(self, pairs, staging, output, toolchain=None):
        calls.append(staging)
        if "bad" in str(output):
            raise CaptureFailedError("lcov --capture failed:\ngeninfo: ERROR: boom")
        output.write_text("TN:\nend_of_record\n")
        return output

    monkeypatch.setattr("otto.coverage.merge.merger.LcovMerger.capture_pairs", capture_pairs)
    monkeypatch.setattr(produce, "build_capture", lambda **kw: _FakeCapture())
    return calls


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_the_others(tmp_path, fake_capture):
    cov, root = _setup(tmp_path)
    with pytest.raises(CoverageCaptureError) as e:
        await produce.produce_captures(
            cov, tier="system", project_root=root, labs=["l"], products={}
        )
    result = e.value.result
    assert [p.key.product for p in result.failed] == ["bad"]
    assert [k.product for k in result.captured] == ["good"]
    assert result.written == [cov / "h/good/capture.json"]
    assert (cov / "h/bad/capture.failed").read_text().startswith("lcov --capture failed")
    assert (cov / "h/good/capture.json").is_file()
    assert "geninfo: ERROR: boom" in str(e.value)
    assert "h/bad:\nlcov --capture failed" in str(e.value)


@pytest.mark.asyncio
async def test_missing_tool_fails_only_its_product(tmp_path, fake_capture, monkeypatch):
    cov, root = _setup(tmp_path)
    real = produce.resolve_toolchains

    def resolve(dirs, recorded):
        if any(d.name == "bad" for d in dirs):
            raise CoverageToolMissingError("gcov-14 is not on PATH")
        return real(dirs, recorded)

    monkeypatch.setattr(produce, "resolve_toolchains", resolve)
    with pytest.raises(CoverageCaptureError) as e:
        await produce.produce_captures(
            cov, tier="system", project_root=root, labs=["l"], products={}
        )
    assert [p.key.product for p in e.value.result.failed] == ["bad"]
    assert "gcov-14" in (cov / "h/bad/capture.failed").read_text()
    assert (cov / "h/good/capture.json").is_file()


@pytest.mark.asyncio
async def test_only_limits_production(tmp_path, fake_capture):
    cov, root = _setup(tmp_path, names=("good", "other"))
    result = await produce.produce_captures(
        cov,
        tier="system",
        project_root=root,
        labs=["l"],
        products={},
        only=[produce.ProductKey("h", "good")],
    )
    assert [k.product for k in result.captured] == ["good"]
    assert not (cov / "h/other/capture.json").exists()


@pytest.mark.asyncio
async def test_a_second_run_clears_the_old_failure_marker(tmp_path, fake_capture):
    cov, root = _setup(tmp_path, names=("good",))
    (cov / "h/good/capture.failed").write_text("old failure\n")
    await produce.produce_captures(cov, tier="system", project_root=root, labs=["l"], products={})
    assert not (cov / "h/good/capture.failed").exists()
    assert (cov / "h/good/capture.json").is_file()


@pytest.mark.asyncio
async def test_unpairable_counters_fail_only_their_product(tmp_path, fake_capture):
    cov, root = _setup(tmp_path, names=("good", "orphan"))
    (root / "x/orphan.c.gcno").unlink()
    with pytest.raises(CoverageCaptureError) as e:
        await produce.produce_captures(
            cov, tier="system", project_root=root, labs=["l"], products={}
        )
    assert [p.key.product for p in e.value.result.failed] == ["orphan"]
    assert "no notes file" in (cov / "h/orphan/capture.failed").read_text()
    assert str(e.value).count("h/orphan:") == 1
    assert (cov / "h/good/capture.json").is_file()


@pytest.mark.asyncio
async def test_pending_capture_at_other_commit_refused_before_pairing(tmp_path, monkeypatch):
    from otto.coverage.capture import gitio

    cov, root = _setup(tmp_path, names=("good",))
    (root / "x/good.c.gcno").write_bytes(b"oncg*33B\x02\x00\x00\x00")  # would stamp-mismatch
    (cov / "h/good/capture.json").write_text("{}")  # an earlier capture stays untouched
    monkeypatch.setattr(gitio, "head_commit", lambda root: "b" * 40)
    called = []

    async def capture_pairs(self, *args, **kwargs):
        called.append(args)

    monkeypatch.setattr("otto.coverage.merge.merger.LcovMerger.capture_pairs", capture_pairs)
    with pytest.raises(CaptureCommitError):
        await produce.produce_captures(
            cov,
            tier="system",
            project_root=root,
            labs=["l"],
            products={},
            base_commit="a" * 40,
        )
    assert not (cov / "h/good/capture.failed").exists()
    assert (cov / "h/good/capture.json").read_text() == "{}"
    assert called == []


@pytest.mark.asyncio
async def test_clang_staging_paths_resolve_through_the_staging_dir(tmp_path, monkeypatch):
    """A clang SF: under the staging dir resolves to the project file it names."""
    cov, root = _setup(tmp_path, names=("good",))
    (root / "src").mkdir()
    (root / "src/a.c").write_text("int a;\n")
    seen = []

    async def capture_pairs(self, pairs, staging, output, toolchain=None):
        seen.append(staging)
        output.write_text(f"TN:\nSF:{staging}/x/../src/a.c\nDA:1,1\nend_of_record\n")
        return output

    monkeypatch.setattr("otto.coverage.merge.merger.LcovMerger.capture_pairs", capture_pairs)
    monkeypatch.setattr(produce, "build_capture", lambda **kw: _FakeCapture())
    await produce.produce_captures(cov, tier="system", project_root=root, labs=["l"], products={})

    (staging,) = seen
    assert staging.is_absolute()
    assert staging == (cov / ".otto-stage" / "h" / "good").resolve()
    resolved = (cov / "h/good/board.resolved.info").read_text()
    assert f"SF:{root / 'src/a.c'}\n" in resolved


@pytest.mark.asyncio
async def test_a_products_object_list_is_read_from_its_definition(tmp_path, fake_capture):
    from otto.coverage.objects import Unavailable

    cov, root = _setup(tmp_path, names=("good",))
    reads = []

    class _Cache:
        def read(self, product):
            reads.append(product)
            return Unavailable(product="good", reason="test")

    sentinel = object()
    await produce.produce_captures(
        cov,
        tier="system",
        project_root=root,
        labs=["l"],
        products={produce.ProductKey("h", "good"): sentinel},  # ty: ignore[invalid-argument-type]
        objects=_Cache(),  # ty: ignore[invalid-argument-type]
    )
    assert reads == [sentinel]


@pytest.mark.asyncio
async def test_same_named_products_on_two_hosts_read_their_own_definitions(tmp_path, fake_capture):
    """Two hosts may carry different builds under one product name: each
    host's counters are paired against that host's own definition."""
    from otto.coverage.objects import Unavailable

    cov, root = _setup(tmp_path, names=("good",))
    other = cov / "h2" / "good" / "x" / "good.c.gcda"
    other.parent.mkdir(parents=True)
    other.write_bytes(_DATA)
    reads = []

    class _Cache:
        def read(self, product):
            reads.append(product)
            return Unavailable(product="good", reason="test")

    on_h, on_h2 = object(), object()
    await produce.produce_captures(
        cov,
        tier="system",
        project_root=root,
        labs=["l"],
        products={  # ty: ignore[invalid-argument-type]
            produce.ProductKey("h", "good"): on_h,
            produce.ProductKey("h2", "good"): on_h2,
        },
        objects=_Cache(),  # ty: ignore[invalid-argument-type]
    )
    assert reads == [on_h, on_h2]


@pytest.mark.asyncio
async def test_a_product_with_no_definition_says_why_it_pairs_by_path(
    tmp_path, fake_capture, caplog
):
    """Report-time capture outside the lab that declared the product: one
    product-labelled INFO line says the object list is unavailable and why."""
    import logging

    cov, root = _setup(tmp_path, names=("good",))
    with caplog.at_level(logging.INFO):
        await produce.produce_captures(
            cov, tier="system", project_root=root, labs=["l"], products={}
        )

    lines = [r.getMessage() for r in caplog.records if "object list unavailable" in r.getMessage()]
    want = "h/good: object list unavailable: no definition of this product in the active lab"
    assert lines == [f"{want} — pairing by path"]


def test_pending_capture_at_other_commit_is_refused(tmp_path, monkeypatch):
    from otto.coverage.capture import gitio, model

    monkeypatch.setattr(gitio, "head_commit", lambda root: "b" * 40)
    info = tmp_path / "h/app/board.resolved.info"
    info.parent.mkdir(parents=True)
    info.write_text("TN:\nend_of_record\n")
    with pytest.raises(CaptureCommitError) as e:
        model.build_capture(
            info_path=info,
            tier="system",
            project_root=tmp_path,
            board="h",
            product="app",
            labs=["l"],
            base_commit="a" * 40,
        )
    assert "aaaaaaaaaaaa" in str(e.value)
    assert "bbbbbbbbbbbb" in str(e.value)


@pytest.fixture
def staging_capture(monkeypatch):
    """A capture that stages like the real one: the leaf exists while lcov runs."""
    seen = []

    async def capture_pairs(self, pairs, staging, output, toolchain=None):
        (staging / "x").mkdir(parents=True)
        (staging / "x" / "a.gcda").write_bytes(_DATA)
        seen.append(staging)
        if "bad" in str(output):
            raise CaptureFailedError("lcov --capture failed:\ngeninfo: ERROR: boom")
        output.write_text("TN:\nend_of_record\n")
        return output

    monkeypatch.setattr("otto.coverage.merge.merger.LcovMerger.capture_pairs", capture_pairs)
    monkeypatch.setattr(produce, "build_capture", lambda **kw: _FakeCapture())
    return seen


@pytest.mark.asyncio
async def test_staging_is_removed_once_every_product_is_done(tmp_path, staging_capture):
    """The staging tree is scratch: a captured and a failed product both leave none."""
    cov, root = _setup(tmp_path)
    with pytest.raises(CoverageCaptureError):
        await produce.produce_captures(
            cov, tier="system", project_root=root, labs=["l"], products={}
        )
    assert len(staging_capture) == 2
    assert not (cov / produce.STAGE_DIR).exists()
    assert sorted(p.name for p in cov.iterdir()) == [".otto_cov_meta.json", "h"]


@pytest.mark.asyncio
async def test_staging_removal_spares_another_products_leaf(tmp_path, staging_capture):
    """Only the captured product's leaf goes; a sibling's leaf keeps ``.otto-stage``."""
    cov, root = _setup(tmp_path, names=("good", "other"))
    sibling = cov / produce.STAGE_DIR / "h" / "other"
    sibling.mkdir(parents=True)
    await produce.produce_captures(
        cov,
        tier="system",
        project_root=root,
        labs=["l"],
        products={},
        only=[produce.ProductKey("h", "good")],
    )
    assert not (cov / produce.STAGE_DIR / "h" / "good").exists()
    assert sibling.is_dir()
