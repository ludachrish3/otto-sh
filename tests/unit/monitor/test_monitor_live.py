"""The lab-aware monitor layer: selection and the live run."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.repo import MonitorSettings
from otto.config.scope import EmptySelectionError
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.monitor.errors import MonitorInputError, MonitorTlsError, NoMonitorableHostsError
from otto.monitor.export import build_db_export
from otto.monitor.live import LiveReport, run_live, select_monitor_hosts


def _unix(name: str) -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name),
        creds=[Cred(login="a", password="b")],
        log=LogMode.NORMAL,
    )


def _console(name: str, *, snmp: bool = False):
    spec = {"ip": "192.0.2.1", "os_type": "embedded", "command_frame": "zephyr"}
    if snmp:
        spec["snmp"] = {"oids": ["1.3.6.1.2.1.1.3.0"]}
    return create_host_from_dict(spec, element=Element(name))


@pytest.fixture
def lab(monkeypatch):
    """Stand in for the active lab: all_hosts honours the pattern; no repos; no scope."""
    hosts = [_unix("web1"), _console("z1"), _console("s1", snmp=True)]

    def all_hosts(pattern=None):
        picked = [h for h in hosts if pattern is None or pattern.fullmatch(h.id)]
        if pattern is not None and not picked:
            raise EmptySelectionError(pattern.pattern, len(hosts))
        return iter(picked)

    monkeypatch.setattr("otto.config.fleet.all_hosts", all_hosts)
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))
    monkeypatch.setattr("otto.config.get_repos", list)
    return hosts


async def _close_archive_only(self) -> None:
    """Stand-in for ``MetricCollector.close``: release the archive, touch no host."""
    await self.close_db()


@pytest.fixture
def served():
    """Fake the server and the collector's run/close, so a live run returns at once."""
    server = MagicMock(serve=AsyncMock())
    with (
        patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls,
        patch("otto.monitor.collector.MetricCollector.run", new=AsyncMock()),
        patch("otto.monitor.collector.MetricCollector.close", new=_close_archive_only),
    ):
        yield server_cls


class TestSelect:
    def test_keeps_unix_and_snmp_hosts(self, lab):
        assert [h.id for h in select_monitor_hosts(None)] == ["web1", "s1"]

    def test_an_empty_string_means_no_filter(self, lab):
        assert [h.id for h in select_monitor_hosts("")] == ["web1", "s1"]

    def test_a_compiled_pattern_is_used_as_is(self, lab):
        assert [h.id for h in select_monitor_hosts(re.compile("s.*"))] == ["s1"]

    def test_a_bad_regex_names_the_hosts_field(self, lab):
        with pytest.raises(MonitorInputError, match="not a valid regex") as e:
            select_monitor_hosts("(")
        assert e.value.field == "hosts"

    def test_an_unmatched_pattern_is_the_selection_error(self, lab):
        with pytest.raises(EmptySelectionError):
            select_monitor_hosts("nothing")

    def test_a_selection_of_only_consoles_names_them(self, lab):
        with pytest.raises(NoMonitorableHostsError) as e:
            select_monitor_hosts("z.*")
        assert e.value.walked == ["z1"]


class TestRunLiveRefusesBeforeAnyFile:
    @pytest.mark.asyncio
    async def test_interval(self, lab, tmp_path, monkeypatch):
        """The interval is refused first, ahead of a selection and a TLS that would refuse too.

        The session builder re-checks the interval, so a world where nothing
        else refuses could not tell run_live's own check from the builder's.
        """
        db = tmp_path / "m.db"
        bad = SimpleNamespace(
            name="r", monitor_settings=MonitorSettings(tls_cert=tmp_path / "missing.pem")
        )
        monkeypatch.setattr("otto.config.get_repos", lambda: [bad])
        with pytest.raises(MonitorInputError, match="at least") as e:
            await run_live(hosts="z.*", interval=0.5, db=db)
        assert e.value.field == "interval"
        assert not db.exists()

    @pytest.mark.asyncio
    async def test_selection(self, lab, tmp_path):
        db = tmp_path / "m.db"
        with pytest.raises(NoMonitorableHostsError):
            await run_live(hosts="z.*", db=db)
        assert not db.exists()

    @pytest.mark.asyncio
    async def test_tls(self, lab, tmp_path, monkeypatch):
        db = tmp_path / "m.db"
        bad = SimpleNamespace(
            name="r", monitor_settings=MonitorSettings(tls_cert=tmp_path / "missing.pem")
        )
        monkeypatch.setattr("otto.config.get_repos", lambda: [bad])
        with pytest.raises(MonitorTlsError):
            await run_live(db=db)
        assert not db.exists()


@pytest.mark.asyncio
async def test_run_live_serves_then_finishes_and_reports(lab, tmp_path):
    db = tmp_path / "m.db"
    server = MagicMock(serve=AsyncMock())
    with (
        patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls,
        patch("otto.monitor.collector.MetricCollector.run", new=AsyncMock()),
        patch("otto.monitor.collector.MetricCollector.close", new=_close_archive_only),
    ):
        report = await run_live(hosts="web1", db=db, label="L")
    assert isinstance(report, LiveReport)
    assert (report.hosts, report.db) == (["web1"], db)
    assert report.end >= report.start
    kwargs = server_cls.call_args.kwargs
    assert kwargs["mode"] == "live"
    assert kwargs["frame"].id == report.session_id
    assert build_db_export(str(db)).sessions[0].end is not None


@pytest.mark.asyncio
async def test_run_live_hands_the_server_the_declared_cert_and_key(
    lab, served, tls_pair, monkeypatch
):
    cert, key = tls_pair
    repo = SimpleNamespace(name="r", monitor_settings=MonitorSettings(tls_cert=cert, tls_key=key))
    monkeypatch.setattr("otto.config.get_repos", lambda: [repo])
    await run_live(hosts="web1")
    kwargs = served.call_args.kwargs
    assert (kwargs["tls_cert"], kwargs["tls_key"]) == (cert, key)


@pytest.mark.asyncio
async def test_cancelled_live_run_still_stamps_the_archive_end(lab, tmp_path):
    db = tmp_path / "m.db"
    server = MagicMock(serve=AsyncMock(side_effect=asyncio.CancelledError))
    with (
        patch("otto.monitor.server.MonitorServer", return_value=server),
        patch("otto.monitor.collector.MetricCollector.run", new=AsyncMock()),
        patch("otto.monitor.collector.MetricCollector.close", new=_close_archive_only),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_live(db=db)
    assert build_db_export(str(db)).sessions[0].end is not None


@pytest.mark.asyncio
async def test_a_stopped_server_cancels_the_collection_task(lab):
    """When the server stops, the collection task is cancelled and awaited, then reported."""
    cancelled = []
    collecting = asyncio.Event()

    async def run_until_cancelled(self, interval, duration=None):
        del self, interval, duration
        collecting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def serve_until_collecting():
        # Stop only once collection is running, so the cancel lands inside it.
        await collecting.wait()

    server = MagicMock(serve=serve_until_collecting)
    with (
        patch("otto.monitor.server.MonitorServer", return_value=server),
        patch("otto.monitor.collector.MetricCollector.run", new=run_until_cancelled),
        patch("otto.monitor.collector.MetricCollector.close", new=AsyncMock()),
    ):
        report = await asyncio.wait_for(run_live(hosts="web1"), timeout=5)
    assert cancelled == [True]
    assert report.end >= report.start


@pytest.mark.asyncio
async def test_tunnel_discovery_covers_the_whole_lab_not_the_selection(lab):
    seen = []

    async def discover(active_lab):
        seen.append(active_lab)
        return []

    whole = SimpleNamespace(links=[])
    server = MagicMock(serve=AsyncMock())
    with (
        patch("otto.config.fleet.get_lab", return_value=whole),
        patch("otto.tunnel.records.discover_tunnel_records", side_effect=discover),
        patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls,
        patch("otto.monitor.collector.MetricCollector.run", new=AsyncMock()),
        patch("otto.monitor.collector.MetricCollector.close", new=AsyncMock()),
    ):
        await run_live(hosts="web1")
        collector = server_cls.call_args.args[0]  # run_live passes it positionally
        await collector._tunnel_source()
    assert seen == [whole]


class TestDrivingRepoScopeGate:
    """D3 fires at the monitor fleet build, not only at ``otto run``'s verbs.

    The gap this closes is invisible in a single-repo world — there the union
    is empty too, and ``require_nonempty_fleet`` refuses. With a DEPENDENCY
    admitting hosts, the union is healthy and a live run happily dashboards
    the dependency's machines while the driving project's own declaration
    says this lab is not its world.

    Every fixture below gives the driving repo and the dependency DIFFERENT
    verdicts, and ``get_repos()`` hands back bootstrap's order (driving first)
    while the walk order elsewhere heads with a dependency — so a gate asking
    about the wrong repo is visible in both directions.
    """

    @staticmethod
    def _scope(name, *, excluded):
        from otto.config.scope import ProjectScope

        return ProjectScope(
            repo_name=name,
            declared=True,
            config=None,
            applicable_labs=frozenset() if excluded else frozenset({"bench"}),
            universe=frozenset() if excluded else frozenset({"box"}),
            excluded=excluded,
            sut_dir=f"/repos/{name}",
            loaded_labs=("bench",),
            lab_patterns=(f"{name}-lab",),
            host_patterns=(".*",),
        )

    @classmethod
    def _world(cls, monkeypatch, excluded):
        """Wire a two-repo world (driving ``app``, dependency ``base``) and record contacts.

        The fleet build is RECORDED, never made to raise: a probe that raised
        would be captured by any surrounding handler and read as the gate's own
        refusal. An empty list is then the only evidence that nothing was
        walked.
        """
        contacted = []

        def _all_hosts(*args, **kwargs):
            del args, kwargs
            contacted.append("walked")
            return iter([_unix("box")])

        scopes = {
            "app": cls._scope("app", excluded=excluded == "app"),
            "base": cls._scope("base", excluded=excluded == "base"),
        }
        monkeypatch.setattr("otto.config.fleet.all_hosts", _all_hosts)
        monkeypatch.setattr(
            "otto.config.get_repos",
            lambda: [
                SimpleNamespace(name="app", monitor_settings=MonitorSettings()),
                SimpleNamespace(name="base", monitor_settings=MonitorSettings()),
            ],
        )
        monkeypatch.setattr("otto.context.get_context", lambda: SimpleNamespace(scopes=scopes))
        return contacted

    @pytest.mark.asyncio
    async def test_the_driving_repos_unusable_scope_refuses_the_fleet_build(
        self, lab, served, monkeypatch
    ):
        """Raised naming the driving repo — and nothing walked.

        The contact list is what makes this more than "it refused": a gate
        that fired after the fleet was built would still refuse, having already
        walked hosts the declaration says are none of this project's business.
        """
        from otto.bootstrap import ProjectScopeError

        contacted = self._world(monkeypatch, excluded="app")

        with pytest.raises(ProjectScopeError) as e:
            await run_live()

        message = " ".join(str(e.value).split())
        assert "'app'" in message
        assert "app-lab" in message
        assert "base" not in message  # a healthy dependency is not a suspect
        assert contacted == []
        served.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_excluded_dependency_does_not_stop_the_monitor(self, lab, served, monkeypatch):
        """D3's asymmetry, and the mirror that kills a gate pointed at the wrong repo.

        ``get_repos()[0]`` is the driving project; the walk order's first entry
        is a dependency (``get_ordered_repos`` is a topological reorder). A gate
        reading the latter refuses here, where the monitor must simply run.
        """
        contacted = self._world(monkeypatch, excluded="base")

        report = await run_live()

        assert report.hosts == ["box"]
        assert contacted == ["walked"]

    @pytest.mark.asyncio
    async def test_a_world_with_no_repos_is_untouched(self, lab, served, monkeypatch):
        """Monitoring a lab from outside any project is not a project activity to refuse.

        A library lab, or a checkout with no ``OTTO_SUT_DIRS``: there is no
        current repo, so there is no verdict to enforce and the gate must not
        invent one (nor die indexing an empty list).
        """
        monkeypatch.setattr("otto.config.get_repos", list)

        report = await run_live()

        assert report.hosts == ["web1", "s1"]

    @pytest.mark.asyncio
    async def test_an_unreachable_context_leaves_the_monitor_alone(self, lab, served, monkeypatch):
        """The lookups are guarded, not the refusal.

        A live run happens in worlds where the context lookup raises outright
        (a lab loaded by a library caller). A gate that could not compute a
        verdict has no verdict to enforce — but the guard must sit on the
        LOOKUP, never around ``require_current_scope``, or a real refusal
        would be swallowed with it.
        """

        def _boom():
            raise RuntimeError("no bootstrap here")

        monkeypatch.setattr(
            "otto.config.get_repos",
            lambda: [SimpleNamespace(name="app", monitor_settings=MonitorSettings())],
        )
        monkeypatch.setattr("otto.context.get_context", _boom)

        report = await run_live()

        assert report.hosts == ["web1", "s1"]

    @pytest.mark.asyncio
    async def test_unreachable_repos_pass_the_gate_and_stop_at_tls(self, lab, served, monkeypatch):
        """A ``get_repos()`` that raises is no D3 verdict, so the walk still runs.

        The run itself still needs the repos to resolve the declared dashboard
        TLS — the library always resolves it — so the lookup's own error
        surfaces from there, never as a scope refusal.
        """
        contacted = []

        def _boom():
            raise RuntimeError("no bootstrap here")

        def _all_hosts(*args, **kwargs):
            del args, kwargs
            contacted.append("walked")
            return iter([_unix("box")])

        monkeypatch.setattr("otto.config.get_repos", _boom)
        monkeypatch.setattr("otto.config.fleet.all_hosts", _all_hosts)

        with pytest.raises(RuntimeError, match="no bootstrap here"):
            await run_live()

        assert contacted == ["walked"]
        served.assert_not_called()
