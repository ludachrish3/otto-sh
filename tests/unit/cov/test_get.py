"""``get_coverage``: every ``otto cov get`` rule, as a library call.

Moved here (retargeted from ``tests/unit/cli/test_cov.py``'s ``TestResolveTester``,
``TestCovGetValidation``, ``TestCovGetSuccess`` and ``TestCaptureAnnotations``) now
that the CLI's former ``get`` pipeline lives in :func:`otto.coverage.get.get_coverage`.
Each test that used to invoke ``cov get`` through ``CliRunner`` and assert an
``exit_code``/logged message now awaits :func:`~otto.coverage.get.get_coverage`
directly under ``pytest.raises`` on the library exception type, per spec §3.1's
refusal order: configuration, tier, ticket, instrumentation, repository,
destination — all before any host is actually fetched from.
"""

import asyncio
import contextvars
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto import layout
from otto.config.coverage_settings import CoverageConfigError
from otto.config.scope import EmptySelectionError
from otto.coverage.capture import produce as produce_module
from otto.coverage.capture.gitio import GitUnavailableError
from otto.coverage.capture.produce import ProduceResult, ProductCapture
from otto.coverage.collect import CollectResult
from otto.coverage.errors import (
    CoverageCaptureError,
    CoverageInputError,
    CoverageNotInstrumentedError,
    NoCoverageDataError,
    NoCoverageHostsError,
)
from otto.coverage.get import _resolve_tester, get_coverage
from otto.coverage.reports import CleanReport
from otto.coverage.tiers import TierConfig
from otto.coverage.tree import ProductKey
from otto.result import Result
from otto.utils import Status
from tests._fixtures.gitrepo import TmpGitRepo

OK = Result(Status.Success)


def _produced(*captures: Path) -> ProduceResult:
    """A produce outcome in which each ``<host>/<product>/capture.json`` captured."""
    return ProduceResult(
        [ProductCapture(ProductKey(c.parent.parent.name, c.parent.name), c, None) for c in captures]
    )


def _fake_capture(root):
    """Stand in for ``LcovMerger.capture_pairs``: write a minimal valid lcov record."""

    async def _capture(self, pairs, staging, output, toolchain=None):
        output.write_text(f"TN:\nSF:{root / 'f.c'}\nDA:1,3\nend_of_record\n")
        return output

    return _capture


def _fake_collect_one_board(notes_root):
    """Stand in for the embedded collector: one board, one product dir.

    The counters' matching notes file is written under *notes_root* so the
    real pairing step finds it.
    """

    async def fake_collect(staging_root, pattern=None, *, failures):
        product_dir = staging_root / "board1" / "app"
        product_dir.mkdir(parents=True)
        (product_dir / "x.gcda").write_bytes(b"adcg*33B\x01\x00\x00\x00")
        (notes_root / "x.gcno").write_bytes(b"oncg*33B\x01\x00\x00\x00")
        return {ProductKey("board1", "app"): product_dir}

    return fake_collect


def _product_double(name="app", *, instrumented=True):
    """A stand-in :class:`otto.host.product.Product` with a known verdict."""
    product = MagicMock()
    product.name = name
    product.instrumented.return_value = instrumented
    return product


def _host_double(host_id="host1", *, products=("app",), cls=None, instrumented=True):
    """A lab host double carrying instrumentation-scannable products."""
    host = MagicMock()
    host.id = host_id
    host.name = host_id
    host.products = [_product_double(p, instrumented=instrumented) for p in products]
    if cls is not None:
        host.__class__ = cls
    return host


def _embedded_board(host_id="board1", *, products=("app",), instrumented=True):
    """An embedded host double: no filesystem to fetch, so no fetcher runs."""
    from otto.host.embedded_host import EmbeddedHost

    return _host_double(host_id, products=products, cls=EmbeddedHost, instrumented=instrumented)


def _project(coverage_cfg, root=None, name="sut"):
    """A project double carrying the ``[coverage]`` settings table."""
    project = MagicMock()
    project.settings = {"coverage": coverage_cfg} if coverage_cfg is not None else {}
    project.root = root or Path("/nonexistent/sut")
    project.name = name
    return project


@pytest.fixture(autouse=True)
def _gcc13_system_gcov(monkeypatch):
    """Pin the system gcov to gcc 13, the major the fake counters carry.

    The capture step resolves each product's gcov from its ``.gcda`` stamp;
    this keeps it off the real ``gcov --version`` probe and on the default tool.
    """
    monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", lambda gcov="gcov": 13)


@pytest.fixture
def git_sut(tmp_path):
    """A real one-commit git repository standing in for the project checkout."""
    project = TmpGitRepo(tmp_path / "sut")
    project.write("f.c", "int a;\n")
    project.commit("init")
    return project.root


async def _with_nothing_installed(coro):
    """Await *coro* in a fresh, empty execution context: no context, policy or resolver.

    The task is created inside ``contextvars.Context().run``, so it runs in a
    copy of that empty context whatever the caller has installed.
    """
    return await contextvars.Context().run(asyncio.ensure_future, coro)


@pytest.fixture
def all_hosts_of(monkeypatch):
    """Install *hosts as ``otto.fleet.all_hosts``'s answer."""

    def install(*hosts):
        monkeypatch.setattr("otto.fleet.all_hosts", lambda pattern=None, **kw: iter(hosts))

    return install


# ── _resolve_tester — identity defaults (spec decision 15) ──────────────────


class TestResolveTester:
    def test_explicit_overrides_win(self, monkeypatch, tmp_path):
        monkeypatch.setattr("getpass.getuser", lambda: pytest.fail("should not be called"))
        monkeypatch.setattr(
            "otto.coverage.capture.gitio.config_value",
            lambda *a: pytest.fail("should not be called"),
        )
        tester = _resolve_tester("Bob", "bob@x.com", tmp_path)
        assert tester == {"name": "Bob", "email": "bob@x.com"}

    def test_defaults_from_getpass_and_git_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr("getpass.getuser", lambda: "alice")
        monkeypatch.setattr(
            "otto.coverage.capture.gitio.config_value", lambda root, key: "alice@example.com"
        )
        tester = _resolve_tester(None, None, tmp_path)
        assert tester == {"name": "alice", "email": "alice@example.com"}

    def test_omits_email_when_git_config_unset(self, monkeypatch, tmp_path):
        monkeypatch.setattr("getpass.getuser", lambda: "alice")
        monkeypatch.setattr("otto.coverage.capture.gitio.config_value", lambda root, key: None)
        tester = _resolve_tester(None, None, tmp_path)
        assert tester == {"name": "alice"}
        assert "email" not in tester

    @pytest.mark.parametrize("failure", ["not_a_git_repo", "command_failed"])
    def test_omits_email_when_git_cannot_answer(self, monkeypatch, tmp_path, failure):
        from otto.coverage.capture.gitio import GitCommandFailedError, NotAGitRepoError

        exc = NotAGitRepoError if failure == "not_a_git_repo" else GitCommandFailedError
        monkeypatch.setattr("getpass.getuser", lambda: "alice")

        def boom(root, key):
            raise exc("git config user.email failed (rc=128): ...")

        monkeypatch.setattr("otto.coverage.capture.gitio.config_value", boom)
        tester = _resolve_tester(None, None, tmp_path)
        assert tester == {"name": "alice"}

    def test_missing_git_propagates_rather_than_reading_as_no_email(self, monkeypatch, tmp_path):
        from otto.coverage.capture.gitio import GitMissingError

        monkeypatch.setattr("getpass.getuser", lambda: "alice")

        def boom(root, key):
            raise GitMissingError("git executable not found")

        monkeypatch.setattr("otto.coverage.capture.gitio.config_value", boom)
        with pytest.raises(GitMissingError):
            _resolve_tester(None, None, tmp_path)

    def test_name_override_with_default_email(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "otto.coverage.capture.gitio.config_value", lambda root, key: "carol@example.com"
        )
        tester = _resolve_tester("Carol", None, tmp_path)
        assert tester == {"name": "Carol", "email": "carol@example.com"}

    def test_email_reads_project_checkout_not_cwd(self, monkeypatch, tmp_path):
        def make_git_repo(name: str, email: str) -> Path:
            project = TmpGitRepo(tmp_path / name)
            project.git("config", "user.email", email)
            return project.root

        sut = make_git_repo("sut", "sut@example.com")
        elsewhere = make_git_repo("elsewhere", "wrong@example.com")
        monkeypatch.chdir(elsewhere)
        tester = _resolve_tester(None, None, sut)
        assert tester["email"] == "sut@example.com"


# ── get_coverage — refusals, in spec §3.1 order ──────────────────────────────


@pytest.mark.asyncio
async def test_no_coverage_config_raises(all_hosts_of):
    all_hosts_of()
    project = _project(None)
    with pytest.raises(CoverageConfigError):
        await get_coverage(projects=[project])


@pytest.mark.asyncio
async def test_a_non_string_hosts_selector_is_refused_by_name(all_hosts_of):
    """A list where the ``hosts`` regex belongs is refused by name, not by re.compile."""
    all_hosts_of()
    project = _project({"hosts": ["host1", "host2"]})
    with pytest.raises(CoverageConfigError, match="hosts must be a string"):
        await get_coverage(projects=[project])


@pytest.mark.asyncio
async def test_a_selector_matching_no_host_refuses(monkeypatch):
    """An empty host selection refuses as NoCoverageHostsError, chaining the walk's own error.

    ``all_hosts`` is a generator — the error surfaces on the first ``next()``,
    inside ``get_coverage``'s own ``list(...)`` call, before any fetch.
    """

    def _raising_all_hosts(*args, **kwargs):
        raise EmptySelectionError("sensor", 3)
        yield  # pragma: no cover — unreachable; keeps this a generator function

    monkeypatch.setattr("otto.fleet.all_hosts", _raising_all_hosts)
    project = _project({"hosts": "sensor"})
    with pytest.raises(NoCoverageHostsError) as excinfo:
        await get_coverage(projects=[project])
    assert isinstance(excinfo.value.__cause__, EmptySelectionError)
    assert "pattern 'sensor' fullmatches none of the 3 host(s)" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_local_only_selector_names_no_keyword_a_library_caller_cannot_pass(monkeypatch):
    """No coverage call takes ``include_local``, so ``get_coverage`` cannot advise passing it."""

    def _hidden(pattern=None, **_kw):
        raise EmptySelectionError(pattern.pattern, 4, excluded_by=["include_local"], matched_size=1)
        yield  # pragma: no cover — unreachable; keeps this a generator function

    monkeypatch.setattr("otto.fleet.all_hosts", _hidden)
    with pytest.raises(NoCoverageHostsError) as excinfo:
        await get_coverage(projects=[_project({"hosts": "local"})])
    text = str(excinfo.value)
    assert text.startswith("pattern 'local' fullmatches 1 of the 4 host(s)")
    assert "coverage does not walk the built-in 'local' host" in text
    assert "name a lab host in the pattern instead" in text
    assert "include_local" not in text
    assert isinstance(excinfo.value.__cause__, EmptySelectionError)


@pytest.mark.asyncio
async def test_unknown_tier_lists_configured_tiers(all_hosts_of):
    all_hosts_of()
    project = _project({"hosts": ".*"})
    with pytest.raises(CoverageInputError) as exc:
        await get_coverage(tier="bogus", projects=[project])
    assert exc.value.field == "tier"
    assert "bogus" in str(exc.value)
    assert "system" in str(exc.value)


@pytest.mark.asyncio
async def test_a_manual_tier_without_a_ticket_is_refused(all_hosts_of):
    all_hosts_of()
    project = _project(
        {
            "tiers": {
                "manual": {"kind": "manual", "precedence": 1},
                "system": {"kind": "e2e", "precedence": 2},
            }
        }
    )
    with pytest.raises(CoverageInputError) as exc:
        await get_coverage(tier="manual", projects=[project])
    assert exc.value.field == "ticket"
    assert "requires a ticket" in str(exc.value)


@pytest.mark.asyncio
async def test_an_ambiguous_default_tier_is_refused_listing_the_candidates(all_hosts_of):
    """No tier given and more than one e2e-kind tier configured is ambiguous."""
    all_hosts_of()
    project = _project(
        {
            "tiers": {
                "sys_a": {"kind": "e2e", "precedence": 1},
                "sys_b": {"kind": "e2e", "precedence": 2},
            }
        }
    )
    with pytest.raises(CoverageInputError) as exc:
        await get_coverage(projects=[project])
    assert exc.value.field == "tier"
    assert "sys_a" in str(exc.value)
    assert "sys_b" in str(exc.value)


@pytest.mark.asyncio
async def test_get_with_no_instrumented_product_raises(all_hosts_of, git_sut):
    """A lab whose products are all uninstrumented is refused before any fetch."""
    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1", products=("app",), instrumented=False)
    all_hosts_of(board)
    with pytest.raises(CoverageNotInstrumentedError, match="no instrumented product"):
        await get_coverage(projects=[project])


@pytest.mark.asyncio
async def test_get_with_unknown_instrumentation_verdict_raises(all_hosts_of, git_sut):
    """An ``unknown`` (``None``) verdict counts as not instrumented too."""
    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1", products=("app",), instrumented=None)
    all_hosts_of(board)
    with pytest.raises(CoverageNotInstrumentedError):
        await get_coverage(projects=[project])


@pytest.mark.asyncio
async def test_the_engines_no_data_refusal_passes_through(all_hosts_of, git_sut):
    """``collect_coverage``'s own ``NoCoverageDataError`` — raised when nothing was
    fetched from any host at all — passes through ``get_coverage`` unframed and
    byte-identical. This is pure pass-through (the message/search-list text is
    the engine's to own and is covered by its own tests); ``get_coverage``'s
    *own* no-captures refusal, raised after a successful fetch that produced
    no capture.json, is a separate rule tested separately below."""
    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("zephyr37-fat")
    all_hosts_of(board)
    collect_mock = AsyncMock(
        side_effect=NoCoverageDataError(
            "no .gcda counters retrieved from any product (searched: zephyr37-fat:app:console)"
        )
    )
    with (
        patch("otto.coverage.collect.collect_coverage", collect_mock),
        pytest.raises(NoCoverageDataError, match="zephyr37-fat"),
    ):
        await get_coverage(Path("/tmp/out"), projects=[project])


@pytest.mark.asyncio
async def test_no_captures_produced_raises_no_coverage_data(all_hosts_of, git_sut, tmp_path):
    """``collect_coverage`` fetched something, but produced no captures: ``get_coverage``
    raises on its own ``captures_written`` check, naming every searched product."""
    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1")
    all_hosts_of(board)
    product_dir = tmp_path / "cov" / "board1" / "app"
    collect_mock = AsyncMock(
        return_value=CollectResult(
            cov_dir=tmp_path / "cov",
            product_dirs={ProductKey("board1", "app"): product_dir},
            produce=ProduceResult([]),
        )
    )
    with (
        patch("otto.coverage.collect.collect_coverage", collect_mock),
        pytest.raises(NoCoverageDataError, match="board1/app"),
    ):
        await get_coverage(tmp_path, projects=[project])


@pytest.mark.asyncio
async def test_get_own_no_captures_refusal_names_every_product(all_hosts_of, git_sut, tmp_path):
    """``get_coverage``'s own no-captures refusal — distinct from the engine's
    ``NoCoverageDataError`` above — names *every* searched ``host/product``
    pair when more than one contributed a staging dir but none produced a
    capture, not just the first."""
    project = _project({"hosts": ".*"}, root=git_sut)
    t1 = _host_double("t1")
    zephyr = _embedded_board("zephyr")
    all_hosts_of(t1, zephyr)
    product_dir1 = tmp_path / "cov" / "t1" / "app"
    product_dir2 = tmp_path / "cov" / "zephyr" / "ext"
    collect_mock = AsyncMock(
        return_value=CollectResult(
            cov_dir=tmp_path / "cov",
            product_dirs={
                ProductKey("t1", "app"): product_dir1,
                ProductKey("zephyr", "ext"): product_dir2,
            },
            produce=ProduceResult([]),
        )
    )
    with (
        patch("otto.coverage.collect.collect_coverage", collect_mock),
        pytest.raises(NoCoverageDataError) as exc,
    ):
        await get_coverage(tmp_path, projects=[project])

    assert "t1/app, zephyr/ext" in str(exc.value)


@pytest.mark.asyncio
async def test_a_project_that_is_not_a_git_checkout_is_refused(all_hosts_of, tmp_path):
    not_a_git_repo = tmp_path / "not_a_git_repo"
    not_a_git_repo.mkdir()
    project = _project({"hosts": ".*"}, root=not_a_git_repo)
    board = _embedded_board("board1")
    all_hosts_of(board)
    with pytest.raises(GitUnavailableError):
        await get_coverage(projects=[project])


@pytest.mark.asyncio
async def test_get_defaults_to_the_per_invocation_output_dir(all_hosts_of, git_sut, tmp_path):
    """With an installed context and no ``output_dir`` given, ``get_coverage`` writes
    into ``ctx.output_dir / "cov"`` — the per-invocation output directory."""
    from otto.context import OttoContext, reset_context, set_context
    from otto.invocation import RunPolicy
    from otto.labs.loader import Lab

    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1")
    all_hosts_of(board)

    cap = tmp_path / "cov" / "board1" / "app" / "capture.json"
    collect_mock = AsyncMock(
        return_value=CollectResult(
            cov_dir=tmp_path / "cov",
            product_dirs={ProductKey("board1", "app"): cap.parent},
            produce=_produced(cap),
        )
    )

    invocation_dir = layout.command_output_dir(tmp_path / "xdir", "cov") / "20260703_120000_000_get"
    invocation_dir.mkdir(parents=True)
    token = set_context(OttoContext(lab=Lab(name="t"), policy=RunPolicy(output_dir=invocation_dir)))
    try:
        with patch("otto.coverage.collect.collect_coverage", collect_mock):
            report = await get_coverage(projects=[project])
    finally:
        reset_context(token)

    assert report.cov_dir == invocation_dir.resolve() / "cov"
    assert isinstance(collect_mock.await_args.kwargs["tier"], TierConfig)


@pytest.mark.asyncio
async def test_no_output_dir_and_no_context_is_refused(all_hosts_of, git_sut):
    """No ``output_dir`` and no context (a bare programmatic call) fails clean —
    after config/tier/ticket/instrumentation/repository validation, so those
    never get masked by it."""
    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1")
    all_hosts_of(board)

    with pytest.raises(CoverageInputError) as exc:
        await _with_nothing_installed(get_coverage(projects=[project]))
    assert exc.value.field == "output_dir"


@pytest.mark.asyncio
async def test_get_manual_tier_writes_capture_and_manual_store(all_hosts_of, git_sut, tmp_path):
    from otto.coverage.capture.model import Capture

    project = _project(
        {
            "tiers": {
                "manual": {"kind": "manual", "precedence": 1},
                "system": {"kind": "e2e", "precedence": 2},
            }
        },
        root=git_sut,
    )
    board = _embedded_board("board1")
    all_hosts_of(board)

    cap_path = tmp_path / "cov" / "board1" / "app" / "capture.json"
    cap = Capture(
        tier="manual",
        product="app",
        base_commit="deadbeef",
        board="board1",
        ticket="T-1",
        note="session note",
        tester={"name": "Bob", "email": "bob@x.com"},
    )
    cap.save(cap_path)
    collect_mock = AsyncMock(
        return_value=CollectResult(
            cov_dir=tmp_path / "cov",
            product_dirs={ProductKey("board1", "app"): cap_path.parent},
            produce=_produced(cap_path),
        )
    )

    with patch("otto.coverage.collect.collect_coverage", collect_mock):
        report = await get_coverage(
            tmp_path,
            tier="manual",
            ticket="T-1",
            note="session note",
            tester_name="Bob",
            tester_email="bob@x.com",
            projects=[project],
        )

    assert report.captures == [cap_path]
    assert len(report.manual_captures) == 1
    manual_dir = git_sut / ".otto" / "coverage" / "manual"
    assert list(manual_dir.glob("*.json")) == report.manual_captures
    manual_cap = Capture.load(report.manual_captures[0])
    assert manual_cap.ticket == "T-1"
    resolved_tier = collect_mock.await_args.kwargs["tier"]
    assert isinstance(resolved_tier, TierConfig)
    assert resolved_tier.name == "manual"


@pytest.mark.asyncio
async def test_get_default_tier_no_manual_store(all_hosts_of, git_sut, tmp_path):
    from otto.coverage.capture.model import Capture

    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1")
    all_hosts_of(board)

    cap_path = tmp_path / "cov" / "board1" / "app" / "capture.json"
    cap = Capture(tier="system", product="app", base_commit="deadbeef", board="board1")
    cap.save(cap_path)
    collect_mock = AsyncMock(
        return_value=CollectResult(
            cov_dir=tmp_path / "cov",
            product_dirs={ProductKey("board1", "app"): cap_path.parent},
            produce=_produced(cap_path),
        )
    )

    with patch("otto.coverage.collect.collect_coverage", collect_mock):
        report = await get_coverage(tmp_path, projects=[project])

    assert report.manual_captures == []
    manual_dir = git_sut / ".otto" / "coverage" / "manual"
    assert not manual_dir.exists() or not list(manual_dir.glob("*.json"))
    resolved_tier = collect_mock.await_args.kwargs["tier"]
    assert isinstance(resolved_tier, TierConfig)
    assert resolved_tier.name == "system"


@pytest.mark.asyncio
async def test_resolve_get_tier_called_once_across_full_get_flow(all_hosts_of, git_sut, tmp_path):
    """``get_coverage`` resolves the tier once; ``collect_coverage`` must not
    re-resolve it from the name (it is passed the already-resolved object).

    Regression for the double-resolve: before the fix, ``get_coverage``
    resolved the tier via ``resolve_get_tier`` and then passed only the
    resolved tier's *name* into ``collect_coverage``, which re-resolves by
    name itself whenever it is not handed a already-resolved
    :class:`~otto.coverage.tiers.TierConfig` (``collect.py``'s
    ``isinstance(tier, TierConfig)`` check). Mocking ``collect_coverage``
    wholesale — as an earlier version of this test did — hides that branch
    entirely, so a mutation back to ``tier=resolved.name`` would still pass.
    This runs the real ``collect_coverage``, faking only the embedded
    collector and the lcov merge step underneath it, so the ``isinstance``
    check actually executes.
    """
    from otto.coverage import tiers as tiers_module

    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1")
    all_hosts_of(board)

    resolve_spy = MagicMock(wraps=tiers_module.resolve_get_tier)
    with (
        patch.object(tiers_module, "resolve_get_tier", resolve_spy),
        patch(
            "otto.coverage.fetcher.embedded.collect_embedded_coverage",
            _fake_collect_one_board(git_sut),
        ),
        patch.object(produce_module.LcovMerger, "capture_pairs", _fake_capture(git_sut)),
    ):
        report = await get_coverage(tmp_path, projects=[project])

    assert resolve_spy.call_count == 1
    assert report.captures == [tmp_path / "cov" / "board1" / "app" / "capture.json"]


@pytest.mark.asyncio
async def test_get_clean_resets_only_the_pairs_that_captured(all_hosts_of, git_sut, tmp_path):
    """``clean=True`` scopes the post-fetch clean to exactly the (host, product)
    pairs that captured — not every host that contributed counters, nor the
    raw ``[coverage].hosts`` match set."""
    project = _project({"hosts": ".*"}, root=git_sut)
    t1 = _host_double("t1")
    zephyr = _embedded_board("zephyr")
    t2 = _host_double("t2")  # matched by the selector but contributes nothing
    all_hosts_of(t1, zephyr, t2)

    cap_path = tmp_path / "cov" / "t1" / "app" / "capture.json"
    d1, d2 = cap_path.parent, tmp_path / "cov" / "zephyr" / "ext"
    collected = CollectResult(
        cov_dir=tmp_path / "cov",
        product_dirs={ProductKey("t1", "app"): d1, ProductKey("zephyr", "ext"): d2},
        produce=_produced(cap_path),
    )
    clean_mock = AsyncMock(
        return_value=CleanReport(hosts={"t1": {"app": OK}, "zephyr": {"ext": OK}})
    )
    collect_mock = AsyncMock(return_value=collected)

    with (
        patch("otto.coverage.collect.collect_coverage", collect_mock),
        patch("otto.coverage.collect.clean_coverage", clean_mock),
    ):
        report = await get_coverage(tmp_path, clean=True, projects=[project])

    clean_mock.assert_awaited_once_with([project], pairs=[ProductKey("t1", "app")])
    assert report.ok
    assert isinstance(collect_mock.await_args.kwargs["tier"], TierConfig)


@pytest.mark.asyncio
async def test_a_failed_get_clean_keeps_the_captures_and_reports_not_ok(
    all_hosts_of, git_sut, tmp_path
):
    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1")
    all_hosts_of(board)

    cap_path = tmp_path / "cov" / "board1" / "app" / "capture.json"
    collected = CollectResult(
        cov_dir=tmp_path / "cov",
        product_dirs={ProductKey("board1", "app"): cap_path.parent},
        produce=_produced(cap_path),
    )
    clean_mock = AsyncMock(
        return_value=CleanReport(hosts={"board1": {"app": Result(Status.Error, msg="denied")}})
    )
    collect_mock = AsyncMock(return_value=collected)

    with (
        patch("otto.coverage.collect.collect_coverage", collect_mock),
        patch("otto.coverage.collect.clean_coverage", clean_mock),
    ):
        report = await get_coverage(tmp_path, clean=True, projects=[project])

    assert report.captures == [cap_path]
    assert not report.ok
    assert isinstance(collect_mock.await_args.kwargs["tier"], TierConfig)


@pytest.mark.asyncio
async def test_each_refusal_fires_before_any_host_is_touched(all_hosts_of, tmp_path):
    """Configuration, tier, ticket, instrumentation, repository, destination:
    in that order — none of them ever calls ``collect_coverage``/``clean_coverage``."""
    project_git_repo = TmpGitRepo(tmp_path / "sut")
    project_git_repo.write("f.c", "int a;\n")
    project_git_repo.commit("init")
    git_sut = project_git_repo.root

    not_a_git_repo = tmp_path / "not_a_git_repo"
    not_a_git_repo.mkdir()

    scenarios = [
        # (project, hosts, kwargs, expected exception)
        (_project(None), [], {}, CoverageConfigError),
        (_project({"hosts": ".*"}), [], {"tier": "bogus"}, CoverageInputError),
        (
            _project(
                {
                    "tiers": {
                        "manual": {"kind": "manual", "precedence": 1},
                        "system": {"kind": "e2e", "precedence": 2},
                    }
                }
            ),
            [],
            {"tier": "manual"},
            CoverageInputError,
        ),
        (
            _project({"hosts": ".*"}, root=git_sut),
            [_embedded_board("board1", instrumented=False)],
            {},
            CoverageNotInstrumentedError,
        ),
        (
            _project({"hosts": ".*"}, root=not_a_git_repo),
            [_embedded_board("board1")],
            {},
            GitUnavailableError,
        ),
        (
            _project({"hosts": ".*"}, root=git_sut),
            [_embedded_board("board1")],
            {},
            CoverageInputError,  # output_dir refusal, with no context installed
        ),
    ]

    for i, (project, hosts, kwargs, expected) in enumerate(scenarios):
        collect_mock = AsyncMock()
        clean_mock = AsyncMock()
        all_hosts_of(*hosts)
        with (
            patch("otto.coverage.collect.collect_coverage", collect_mock),
            patch("otto.coverage.collect.clean_coverage", clean_mock),
        ):
            if i == len(scenarios) - 1:
                with pytest.raises(expected):
                    await _with_nothing_installed(get_coverage(projects=[project], **kwargs))
            else:
                with pytest.raises(expected):
                    await get_coverage(output_dir=tmp_path, projects=[project], **kwargs)
        collect_mock.assert_not_awaited()
        clean_mock.assert_not_awaited()


# ── tester/ticket/note annotation — tester stays manual-only ─────────────────
#
# `_capture_annotations` was a separate CLI helper; `get_coverage` now makes
# the same tier-aware decision inline (ticket/note annotate every tier kind,
# tester attribution is manual-only), so these assert directly on what it
# passes into `collect_coverage`.


@pytest.mark.asyncio
async def test_e2e_kind_keeps_ticket_and_note_but_no_tester(all_hosts_of, git_sut, tmp_path):
    project = _project({"hosts": ".*"}, root=git_sut)
    board = _embedded_board("board1")
    all_hosts_of(board)

    cap_path = tmp_path / "cov" / "board1" / "app" / "capture.json"
    collect_mock = AsyncMock(
        return_value=CollectResult(
            cov_dir=tmp_path / "cov",
            product_dirs={ProductKey("board1", "app"): cap_path.parent},
            produce=_produced(cap_path),
        )
    )
    with patch("otto.coverage.collect.collect_coverage", collect_mock):
        await get_coverage(
            tmp_path,
            ticket="CI-77",
            note="nightly run",
            tester_name="Al",
            tester_email="al@x",
            projects=[project],
        )

    _, kwargs = collect_mock.await_args
    assert kwargs["tester"] is None
    assert (kwargs["ticket"], kwargs["note"]) == ("CI-77", "nightly run")
    assert isinstance(kwargs["tier"], TierConfig)


@pytest.mark.asyncio
async def test_manual_kind_resolves_tester(all_hosts_of, git_sut, tmp_path):
    from otto.coverage.capture.model import Capture

    project = _project(
        {
            "tiers": {
                "manual": {"kind": "manual", "precedence": 1},
                "system": {"kind": "e2e", "precedence": 2},
            }
        },
        root=git_sut,
    )
    board = _embedded_board("board1")
    all_hosts_of(board)

    cap_path = tmp_path / "cov" / "board1" / "app" / "capture.json"
    Capture(tier="manual", product="app", base_commit="deadbeef", board="board1").save(cap_path)
    collect_mock = AsyncMock(
        return_value=CollectResult(
            cov_dir=tmp_path / "cov",
            product_dirs={ProductKey("board1", "app"): cap_path.parent},
            produce=_produced(cap_path),
        )
    )
    with patch("otto.coverage.collect.collect_coverage", collect_mock):
        await get_coverage(
            tmp_path,
            tier="manual",
            ticket="T-1",
            tester_name="Al",
            tester_email="al@x",
            projects=[project],
        )

    _, kwargs = collect_mock.await_args
    assert kwargs["tester"] == {"name": "Al", "email": "al@x"}
    assert kwargs["ticket"] == "T-1"
    assert isinstance(kwargs["tier"], TierConfig)
    assert kwargs["tier"].name == "manual"


def _one_failed_capture(cov_dir: Path) -> "tuple[Path, CoverageCaptureError]":
    """``board1/good`` captured (a real manual capture on disk); ``board1/bad`` failed."""
    from otto.coverage.capture.model import Capture

    good = cov_dir / "board1" / "good" / "capture.json"
    Capture(
        tier="manual", product="good", base_commit="deadbeef", board="board1", ticket="T-1"
    ).save(good)
    outcome = ProduceResult(
        [
            ProductCapture(ProductKey("board1", "good"), good, None),
            ProductCapture(ProductKey("board1", "bad"), None, "lcov --capture failed: boom"),
        ]
    )
    return good, CoverageCaptureError(outcome)


_MANUAL_TIERS = {
    "tiers": {
        "manual": {"kind": "manual", "precedence": 1},
        "system": {"kind": "e2e", "precedence": 2},
    }
}


@pytest.mark.asyncio
async def test_a_failed_product_keeps_the_good_ones_manual_capture_and_cleans_only_it(
    all_hosts_of, git_sut, tmp_path
):
    """One product fails under ``--tier manual --clean``: the good one is kept and cleaned."""
    from otto.coverage.capture.model import Capture

    project = _project(_MANUAL_TIERS, root=git_sut)
    all_hosts_of(_embedded_board("board1", products=("good", "bad")))
    _good, failure = _one_failed_capture(tmp_path / "cov")
    report = CleanReport(hosts={"board1": {"good": OK}})
    clean_mock = AsyncMock(return_value=report)

    with (
        patch("otto.coverage.collect.collect_coverage", AsyncMock(side_effect=failure)),
        patch("otto.coverage.collect.clean_coverage", clean_mock),
        pytest.raises(CoverageCaptureError) as excinfo,
    ):
        await get_coverage(tmp_path, tier="manual", ticket="T-1", clean=True, projects=[project])

    assert excinfo.value is failure
    manual = list((git_sut / ".otto" / "coverage" / "manual").glob("*.json"))
    assert [Capture.load(m).product for m in manual] == ["good"]
    clean_mock.assert_awaited_once_with([project], pairs=[ProductKey("board1", "good")])
    assert excinfo.value.clean is report


@pytest.mark.asyncio
async def test_a_failed_product_without_clean_cleans_nothing(all_hosts_of, git_sut, tmp_path):
    project = _project(_MANUAL_TIERS, root=git_sut)
    all_hosts_of(_embedded_board("board1", products=("good", "bad")))
    _good, failure = _one_failed_capture(tmp_path / "cov")
    clean_mock = AsyncMock()

    with (
        patch("otto.coverage.collect.collect_coverage", AsyncMock(side_effect=failure)),
        patch("otto.coverage.collect.clean_coverage", clean_mock),
        pytest.raises(CoverageCaptureError),
    ):
        await get_coverage(tmp_path, tier="manual", ticket="T-1", projects=[project])

    clean_mock.assert_not_awaited()
    assert failure.clean is None


@pytest.mark.asyncio
async def test_capture_failure_and_clean_failure_both_surface(all_hosts_of, git_sut, tmp_path):
    """The capture failure propagates and carries the failed clean of the good product."""
    project = _project({"hosts": ".*"}, root=git_sut)
    all_hosts_of(_embedded_board("board1", products=("good", "bad")))
    _good, failure = _one_failed_capture(tmp_path / "cov")
    bad = CleanReport(hosts={"board1": {"good": Result(Status.Error, msg="denied")}})

    with (
        patch("otto.coverage.collect.collect_coverage", AsyncMock(side_effect=failure)),
        patch("otto.coverage.collect.clean_coverage", AsyncMock(return_value=bad)),
        pytest.raises(CoverageCaptureError, match="board1/bad") as excinfo,
    ):
        await get_coverage(tmp_path, clean=True, projects=[project])

    assert excinfo.value.clean is bad
    assert not excinfo.value.clean.ok
