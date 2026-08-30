"""Per-project lab/host scoping: the ONE membership predicate, its config, its resolver.

Spec: ``docs/superpowers/specs/2026-08-18-project-lab-host-scoping-design.md`` §5.

Every consumer — ingest gating, fleet iteration, the resolver, the status
display — answers "does this repo target this host?" by calling
:func:`repo_targets`. One predicate in one module is what makes those answers
agree *structurally*; a second copy of the ``fullmatch``-both-axes rule would
drift the moment one of them grew a special case. :func:`resolve_scopes` is
held to the same rule: it computes the per-repo verdicts by calling the
predicate, never by re-deriving membership from the patterns.

The resolver is PURE — no I/O, no ``bootstrap()``, no lab loading. It is handed
the repos, the loaded lab's component names and the loaded hosts, and returns
verdicts; the caller decides which repo is "current" and when to enforce. That
split is what lets the D3 consequences be *stored* at context creation and
*raised* only at project-layer entry (§5), so a scoping typo never bricks an
explicit ``otto host <id> <verb>``.

Layering: this module sits in ``otto.config`` so ``otto.config.fleet`` can
import it without inverting tach's layers; it must never import from
``otto.project``. Its runtime imports are ``dataclasses``, ``re`` and
``otto.errors`` — the last is a zero-import leaf (tach declares the edge), so
the module stays as import-light as "stdlib-only" was, while
:class:`EmptySelectionError` can be defined where the selection happens. The
pydantic spec the config is built from is a ``TYPE_CHECKING`` annotation, so
importing the predicate never drags the settings models in, and
``ProjectScopeError`` — shared with bootstrap's D2 refusal rather than
duplicated — is imported inside the functions that raise it.
"""

import dataclasses
import re
from typing import TYPE_CHECKING

from ..errors import OttoError

if TYPE_CHECKING:
    from ..context import OttoContext
    from ..host.host import Host
    from ..models.settings import ProjectScopeSpec
    from .repo import Repo


class EmptySelectionError(OttoError, ValueError):
    """A runtime host pattern selected no host to walk (D6).

    Two ways a selection ends up empty, one class, two messages. The pattern
    fullmatched nothing in the base set; or it fullmatched hosts that a
    membership flag (``include_containers`` / ``include_local``) then removed,
    leaving the walk just as empty. They stay one class because the caller's
    handling is identical — the selection is empty, refuse — and split into two
    messages because the READER's next edit is not: one goes to the regex, the
    other to the flag. A caller that wants to tell them apart reads the
    instance's ``excluded_by``.

    Rooted here rather than beside :class:`~otto.bootstrap.ProjectScopeError`,
    and the difference is what each one is ABOUT. ``ProjectScopeError`` is a
    ``BootstrapError``: a repo's ``[project]`` declaration cannot work, the
    fix is an edit to a ``settings.toml``, and it carries a ``sut_dir`` to
    name the file. This one is a bad *selection at the call* — a ``--hosts``
    regex or a ``pattern=`` argument that picked nothing out of a fleet that
    is otherwise fine — so there is no repo to blame and no file to point at.
    Framing it as a bootstrap failure would send every reader to the wrong
    place. ``ValueError`` is the stdlib root the house convention wants for a
    rejected argument, and it is what the CLI's existing selection errors
    (:class:`~otto.suite.run.NoTestsMatchedError`,
    :class:`~otto.suite.selection.UnknownSelectionError`) already use.

    A silently empty selection is the failure mode this design rates worse
    than a crash (§6), which is why the class exists at all rather than the
    walk simply yielding nothing.
    """

    def __init__(
        self,
        pattern: str,
        base_size: int,
        *,
        excluded_by: "list[str] | None" = None,
        matched_size: int = 0,
    ) -> None:
        """Frame the arguments into whichever of the two D6 messages applies.

        Both messages are built HERE, not at the call sites, so the fullmatch
        explanation, the ``.*`` hint and the flag names cannot drift between
        the context method and the module-level generator that delegates to it.

        Args:
            pattern: The regex source that was fullmatched, as the user typed it.
            base_size: How many hosts the run could have walked before the
                pattern was applied — the denominator both messages quote.
            excluded_by: Membership-flag names (``include_containers`` /
                ``include_local``) that together hid EVERY match. Empty or
                ``None`` means the pattern simply matched nothing, which is the
                original D6 message. Keyword-only, so the two-argument call
                the plain case makes still reads as the whole story.
            matched_size: How many hosts the pattern did fullmatch. Only the
                hidden-by-flag message quotes it; the plain case matched zero
                by definition.
        """
        self.excluded_by = sorted(excluded_by or [])
        self.matched_size = matched_size
        if self.excluded_by:
            message = _HIDDEN_BY_FLAGS.format(
                pattern=pattern,
                size=base_size,
                matched=matched_size,
                flags=", ".join(f"{flag}=True" for flag in self.excluded_by),
            )
        else:
            message = _EMPTY_SELECTION.format(pattern=pattern, size=base_size)
        super().__init__(message)
        self.pattern = pattern
        self.base_size = base_size


_EMPTY_SELECTION = """\
pattern {pattern!r} fullmatches none of the {size} host(s) this run may walk,
so the selection is empty and nothing would be contacted.

Host patterns are FULL matches, never substring searches. To match by prefix,
append a wildcard — '{pattern}.*' — wrapping any alternation first, as
'({pattern}).*'.\
"""
"""D6's refusal. It states the base-set SIZE rather than listing the ids: the
fleet this fires on can be hundreds of hosts, and a wall of ids is what makes
readers skip the one sentence that tells them what to type."""


_HIDDEN_BY_FLAGS = """\
pattern {pattern!r} fullmatches {matched} of the {size} host(s) this run may walk,
but every one of them is held out of fleet iteration by a membership flag, so
the selection is empty and nothing would be contacted.

The regex is not the problem — do not widen it. Container hosts and the
built-in 'local' host are never part of a fleet sweep. To walk them anyway,
pass {flags}; to reach one directly, use 'otto host <id> <verb>'.\
"""
"""The other half of D6, for a selection its own defaults emptied.

Deliberately opens by saying what DIDN'T go wrong. The reader arrives holding a
pattern that matched, and the first instinct on any empty-selection error is to
loosen the regex — which here changes nothing, twice, before anyone suspects a
flag they never typed."""


@dataclasses.dataclass(frozen=True)
class ProjectScopeConfig:
    """Compiled ``[project]`` declaration for one repo.

    The runtime half of the two-type split the other settings sections use:
    :class:`~otto.models.settings.ProjectScopeSpec` validates the TOML, this
    holds the compiled patterns. Compiling once at settings parse means a bad
    regex fails loudly at bootstrap rather than being re-discovered — or worse,
    silently non-matching — on every fleet walk.
    """

    lab_patterns: "list[re.Pattern[str]]"
    """Labs this repo applies to; a lab is applicable when ANY entry fullmatches.

    An empty list therefore matches no lab at all. That is deliberate: an
    unset ``lab_patterns`` is not "every lab" (D2 — match-all must be written
    out as ``[".*"]``), and the loud complaint about a provider-registering
    repo that never declared one belongs to the bootstrap check, not here.
    """

    host_patterns: "list[re.Pattern[str]]"
    """Hosts of interest within those labs; ORed the same way (D7)."""

    @classmethod
    def from_spec(cls, spec: "ProjectScopeSpec") -> "ProjectScopeConfig":
        """Compile a validated ``[project]`` spec into its runtime form.

        The spec already proved every pattern compiles, so this cannot raise
        for a spec that came through pydantic.
        """
        return cls(
            lab_patterns=[re.compile(p) for p in (spec.lab_patterns or [])],
            host_patterns=[re.compile(p) for p in spec.host_patterns],
        )


def repo_targets(scope: "ProjectScopeConfig | None", lab_name: str, host_id: str) -> bool:
    """Report whether *scope* admits the host *host_id* of lab *lab_name*.

    Both axes must pass: some ``lab_patterns`` entry fullmatches *lab_name*
    **and** some ``host_patterns`` entry fullmatches *host_id*.

    ``re.fullmatch``, never ``re.search`` (D6) — under ``search`` a declared
    ``bench`` would quietly pull in ``bench-overflow``, and a scoping mistake
    that *widens* the fleet is exactly the one no test notices.

    Args:
        scope: The repo's compiled ``[project]`` declaration, or ``None`` when
            the repo declared no ``[project]`` table. ``None`` admits
            everything — an undeclared repo scopes nothing out, which is what
            the whole-lab fallback (§6) is built on.
        lab_name: A lab *component* name (``a+b`` merges keep both).
        host_id: The host's ``id``.

    Returns:
        True when the repo targets that (lab, host) pair.

    >>> import re
    >>> from otto.config.scope import ProjectScopeConfig, repo_targets
    >>> scope = ProjectScopeConfig([re.compile("bench")], [re.compile(".*")])
    >>> repo_targets(scope, "bench", "sensor-1")
    True
    >>> repo_targets(scope, "bench-overflow", "sensor-1")
    False
    >>> repo_targets(None, "any-lab", "any-host")
    True
    """
    if scope is None:
        return True
    return _lab_applies(scope, lab_name) and any(p.fullmatch(host_id) for p in scope.host_patterns)


def _lab_applies(scope: "ProjectScopeConfig | None", lab_name: str) -> bool:
    """Report whether *scope* applies to the lab *lab_name* (the lab axis alone).

    Private, and called by BOTH :func:`repo_targets` and :func:`resolve_scopes`
    — the resolver needs the lab axis without a host to pair it with (a lab can
    be applicable while holding no matching host, which is a distinct verdict),
    and spelling ``any(p.fullmatch(lab_name) ...)`` a second time there would
    put the module's one rule in two places. ``None`` applies everywhere, for
    the same reason it targets everything.
    """
    if scope is None:
        return True
    return any(p.fullmatch(lab_name) for p in scope.lab_patterns)


def scope_for_repo(owner: "str | None") -> "ProjectScopeConfig | None":
    """Look up the compiled ``[project]`` declaration of the repo named *owner*.

    The NAME-to-declaration hop the ingest gate needs. A provider registration
    carries the registering repo's name
    (:func:`otto.registry.get_registering_repo`), while :func:`repo_targets`
    judges a compiled :class:`ProjectScopeConfig`; the repos that hold both are
    reachable only through ``config.get_repos()``, which bootstraps lazily.
    That import is function-scope on purpose: this module sits low in the
    layering and ``otto.config``'s package init would import back into it.

    EVERY "cannot answer" answers ``None``, which :func:`repo_targets` reads as
    *admits everything*, and that direction is deliberate. This lookup is
    consulted from the host factory, which runs in bare library use, in
    ``otto init`` scaffolding and in several hundred unit tests — none of which
    have a bootstrap to ask. Raising or refusing there would turn "otto could
    not find its config" into "your host has no products", which is the same
    silent-wrong-answer this scoping exists to prevent, only pointed the other
    way. A gate is a NARROWING; when it cannot compute one, it narrows nothing.

    An owner no repo claims is admitted for the same reason, and is NOT the
    unknown-owner refusal :func:`scoped_ids` raises: there a caller asked to be
    bounded BY a repo and getting the whole lab instead would be a widening,
    while here a provider arrived carrying a name this process cannot resolve
    (a repo bootstrap skipped, a marker from another run) and refusing it would
    take a working lab offline.

    Args:
        owner: ``Repo.name`` of the repo being asked about, or ``None`` for a
            registration made outside any repo's init import.

    Returns:
        That repo's compiled declaration, or ``None`` when the owner is
        ``None``, names no known repo, names a repo with no ``[project]``
        table, or the repos cannot be reached at all.
    """
    if owner is None:
        return None
    try:
        from . import get_repos  # function-scope: config's init imports back into this module

        repos = get_repos()
    except Exception:  # noqa: BLE001 — see above: ANY failure to reach config admits, deliberately
        return None
    for repo in repos or ():
        if repo.name == owner:
            return repo.project_scope
    return None


@dataclasses.dataclass(frozen=True)
class ProjectScope:
    """One repo's resolved fleet of interest for one loaded lab (spec §5).

    A *verdict*, not a live view: :attr:`universe` is the host set as it stood
    at resolution, used for display (``status --full``) and for the D3
    abort/skip decision. Fleet iteration re-evaluates :func:`repo_targets`
    instead, so a container that joins the lab after resolution is scoped
    correctly rather than frozen out.

    The last four fields exist so a verdict can explain ITSELF. D3's errors
    have to say what was loaded, what was declared, and which file to edit; a
    :func:`require_current_scope` that had to be re-handed the repos and the
    lab to build that sentence would be one more argument every caller could
    get wrong — and the display surface wants the same three facts anyway.
    """

    repo_name: str
    """The repo this verdict is about (``Repo.name``); the dict key too."""

    declared: bool
    """Whether the repo has a ``[project]`` table at all.

    False is NOT "targets nothing" — it is the whole-lab fallback (§6), which
    is why it is carried separately from :attr:`excluded`. Product-less repos
    and every pre-``[project]`` project live here.
    """

    config: "ProjectScopeConfig | None"
    """The repo's compiled declaration — what :func:`scoped_ids` re-asks LIVE.

    Carried on the verdict because the verdict is what a fleet walk is handed,
    and a walk must re-evaluate :func:`repo_targets` per host rather than read
    :attr:`universe` (see the class docstring). Recompiling from
    :attr:`host_patterns` at every walk would work and would also put the
    "unset means undeclared" rule in a second place; the compiled object is
    already immutable, so carrying it costs nothing and drifts from nothing.
    """

    applicable_labs: frozenset[str]
    """Loaded lab COMPONENTS this repo applies to (``a+b`` contributes both).

    An undeclared repo applies to all of them: it scoped nothing out.
    """

    universe: frozenset[str]
    """Host ids this repo targeted AT RESOLVE TIME — display and abort data only.

    Fleet walks must NOT iterate this snapshot; they re-evaluate
    :func:`repo_targets` live so hosts that join after resolution (docker
    containers) scope correctly.
    """

    excluded: bool
    """Declared, yet no loaded lab applies — D3's skip (dependency) or abort (current).

    Distinct from an empty :attr:`universe`, deliberately. "Wrong lab loaded"
    and "no host here matches your host_patterns" have different fixes, and a
    single flag covering both would send half the readers to the wrong one.
    """

    sut_dir: str
    """The repo's directory — the ``settings.toml`` to edit, and what rides the error."""

    loaded_labs: "tuple[str, ...]"
    """Every loaded component lab, in load order — the "what was loaded" half of D3.

    Carried per verdict, and identical across them, so that an error or a
    status row can be rendered from the verdict alone. A colon in this
    summary line would be read by napoleon as a ``type: description`` split,
    which is why these docstrings phrase themselves with dashes.
    """

    lab_patterns: "tuple[str, ...]"
    """The declared ``lab_patterns``, as written (empty when undeclared)."""

    host_patterns: "tuple[str, ...]"
    """The declared ``host_patterns``, as written (empty when undeclared)."""


def resolve_scopes(
    repos: "list[Repo]",
    component_names: "list[str]",
    hosts: "dict[str, Host]",
    exclude_ids: "frozenset[str]" = frozenset(),
) -> "dict[str, ProjectScope]":
    """Resolve every repo's fleet of interest against one loaded lab.

    Pure and cheap: it reads exactly three things — each repo's ``name`` and
    ``project_scope``, and each host's ``source_lab`` — and returns verdicts.
    Nothing is raised here even when a verdict is fatal (:func:`require_current_scope`
    is where D3 fires), because resolution runs at context creation and the
    consequence belongs at project-layer entry.

    *exclude_ids* is how the built-in ``local`` host stays out of every
    universe, and the indirection is deliberate on two counts. Layering: this
    module must not import ``otto.host``, so it cannot know the built-in id.
    Correctness: ``local``'s ``source_lab`` is stamped with whichever component
    the CLI happened to list first, so a membership rule that keyed on that
    stamp would make ``-l a+b`` and ``-l b+a`` resolve differently — the
    caller, which knows both the id and that ``local`` is not fleet, passes it
    in. ``include_local`` remains the ambient walks' own knob (§6).

    Args:
        repos: The repos to resolve (bootstrap's set; order is preserved in the
            returned mapping, which is keyed by name).
        component_names: The loaded lab's ``component_names`` — the component
            labs, never the composite ``a+b`` display name.
        hosts: The loaded lab's ``hosts`` mapping, ``{id: host}``.
        exclude_ids: Host ids no universe may contain, whatever the patterns
            say. Defaults to excluding nothing.

    Returns:
        ``{repo name: ProjectScope}``.

    >>> import types
    >>> from otto.config.scope import ProjectScopeConfig, resolve_scopes
    >>> import re
    >>> repo = types.SimpleNamespace(  # only .name/.project_scope/.sut_dir are read
    ...     name="sensors",
    ...     sut_dir="/repos/sensors",
    ...     project_scope=ProjectScopeConfig([re.compile("bench")], [re.compile("sensor-.*")]),
    ... )
    >>> hosts = {
    ...     "sensor-1": types.SimpleNamespace(source_lab="bench"),
    ...     "gw-1": types.SimpleNamespace(source_lab="bench"),
    ... }
    >>> scope = resolve_scopes([repo], ["bench"], hosts)["sensors"]
    >>> sorted(scope.universe), scope.excluded
    (['sensor-1'], False)
    """
    scopes: dict[str, ProjectScope] = {}
    for repo in repos:
        config = repo.project_scope
        applicable = frozenset(name for name in component_names if _lab_applies(config, name))
        # An undeclared repo's universe is the whole lab BY CONSTRUCTION —
        # never per-host evaluation. The short-circuit is load-bearing, not an
        # optimization: repos are routinely VISIBLE without declaring anything
        # (the integration tree's session fixture leaves OTTO_SUT_DIRS ambient
        # for the rest of its worker), and a world with zero declarations must
        # not demand attributes (``source_lab``) that hosts built before this
        # feature never carried. Only a declaration may raise the bar.
        universe = frozenset(
            host_id
            for host_id, host in hosts.items()
            if host_id not in exclude_ids
            and (config is None or repo_targets(config, host.source_lab, host_id))
        )
        scopes[repo.name] = ProjectScope(
            repo_name=repo.name,
            declared=config is not None,
            config=config,
            applicable_labs=applicable,
            universe=universe,
            excluded=config is not None and not applicable,
            sut_dir=str(repo.sut_dir),
            loaded_labs=tuple(component_names),
            lab_patterns=() if config is None else tuple(p.pattern for p in config.lab_patterns),
            host_patterns=() if config is None else tuple(p.pattern for p in config.host_patterns),
        )
    return scopes


_NO_APPLICABLE_LAB = """\
repo '{name}' declares [project] lab_patterns that match none of the loaded
labs, so no host in this run belongs to its fleet of interest.

    loaded labs:  {loaded}
    lab_patterns: {patterns}

Load a lab this repo applies to (otto -l <lab>), or widen lab_patterns in
{sut_dir}/.otto/settings.toml.\
"""
"""D3's abort for the current repo. Both halves are printed because either one
alone reads as the other's fault: patterns without the loaded labs looks like a
typo in the regex, loaded labs without the patterns looks like the wrong -l."""

_EMPTY_UNIVERSE = """\
repo '{name}' applies to loaded lab(s) {applicable}, but its [project]
host_patterns match no host there, so every fleet walk it drives would be
empty.

    host_patterns: {patterns}

Widen host_patterns in {sut_dir}/.otto/settings.toml (['.*'] is every host in
those labs), or load a lab that holds the hosts this repo targets.\
"""
"""The other current-repo abort. Named separately from the one above precisely
because the fix differs: the lab was RIGHT here, and telling this user to load
a different one would send them the wrong way."""


def unusable_scope(scope: "ProjectScope") -> bool:
    """Report whether *scope*'s declared fleet of interest cannot work — D3's one condition.

    THE TWIN OF ``_unusable_scope_message``, and it exists for the same
    reason: two sites act on this verdict — the current repo's abort
    (:func:`require_current_scope`) and the orchestrator's dependency skip
    (``otto.project.orchestrator._applicable``) — and D3's asymmetry is
    about WHAT THEY DO, never about which condition they read. Spelling the
    condition out at both would let them drift, and a skip that fired on one
    unusable shape while the walk raised on the other is exactly the defect
    this function closes: the dependency's own fleet walk raises out of the
    walk, and one project's declaration takes down another project's run.

    ``declared`` is load-bearing. An undeclared repo's universe is empty
    whenever the LAB is empty, and that is the whole-lab fallback (§6) rather
    than a failure — refusing there would break every product-less repo and
    every pre-``[project]`` project.

    Args:
        scope: One resolved verdict.

    Returns:
        True when the repo declared a ``[project]`` scope that admits no host
        — either because no loaded lab applies (:attr:`ProjectScope.excluded`)
        or because its ``host_patterns`` match nothing in the labs that do.
    """
    return scope.declared and (scope.excluded or not scope.universe)


def _switched(names: "tuple[str, ...]") -> "frozenset[str]":
    """Project a switch tuple onto its PEP-503 normalized form, for comparison.

    THE read-side half of the ``include_projects``/``exclude_projects``
    contract, and the reason those fields need no ``__post_init__``.
    ``OttoContext`` is a plain dataclass that anyone may construct, so
    "the values are normalized" cannot be a WRITE-time promise — a library
    caller passing ``("My_Repo",)`` would otherwise have its explicit switch
    silently ignored, which is the failure mode this module exists to refuse.
    Normalizing HERE makes the invariant hold at the only place that reads it.

    Shared by :func:`active` and :func:`switched_off` rather than spelled at
    each, because the two must agree about what "the same repo" means: a
    ``switched_off`` that said False where ``active`` said False-because-excluded
    would attribute the verdict to the wrong cause.

    The tuples hold one entry per ``-I``/``-E`` the user typed, so rebuilding
    the set per call is free and keeps the function pure.
    """
    from ..models.dependencies import normalize_name

    return frozenset(normalize_name(n) for n in names)


def active(repo_name: str, ctx: "OttoContext") -> bool:
    """Report whether *repo_name* participates in this invocation — THE authority.

    Every enforcement point (bootstrap-error demotion via
    :func:`inactive_before_lab`'s projection, the orchestrator walks,
    instruction dispatch) consults this one predicate, so the resolution
    order is stated once: explicit switch > lab inference > default-on.
    Pure over ``(ctx.include_projects, ctx.exclude_projects, ctx.scopes)``;
    no I/O.

    A missing verdict is active on purpose: it covers the no-labs-loaded
    invocation (``ctx.scopes`` is empty), the undeclared repo (whole-lab
    fallback, scoping spec §6), and the library context alike — in every
    case there is no signal that would justify leaving the repo out.

    Args:
        repo_name: The repo's declared ``Repo.name``, spelled exactly as the
            repo declares it. ``ctx.scopes`` is keyed by that raw name, so a
            user-typed variant (different case, ``_`` for ``-``) finds no
            verdict and therefore fails OPEN — resolving ACTIVE. Callers
            holding a user-supplied spelling must map it back to a declared
            name before asking.
        ctx: The runtime context supplying the switches and the lab verdicts.
    """
    from ..models.dependencies import normalize_name

    name = normalize_name(repo_name)
    if name in _switched(ctx.exclude_projects):
        return False
    if name in _switched(ctx.include_projects):
        return True
    # RAW name, deliberately: ``ctx.scopes`` is keyed by ``Repo.name`` exactly as
    # written, so normalizing this key would miss the verdict of every repo whose
    # name holds an ``_``, a ``.`` or a capital — and a missing verdict resolves
    # ACTIVE, i.e. it would fail OPEN into the silent widening D6 refuses. The
    # switches above normalize because they carry USER-typed names; this does not
    # because it carries a declared one.
    verdict = ctx.scopes.get(repo_name)
    if verdict is None:
        return True
    return not unusable_scope(verdict)


def switched_off(repo_name: str, ctx: "OttoContext") -> bool:
    """Report whether *repo_name* was explicitly ``--exclude-projects``'d.

    Attribution only — every message that names the SWITCH as the reason
    reads this, while the combined verdict stays :func:`active`'s alone.
    Normalizes both sides through ``_switched``, the same way
    :func:`active` does, so the two cannot disagree about identity. Unlike
    :func:`active` this one reads no lab verdict, so *repo_name* may be any
    spelling — normalization is the whole comparison.
    """
    from ..models.dependencies import normalize_name

    return normalize_name(repo_name) in _switched(ctx.exclude_projects)


def inactive_before_lab(
    scope: "ProjectScopeConfig | None", lab_selection: "list[str] | None"
) -> bool:
    """Report whether *scope* is inactive on the LAB AXIS alone, for seams that run pre-lab.

    The lab-axis projection of :func:`active`.
    Bootstrap-error demotion fires before the lab is built, so it cannot ask
    for a :class:`ProjectScope` verdict; what it CAN know is the ``-l``
    selection (already split into component names) and the repo's declared
    ``lab_patterns``. This is deliberately the narrower test: a repo that is
    inactive only because it is host-starved is NOT detected here, and its
    bootstrap errors stay fatal (plan deviation 1).
    """
    if not lab_selection or scope is None:
        return False
    return not any(_lab_applies(scope, name) for name in lab_selection)


def _unusable_scope_message(scope: "ProjectScope") -> str:
    """Render the D3 message for a repo whose fleet of interest cannot work.

    Extracted so the two sites that reach this condition — D3's project-layer
    entry check (:func:`require_current_scope`) and an owner-bound fleet walk
    that admits nothing (:func:`require_nonempty_fleet`) — cannot describe the
    same repo differently. Which of the two templates applies is the verdict's
    own ``excluded`` flag, and that choice is the point: "wrong lab loaded" and
    "no host here matches your host_patterns" have different fixes.

    Args:
        scope: A DECLARED verdict whose fleet is unusable. Undeclared verdicts
            never reach here — the fallback is not a failure.

    Returns:
        The message, ready to hand to ``ProjectScopeError`` verbatim.
    """
    if scope.excluded:
        return _NO_APPLICABLE_LAB.format(
            name=scope.repo_name,
            loaded=", ".join(scope.loaded_labs) or "(none)",
            patterns=", ".join(scope.lab_patterns) or "(none)",
            sut_dir=scope.sut_dir,
        )
    return _EMPTY_UNIVERSE.format(
        name=scope.repo_name,
        applicable=", ".join(sorted(scope.applicable_labs)) or "(none)",
        patterns=", ".join(scope.host_patterns) or "(none)",
        sut_dir=scope.sut_dir,
    )


def require_current_scope(scopes: "dict[str, ProjectScope]", current_repo_name: str) -> None:
    """Enforce D3 for the CURRENT repo: raise when its fleet of interest is unusable.

    Call at project-layer entry (default instructions, a suite's ``ensure``
    marker steps, monitor fleet build) — not at context creation, so explicit
    ``otto host <id> <verb>`` targeting still works when a declaration is
    wrong. Which repo is current (``bootstrap().repos[0]``, the driving
    project) is the caller's reading; this function is handed the name.

    A DEPENDENCY in the same *scopes* mapping is never enforced here: an
    excluded dependency is skipped loudly by the orchestrator and shown as
    not-applicable by ``status``, which is D3's deliberate asymmetry — one
    project's fleet declaration must not veto another's run.

    Args:
        scopes: The resolver's output.
        current_repo_name: ``Repo.name`` of the driving project. A name absent
            from *scopes* returns None: no verdict was resolved for it (a repo
            bootstrap skipped, say), and that failure is already reported.

    Returns:
        None — always, when there is nothing to refuse.

    Raises:
        otto.bootstrap.ProjectScopeError: The same class bootstrap's D2 check
            raises; the taxonomy has one "this repo's scope declaration cannot
            work" error, not one per site. Message names the repo, what was
            loaded, what was declared, and the file to edit.
    """
    from ..bootstrap import ProjectScopeError  # function-scope: keeps this module import-light

    scope = scopes.get(current_repo_name)
    # The condition is :func:`unusable_scope`'s, never a copy of it — the
    # orchestrator's dependency skip reads the same predicate, and D3's
    # asymmetry is about the RESPONSE (abort here, skip there), not about which
    # verdicts qualify. Undeclared never qualifies: that is the whole-lab
    # fallback (§6), including when its universe is empty because the LAB is
    # empty, which is a lab problem and not this check's to diagnose.
    if scope is None or not unusable_scope(scope):
        return
    raise ProjectScopeError(scope.sut_dir, _unusable_scope_message(scope))


def scoped_ids(
    hosts: "dict[str, Host]",
    scopes: "dict[str, ProjectScope]",
    owner: "str | None",
) -> "set[str]":
    """Return the host ids a fleet walk may iterate — evaluated LIVE, not read off a snapshot.

    The ambient universe of spec §6, in one place so both fleet surfaces
    (:meth:`otto.context.OttoContext.all_hosts` and the module-level
    :func:`otto.config.fleet.all_hosts` that delegates to it) cannot disagree.
    Membership is re-derived by calling :func:`repo_targets` against *hosts* as
    they stand at the call, so a docker container registered after the context
    was created is scoped correctly instead of frozen out — which is precisely
    what reading :attr:`ProjectScope.universe` here would do.

    Three answers, in this order:

    * **No repo declared** ``[project]`` — every id in *hosts*. The whole-lab
      fallback (§6): product-less repos and every pre-``[project]`` project
      keep today's behavior. "No repo declared" is the condition, not "this
      repo did not": an undeclared repo's own verdict admits everything, so
      folding it into the union below would silently restore the whole lab the
      moment a product-less repo shared the run.
    * *owner* **names a declared repo** — that repo's admission alone. A known
      repo that declared no ``[project]`` gets the fallback for the same reason
      as above: its own verdict admits everything. An owner that is not in
      *scopes* AT ALL is a different thing entirely and is REFUSED — see
      Raises.
    * *owner* **is None** — the union across the DECLARED repos (D7), which is
      the fleet that host-global operations (cleanup, toolchain, debug logs)
      walk.

    The built-in ``local`` host and container hosts are NOT filtered here.
    Scoping answers "may this walk reach that host at all"; ``include_local``
    and ``include_containers`` remain the walk's own knobs, applied after (§6)
    — so an admin passing ``include_local=True`` still gets the runner,
    exactly as before. ``local`` stays out of the resolver's stored
    :attr:`ProjectScope.universe` by ``exclude_ids``, which is where the D3
    abort reads its "this repo's fleet is empty" verdict from.

    Args:
        hosts: The lab's live ``{id: host}`` mapping — read at call time.
        scopes: :func:`resolve_scopes`' output for this run.
        owner: ``Repo.name`` whose universe bounds this walk, or ``None`` for
            the union. The repo-scoped context view (spec §7,
            ``ctx.for_repo(repo)``) is what supplies a name; a plain context
            always passes ``None``.

    Returns:
        The admissible ids, as a set.

    Raises:
        otto.bootstrap.ProjectScopeError: *owner* names a repo that is not in
            *scopes*, while *scopes* holds verdicts. Silently falling back to
            the whole lab there would turn a caller's typo — or a repo missing
            from ``get_ordered_repos()`` — into exactly the quiet WIDENING this
            design exists to prevent. An empty *scopes* is not that case: it
            means the repos were never reachable (a library context, an
            unavailable bootstrap), and every owner is unknown for a reason
            that is not the caller's, so the fallback stands.

    >>> import re
    >>> import types
    >>> from otto.config.scope import ProjectScope, ProjectScopeConfig, scoped_ids
    >>> config = ProjectScopeConfig([re.compile("bench")], [re.compile("sensor-.*")])
    >>> verdict = ProjectScope(
    ...     repo_name="sensors",
    ...     declared=True,
    ...     config=config,
    ...     applicable_labs=frozenset({"bench"}),
    ...     universe=frozenset({"sensor-1"}),
    ...     excluded=False,
    ...     sut_dir="/repos/sensors",
    ...     loaded_labs=("bench",),
    ...     lab_patterns=("bench",),
    ...     host_patterns=("sensor-.*",),
    ... )
    >>> hosts = {
    ...     "sensor-1": types.SimpleNamespace(source_lab="bench"),
    ...     "gw-1": types.SimpleNamespace(source_lab="bench"),
    ... }
    >>> sorted(scoped_ids(hosts, {"sensors": verdict}, None))
    ['sensor-1']
    >>> sorted(scoped_ids(hosts, {}, None))  # nothing declared — the whole lab
    ['gw-1', 'sensor-1']
    """
    if owner is not None and scopes and owner not in scopes:
        # Checked BEFORE the fallback below, so an unknown owner is refused
        # whether or not anything declared: the caller asked to be bounded by a
        # repo that does not exist, and answering "the whole lab" to that is
        # the widening, not a degraded mode.
        from ..bootstrap import ProjectScopeError  # function-scope: keeps this module import-light

        raise ProjectScopeError(
            "",  # no repo, so no settings.toml to send the reader to
            _UNKNOWN_OWNER.format(owner=owner, known=", ".join(sorted(scopes)) or "(none)"),
        )
    declared = [scope for scope in scopes.values() if scope.declared]
    if not declared:
        return set(hosts)
    if owner is not None:
        owned = scopes[owner]  # present: the unknown-owner refusal above ran first
        if not owned.declared:
            return set(hosts)
        declared = [owned]
    return {
        host_id
        for host_id, host in hosts.items()
        if any(repo_targets(scope.config, host.source_lab, host_id) for scope in declared)
    }


_UNKNOWN_OWNER = """\
a fleet walk was bound to repo '{owner}', which is not among this run's
resolved repos — so no [project] declaration can bound it.

    resolved repos: {known}

A walk bound to a repo otto never resolved would fall back to the WHOLE lab,
which is the silent widening this scoping exists to prevent. Bind the walk to
one of the repos above, or use the plain context to walk their union."""
"""Not a user-facing config mistake but a caller one, which is why it names the
resolved set rather than a file to edit: whoever built the repo-scoped view
passed a name otto does not know, and the resolved names are what tells them
whether it is a typo or a repo bootstrap skipped."""


_EMPTY_FLEET = """\
no host in this run belongs to any project's fleet of interest, so every fleet
walk would be empty.

    loaded labs:  {loaded}
    declared by:  {repos}

Load a lab these projects apply to (otto -l <lab>), or widen [project]
lab_patterns / host_patterns in their .otto/settings.toml.\
"""
"""Spec §10 row 5. Deliberately fleet-shaped rather than repo-shaped: the
per-repo aborts in :func:`require_current_scope` fire for the CURRENT project
and can name one file, while this one fires when the union came out empty and
any of the contributing declarations could be the reason."""


def require_nonempty_fleet(
    scopes: "dict[str, ProjectScope]",
    admissible: "set[str]",
    owner: "str | None" = None,
) -> None:
    """Refuse a fleet walk whose base set is empty while some repo declared one (§10 row 5).

    Call at the fleet surfaces, after :func:`scoped_ids`. The asymmetry with
    the undeclared case is the point: an empty walk under the whole-lab
    fallback means the LAB is empty, which is not this check's to diagnose and
    has always been silent; an empty walk under a declaration means the
    declarations and the loaded lab disagree, and a sweep that quietly touches
    nothing is the failure this design exists to make loud.

    *owner* decides WHICH failure gets reported, and getting that wrong is not
    cosmetic. An owner-bound walk that admits nothing while the union is
    healthy is ONE repo's problem: the fleet-shaped message below would claim
    "every fleet walk would be empty" — false, the plain-context walk works —
    list every declaring repo as a suspect, and point at the FIRST one's
    ``settings.toml``, sending the reader to edit a file that is not the cause.
    With *owner* set, the same condition is reported in D3's per-repo framing
    instead, built from that repo's own verdict.

    Args:
        scopes: :func:`resolve_scopes`' output for this run.
        admissible: What :func:`scoped_ids` just returned.
        owner: The ``Repo.name`` this walk was bound to, or ``None`` for a
            plain-context union walk. An owner that declared nothing falls
            through to the fleet-shaped message deliberately: its base set was
            the whole lab, so an empty one says the LAB is empty and says
            nothing about that repo's declaration.

    Returns:
        None — always, when there is nothing to refuse.

    Raises:
        otto.bootstrap.ProjectScopeError: The base set is empty and at least
            one repo declared ``[project]``. Same class as D3's per-repo
            aborts: the taxonomy has one "a scope declaration cannot work"
            error, and this is that condition read at a fleet surface.
    """
    from ..bootstrap import ProjectScopeError  # function-scope: keeps this module import-light

    if admissible:
        return
    declared = [scope for scope in scopes.values() if scope.declared]
    if not declared:
        return
    if owner is not None:
        owned = scopes.get(owner)
        if owned is not None and owned.declared:
            raise ProjectScopeError(owned.sut_dir, _unusable_scope_message(owned))
    raise ProjectScopeError(
        declared[0].sut_dir,
        _EMPTY_FLEET.format(
            loaded=", ".join(declared[0].loaded_labs) or "(none)",
            repos=", ".join(
                f"{scope.repo_name} (lab_patterns: {', '.join(scope.lab_patterns) or '(none)'})"
                for scope in declared
            ),
        ),
    )
