"""``otto link``'s endpoints and middleboxes are scoped; the traces otto left are found lab-wide.

A scoped run's view (``ctx.lab``) refuses a host that is in the lab and that
no active project targets. ``impair``, ``check`` and ``find_link`` turn that
refusal into the :class:`ValueError` a link verb raises, for an endpoint
(through ``find_link``'s diagnostic) and for an in-path middlebox (through
``lab_host``), in a dry run and a real run alike. ``list`` shows the links
the view holds, which keep a link whose endpoint the lab does not contain
(Q32). The self-lockout guard and ``repair`` read the whole lab.

The doubles are the scripted hosts of ``test_manage_impair`` and
``_check_fakes``; :func:`tests._fixtures.fleet.install_scoped_double` scopes
them with one real declaring project.
"""

import pytest

from otto.cli.link import link_app
from otto.link.check import check_link
from otto.link.manage import find_link, impair_link, read_link_states
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.fleet import install_scoped_double
from tests.conftest import active_context

from ._check_fakes import bed as check_bed
from .test_manage_impair import INPATH, LINK, FakeHost, _bed

runner = DispatchRunner()

OUTER = Link(
    a=LINK.a,
    b=LinkEndpoint(host="test3", interface="eth1.100", ip="10.10.201.13"),
    name="outer",
)
"""A link from ``test1`` to ``test3``, which drops out of a view that does not target ``test3``."""

GHOST = Link(
    a=LINK.a,
    b=LinkEndpoint(host="ghost", interface="eth0", ip="10.10.201.99"),
    name="ghostly",
)
"""A link whose far end names a host the lab does not contain."""

_OUTSIDE = "but outside this run's scope"

_DELAY_20 = "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"


def _scoped(tmp_path, *, targets, link=LINK, extra_links=(), extra_hosts=()):
    """The ``test_manage_impair`` bed under a scope targeting *targets*; the context and hosts."""
    lab, test1, test2, test3 = _bed(link)
    lab.links.extend(extra_links)
    for host in extra_hosts:
        lab.hosts[host.id] = host
    ctx = install_scoped_double(tmp_path, lab, targets=targets)
    return ctx, test1, test2, test3


def _refusal(ctx, host_id: str) -> str:
    """The text the scoped view refuses *host_id* with."""
    from otto.lab import HostOutOfScopeError

    with pytest.raises(HostOutOfScopeError) as refused:
        ctx.lab.hosts[host_id]
    text = str(refused.value)
    assert f"host {host_id!r} is in lab 'bed' but outside this run's scope" in text
    return text


# ── fail-first ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_impair_out_of_scope_endpoint_names_host(tmp_path) -> None:
    """Fail-first: impairing a link with an out-of-scope endpoint names that host and the fix."""
    ctx, test1, test2, _ = _scoped(tmp_path, targets=["test1", "test3"])

    with pytest.raises(ValueError, match=_OUTSIDE) as refused:
        await impair_link(ctx.lab, ident="edge", params=ImpairmentParams(delay_ms=50.0))

    assert str(refused.value) == (
        f"link 'edge' has an endpoint outside this run's scope: {_refusal(ctx, 'test2')}"
    )
    assert test1.commands == test2.commands == []


@pytest.mark.asyncio
async def test_impair_out_of_scope_middlebox_refused(tmp_path) -> None:
    """Fail-first: an in-path link whose middlebox is out of scope is refused with its refusal."""
    ctx, test1, test2, test3 = _scoped(tmp_path, targets=["test1", "test2"], link=INPATH)

    with pytest.raises(ValueError, match=_OUTSIDE) as refused:
        await impair_link(ctx.lab, ident="dataplane", params=ImpairmentParams(delay_ms=50.0))

    assert str(refused.value) == _refusal(ctx, "test3")
    assert test1.commands + test2.commands + test3.commands == []


@pytest.mark.asyncio
async def test_check_out_of_scope_middlebox_refused(tmp_path) -> None:
    """Fail-first: ``check`` refuses an out-of-scope middlebox as ``impair`` does."""
    lab, test1, test2, test3 = check_bed()
    ctx = install_scoped_double(tmp_path, lab, targets=["test1", "test2"])

    with pytest.raises(ValueError, match=_OUTSIDE) as refused:
        await check_link(lab=ctx.lab, ident="dataplane")

    assert str(refused.value) == _refusal(ctx, "test3")
    assert test1.commands + test2.commands + test3.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("targets", "ident"),
    [(["test1", "test2"], "dataplane"), (["test1", "test3"], "edge")],
    ids=["middlebox", "endpoint"],
)
async def test_dry_run_impair_and_check_refuse_alike(tmp_path, targets, ident) -> None:
    """Fail-first: ``impair`` and ``check``, real and dry run, raise one refusal for one host.

    Each of the four calls reaches the host through the same lookup, so a
    preview never promises what the real run refuses. No host is contacted.
    """
    lab, *hosts = check_bed()
    view = install_scoped_double(tmp_path, lab, targets=targets).lab

    async def impair() -> object:
        return await impair_link(view, ident=ident, params=ImpairmentParams(delay_ms=1.0))

    async def check() -> object:
        return await check_link(lab=view, ident=ident)

    messages = []
    for dry_run in (False, True):
        with active_context(dry_run=dry_run):
            for call in (impair, check):
                with pytest.raises(ValueError, match=_OUTSIDE) as refused:
                    await call()
                messages.append(str(refused.value))

    assert len(set(messages)) == 1, messages
    assert all(host.commands == [] for host in hosts)


def test_find_link_out_of_scope_endpoint_diagnostic(tmp_path) -> None:
    """Fail-first: a link the view drops is named, by id or by name, with its endpoint's refusal."""
    ctx, *_ = _scoped(tmp_path, targets=["test1", "test3"])
    refusal = _refusal(ctx, "test2")

    for ident in (LINK.id, "edge"):
        with pytest.raises(ValueError, match=_OUTSIDE) as refused:
            find_link(lab=ctx.lab, ident=ident)
        assert str(refused.value) == (
            f"link {ident!r} has an endpoint outside this run's scope: {refusal}"
        )


# ── regression ──────────────────────────────────────────────────────────────


def test_find_link_known_lists_scoped_links(tmp_path) -> None:
    """Regression: a miss lists only the links the view holds."""
    ctx, *_ = _scoped(tmp_path, targets=["test1", "test2"], extra_links=[OUTER])

    with pytest.raises(ValueError, match="no link 'nope'") as missed:
        find_link(lab=ctx.lab, ident="nope")

    assert str(missed.value) == f"no link 'nope' in the loaded lab (known: {LINK.id})"


@pytest.mark.asyncio
async def test_unloaded_endpoint_link_listed_and_impair_names_missing_host(tmp_path) -> None:
    """Regression (Q32): a link to a host the lab lacks stays listed, unimpairable, named.

    Only an endpoint that is in the lab and out of scope drops a link; an
    endpoint the lab does not contain keeps today's behaviour.
    """
    ctx, test1, *_ = _scoped(tmp_path, targets=["test1", "test2"], extra_links=[GHOST])

    assert [link.id for link in ctx.lab.static_links()] == [LINK.id, GHOST.id]
    ghost = next(s for s in await read_link_states(ctx.lab) if s.link.id == GHOST.id)
    assert not ghost.impairable
    assert ghost.refusal == "link references host 'ghost' not in the loaded lab"
    with pytest.raises(ValueError, match="names 'ghost', which the loaded lab does not contain"):
        await impair_link(ctx.lab, ident="ghostly", params=ImpairmentParams(delay_ms=1.0))
    assert test1.sudo_commands == []


def test_link_list_scoped(tmp_path) -> None:
    """Regression (Q11): ``otto link list`` shows the links whose endpoints are in scope."""
    _scoped(tmp_path, targets=["test1", "test2"], extra_links=[OUTER])

    result = runner.invoke(link_app, ["list"])

    assert result.exit_code == 0, result.output
    assert LINK.id in result.output
    assert OUTER.id not in result.output


def test_link_list_keeps_an_outside_middlebox_link_and_impair_exits_1(tmp_path) -> None:
    """Regression: listing judges a link's endpoints; ``impair`` refuses its outside middlebox.

    ``dataplane`` runs from ``test1`` to ``test2`` through ``test3``, which
    no project targets. Its endpoints are in scope, so ``list`` shows it, and
    ``impair`` exits 1 with the middlebox's refusal, contacting no host.
    """
    ctx, test1, test2, test3 = _scoped(tmp_path, targets=["test1", "test2"], link=INPATH)

    listed = runner.invoke(link_app, ["list"])
    impaired = runner.invoke(link_app, ["impair", "dataplane", "--delay", "1"])

    assert listed.exit_code == 0, listed.output
    assert INPATH.id in listed.output
    assert impaired.exit_code == 1, impaired.output
    assert "".join(_refusal(ctx, "test3").split()) in "".join(impaired.output.split())
    assert test1.commands + test2.commands + test3.commands == []


def test_link_check_exits_1_on_an_outside_middlebox(tmp_path) -> None:
    """Regression: ``otto link check`` exits 1 with the middlebox's refusal, as the docs say."""
    ctx, test1, test2, test3 = _scoped(tmp_path, targets=["test1", "test2"], link=INPATH)

    checked = runner.invoke(link_app, ["check", "dataplane"])

    assert checked.exit_code == 1, checked.output
    assert "".join(_refusal(ctx, "test3").split()) in "".join(checked.output.split())
    assert test1.commands + test2.commands + test3.commands == []


@pytest.mark.asyncio
async def test_self_lockout_guard_sees_out_of_scope_dependent(tmp_path) -> None:
    """Regression: a host no project targets that hops through an endpoint still protects it.

    ``beet-seed`` reaches otto through ``test1`` on ``eth1.100``'s subnet, so
    impairing that netdev would cut it off, whatever the run's scope.
    """
    beet = FakeHost(id="beet-seed", ip="10.10.201.77")
    beet.hop = "test1"  # type: ignore[attr-defined]
    ctx, test1, test2, _ = _scoped(tmp_path, targets=["test1", "test2"], extra_hosts=[beet])
    assert "beet-seed" not in ctx.lab.hosts

    with pytest.raises(ValueError, match="management path to 'beet-seed'"):
        await impair_link(ctx.lab, ident="edge", params=ImpairmentParams(delay_ms=1.0))

    assert test1.sudo_commands == test2.sudo_commands == []


def test_link_repair_reaches_out_of_scope_link(tmp_path) -> None:
    """Regression: ``otto link repair`` clears a link whose endpoint has left the scope."""
    ctx, test1, test2, _ = _scoped(tmp_path, targets=["test1", "test3"])
    test2.qdisc_texts = [_DELAY_20, ""]  # impaired, then clear after the repair
    assert ctx.lab.static_links() == []  # the view holds no link to repair

    result = runner.invoke(link_app, ["repair", "edge"])

    assert result.exit_code == 0, result.output
    assert "tc qdisc del dev eth1.200 root" in test2.sudo_commands
    assert test1.sudo_commands == []
