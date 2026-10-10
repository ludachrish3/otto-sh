"""``clean_coverage``: every coverage host, every instrumented product, one result each."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.coverage_settings import CoverageConfigError
from otto.config.scope import EmptySelectionError
from otto.coverage.collect import clean_coverage
from otto.coverage.errors import NoCoverageHostsError
from otto.result import Result
from otto.utils import Status
from tests._fixtures.bootstrap_seam import fake_bootstrap_result


def _product(name, result, *, instrumented=True):
    p = MagicMock()
    p.name = name
    p.instrumented.return_value = instrumented
    p.reset_coverage = AsyncMock(return_value=result)
    return p


def _host(host_id, *products, cls=None):
    h = MagicMock(spec=cls) if cls else MagicMock()
    h.id = host_id
    h.products = list(products)
    return h


def _repo(cov=True, hosts=".*"):
    r = MagicMock()
    r.settings = {"coverage": {"hosts": hosts}} if cov else {}
    return r


@pytest.fixture
def lab(monkeypatch):
    """Install *hosts* as the lab ``all_hosts`` and ``do_for_all_hosts`` walk.

    Mirrors the real ``all_hosts`` (``otto.context.OttoContext.all_hosts``)
    on the two things ``clean_coverage`` depends on: a pattern that
    fullmatches none of a non-empty base set raises ``EmptySelectionError``
    (D6) rather than silently yielding nothing, while a genuinely empty base
    set (no hosts at all) stays silent — that is the long-standing "empty lab"
    behavior, not a selection failure. The otto runner is never part of the
    base set a pattern-scoped walk admits, so a ``LocalHost`` passed in here
    is dropped from the fleet, exactly as production does by never passing
    ``include_local=True``.
    """

    def install(*hosts):
        from otto.host.local_host import LocalHost

        fleet = [h for h in hosts if not isinstance(h, LocalHost)]

        def fake_all_hosts(pattern=None, **kw):
            if pattern is None:
                matched = fleet
            else:
                matched = [h for h in fleet if pattern.fullmatch(h.id)]
                if fleet and not matched:
                    raise EmptySelectionError(pattern.pattern, len(fleet))
            return iter(matched)

        monkeypatch.setattr("otto.config.fleet.all_hosts", fake_all_hosts)

        async def fake_do_for_all(method, *args, pattern=None, **kw):
            out = {}
            for h in fleet:
                if pattern is None or pattern.fullmatch(h.id):
                    try:
                        out[h.id] = await method(h, *args)
                    except Exception as e:  # noqa: BLE001 — mirrors return_exceptions=True
                        out[h.id] = e
            return out

        monkeypatch.setattr("otto.config.fleet.do_for_all_hosts", fake_do_for_all)

    return install


@pytest.mark.asyncio
async def test_every_instrumented_product_on_every_host_is_reset_once(lab):
    a, b = _product("app", Result(Status.Success)), _product("ext", Result(Status.Success))
    lab(_host("t1", a), _host("zephyr", b))
    report = await clean_coverage([_repo()])
    assert report.hosts == {
        "t1": {"app": Result(Status.Success)},
        "zephyr": {"ext": Result(Status.Success)},
    }
    a.reset_coverage.assert_awaited_once()
    b.reset_coverage.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_reset_is_a_result_not_a_log_line(lab):
    lab(_host("t1", _product("app", Result(Status.Error, msg="denied"))))
    report = await clean_coverage([_repo()])
    assert not report.ok
    assert report.failed[0].reason == "denied"


@pytest.mark.asyncio
async def test_a_host_whose_reset_raises_reports_every_product_failed(lab):
    boom = _product("app", Result(Status.Success))
    boom.reset_coverage = AsyncMock(side_effect=ConnectionResetError("peer reset"))
    other = _product("lib", Result(Status.Success))
    lab(_host("t1", boom, _product("tool", Result(Status.Success))), _host("t2", other))
    report = await clean_coverage([_repo()])
    assert set(report.hosts["t1"]) == {"app", "tool"}
    assert all("ConnectionResetError: peer reset" in r.msg for r in report.hosts["t1"].values())
    assert report.hosts["t2"] == {"lib": Result(Status.Success)}


@pytest.mark.asyncio
async def test_no_coverage_section_refuses():
    with pytest.raises(CoverageConfigError):
        await clean_coverage([_repo(cov=False)])


@pytest.mark.asyncio
async def test_a_selector_matching_no_host_refuses_with_the_selection_error_chained(lab):
    """``all_hosts`` never yields the otto runner and raises ``EmptySelectionError``
    (D6) when a pattern fullmatches none of a non-empty base set — never a
    silent empty walk. ``clean_coverage`` reframes that as the typed
    ``NoCoverageHostsError`` callers already catch, chaining the original so
    the selector text (what the reader needs to fix) is never lost."""
    lab(_host("t1", _product("app", Result(Status.Success))))
    with pytest.raises(NoCoverageHostsError, match="nothing to clean") as excinfo:
        await clean_coverage([_repo(hosts="nope-.*")])
    assert "nope-.*" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, EmptySelectionError)


@pytest.mark.asyncio
async def test_host_ids_matching_none_of_the_matched_hosts_refuses(lab):
    lab(_host("t1", _product("app", Result(Status.Success))))
    with pytest.raises(NoCoverageHostsError, match="nothing to clean"):
        await clean_coverage([_repo()], host_ids=["nope"])


@pytest.mark.asyncio
async def test_host_ids_narrow_the_walk(lab):
    a, b = _product("app", Result(Status.Success)), _product("lib", Result(Status.Success))
    lab(_host("t1", a), _host("t2", b))
    report = await clean_coverage([_repo()], host_ids=["t2"])
    assert list(report.hosts) == ["t2"]
    a.reset_coverage.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_declined_reset_is_reported_not_run(lab):
    lab(_host("t1", _product("app", Result(Status.NotRun))))
    report = await clean_coverage([_repo()])
    assert report.ok
    assert report.not_run == [("t1", "app")]


@pytest.mark.asyncio
async def test_a_malformed_selector_is_refused_by_name(lab):
    """The ``[coverage].hosts`` loader runs here too, refusing a wrong shape
    before any host is touched — as it does in ``collect_coverage``."""
    lab()
    repo = MagicMock()
    repo.settings = {"coverage": {"hosts": 123}}
    with pytest.raises(CoverageConfigError, match="hosts must be a string"):
        await clean_coverage([repo])


@pytest.mark.asyncio
async def test_a_malformed_product_name_never_reaches_the_host(lab):
    """A product name that is not a single safe path segment must not steer a
    reset into another host's directory: it is rejected before the hook
    runs, and — like any other raise from the per-host walk — surfaces as a
    failed result for every product on that host rather than a raw raise out
    of ``clean_coverage``."""
    bad = _product("../x", Result(Status.Success))
    ok = _product("tool", Result(Status.Success))
    lab(_host("t1", bad, ok))
    report = await clean_coverage([_repo()])
    bad.reset_coverage.assert_not_awaited()
    ok.reset_coverage.assert_not_awaited()
    assert set(report.hosts["t1"]) == {"../x", "tool"}
    assert all("product name" in r.msg for r in report.hosts["t1"].values())
    assert not report.ok


@pytest.mark.asyncio
async def test_no_hosts_in_the_lab_refuses(lab):
    """A configured lab with no hosts at all has nothing to clean."""
    lab()
    with pytest.raises(NoCoverageHostsError, match="nothing to clean"):
        await clean_coverage([_repo()])


@pytest.mark.asyncio
async def test_defaults_to_the_runs_repos_when_none_given(lab):
    lab(_host("t1", _product("app", Result(Status.Success))))
    result = fake_bootstrap_result([_repo()])
    with patch("otto.bootstrap.bootstrap", return_value=result) as composition_root:
        report = await clean_coverage()
    composition_root.assert_called_once()
    assert report.hosts == {"t1": {"app": Result(Status.Success)}}


@pytest.mark.asyncio
async def test_an_uninstrumented_product_is_never_touched(lab):
    """``instrumented_products`` filters the walk: a product whose build is
    not instrumented is never reset and never named in the report."""
    instrumented = _product("app", Result(Status.Success))
    skipped = _product("dbg", Result(Status.Success), instrumented=False)
    lab(_host("t1", instrumented, skipped))
    report = await clean_coverage([_repo()])
    skipped.reset_coverage.assert_not_awaited()
    instrumented.reset_coverage.assert_awaited_once()
    assert set(report.hosts["t1"]) == {"app"}


@pytest.mark.asyncio
async def test_a_host_with_no_returned_outcome_is_reported_failed(lab, monkeypatch):
    """A host the walk was scoped to but that comes back with no entry at all
    in ``do_for_all_hosts``'s result must not default to an empty (= ok)
    report — Chris's ruling makes an unclear outcome a failure, not a pass."""
    lab(_host("t1", _product("app", Result(Status.Success))))

    async def fake_do_for_all_with_a_gap(method, *args, pattern=None, **kw):
        return {}  # "t1" was walked but nothing at all came back for it

    monkeypatch.setattr("otto.config.fleet.do_for_all_hosts", fake_do_for_all_with_a_gap)

    report = await clean_coverage([_repo()])
    assert not report.ok
    assert set(report.hosts["t1"]) == {"app"}
    assert "no result" in report.hosts["t1"]["app"].msg
    assert "t1" in report.hosts["t1"]["app"].msg
