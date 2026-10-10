"""``fleet_of_interest`` answers what a fleet walk would walk, without walking it.

Spec ``docs/superpowers/specs/2026-10-06-repo-and-scope-inputs-design.md`` §3
and §8. The ORACLE is the walk itself: ``ctx.all_hosts(...)`` for the union and
``ctx.for_repo(owner).all_hosts(...)`` for one repo, on a non-sentinel context
built on the same lab, repos and ``-E``, and handed one bootstrap result
(``bootstrap=fake_bootstrap_result(...)``) holding those same repos. Over
every generated case the query must equal the walk's ids, in the lab's
order. The three places the two differ on purpose are tested on their own
below.

Every axis of the generator is proved live by a planted divergence in the
function under test (``_MUTANTS``): each one must turn the differential red. A
generator that never reached an axis would let that axis's mutant through, and
the mutation test names it.

Real ``Repo`` objects parsed from real ``settings.toml`` files and real hosts
built by the factory, as in ``test_fleet_scoping.py``: the scoping chain reads
what settings parsing and lab loading produce.
"""

import dataclasses
import itertools
import logging
import re

import pytest

from otto.bootstrap import ProjectScopeError
from otto.config.fleet import fleet_of_interest
from otto.config.scope import EmptySelectionError, scopes_of
from otto.context import LIBRARY_LAB_NAME, OttoContext, set_context
from otto.models.dependencies import normalize_name
from tests._fixtures.bootstrap_seam import fake_bootstrap_result
from tests._fixtures.fleet import _lab, _repo, add_builtin_local, install_scoped_context


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


@dataclasses.dataclass(frozen=True)
class _World:
    """One lab and a pool of repos, built once per module."""

    lab: object
    repos: dict


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Hosts ``h1 h2 h3 h1.r1.api local`` (in that lab order) and eight repos.

    ``h1``/``h2`` sit in lab ``a``, ``h3`` in lab ``b``; the container joins
    lab ``a``; ``local`` is stamped ``a`` (``add_builtin_local`` stamps the
    first component). The repos are chosen so each §8 axis has a case that
    tells a right answer from a wrong one:

    * ``plain`` declares nothing;
    * ``alpha`` admits ``h1`` and the container, ``beta`` admits ``h2``;
    * ``every-a`` admits all of lab ``a``, ``local`` included, while
      ``alpha``/``beta``/``bravo`` do not admit ``local``;
    * ``bravo`` admits lab ``b``; ``ghost`` applies to no loaded lab;
    * ``dup-first`` and ``dup-second`` are both named ``dup`` and admit
      different hosts, so "last wins" is observable.
    """
    base = tmp_path_factory.mktemp("fleet_of_interest")
    lab = _lab(("h1", "a"), ("h2", "a"), ("h3", "b"))
    lab.add_host(_container(lab.hosts["h1"], "api", "a"))
    add_builtin_local(lab)
    repos = {
        "plain": _repo(base, "plain"),
        "alpha": _repo(base, "alpha", labs=["a"], hosts=["h1", r"h1\.r1\.api"]),
        "beta": _repo(base, "beta", labs=["a"], hosts=["h2"]),
        "every-a": _repo(base, "every-a", labs=["a"], hosts=[".*"]),
        "bravo": _repo(base, "bravo", labs=["b"], hosts=[".*"]),
        "ghost": _repo(base, "ghost", labs=["z"], hosts=[".*"]),
        "dup-first": _repo(base / "first", "dup", labs=["a"], hosts=["h2"]),
        "dup-second": _repo(base / "second", "dup", labs=["b"], hosts=[".*"]),
    }
    assert list(lab.hosts) == ["h1", "h2", "h3", "h1.r1.api", "local"]
    return _World(lab=lab, repos=repos)


@dataclasses.dataclass(frozen=True)
class _Case:
    """One generated input: which pool repos, in which order, and the knobs."""

    repo_keys: list
    owner: object
    exclude_projects: object
    include_containers: bool
    include_local: bool


_UNIQUE_KEYS = ["plain", "alpha", "beta", "every-a", "bravo", "ghost"]
_DUPLICATE_LISTS = [
    ["dup-first", "dup-second"],
    ["dup-second", "dup-first"],
    ["alpha", "dup-first", "dup-second"],
    ["alpha", "dup-second", "dup-first"],
]


def _cases(world):
    """Every repo list of up to three unique repos, plus the duplicate-name lists.

    For each list: owner unset and set to each name; ``-E`` unset, naming no
    repo, naming each declaring repo, and (with ``every-a`` present) spelled
    ``Every_A``; and the four flag combinations.
    """
    lists = [
        list(combo) for size in range(4) for combo in itertools.combinations(_UNIQUE_KEYS, size)
    ]
    for keys in lists + _DUPLICATE_LISTS:
        repos = [world.repos[key] for key in keys]
        names = list(dict.fromkeys(repo.name for repo in repos))
        declarers = list(
            dict.fromkeys(repo.name for repo in repos if repo.project_scope is not None)
        )
        excludes = [None, ["nobody"], *([name] for name in declarers)]
        if "every-a" in names:
            excludes.append(["Every_A"])
        for owner, exclude, (containers, local) in itertools.product(
            [None, *names], excludes, itertools.product([False, True], repeat=2)
        ):
            yield _Case(keys, owner, exclude, containers, local)


@pytest.fixture(scope="module")
def oracle(world):
    """``[(case, walked ids or None)]`` for every generated case, computed once.

    ``None`` records a walk that refused an empty declared fleet: §3's first
    stated exception, where the query answers ``[]``. The refusal is checked to
    be exactly that one — the walk's own admissible set, read without the
    refusal, is empty — so no other ``ProjectScopeError`` can hide behind it.

    Module-scoped, so it runs OUTSIDE the root conftest's per-test bootstrap
    state restore: it must never reach the real composition root, whose cached
    discovery would outlive this module and poison later tests on the worker.
    ``otto.bootstrap.discover`` refuses for the fixture's duration, so a future
    reach fails loudly here instead.
    """

    def _refuse_discovery():
        raise AssertionError("the oracle reached the real composition root")

    results = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("otto.bootstrap.discover", _refuse_discovery)
        # One INFO line per context ("fleet of interest: N of M lab hosts")
        # over thousands of cases is noise in every live-log run, and the line
        # is not under test here.
        mp.setattr(logging.getLogger("otto.context"), "disabled", True)
        for case in _cases(world):
            # The context reads its repos from the one result it is handed.
            ctx = OttoContext(
                lab=world.lab,
                exclude_projects=tuple(case.exclude_projects or ()),
                bootstrap=fake_bootstrap_result([world.repos[key] for key in case.repo_keys]),
            )
            surface = ctx if case.owner is None else ctx.for_repo(case.owner)
            try:
                walked = [
                    host.id
                    for host in surface.all_hosts(
                        include_containers=case.include_containers,
                        include_local=case.include_local,
                    )
                ]
            except ProjectScopeError:
                assert not ctx.admissible_ids(case.owner, require_nonempty=False), case
                walked = None
            results.append((case, walked))
    return results


def _mismatches(candidate, world, oracle):
    """Describe every case where *candidate* and the walk disagree."""
    found = []
    for case, walked in oracle:
        expected = [] if walked is None else walked
        try:
            got = candidate(
                world.lab,
                [world.repos[key] for key in case.repo_keys],
                owner=case.owner,
                exclude_projects=(
                    None if case.exclude_projects is None else list(case.exclude_projects)
                ),
                include_containers=case.include_containers,
                include_local=case.include_local,
            )
        except Exception as exc:  # noqa: BLE001 — a candidate that raises has diverged
            got = f"raised {exc!r}"
        if got != expected:
            found.append(f"{case}: walk={walked!r} query={got!r}")
    return found


def test_the_query_equals_the_walk_on_every_generated_case(world, oracle):
    refused = sum(1 for _, walked in oracle if walked is None)
    # Both shapes must be present, or half of the comparison is untested here.
    assert 0 < refused < len(oracle), (refused, len(oracle))
    mismatches = _mismatches(fleet_of_interest, world, oracle)
    assert mismatches == [], f"{len(mismatches)} of {len(oracle)} cases differ:\n" + "\n".join(
        mismatches[:20]
    )


# ── planted divergences, one per §8 axis ──────────────────────────────────────


def _declares(repo):
    return repo.project_scope is not None


def _mutant_no_declaration_is_empty(lab, repos, **kw):
    """Axis: no repo declared. Answers nothing instead of the whole lab."""
    if not any(_declares(repo) for repo in repos):
        return []
    return fleet_of_interest(lab, repos, **kw)


def _mutant_union_is_the_first_declarer(lab, repos, **kw):
    """Axis: several repos declared. The union keeps only the first declarer."""
    if kw["owner"] is None:
        first = next((repo for repo in repos if _declares(repo)), None)
        repos = [repo for repo in repos if not _declares(repo) or repo is first]
    return fleet_of_interest(lab, repos, **kw)


def _mutant_undeclared_widens_the_union(lab, repos, **kw):
    """Axis: a declared repo beside an undeclared one. The undeclared one restores the lab."""
    declaring = [repo for repo in repos if _declares(repo)]
    if kw["owner"] is None and declaring and len(declaring) < len(repos):
        repos = [repo for repo in repos if not _declares(repo)]
    return fleet_of_interest(lab, repos, **kw)


def _mutant_owner_ignored(lab, repos, **kw):
    """Axis: owner set. Answers the union instead of the owner's fleet."""
    return fleet_of_interest(lab, repos, **{**kw, "owner": None})


def _mutant_exclude_ignored(lab, repos, **kw):
    """Axis: ``-E`` on a declaring repo, without an owner. The switch does nothing."""
    return fleet_of_interest(lab, repos, **{**kw, "exclude_projects": None})


def _mutant_exclude_binds_the_owner(lab, repos, **kw):
    """Axis: ``-E`` on a declaring repo, with that repo as owner. The switch empties it."""
    switched = {normalize_name(name) for name in kw["exclude_projects"] or []}
    if kw["owner"] is not None and normalize_name(kw["owner"]) in switched:
        return []
    return fleet_of_interest(lab, repos, **kw)


def _mutant_exclude_not_normalized(lab, repos, **kw):
    """Axis: ``-E`` spelled differently from the name (``Every_A``). Only exact names count."""
    names = {repo.name for repo in repos}
    kept = [name for name in kw["exclude_projects"] or [] if name in names]
    return fleet_of_interest(lab, repos, **{**kw, "exclude_projects": kept})


def _mutant_unknown_exclude_refused(lab, repos, **kw):
    """Axis: an ``-E`` name that matches no repo. The query validates it."""
    known = {normalize_name(repo.name) for repo in repos}
    if any(normalize_name(name) not in known for name in kw["exclude_projects"] or []):
        raise ValueError("unknown --exclude-projects name")
    return fleet_of_interest(lab, repos, **kw)


def _mutant_containers_flag_ignored(lab, repos, **kw):
    """Axis: container hosts with ``include_containers`` on. The flag does nothing."""
    return fleet_of_interest(lab, repos, **{**kw, "include_containers": False})


def _mutant_local_flag_ignored(lab, repos, **kw):
    """Axis: ``local`` with ``include_local`` on. The flag does nothing."""
    return fleet_of_interest(lab, repos, **{**kw, "include_local": False})


def _mutant_local_flag_bypasses_scope(lab, repos, **kw):
    """Axis: a declaring scope that does NOT admit ``local``. The flag adds it anyway."""
    got = fleet_of_interest(lab, repos, **kw)
    if kw["include_local"] and "local" not in got:
        got = [*got, "local"]
    return got


def _mutant_first_duplicate_wins(lab, repos, **kw):
    """Axis: two repos with the same name. The first one wins instead of the last."""
    seen = set()
    kept = []
    for repo in repos:
        if repo.name not in seen:
            seen.add(repo.name)
            kept.append(repo)
    return fleet_of_interest(lab, kept, **kw)


_MUTANTS = {
    "no repo declared": _mutant_no_declaration_is_empty,
    "several declared": _mutant_union_is_the_first_declarer,
    "declared beside undeclared": _mutant_undeclared_widens_the_union,
    "owner set": _mutant_owner_ignored,
    "-E without owner": _mutant_exclude_ignored,
    "-E with owner": _mutant_exclude_binds_the_owner,
    "-E spelling": _mutant_exclude_not_normalized,
    "-E matching no repo": _mutant_unknown_exclude_refused,
    "include_containers": _mutant_containers_flag_ignored,
    "include_local": _mutant_local_flag_ignored,
    "local outside the scope": _mutant_local_flag_bypasses_scope,
    "duplicate names": _mutant_first_duplicate_wins,
}


def test_every_axis_has_a_planted_divergence_the_differential_catches(world, oracle):
    blind = [axis for axis, mutant in _MUTANTS.items() if not _mismatches(mutant, world, oracle)]
    assert blind == [], f"the differential cannot see these planted divergences: {blind}"


# ── §3's three stated exceptions, each on its own ─────────────────────────────


def test_an_empty_declared_fleet_the_walk_refuses_and_the_query_answers_empty(world):
    ghost = world.repos["ghost"]
    ctx = install_scoped_context(world.lab, [ghost])

    with pytest.raises(ProjectScopeError):
        list(ctx.all_hosts())
    with pytest.raises(ProjectScopeError):
        list(ctx.for_repo("ghost").all_hosts())
    assert fleet_of_interest(world.lab, [ghost]) == []
    assert fleet_of_interest(world.lab, [ghost], owner="ghost") == []


def test_an_unknown_owner_is_refused_even_where_the_walk_falls_back(world):
    ctx = install_scoped_context(world.lab, [])
    # The walk: no scope resolved, so an unknown owner falls back to the whole lab.
    assert [host.id for host in ctx.for_repo("x").all_hosts()] == ["h1", "h2", "h3"]
    # The query: the caller handed the repos over, so the whole lab would widen.
    with pytest.raises(ProjectScopeError, match=r"none of the repos it was\s+given is named 'x'"):
        fleet_of_interest(world.lab, [], owner="x")

    alpha = world.repos["alpha"]
    ctx = install_scoped_context(world.lab, [alpha])
    with pytest.raises(ProjectScopeError):
        list(ctx.for_repo("x").all_hosts())
    with pytest.raises(ProjectScopeError, match=r"none of the repos it was\s+given is named 'x'"):
        fleet_of_interest(world.lab, [alpha], owner="x")


def test_on_the_sentinel_lab_the_walk_takes_the_whole_lab_and_the_query_scopes_it(world):
    lab = _lab(("h1", "a"), ("h2", "a"))
    lab.name = LIBRARY_LAB_NAME
    ctx = OttoContext(lab=lab)

    assert [host.id for host in ctx.all_hosts()] == ["h1", "h2"]
    assert fleet_of_interest(lab, [world.repos["alpha"]]) == ["h1"]


# ── purity, and the one membership-flag rule ──────────────────────────────────


def test_the_query_reads_nothing_but_its_arguments(world, monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise AssertionError("fleet_of_interest read the composition root")

    monkeypatch.setattr("otto.bootstrap.bootstrap", _refuse)
    monkeypatch.setattr("otto.bootstrap.discover", _refuse)
    monkeypatch.setattr("otto.config.lab.load_lab", _refuse)
    # The hostile ambient world: an installed context on a decoy lab. A query
    # that read the active context instead of its `lab` argument answers ["d1"].
    set_context(OttoContext(lab=_lab(("d1", "a"))))

    got = fleet_of_interest(world.lab, [world.repos["alpha"]], include_containers=True)
    assert got == ["h1", "h1.r1.api"]

    # The injection is live: the read the walk makes does reach the refusal.
    with pytest.raises(AssertionError, match="read the composition root"):
        scopes_of(OttoContext(lab=world.lab))  # the read is the act under test


def test_every_membership_reader_consults_the_one_flag_rule(world, monkeypatch):
    """The walk, its empty-selection prediction and the query share one helper.

    A planted rule that holds out ``h2`` alone, and admits the container and
    ``local`` with both flags off, can only reach all three readers if each one
    asks the helper rather than repeating the isinstance tests.
    """

    def _hold_out_h2_only(host, *, include_containers, include_local):
        return "include_containers" if host.id == "h2" else None

    monkeypatch.setattr("otto.config.fleet._flag_holding_out", _hold_out_h2_only)
    plain = world.repos["plain"]
    ctx = install_scoped_context(world.lab, [plain])

    expected = ["h1", "h3", "h1.r1.api", "local"]
    assert [host.id for host in ctx.all_hosts()] == expected
    assert fleet_of_interest(world.lab, [plain]) == expected
    with pytest.raises(EmptySelectionError) as excinfo:
        list(ctx.all_hosts(re.compile("h2")))
    assert excinfo.value.excluded_by == ["include_containers"]


def test_owner_is_not_normalized_in_the_query_or_the_walk(world):
    """Spec 4 §3: the owner is not normalized, in the query as in the walk."""
    repos = [world.repos["alpha"], world.repos["beta"]]
    ctx = install_scoped_context(world.lab, repos)

    with pytest.raises(ProjectScopeError):
        list(ctx.for_repo("Alpha").all_hosts())
    with pytest.raises(ProjectScopeError):
        fleet_of_interest(world.lab, repos, owner="Alpha")
