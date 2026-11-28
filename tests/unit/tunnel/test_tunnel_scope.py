"""``otto tunnel``'s user-named hosts are scoped; the tunnels otto left are found lab-wide.

A scoped run's view (``ctx.lab``) refuses a host that is in the lab and that
no active project targets. ``add`` and ``check`` turn that refusal into the
:class:`ValueError` (or the check's refusal) they give for a bad hop, before
any host is contacted, in a dry run and a real run alike. ``add``'s conflict
scan and ``list`` / ``remove`` read the whole lab, so a tunnel on a host that
left the scope still holds its port and is still reaped.

The doubles are the scripted hosts of ``test_manage_add`` and
``test_manage_remove``; :func:`tests._fixtures.fleet.install_scoped_double`
scopes them with one real declaring project.
"""

import asyncio
from unittest.mock import patch

import pytest

from otto.cli.tunnel import tunnel_app
from otto.tunnel.check import REFUSAL_HINT, check_tunnel
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND
from otto.tunnel.manage import add_tunnel
from otto.tunnel.model import Tunnel, TunnelHop
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.fleet import DoubleLab, install_scoped_double
from tests.conftest import active_context

from . import test_manage_add as add_fakes
from . import test_manage_remove as remove_fakes

runner = DispatchRunner()

_LO = 49152

_OUTSIDE = "but outside this run's scope"


def _add_bed(tmp_path, *, targets):
    """Hosts ``a``, ``b`` and ``c`` sharing one ``calls`` log, scoped to *targets*."""
    calls: list = []
    hosts = {
        host_id: add_fakes.FakeHost(host_id, ip=f"10.0.0.{n}", calls=calls)
        for n, host_id in enumerate(("a", "b", "c"), start=1)
    }
    ctx = install_scoped_double(tmp_path, DoubleLab(hosts=hosts), targets=targets)
    return ctx, hosts, calls


def _refusal(ctx, host_id: str) -> str:
    """The text the scoped view refuses *host_id* with."""
    from otto.lab import HostOutOfScopeError

    with pytest.raises(HostOutOfScopeError) as refused:
        ctx.lab.hosts[host_id]
    text = str(refused.value)
    assert f"host {host_id!r} is in lab 'bed' but outside this run's scope" in text
    return text


# ── fail-first ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hosts", "dest"),
    [([("a", None), ("c", None)], None), ([("a", None), ("b", None)], ("c", None))],
    ids=["chain", "dest"],
)
def test_tunnel_add_out_of_scope_endpoint_refused(tmp_path, hosts, dest) -> None:
    """Fail-first: ``add`` refuses an out-of-scope hop or ``--dest``, real and dry run alike."""
    ctx, _hosts, calls = _add_bed(tmp_path, targets=["a", "b"])

    messages = []
    for dry_run in (False, True):
        with active_context(dry_run=dry_run), pytest.raises(ValueError, match=_OUTSIDE) as refused:
            asyncio.run(add_tunnel(ctx.lab, hosts=hosts, port=8080, dest=dest))
        messages.append(str(refused.value))

    assert messages == [_refusal(ctx, "c")] * 2
    assert calls == []


def _container_on(parent, cid: str):
    """A :class:`~otto.host.docker_host.DockerContainerHost` on *parent*, nothing run at build."""
    from otto.host.docker_host import DockerContainerHost

    ctr = DockerContainerHost.__new__(DockerContainerHost)
    object.__setattr__(ctr, "id", cid)
    object.__setattr__(ctr, "parent", parent)
    object.__setattr__(ctr, "container_id", "abc123")
    object.__setattr__(ctr, "has_bash", True)
    return ctr


def test_tunnel_add_out_of_scope_dest_refused_before_a_container_probe(tmp_path) -> None:
    """Fail-first: an outside ``--dest`` refuses before a container endpoint is probed.

    The chain ``b -> a -> a.ctr`` is in scope (the container by its parent);
    ``--dest c`` is not. Resolving a container contacts its parent, so the
    destination is admitted with every chain host before any hop is resolved,
    in a real run as in a dry run.
    """
    ctx, hosts, calls = _add_bed(tmp_path, targets=["a", "b"])
    ctr = _container_on(hosts["a"], "a.ctr")
    ctr.source_lab = "bed"
    ctx.unscoped.lab.hosts[ctr.id] = ctr
    chain = [("b", None), ("a", None), (ctr.id, None)]

    async def probe(_host):
        raise AssertionError("a container was probed before the destination was admitted")

    messages = []
    with patch("otto.tunnel.manage._device_running", probe):
        for dry_run in (False, True):
            with (
                active_context(dry_run=dry_run),
                pytest.raises(ValueError, match=_OUTSIDE) as refused,
            ):
                asyncio.run(add_tunnel(ctx.lab, hosts=chain, port=8080, dest=("c", None)))
            messages.append(str(refused.value))

    assert messages == [_refusal(ctx, "c")] * 2
    assert calls == []


@pytest.mark.asyncio
async def test_tunnel_check_out_of_scope_endpoint_refused(tmp_path) -> None:
    """Fail-first: ``check`` reports an out-of-scope hop as its refusal, touching no host."""
    ctx, _hosts, calls = _add_bed(tmp_path, targets=["a", "b"])

    report = await check_tunnel(lab=ctx.lab, hosts=[("a", None), ("c", None)], port=8080)

    assert report.refusal == _refusal(ctx, "c")
    assert report.refusal_hint == REFUSAL_HINT
    assert calls == []


# ── regression ──────────────────────────────────────────────────────────────


def test_tunnel_add_conflict_discovery_whole_lab(tmp_path) -> None:
    """Regression: a live tunnel to a host that left the scope still holds its service port.

    ``a`` and ``b`` are in scope; a tunnel from ``a`` to ``c`` binds
    ``tcp:8080`` on ``a``, and the scan that finds it reads ``c`` too.
    """
    ctx, hosts, calls = _add_bed(tmp_path, targets=["a", "b"])
    live = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("c")))
    for host_id in ("a", "c"):
        hosts[host_id].ps_texts = [add_fakes._full_ps(live, host_id, _LO, _LO + 1)]

    with pytest.raises(ValueError, match=rf"tunnel '{live.id}' already binds tcp:8080 on a"):
        asyncio.run(add_tunnel(ctx.lab, hosts=[("a", None), ("b", None)], port=8080))

    assert ("c", DISCOVERY_PS_COMMAND) in calls
    assert not any(cmd.startswith(add_fakes._LAUNCH_PREFIX) for _host, cmd in calls)


@pytest.mark.parametrize("verb", ["add", "check"])
def test_tunnel_cli_out_of_scope_host_exits_1(tmp_path, verb) -> None:
    """Regression: ``otto tunnel add`` and ``check`` exit 1 with the out-of-scope text."""
    ctx, _hosts, calls = _add_bed(tmp_path, targets=["a", "b"])

    result = runner.invoke(tunnel_app, [verb, "--hosts", "a,c", "--port", "8080"])

    assert result.exit_code == 1, result.output
    assert "".join(_refusal(ctx, "c").split()) in "".join(result.output.split())
    assert calls == []


def test_tunnel_list_and_remove_whole_lab(tmp_path) -> None:
    """Regression: ``otto tunnel list`` and ``remove`` reach a tunnel whose hosts left the scope."""
    live = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("c"), TunnelHop("d")))
    hosts = {"a": remove_fakes.FakeHost("a")}
    for host_id, pid_start in (("c", 100), ("d", 200)):
        text, _pids = remove_fakes._full_ps(live, host_id, _LO, _LO + 1, pid_start)
        # The list's scan, the remove's scan, then gone at the post-kill verify.
        hosts[host_id] = remove_fakes.FakeHost(host_id, ps_texts=[text, text, ""])
    ctx = install_scoped_double(tmp_path, DoubleLab(hosts=hosts), targets=["a"])
    assert list(ctx.lab.hosts) == ["a"]

    with (
        patch("otto.declared.get_projects", return_value=[]),
        patch("otto.cli.tunnel.record_tunnel_ids"),
    ):
        listed = runner.invoke(tunnel_app, ["list"])
        removed = runner.invoke(tunnel_app, ["remove", live.id])

    assert listed.exit_code == 0, listed.output
    assert live.id in listed.output
    assert removed.exit_code == 0, removed.output
    assert f"removed {live.id}" in removed.output
    for host_id in ("c", "d"):
        assert any(cmd.startswith("kill ") for cmd in hosts[host_id].commands)
