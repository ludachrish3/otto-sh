"""repo_targets is the ONLY membership logic (spec §5); these pin its semantics."""

import re
import textwrap
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from otto.config.repo import Repo
from otto.config.scope import (
    ProjectScopeConfig,
    active,
    inactive_before_lab,
    repo_targets,
    switched_off,
)
from tests._fixtures.scoping import verdict
from tests._fixtures.sutrepo import make_sut_repo


def _cfg(labs, hosts=(".*",)):
    return ProjectScopeConfig(
        lab_patterns=[re.compile(p) for p in labs],
        host_patterns=[re.compile(p) for p in hosts],
    )


def test_fullmatch_not_search_on_lab_names():
    # `bench` must NOT match `bench-overflow` — fullmatch, not substring (D6).
    assert repo_targets(_cfg(["bench"]), "bench", "h1")
    assert not repo_targets(_cfg(["bench"]), "bench-overflow", "h1")


def test_fullmatch_not_search_on_host_ids():
    cfg = _cfg([".*"], hosts=["host-1"])
    assert repo_targets(cfg, "lab", "host-1")
    assert not repo_targets(cfg, "lab", "host-10")


def test_host_patterns_or_together():
    cfg = _cfg([".*"], hosts=["sensor-.*", r"gw-\d+"])
    assert repo_targets(cfg, "lab", "sensor-3")
    assert repo_targets(cfg, "lab", "gw-7")
    assert not repo_targets(cfg, "lab", "camera-1")


def test_both_axes_must_pass():
    cfg = _cfg(["tech-.*"], hosts=["sensor-.*"])
    assert repo_targets(cfg, "tech-1", "sensor-1")
    assert not repo_targets(cfg, "other", "sensor-1")  # lab fails
    assert not repo_targets(cfg, "tech-1", "gw-1")  # host fails


def test_none_scope_targets_everything():
    # An undeclared repo scopes nothing out (fallback feeds off this).
    assert repo_targets(None, "any-lab", "any-host")


def test_no_lab_patterns_targets_nothing():
    # `[project]` with only host_patterns admits no lab: an empty pattern list
    # cannot fullmatch anything. D2's bootstrap check is what turns that into a
    # loud error; the predicate itself stays total and silent.
    assert not repo_targets(_cfg([], hosts=[".*"]), "any-lab", "any-host")


def test_from_spec_compiles_both_axes():
    from otto.models.settings import ProjectScopeSpec

    spec = ProjectScopeSpec.model_validate(
        {"lab_patterns": ["tech-.*"], "host_patterns": [r"gw-\d+"]}
    )
    cfg = ProjectScopeConfig.from_spec(spec)
    assert repo_targets(cfg, "tech-1", "gw-2")
    assert not repo_targets(cfg, "tech-1", "gw-x")


def test_from_spec_none_lab_patterns_compiles_to_empty():
    # `lab_patterns` unset is not "match all" — from_spec must not invent one.
    from otto.models.settings import ProjectScopeSpec

    spec = ProjectScopeSpec.model_validate({})
    cfg = ProjectScopeConfig.from_spec(spec)
    assert cfg.lab_patterns == []
    assert not repo_targets(cfg, "any-lab", "any-host")


def _repo(tmp_path, settings_body=""):
    sut = make_sut_repo(tmp_path / "repo", name="tmp_repo", extra=textwrap.dedent(settings_body))
    return Repo(sut_dir=sut)


def test_repo_without_project_block_has_no_scope(tmp_path):
    # No `[project]` ⇒ None, which repo_targets reads as "targets everything".
    assert _repo(tmp_path).project_scope is None


def test_repo_project_block_compiles_into_scope(tmp_path):
    repo = _repo(
        tmp_path,
        """
        [project]
        lab_patterns = ["tech-.*"]
        host_patterns = ["sensor-.*", 'gw-\\d+']  # TOML literal string: keeps the backslash
        """,
    )

    assert repo.project_scope is not None
    assert repo_targets(repo.project_scope, "tech-1", "gw-7")
    assert not repo_targets(repo.project_scope, "tech-1", "camera-1")
    assert not repo_targets(repo.project_scope, "bench", "gw-7")


def test_repo_project_block_defaults_host_patterns_to_all(tmp_path):
    repo = _repo(
        tmp_path,
        """
        [project]
        lab_patterns = ["bench"]
        """,
    )

    assert repo.project_scope is not None
    assert repo_targets(repo.project_scope, "bench", "anything-at-all")
    assert not repo_targets(repo.project_scope, "bench-overflow", "anything-at-all")


BAD_LAB_PATTERN = """
    [project]
    lab_patterns = ["("]
    """


def test_repo_invalid_pattern_fails_the_parse(tmp_path, monkeypatch):
    """A bad regex must fail settings parse, not surface later as a silent non-match.

    Second leg: and the failure must reach the user carrying the repo it came
    from. Pydantic names the FIELD (``project.lab_patterns``) and nothing else,
    which in a fleet of a dozen repos leaves the reader hunting; bootstrap's
    phase-1 repo-load containment is what supplies the frame. The identity it
    can name is the ``sut_dir`` — the parse died before ``name`` was read out of
    the same file — so the directory, not the declared name, is what is pinned
    (they are deliberately different here so the assertion cannot pass on the
    wrong one).
    """
    with pytest.raises(ValidationError, match="lab_patterns"):
        _repo(tmp_path, BAD_LAB_PATTERN)

    from otto import bootstrap as bs

    sut = make_sut_repo(
        tmp_path / "sensor-fleet-repo", name="sensors", extra=textwrap.dedent(BAD_LAB_PATTERN)
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(sut))
    bs.invalidate()  # a prior test's cached discovery would answer instead
    (surfaced,) = bs.bootstrap().errors  # what `otto <anything>` prints at startup
    assert "sensor-fleet-repo" in str(surfaced)
    assert "lab_patterns" in str(surfaced)


# --------------------------------------------------------------------------
# Resolver (spec §5) and its D3 verdicts.
#
# Real ``Repo`` objects and real hosts throughout: the resolver's whole job is
# to read what settings parsing and lab loading produced, so a hand-built stub
# of either would pin the stub's shape rather than the chain's.
# --------------------------------------------------------------------------


def _toml_list(patterns):
    # TOML literal strings (single quotes) keep backslashes verbatim — a
    # double-quoted `gw-\d+` would arrive as an escape error, not a regex.
    return "[" + ", ".join(f"'{p}'" for p in patterns) + "]"


def _project_repo(tmp_path, name, *, labs=None, hosts=None):
    """A real ``Repo`` whose ``[project]`` block declares *labs* / *hosts*."""
    body = ""
    if labs is not None or hosts is not None:
        lines = ["[project]"]
        if labs is not None:
            lines.append(f"lab_patterns = {_toml_list(labs)}")
        if hosts is not None:
            lines.append(f"host_patterns = {_toml_list(hosts)}")
        body = "\n".join(lines)
    sut = make_sut_repo(tmp_path / name, name=name, extra=body)
    return Repo(sut_dir=sut)


def _fleet(*pairs):
    """``{host_id: host}`` from ``(element, lab)`` pairs, stamped by the factory."""
    from otto.host.factory import create_host_from_dict

    hosts = {}
    for index, (element, lab_name) in enumerate(pairs, start=1):
        host = create_host_from_dict(
            {
                "element": element,
                "os_type": "unix",
                "ip": f"10.0.0.{index}",
                "creds": [{"login": "admin", "password": "admin"}],
            },
            lab_name=lab_name,
        )
        hosts[host.id] = host
    return hosts


def test_declared_and_matching_labs_scopes_to_the_matching_subset(tmp_path):
    from otto.config.scope import resolve_scopes

    repo = _project_repo(tmp_path, "sensors", labs=["tech-.*"])
    hosts = _fleet(("gw-1", "tech-1"), ("sensor-1", "bench"))

    scope = resolve_scopes([repo], ["tech-1", "bench"], hosts)["sensors"]

    assert scope.repo_name == "sensors"
    assert scope.declared is True
    assert scope.excluded is False
    assert scope.applicable_labs == frozenset({"tech-1"})
    assert scope.universe == frozenset({"gw-1"})


def test_declared_zero_applicable_labs_is_excluded(tmp_path):
    # D3's dependency-exclusion / current-abort condition, as a flag.
    from otto.config.scope import resolve_scopes

    repo = _project_repo(tmp_path, "sensors", labs=["sensor-fleet"])

    scope = resolve_scopes([repo], ["bench"], _fleet(("sensor-1", "bench")))["sensors"]

    assert scope.excluded is True
    assert scope.applicable_labs == frozenset()
    assert scope.universe == frozenset()


def test_declared_labs_with_zero_matching_hosts_is_not_excluded(tmp_path):
    # The lab applies, so the repo is NOT out of scope for this run — its
    # universe is merely empty, which is a different verdict with a different
    # message. Collapsing the two would tell a user to load another lab when
    # the lab was right and the host pattern was wrong.
    from otto.config.scope import resolve_scopes

    repo = _project_repo(tmp_path, "sensors", labs=["bench"], hosts=[r"gw-\d+"])

    scope = resolve_scopes([repo], ["bench"], _fleet(("sensor-1", "bench")))["sensors"]

    assert scope.excluded is False
    assert scope.applicable_labs == frozenset({"bench"})
    assert scope.universe == frozenset()


def test_undeclared_repo_feeds_the_whole_lab_fallback(tmp_path):
    # §6's fallback: a repo with no `[project]` scopes nothing out, so its
    # universe is every loaded host — that set is what the fallback iterates.
    from otto.config.scope import resolve_scopes

    repo = _project_repo(tmp_path, "plain")
    hosts = _fleet(("gw-1", "tech-1"), ("sensor-1", "bench"))

    scope = resolve_scopes([repo], ["tech-1", "bench"], hosts)["plain"]

    assert scope.declared is False
    assert scope.excluded is False
    assert scope.applicable_labs == frozenset({"tech-1", "bench"})
    assert scope.universe == frozenset(hosts)


def test_partial_match_in_a_two_lab_combo_keeps_only_the_matching_component(tmp_path):
    # `otto -l a+b` loads one composite Lab whose components are ["a", "b"];
    # a repo declaring only `a` applies to that component alone, and the host
    # from `b` stays out even though `host_patterns` would admit it.
    from otto.config.scope import resolve_scopes

    repo = _project_repo(tmp_path, "sensors", labs=["a"], hosts=[".*"])
    hosts = _fleet(("h-a", "a"), ("h-b", "b"))

    scope = resolve_scopes([repo], ["a", "b"], hosts)["sensors"]

    assert scope.applicable_labs == frozenset({"a"})
    assert scope.universe == frozenset({"h-a"})


def test_exclude_ids_removes_a_host_the_patterns_admit(tmp_path):
    """The built-in ``local`` leaves every universe — by the caller's list, not by name.

    Both legs on purpose: the first INJECTS the hostile condition (``.*`` over
    a lab that holds ``local`` really does admit it), so the second proves
    ``exclude_ids`` is what removes it. Without the first leg the assertion
    would pass just as well against a resolver that never saw the host.

    Why the mechanism is a caller-supplied id set rather than a check for the
    built-in id in here: ``otto.config`` must not import ``otto.host``, and
    ``local``'s ``source_lab`` stamp is CLI-order-dependent under ``-l a+b``,
    so keying its membership on that stamp would make ``a+b`` and ``b+a``
    resolve differently.
    """
    from otto.config.scope import resolve_scopes
    from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID, make_builtin_local_host

    repo = _project_repo(tmp_path, "sensors", labs=["bench"], hosts=[".*"])
    local = make_builtin_local_host()
    local.source_lab = "bench"
    hosts = {**_fleet(("sensor-1", "bench")), local.id: local}

    admitted = resolve_scopes([repo], ["bench"], hosts)["sensors"]
    assert BUILTIN_LOCAL_HOST_ID in admitted.universe

    scoped = resolve_scopes(
        [repo], ["bench"], hosts, exclude_ids=frozenset({BUILTIN_LOCAL_HOST_ID})
    )["sensors"]
    assert scoped.universe == frozenset({"sensor-1"})


def test_excluded_current_repo_aborts_naming_loaded_labs_and_its_patterns(tmp_path):
    from otto.bootstrap import ProjectScopeError
    from otto.config.scope import require_current_scope, resolve_scopes

    repo = _project_repo(tmp_path, "sensors", labs=["sensor-fleet", "tech-.*"])
    scopes = resolve_scopes([repo], ["bench", "lab-2"], _fleet(("sensor-1", "bench")))

    with pytest.raises(ProjectScopeError) as excinfo:
        require_current_scope(scopes, "sensors")

    message = str(excinfo.value)
    assert "sensors" in message  # whose declaration is wrong
    assert "bench" in message  # what was loaded
    assert "lab-2" in message
    assert "sensor-fleet" in message  # what was declared
    assert "tech-.*" in message
    assert "settings.toml" in message  # what to change


def test_current_repo_with_empty_universe_aborts_naming_the_host_patterns(tmp_path):
    from otto.bootstrap import ProjectScopeError
    from otto.config.scope import require_current_scope, resolve_scopes

    repo = _project_repo(tmp_path, "sensors", labs=["bench"], hosts=[r"gw-\d+", "cam-.*"])
    scopes = resolve_scopes([repo], ["bench"], _fleet(("sensor-1", "bench")))

    with pytest.raises(ProjectScopeError) as excinfo:
        require_current_scope(scopes, "sensors")

    message = str(excinfo.value)
    assert r"gw-\d+" in message  # the patterns that matched nothing
    assert "cam-.*" in message
    assert "bench" in message  # the lab they were applied to
    assert "settings.toml" in message


def test_healthy_current_repo_returns_none(tmp_path):
    from otto.config.scope import require_current_scope, resolve_scopes

    repo = _project_repo(tmp_path, "sensors", labs=["bench"], hosts=["sensor-.*"])
    scopes = resolve_scopes([repo], ["bench"], _fleet(("sensor-1", "bench")))

    assert require_current_scope(scopes, "sensors") is None


def test_undeclared_current_repo_never_aborts_even_with_an_empty_fleet(tmp_path):
    # The hostile condition injected deliberately: an undeclared repo's
    # universe is empty here because the lab is. That is the fallback's
    # business (§6), not an abort — a product-less repo must keep working
    # exactly as it did before `[project]` existed.
    from otto.config.scope import require_current_scope, resolve_scopes

    repo = _project_repo(tmp_path, "plain")
    scopes = resolve_scopes([repo], ["bench"], {})

    assert scopes["plain"].universe == frozenset()
    assert require_current_scope(scopes, "plain") is None


def test_excluded_dependency_does_not_abort_the_current_repo(tmp_path):
    # D3's asymmetry: a dependency out of scope is skipped (the flag the
    # orchestrator reads), only the CURRENT repo aborts.
    from otto.config.scope import require_current_scope, resolve_scopes

    current = _project_repo(tmp_path, "app", labs=["bench"])
    dependency = _project_repo(tmp_path, "dep", labs=["nowhere"])
    scopes = resolve_scopes([current, dependency], ["bench"], _fleet(("sensor-1", "bench")))

    assert scopes["dep"].excluded is True
    assert require_current_scope(scopes, "app") is None


@pytest.mark.parametrize(
    ("name", "declaration", "lab_hosts", "unusable"),
    [
        ("healthy", {"labs": ["bench"], "hosts": ["sensor-.*"]}, (("sensor-1", "bench"),), False),
        ("excluded", {"labs": ["nowhere"]}, (("sensor-1", "bench"),), True),
        ("starved", {"labs": ["bench"], "hosts": [r"gw-\d+"]}, (("sensor-1", "bench"),), True),
        ("undeclared", {}, (("sensor-1", "bench"),), False),
        ("undeclared_over_an_empty_lab", {}, (), False),
    ],
)
def test_the_abort_and_the_skip_read_one_predicate(
    tmp_path, name, declaration, lab_hosts, unusable
):
    """``unusable_scope`` must answer exactly what ``require_current_scope`` aborts on.

    THE TWO SITES ARE A PAIR AND MUST NOT DRIFT. D3 aborts the current repo
    and skips a dependency on the SAME condition — declared, and either no
    loaded lab applies or no host in the applicable ones matches — so the
    orchestrator's skip reads this predicate rather than a second copy of the
    condition, exactly as both sites already render one
    ``_unusable_scope_message``. Re-inlining either half here (dropping the
    universe clause, say, or letting the undeclared fallback through) reds this
    row-by-row agreement, whichever end it was inlined at.

    The last two rows are the carve-out that makes the predicate more than
    ``not universe``: an undeclared repo is the whole-lab fallback (§6) even
    when the lab it falls back to is empty.
    """
    from otto.bootstrap import ProjectScopeError
    from otto.config.scope import require_current_scope, resolve_scopes, unusable_scope

    repo = _project_repo(tmp_path, name, **declaration)
    scopes = resolve_scopes([repo], ["bench"], _fleet(*lab_hosts))

    aborted = False
    try:
        require_current_scope(scopes, name)
    except ProjectScopeError:
        aborted = True

    assert unusable_scope(scopes[name]) is unusable
    assert aborted is unusable


def test_unknown_current_repo_name_returns_none(tmp_path):
    # No verdict was resolved for that name (a repo bootstrap skipped, say), so
    # there is nothing to enforce — and inventing an abort here would turn an
    # already-reported bootstrap failure into a second, more confusing one.
    from otto.config.scope import require_current_scope

    assert require_current_scope({}, "absent") is None


# --------------------------------------------------------------------------
# scope_for_repo (spec §5): the NAME -> declaration hop the ingest gate needs.
#
# Provider registrations carry the registering repo's name; repo_targets judges
# a compiled config. Every "cannot answer" answers None — which repo_targets
# reads as ADMITS — because the seam that asks runs in bare library use and in
# every factory unit test, where there is no bootstrap to ask at all.
# --------------------------------------------------------------------------


def test_scope_for_repo_returns_the_named_repos_declaration(tmp_path, monkeypatch):
    from otto import config as config_mod
    from otto.config.scope import scope_for_repo

    wanted = _project_repo(tmp_path, "sensors", labs=["bench"], hosts=["sensor-.*"])
    other = _project_repo(tmp_path, "gateways", labs=["bench"], hosts=["gw-.*"])
    # `other` first, so a lookup that returned the FIRST declaration it saw
    # rather than the matching one answers with the wrong repo's fleet.
    monkeypatch.setattr(config_mod, "get_repos", lambda: [other, wanted])

    scope = scope_for_repo("sensors")

    assert scope is wanted.project_scope
    assert repo_targets(scope, "bench", "sensor-1")
    assert not repo_targets(scope, "bench", "gw-1")


def test_scope_for_repo_admits_an_owner_no_repo_claims(tmp_path, monkeypatch):
    # An owner otto cannot place is NOT the unknown-owner refusal scoped_ids
    # raises. There the caller named a repo to be bounded BY; here a provider
    # arrived carrying a name this process cannot resolve (a stale marker, a
    # repo bootstrap skipped), and refusing ingest over it would take a working
    # lab offline. The gate is a narrowing, so its failure mode is to narrow
    # nothing.
    from otto import config as config_mod
    from otto.config.scope import scope_for_repo

    known = _project_repo(tmp_path, "sensors", labs=["bench"], hosts=["sensor-.*"])
    monkeypatch.setattr(config_mod, "get_repos", lambda: [known])

    assert scope_for_repo("ghost") is None


def test_scope_for_repo_of_an_undeclared_repo_is_none(tmp_path, monkeypatch):
    # A known repo with no `[project]` block: None again, and for the same
    # reason repo_targets reads None as everything — it scoped nothing out.
    from otto import config as config_mod
    from otto.config.scope import scope_for_repo

    plain = _project_repo(tmp_path, "toolsrepo")
    monkeypatch.setattr(config_mod, "get_repos", lambda: [plain])

    assert scope_for_repo("toolsrepo") is None


def test_scope_for_repo_never_asks_config_for_an_unowned_registration(monkeypatch):
    # Kills: looking the owner up before testing it for None. `get_repos()`
    # bootstraps lazily, so a needless call here would drag a full composition
    # root into every library import that registers a provider outside a repo.
    from otto import config as config_mod
    from otto.config.scope import scope_for_repo

    asked = []

    def _record():
        asked.append(1)
        return []

    monkeypatch.setattr(config_mod, "get_repos", _record)

    assert scope_for_repo(None) is None
    assert asked == [], "a registration made outside any repo has nothing to look up"


def test_scope_for_repo_admits_when_config_cannot_be_reached(monkeypatch):
    # The hostile condition is INJECTED, not inherited: `get_repos()` raising is
    # what a bare `import otto` or an in-process unit test looks like from here,
    # and a lookup that let that propagate would turn every such use into an
    # ingest crash.
    from otto import config as config_mod
    from otto.config.scope import scope_for_repo

    def _unavailable():
        raise RuntimeError("no bootstrap in this process")

    monkeypatch.setattr(config_mod, "get_repos", _unavailable)

    assert scope_for_repo("sensors") is None


def test_scope_for_repo_admits_when_config_answers_with_no_repos(monkeypatch):
    # The quieter half of "cannot be reached": a bootstrap that came back with
    # nothing to iterate rather than one that blew up. Same verdict, and it must
    # not become a TypeError on the way there — the lookup has to be TOTAL, or
    # the carve-out it exists to provide is conditional on config's mood.
    from otto import config as config_mod
    from otto.config.scope import scope_for_repo

    monkeypatch.setattr(config_mod, "get_repos", lambda: None)
    assert scope_for_repo("sensors") is None

    monkeypatch.setattr(config_mod, "get_repos", list)
    assert scope_for_repo("sensors") is None


def _ctx(include=(), exclude=(), scopes=None):
    return SimpleNamespace(
        include_projects=tuple(include),
        exclude_projects=tuple(exclude),
        scopes=dict(scopes or {}),
    )


class TestActive:
    """Every cell of (switch state x labs-loaded x scope verdict) — spec section 1."""

    def test_exclude_switch_wins_over_everything(self):
        ctx = _ctx(exclude=("repo-a",), scopes={"repo_a": verdict("repo_a")})
        assert active("repo_a", ctx) is False

    def test_exclude_matches_pep503_normalized(self):
        # The stored tuple is normalized; the queried name is raw.
        assert active("Repo.A", _ctx(exclude=("repo-a",))) is False

    def test_include_switch_overrides_an_excluded_verdict(self):
        ctx = _ctx(include=("repo-a",), scopes={"repo_a": verdict("repo_a", excluded=True)})
        assert active("repo_a", ctx) is True

    def test_exclude_beats_include_when_both_name_the_repo(self):
        # Branch ORDER, pinned. Task 2's CLI rejects the combination as a usage
        # error, but that is a different layer's rule; nothing should be able to
        # swap these two branches here and stay green.
        ctx = _ctx(include=("repo-a",), exclude=("repo-a",))
        assert active("repo-a", ctx) is False

    def test_the_verdict_lookup_uses_the_raw_repo_name(self):
        # `ctx.scopes` is keyed by `Repo.name` as written, so the lookup must NOT
        # normalize — and the name here is deliberately not normalization-invariant
        # (`repo_a` -> `repo-a`) with no switch set, so the verdict branch is the
        # one under test. A normalized lookup misses the verdict entirely and
        # resolves ACTIVE: the silent widening, with the suite otherwise green.
        ctx = _ctx(scopes={"repo_a": verdict("repo_a", excluded=True)})
        assert active("repo_a", ctx) is False

    def test_a_non_normalized_stored_exclude_still_matches(self):
        # Nothing normalizes on WRITE, so `active` must normalize what it READS.
        # An explicit switch that silently does nothing is the fail-open case.
        ctx = _ctx(exclude=("My_Repo",))
        assert active("my_repo", ctx) is False
        assert active("My_Repo", ctx) is False

    def test_a_non_normalized_stored_include_still_matches(self):
        # The include tuple gets the same read-side treatment; without it the
        # switch is ignored and the excluded verdict below wins.
        ctx = _ctx(include=("My_Repo",), scopes={"my_repo": verdict("my_repo", excluded=True)})
        assert active("my_repo", ctx) is True

    def test_no_labs_loaded_means_everything_active(self):
        assert active("anything", _ctx()) is True

    def test_usable_verdict_is_active(self):
        assert active("r", _ctx(scopes={"r": verdict("r")})) is True

    def test_excluded_verdict_is_inactive(self):
        assert active("r", _ctx(scopes={"r": verdict("r", excluded=True)})) is False

    def test_host_starved_verdict_is_inactive(self):
        assert active("r", _ctx(scopes={"r": verdict("r", universe=())})) is False

    def test_undeclared_repo_keeps_the_whole_lab_fallback(self):
        assert active("r", _ctx(scopes={"r": verdict("r", declared=False, universe=())})) is True

    def test_missing_verdict_is_active(self):
        assert active("stranger", _ctx(scopes={"r": verdict("r")})) is True


class TestSwitchedOff:
    def test_true_only_for_excluded_names(self):
        ctx = _ctx(exclude=("repo-a",))
        assert switched_off("Repo.A", ctx) is True
        assert switched_off("repo_b", ctx) is False

    def test_a_non_normalized_stored_exclude_still_attributes(self):
        # Same read-side normalization as `active`, and it has to be the same or
        # a message would name the switch for one repo while the verdict named
        # the lab for it.
        ctx = _ctx(exclude=("My_Repo",))
        assert switched_off("my_repo", ctx) is True
        assert switched_off("My_Repo", ctx) is True


#: One repo name per spelling class the two predicates must agree about: one
#: already normalized, and one whose raw form differs. ``active`` keys its
#: ``ctx.scopes`` lookup RAW while both switch tests normalize — that asymmetry
#: is the seam where a disagreement between them would hide.
_INVARIANT_NAMES = ["repo-a", "Repo_A"]


class TestInactiveImpliesAttributable:
    """Whenever ``active`` says False, the CALLER can always say WHY.

    ``otto.cli.invoke.refuse_inactive_instruction`` reads
    ``ctx.scopes[owner]`` — subscript, not ``.get`` — on the arm where
    :func:`active` returned False and :func:`switched_off` returned False. That
    is sound only because of an invariant spanning the two functions: ``active``
    reaches False either through the ``_switched(exclude)`` test (which IS
    ``switched_off``'s body) or through ``unusable_scope(verdict)``, which it
    can only reach after ``ctx.scopes.get(repo_name)`` returned a verdict. So
    "inactive and not switched off" implies "a verdict is present under that
    exact key".

    Nothing enforced that. A third False path added ahead of the ``.get`` — a
    "declares no hosts at all" short-circuit, say — would keep both functions
    individually correct and turn the gate's subscript into a ``KeyError``
    escaping the leaf preamble as a traceback. This class is the enforcement:
    it asserts the IMPLICATION over the whole table rather than the two
    functions separately, so the invariant fails here, in a scope unit test,
    rather than in a CLI refusal a user is looking at.

    The fix if it ever fires is a new message arm in the gate, NOT a blank
    fallback: a refusal that names no cause is a plausible, content-free error,
    which is worse than the loud failure it replaced.
    """

    def _verdicts(self, name):
        """Every verdict shape ``resolve_scopes`` can produce, plus 'no verdict'."""
        return {
            "absent": None,
            "healthy": verdict(name),
            "excluded": verdict(name, excluded=True),
            "host-starved": verdict(name, universe=()),
            "undeclared": verdict(name, declared=False, universe=()),
        }

    def test_inactive_is_always_either_switched_off_or_carries_a_verdict(self):
        import itertools

        checked = 0
        inactive_seen = 0
        for name in _INVARIANT_NAMES:
            for verdict_kind, scope in self._verdicts(name).items():
                # Both the declared key and a foreign one, so a table that only
                # ever stored the repo under test cannot hide a missing key.
                scope_sets = {"own-key": {} if scope is None else {name: scope}}
                scope_sets["foreign-key-only"] = {"someone-else": verdict("someone-else")}
                for switches, (scope_label, scopes) in itertools.product(
                    [
                        ((), ()),
                        ((name,), ()),
                        ((), (name,)),
                        ((name,), (name,)),
                        (("repo-a",), ()),
                        ((), ("repo-a",)),
                    ],
                    scope_sets.items(),
                ):
                    include, exclude = switches
                    ctx = _ctx(include=include, exclude=exclude, scopes=scopes)
                    checked += 1
                    if active(name, ctx):
                        continue
                    inactive_seen += 1
                    where = (
                        f"name={name!r} verdict={verdict_kind} scopes={scope_label} "
                        f"include={include} exclude={exclude}"
                    )
                    assert switched_off(name, ctx) or name in ctx.scopes, (
                        f"active() returned False with no attributable cause ({where}). "
                        "refuse_inactive_instruction subscripts ctx.scopes[owner] on "
                        "exactly this arm and would raise KeyError at the user."
                    )
        # The loop above is an IMPLICATION, so it passes vacuously if nothing in
        # the table is ever inactive — the exact way this guard could rot into
        # one that cannot fail. Both counts are asserted: the table is the size
        # it should be, and it really does exercise the arm under test.
        assert checked == 120, checked
        assert inactive_seen > 0, "no cell was inactive — the implication held vacuously"


class TestInactiveBeforeLab:
    """The pre-lab projection used by bootstrap-error demotion (plan deviation 1)."""

    def _cfg(self, *labs):
        return ProjectScopeConfig(lab_patterns=[re.compile(p) for p in labs], host_patterns=[])

    def test_no_selection_never_demotes(self):
        assert inactive_before_lab(self._cfg("unix"), None) is False
        assert inactive_before_lab(self._cfg("unix"), []) is False

    def test_undeclared_repo_never_demotes(self):
        assert inactive_before_lab(None, ["unix"]) is False

    def test_non_matching_selection_is_inactive(self):
        assert inactive_before_lab(self._cfg("unix_alt"), ["unix"]) is True

    def test_any_matching_component_keeps_it_active(self):
        assert inactive_before_lab(self._cfg("unix_alt"), ["unix", "unix_alt"]) is False
