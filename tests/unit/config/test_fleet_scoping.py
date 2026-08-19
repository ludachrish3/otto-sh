"""The ambient project universe in both fleet surfaces (spec §6, D6, §10 row 5).

Fleet walks stopped iterating "every host in the loaded lab" and started
iterating "every host some repo declared an interest in". These tests count
CONTACTS — the set a walk actually reaches, asserted with ``==`` — rather than
inspecting results, because the failure this design exists to prevent is a walk
that is quietly too WIDE, and a wrongly-widened walk succeeds on every host it
should never have touched.

Real ``Repo`` objects (parsed from a real ``settings.toml``) and real hosts
(built by the factory, which is what stamps ``source_lab``) throughout: the
scoping chain reads what settings parsing and lab loading produced, so a stub of
either would pin the stub's shape instead of the chain's.
"""

import logging
import re
import textwrap

import pytest

from otto.bootstrap import ProjectScopeError
from otto.config.lab import Lab
from otto.config.repo import Repo
from otto.config.scope import EmptySelectionError
from otto.context import OttoContext, set_context
from tests._fixtures.sutrepo import make_sut_repo


def _toml_list(patterns):
    # TOML literal strings (single quotes) keep backslashes verbatim.
    return "[" + ", ".join(f"'{p}'" for p in patterns) + "]"


def _repo(tmp_path, name, *, labs=None, hosts=None):
    """A real ``Repo`` whose ``[project]`` block declares *labs* / *hosts*.

    Both ``None`` writes no ``[project]`` table at all — the undeclared repo the
    whole-lab fallback is built on, which is a different thing from a repo that
    declared an empty list.
    """
    body = ""
    if labs is not None or hosts is not None:
        lines = ["[project]"]
        if labs is not None:
            lines.append(f"lab_patterns = {_toml_list(labs)}")
        if hosts is not None:
            lines.append(f"host_patterns = {_toml_list(hosts)}")
        body = "\n".join(lines)
    return Repo(sut_dir=make_sut_repo(tmp_path / name, name=name, extra=textwrap.dedent(body)))


def _host(element, lab_name, octet):
    from otto.host.factory import create_host_from_dict

    return create_host_from_dict(
        {
            "element": element,
            "os_type": "unix",
            "ip": f"10.0.0.{octet}",
            "creds": [{"login": "admin", "password": "admin"}],
        },
        lab_name=lab_name,
    )


def _lab(*pairs, component_names=None):
    """A ``Lab`` holding ``(element, source_lab)`` hosts, stamped by the factory."""
    names = list(component_names or sorted({lab_name for _, lab_name in pairs}))
    lab = Lab(name="+".join(names), component_names=names)
    for octet, (element, lab_name) in enumerate(pairs, start=1):
        lab.add_host(_host(element, lab_name, octet))
    return lab


@pytest.fixture
def scoped_context(monkeypatch):
    """Build and install an ``OttoContext`` whose scopes resolve over given repos.

    ``OttoContext.scopes`` reads ``otto.config.get_ordered_repos`` lazily, so
    patching that one seam is what lets a unit test declare a fleet of interest
    without standing up a bootstrap.

    Installation is not undone here: the root conftest's autouse
    ``_reset_otto_context`` snapshot-restores the ContextVar after every test,
    and a ``reset_context(token)`` of our own cannot work anyway — the async
    tests below run their body in a COPY of the context, so the token is from
    a different Context object and ``ContextVar.reset`` raises.
    """

    def _install(lab, repos):
        monkeypatch.setattr("otto.config.get_ordered_repos", lambda: list(repos))
        ctx = OttoContext(lab=lab)
        set_context(ctx)
        return ctx

    return _install


async def _contacted(ctx, **kwargs):
    """Ids a ``do_for_all_hosts`` walk actually reached, in walk order."""
    seen: list[str] = []

    async def _touch(host):
        seen.append(host.id)
        return host.id

    await ctx.do_for_all_hosts(_touch, **kwargs)
    return seen


# ── the base set ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walk_contacts_exactly_the_universe(tmp_path, scoped_context):
    """Three hosts across two labs; one repo declares one of them — the walk hits it alone."""
    lab = _lab(("h1", "a"), ("h2", "a"), ("h3", "b"))
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=["h1"])
    ctx = scoped_context(lab, [repo])

    assert await _contacted(ctx) == ["h1"]


@pytest.mark.asyncio
async def test_no_declarations_means_whole_lab(tmp_path, scoped_context):
    """The §6 fallback: no repo declares ``[project]`` → every host, exactly as before."""
    lab = _lab(("h1", "a"), ("h2", "a"), ("h3", "b"))
    repo = _repo(tmp_path, "r1")
    ctx = scoped_context(lab, [repo])

    assert not ctx.scopes["r1"].declared
    assert sorted(await _contacted(ctx)) == ["h1", "h2", "h3"]


@pytest.mark.asyncio
async def test_a_visible_undeclared_repo_demands_nothing_new_from_hosts(tmp_path, scoped_context):
    """Zero declarations must not read ``source_lab`` — only a declaration raises the bar.

    Repos are routinely VISIBLE to tests and library code without declaring a
    ``[project]`` table: the integration tree's session fixture leaves
    ``OTTO_SUT_DIRS`` pointing at ``tests/repo1`` for the rest of its worker,
    so any later fleet walk resolves scopes over real, undeclared repos. Before
    this feature no fleet walk read any host attribute beyond ``id``; a lab of
    pre-scoping host objects (or protocol-thin test doubles) must keep walking
    exactly as it did. The host below deliberately lacks ``source_lab`` — a
    ``resolve_scopes`` that evaluates ``host.source_lab`` for an UNDECLARED
    repo crashes here with AttributeError, which is how this regression
    surfaced (make coverage, integration-primed worker, two victims in
    ``test_context.py``).
    """

    class _PreScopingHost:
        """A host as the world looked before [project] existed: id and a verb."""

        def __init__(self, host_id):
            self.id = host_id
            self.contacted = []

        async def poke(self):
            self.contacted.append(self.id)

    lab = Lab(name="a")
    bare = _PreScopingHost("h1")
    lab.hosts["h1"] = bare
    ctx = scoped_context(lab, [_repo(tmp_path, "r1")])

    assert not ctx.scopes["r1"].declared
    assert ctx.scopes["r1"].universe == frozenset({"h1"})

    async def _verb(host):
        await host.poke()

    await ctx.do_for_all_hosts(_verb)
    assert bare.contacted == ["h1"]


@pytest.mark.asyncio
async def test_union_for_plain_context(tmp_path, scoped_context):
    """Two repos with disjoint universes — the plain context walks their union (D7).

    ``h3`` is in neither universe, so the assertion cannot pass by accident on a
    walk that simply stopped filtering.
    """
    lab = _lab(("h1", "a"), ("h2", "a"), ("h3", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=["h1"]),
        _repo(tmp_path, "r2", labs=["a"], hosts=["h2"]),
    ]
    ctx = scoped_context(lab, repos)

    assert sorted(await _contacted(ctx)) == ["h1", "h2"]


@pytest.mark.asyncio
async def test_undeclared_repo_alongside_a_declaring_one_does_not_widen_the_fleet(
    tmp_path, scoped_context
):
    """The fallback is "NO repo declares", not "some repo did not".

    An undeclared repo scopes nothing out for ITSELF (``repo_targets(None, …)``
    is True for every host), so a union that folded its verdict in would
    silently restore the whole lab the moment a product-less repo shared a run.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [_repo(tmp_path, "r1", labs=["a"], hosts=["h1"]), _repo(tmp_path, "r2")]
    ctx = scoped_context(lab, repos)

    assert await _contacted(ctx) == ["h1"]


@pytest.mark.asyncio
async def test_owner_scopes_the_walk_to_that_repos_universe(tmp_path, scoped_context):
    """``_scope_owner`` picks ONE repo's universe out of the union.

    The seam the repo-scoped context view (spec §7) is built on: which host set
    a walk gets comes from which OBJECT the call goes through, so this is the
    only place the owner is spelled as an argument.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=["h1"]),
        _repo(tmp_path, "r2", labs=["a"], hosts=["h2"]),
    ]
    ctx = scoped_context(lab, repos)

    assert await _contacted(ctx, _scope_owner="r1") == ["h1"]
    assert await _contacted(ctx, _scope_owner="r2") == ["h2"]


@pytest.mark.asyncio
async def test_lab_axis_bounds_the_universe_too(tmp_path, scoped_context):
    """Two labs, both hosts admitted on the host axis — ``lab_patterns`` separates them."""
    lab = _lab(("gw-1", "a"), ("gw-2", "b"))
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=[".*"])
    ctx = scoped_context(lab, [repo])

    assert await _contacted(ctx) == ["gw-1"]


# ── pattern= is a fullmatch WITHIN the base set (D6) ──────────────────────────


def test_pattern_is_fullmatch_not_search(tmp_path, scoped_context):
    """``h`` no longer selects ``h1``; ``h.*`` does. The deliberate break (§9)."""
    lab = _lab(("h1", "a"), ("h2", "a"))
    ctx = scoped_context(lab, [_repo(tmp_path, "r1")])

    with pytest.raises(EmptySelectionError):
        list(ctx.all_hosts(re.compile("h")))
    assert sorted(h.id for h in ctx.all_hosts(re.compile("h.*"))) == ["h1", "h2"]


def test_pattern_selects_within_the_universe_never_beyond_it(tmp_path, scoped_context):
    """A pattern picks a SUBSET of the universe — never a superset (§1).

    ``h.*`` fullmatches both hosts; only the declared one is contacted, so this
    reds for a surface that applies the pattern to the lab instead.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=["h1"])
    ctx = scoped_context(lab, [repo])

    assert [h.id for h in ctx.all_hosts(re.compile("h.*"))] == ["h1"]


def test_zero_match_error_names_pattern_and_hint(tmp_path, scoped_context):
    """The D6 guard's message: the pattern, the base-set size, and the ``.*`` hint."""
    lab = _lab(("h1", "a"), ("h2", "a"))
    ctx = scoped_context(lab, [_repo(tmp_path, "r1")])

    with pytest.raises(EmptySelectionError, match=r"h\b.*\.\*") as excinfo:
        list(ctx.all_hosts(re.compile("h")))
    message = str(excinfo.value)
    assert "'h'" in message
    assert "'h.*'" in message  # the actionable hint, not just the word "wildcard"
    assert "2" in message  # the base-set size the pattern was matched against


@pytest.mark.asyncio
async def test_zero_match_guard_fires_on_do_for_all_hosts_too(tmp_path, scoped_context):
    """The dispatch surface must not be the quiet one — it walks through ``all_hosts``."""
    lab = _lab(("h1", "a"), ("h2", "a"))
    ctx = scoped_context(lab, [_repo(tmp_path, "r1")])

    with pytest.raises(EmptySelectionError):
        await _contacted(ctx, pattern=re.compile("h"))


def test_no_pattern_never_raises_on_an_empty_lab(scoped_context):
    """No pattern, no selection — an empty walk fails only when something SELECTED."""
    ctx = scoped_context(Lab(name="empty"), [])

    assert list(ctx.all_hosts()) == []


def test_a_pattern_over_an_empty_lab_does_not_blame_the_pattern(scoped_context):
    """An empty BASE SET is not a bad selection, and must not be reported as one.

    Nothing declared here, so the fleet is the lab and the lab is empty — the
    long-standing "no hosts, no walk" behavior every lab-less CLI path relies
    on to reach its own validation. Blaming the regex would send the reader to
    rewrite a pattern that was never the problem.
    """
    ctx = scoped_context(Lab(name="empty"), [])

    assert list(ctx.all_hosts(re.compile(".*"))) == []


# ── §10 row 5: an empty effective fleet is loud ───────────────────────────────


def test_empty_base_set_with_a_declaring_repo_raises(tmp_path, scoped_context):
    """Every contributing repo excluded → the D3 error, not a silent no-op walk."""
    lab = _lab(("h1", "a"), ("h2", "a"))
    repo = _repo(tmp_path, "r1", labs=["some-other-lab"], hosts=[".*"])
    ctx = scoped_context(lab, [repo])

    with pytest.raises(ProjectScopeError) as excinfo:
        list(ctx.all_hosts())
    message = str(excinfo.value)
    assert "r1" in message
    assert "loaded labs" in message


def test_owner_bound_empty_walk_blames_the_owner_not_the_fleet(tmp_path, scoped_context):
    """One repo's empty fleet is reported as ONE repo's, with its own file to edit.

    The union here is healthy — ``r1`` walks ``h1`` fine — so the fleet-shaped
    "every fleet walk would be empty" would be a false statement that also
    lists ``r1`` as a suspect and points at ``r1``'s settings.toml. The reader
    of an ``r2``-bound walk must be sent to ``r2``.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repo1 = _repo(tmp_path, "r1", labs=["a"], hosts=["h1"])
    repo2 = _repo(tmp_path, "r2", labs=["a"], hosts=["nothing-here"])
    ctx = scoped_context(lab, [repo1, repo2])

    with pytest.raises(ProjectScopeError) as excinfo:
        list(ctx.all_hosts(_scope_owner="r2"))
    message = str(excinfo.value)
    assert "r2" in message
    assert "nothing-here" in message  # r2's OWN host_patterns
    # Whitespace-normalized: the template line-wraps mid-phrase, so a raw
    # substring check can never fire (review nit — a guard that cannot fail).
    assert "every fleet walk would be empty" not in " ".join(message.split())
    assert "repo 'r1'" not in message
    assert str(repo1.sut_dir) not in message  # not the file to edit
    assert excinfo.value.sut_dir == str(repo2.sut_dir)


def test_owner_bound_walk_with_no_applicable_lab_gets_the_lab_axis_message(
    tmp_path, scoped_context
):
    """The owner framing keeps D3's two-message split — wrong lab is not wrong hosts.

    Sibling of the test above with the failure moved to the OTHER axis, because
    a single "your scope is empty" message would send this reader to widen
    host_patterns when the lab is what does not match.
    """
    lab = _lab(("h1", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=[".*"]),
        _repo(tmp_path, "r2", labs=["some-other-lab"], hosts=[".*"]),
    ]
    ctx = scoped_context(lab, repos)

    with pytest.raises(ProjectScopeError) as excinfo:
        list(ctx.all_hosts(_scope_owner="r2"))
    message = str(excinfo.value)
    assert "lab_patterns" in message
    assert "some-other-lab" in message
    assert "host_patterns" not in message  # the host axis is not the problem


def test_unknown_owner_refuses_rather_than_widening(tmp_path, scoped_context):
    """A walk bound to a repo otto never resolved must not quietly become a lab-wide walk.

    The fallback is right for a KNOWN repo that declared nothing (its own
    verdict admits everything); for a name that is not in the resolved set at
    all it converts a caller's typo into the silent widening this whole design
    exists to prevent.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    ctx = scoped_context(lab, [_repo(tmp_path, "r1", labs=["a"], hosts=["h1"])])

    with pytest.raises(ProjectScopeError) as excinfo:
        list(ctx.all_hosts(_scope_owner="typo"))
    message = str(excinfo.value)
    assert "typo" in message
    assert "r1" in message  # the resolved names, so a typo is diagnosable


@pytest.mark.asyncio
async def test_a_known_owner_that_declared_nothing_still_gets_the_whole_lab(
    tmp_path, scoped_context
):
    """Undeclared is the fallback, even by name — the unknown-owner refusal must not eat it.

    ``r2`` has no ``[project]`` table, so its universe genuinely IS everything;
    refusing it, or narrowing it to ``r1``'s fleet, would both be wrong.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [_repo(tmp_path, "r1", labs=["a"], hosts=["h1"]), _repo(tmp_path, "r2")]
    ctx = scoped_context(lab, repos)

    assert sorted(await _contacted(ctx, _scope_owner="r2")) == ["h1", "h2"]


def test_an_unresolvable_repo_set_leaves_every_owner_walkable(monkeypatch):
    """No scopes at all is not "every owner is unknown" — it is "scoping is off".

    A library context never bootstraps, so refusing an owner there would make
    the repo-scoped view unusable in exactly the environment that has no
    declarations to enforce.
    """

    def _boom():
        raise RuntimeError("no bootstrap here")

    monkeypatch.setattr("otto.config.get_ordered_repos", _boom)
    ctx = OttoContext(lab=_lab(("h1", "a"), ("h2", "a")))

    assert sorted(h.id for h in ctx.all_hosts(_scope_owner="anything")) == ["h1", "h2"]


# ── live re-evaluation, not the resolver's snapshot ───────────────────────────


def _container(parent, service, source_lab):
    from otto.host.docker_host import DockerContainerHost

    host = DockerContainerHost(
        parent=parent,
        container_id=f"cid-{service}",
        project="r1",
        service=service,
        compose_project="otto-r1",
    )
    host.source_lab = source_lab
    return host


@pytest.mark.asyncio
async def test_late_joining_container_is_scoped_live(tmp_path, scoped_context):
    """A container that joins AFTER context creation is scoped, not frozen out (§5).

    The resolver's ``universe`` is a snapshot taken at context creation; a walk
    that iterated it would miss every ``otto docker up`` container. Both
    containers register the same way after creation, so the pair discriminates
    "scoped live" from "not scoped at all".
    """
    lab = _lab(("h1", "a"))
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=["h1", r"h1\.r1\.api"])
    ctx = scoped_context(lab, [repo])
    assert ctx.scopes["r1"].universe == frozenset({"h1"})  # the snapshot, pre-container

    parent = lab.hosts["h1"]
    lab.add_host(_container(parent, "api", "a"))
    lab.add_host(_container(parent, "db", "a"))

    contacted = await _contacted(ctx, include_containers=True)
    assert sorted(contacted) == ["h1", "h1.r1.api"]


# ── include_local / include_containers stay the walk's own knobs (§6) ─────────


def test_local_is_kept_out_of_the_resolved_universe(tmp_path, scoped_context):
    """The context passes ``exclude_ids={local}`` — the built-in runner is never fleet."""
    from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID, make_builtin_local_host

    lab = _lab(("h1", "a"))
    lab.add_host(make_builtin_local_host())
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=[".*"])
    ctx = scoped_context(lab, [repo])

    assert BUILTIN_LOCAL_HOST_ID in lab.hosts  # `.*` really would admit it
    assert ctx.scopes["r1"].universe == frozenset({"h1"})


@pytest.mark.asyncio
async def test_include_local_still_opts_in_under_scoping(tmp_path, scoped_context):
    """Scoping does not take ``include_local=True`` away — the flag applies after it."""
    from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID, make_builtin_local_host

    lab = _lab(("h1", "a"))
    lab.add_host(make_builtin_local_host())
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=[".*"])
    ctx = scoped_context(lab, [repo])

    assert await _contacted(ctx) == ["h1"]
    assert sorted(await _contacted(ctx, include_local=True)) == ["h1", BUILTIN_LOCAL_HOST_ID]


# ── the module-level surface inherits all of it ───────────────────────────────


@pytest.mark.asyncio
async def test_module_level_fleet_surface_is_scoped_too(tmp_path, scoped_context):
    """``otto.config.fleet.all_hosts`` reads the active context — same universe, same guard."""
    from otto.config.fleet import all_hosts as fleet_all_hosts

    lab = _lab(("h1", "a"), ("h2", "a"))
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=["h1"])
    scoped_context(lab, [repo])

    assert [h.id for h in fleet_all_hosts()] == ["h1"]
    with pytest.raises(EmptySelectionError):
        list(fleet_all_hosts(pattern=re.compile("h")))


# ── deliberately unscoped surfaces (§6) ───────────────────────────────────────


def test_the_otto_host_available_listing_ignores_the_universe(tmp_path, scoped_context, capsys):
    """Explicit ``otto host <id>`` targeting beats scoping, and so does its "did you mean".

    ``get_host`` resolves an out-of-universe host, so the listing that fires
    when an id is wrong must offer it too — a user told "Available hosts: h1"
    while ``otto host h2 run`` works is being lied to. Iterating the fleet
    generator here would also let this ERROR path raise the fleet's own
    empty-universe complaint in place of the message the user came for.
    """
    import typer

    from otto.cli.host import _resolve_host

    lab = _lab(("h1", "a"), ("h2", "a"))
    scoped_context(lab, [_repo(tmp_path, "r1", labs=["a"], hosts=["h1"])])

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_host("nope")
    assert excinfo.value.exit_code == 1  # a refusal, not a bare typer.Exit()'s 0
    listed = capsys.readouterr().out
    assert "h1" in listed
    assert "h2" in listed


# ── no config, no scoping ─────────────────────────────────────────────────────


def test_library_context_has_no_scopes_and_walks_everything():
    """The FD-model library context never bootstraps — it keeps today's behavior."""
    from otto.context import LIBRARY_LAB_NAME

    lab = _lab(("h1", "a"), ("h2", "a"))
    lab.name = LIBRARY_LAB_NAME
    ctx = OttoContext(lab=lab)

    assert ctx.scopes == {}
    assert sorted(h.id for h in ctx.all_hosts()) == ["h1", "h2"]


def test_unavailable_bootstrap_falls_back_to_the_whole_lab(monkeypatch):
    """A repo set that cannot be resolved must not brick every fleet walk."""

    def _boom():
        raise RuntimeError("no bootstrap here")

    monkeypatch.setattr("otto.config.get_ordered_repos", _boom)
    ctx = OttoContext(lab=_lab(("h1", "a"), ("h2", "a")))

    assert ctx.scopes == {}
    assert sorted(h.id for h in ctx.all_hosts()) == ["h1", "h2"]


# ── observability ─────────────────────────────────────────────────────────────


def test_declaring_run_logs_the_fleet_of_interest_once(tmp_path, scoped_context, caplog):
    """One line, at resolution, naming the narrowing (§6)."""
    lab = _lab(("h1", "a"), ("h2", "a"), ("h3", "b"))
    repo = _repo(tmp_path, "r1", labs=["a"], hosts=["h1"])
    ctx = scoped_context(lab, [repo])

    with caplog.at_level(logging.INFO, logger="otto.context"):
        first = ctx.scopes
        second = ctx.scopes
    assert first is second  # resolution is cached, so the line cannot repeat
    lines = [r.message for r in caplog.records if "fleet of interest" in r.message]
    assert lines == ["fleet of interest: 1 of 3 lab hosts (1 repos, 0 excluded)"]


def test_undeclared_run_says_nothing(tmp_path, scoped_context, caplog):
    """No declaration, no narrowing, no line — the fallback is not news."""
    ctx = scoped_context(_lab(("h1", "a")), [_repo(tmp_path, "r1")])

    with caplog.at_level(logging.INFO, logger="otto.context"):
        assert ctx.scopes["r1"].declared is False
    assert not [r for r in caplog.records if "fleet of interest" in r.message]


# ── the repo-scoped view (§7) ─────────────────────────────────────────────────


async def _verb_saw(walker, **kwargs):
    """``(host id, owner)`` pairs the verb dispatched through *walker* actually received.

    The owner is read off the VERB, never off the walk. The view's whole claim
    is that ``owner=`` reaches an owner-accepting host verb without any call
    site naming it, so a recorder that ignored its own kwargs would certify the
    membership and miss the injection entirely.
    """
    seen = []

    async def _verb(host, owner=None):
        seen.append((host.id, owner))
        return host.id

    await walker.do_for_all_hosts(_verb, **kwargs)
    return seen


@pytest.mark.asyncio
async def test_view_walk_supplies_owner_to_host_verbs(tmp_path, scoped_context):
    """A walk through ``for_repo`` hands each verb its owner, with no owner at the call site.

    The repo here targets EVERY host, so nothing about the universe can carry
    this assertion — what is being pinned is the injection alone.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    ctx = scoped_context(lab, [_repo(tmp_path, "r1", labs=["a"], hosts=[".*"])])

    assert sorted(await _verb_saw(ctx.for_repo("r1"))) == [("h1", "r1"), ("h2", "r1")]


@pytest.mark.asyncio
async def test_view_walk_is_universe_bounded(tmp_path, scoped_context):
    """The view's walk contacts ITS repo's universe — the other repo's host is not touched.

    ``h2`` belongs to ``r2`` and is present in the lab throughout, so contact
    equality here fails for a view that bound no owner and inherited the plain
    context's union.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=["h1"]),
        _repo(tmp_path, "r2", labs=["a"], hosts=["h2"]),
    ]
    ctx = scoped_context(lab, repos)

    assert [host_id for host_id, _ in await _verb_saw(ctx.for_repo("r1"))] == ["h1"]


@pytest.mark.asyncio
async def test_view_all_hosts_is_universe_bounded_too(tmp_path, scoped_context):
    """The ITERATION surface is bound as well, and it is a separate override.

    ``ProjectActions.status``/``is_clean``/``owns_products`` read
    ``ctx.all_hosts()`` directly rather than dispatching, so a view that bound
    only its dispatch seam would answer those three questions about the whole
    union while every ACTION stayed correctly scoped.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=["h1"]),
        _repo(tmp_path, "r2", labs=["a"], hosts=["h2"]),
    ]
    ctx = scoped_context(lab, repos)

    assert [host.id for host in ctx.for_repo("r1").all_hosts()] == ["h1"]


@pytest.mark.asyncio
async def test_view_run_on_all_hosts_is_universe_bounded(tmp_path, scoped_context, monkeypatch):
    """The command surface is bound too — one unscoped walk on a scoped facade is a trap.

    ``run_on_all_hosts`` is the fleet API a subclass reaches for when no host
    verb fits, and it is reachable on the view like everything else. Delegating
    it untouched would run the command across the whole union from an object
    whose entire purpose is to bound the fleet.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=["h1"]),
        _repo(tmp_path, "r2", labs=["a"], hosts=["h2"]),
    ]
    ctx = scoped_context(lab, repos)
    ran = []

    async def _run(self, cmds, timeout=None):
        ran.append(self.id)
        return []

    monkeypatch.setattr(type(lab.hosts["h1"]), "run", _run)

    assert sorted(await ctx.for_repo("r1").run_on_all_hosts("uname -a")) == ["h1"]
    assert ran == ["h1"]


@pytest.mark.asyncio
async def test_with_owner_false_dispatches_an_ownerless_verb(tmp_path, scoped_context):
    """The explicit opt-out for host verbs that take no owner (``host.cleanup`` and kin).

    ASSERTED ON THE VALUES, deliberately. ``do_for_all_hosts`` captures a
    per-host exception AS A VALUE in the mapping it returns, so an injected
    ``owner=`` would land here as a ``TypeError`` sitting quietly in the
    results — a test that merely awaited the walk would pass either way.
    """
    lab = _lab(("h1", "a"))
    ctx = scoped_context(lab, [_repo(tmp_path, "r1", labs=["a"], hosts=[".*"])])

    async def _ownerless(host):  # the shape of a host verb that takes no owner
        return f"swept:{host.id}"

    results = await ctx.for_repo("r1").do_for_all_hosts(_ownerless, with_owner=False)
    assert results == {"h1": "swept:h1"}


@pytest.mark.asyncio
async def test_a_mismatched_owner_through_the_view_refuses(tmp_path, scoped_context):
    """Naming another repo's owner through a repo's view is a caller bug, said out loud.

    Overwriting it would be the SAFE direction — the walk would act for ``r1``,
    never for ``r2`` — and that is exactly what makes silence wrong here: the
    caller keeps their belief that they acted for ``r2``, and a healthy-looking
    results mapping never contradicts them. A matching owner is redundant
    rather than wrong (``super().install()`` from a subclass that still spells
    the old argument), so it passes through.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=["h1"]),
        _repo(tmp_path, "r2", labs=["a"], hosts=["h2"]),
    ]
    ctx = scoped_context(lab, repos)

    contacted: list[str] = []

    async def _verb(host, owner=None):
        # Record, don't raise: do_for_all_hosts gathers with
        # return_exceptions=True, so a raising probe is CAPTURED as a mapping
        # value and a walk-then-raise mutant sails past pytest.raises
        # (re-review N1 — the mutant passed 35/35 against the raising shape).
        contacted.append(host.id)

    with pytest.raises(ProjectScopeError) as excinfo:
        await ctx.for_repo("r1").do_for_all_hosts(_verb, owner="r2")
    message = str(excinfo.value)
    assert "'r1'" in message  # the view's repo
    assert "'r2'" in message  # and the one that was asked for
    assert contacted == []  # refused BEFORE the walk — no host was reached

    assert await _verb_saw(ctx.for_repo("r1"), owner="r1") == [("h1", "r1")]


@pytest.mark.asyncio
async def test_plain_context_walks_the_union_and_names_no_owner(tmp_path, scoped_context):
    """The negative control for both halves: the plain context is neither bound nor owned.

    Same lab and same repos as the view tests above. A plain walk reaches the
    union (D7) and hands the verb NO owner — scoping and owner-stamping come
    from which object the call went through, so an implementation that moved
    either one onto the context itself is caught here.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    repos = [
        _repo(tmp_path, "r1", labs=["a"], hosts=["h1"]),
        _repo(tmp_path, "r2", labs=["a"], hosts=["h2"]),
    ]
    ctx = scoped_context(lab, repos)

    assert sorted(await _verb_saw(ctx)) == [("h1", None), ("h2", None)]


def test_view_get_host_stays_unscoped(tmp_path, scoped_context):
    """Explicit targeting beats scoping, through the view as through the context (§6).

    ``get_host`` is how a repo's actions reach a host they NAME — a jump host,
    a shared appliance — and the view narrows fleet walks, not lookups.
    """
    lab = _lab(("h1", "a"), ("h2", "a"))
    ctx = scoped_context(lab, [_repo(tmp_path, "r1", labs=["a"], hosts=["h1"])])

    assert ctx.for_repo("r1").get_host("h2") is lab.hosts["h2"]
