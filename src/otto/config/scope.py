"""Per-project lab/host scoping: the ONE membership predicate, its config, its resolver.

Every consumer — ingest gating, fleet iteration, the resolver, the status
display, the docker facts — answers "does this project target this host?" by
calling :func:`host_targeted`, which judges a container by its parent and
applies :func:`project_targets`' ``fullmatch``-both-axes rule. One predicate in
one module is what makes those answers agree *structurally*; a second copy of
the rule would drift the moment one of them grew a special case.
:func:`resolve_scopes` is held to the same rule: it computes the per-project
verdicts by calling the predicate, never by re-deriving membership from the
patterns. :func:`union_admits` is the same question asked of every active
declaration at once.

The resolver is PURE — no I/O, no ``bootstrap()``, no lab loading. It is handed
the projects, the loaded lab's component names and the loaded hosts, and returns
verdicts; the caller decides which project is "current" and when to enforce. That
split is what lets the D3 consequences be *stored* at context creation and
*raised* only at project-layer entry (§5). An explicit target is judged too:
``get_host`` resolves only the hosts this run targets, so ``otto host <id>
<verb>`` on a host no active declaration targets refuses with
:class:`HostOutOfScopeError`. Its message comes from :func:`host_outside`'s
record of why the host is outside (:func:`outside_message`), and names the
fix: the ``settings.toml`` files to widen (:func:`settings_files`) or the
``-E`` to drop.

Layering: a foundation module, so every layer may import it: the host
layer's ingest gate needs :func:`host_targeted`, and :mod:`otto.declared`
reads the declarations through it. It must never import upward. Its runtime
imports are ``dataclasses``, ``pathlib``, ``re`` and ``otto.errors``, a zero-import leaf,
plus ``otto.models.dependencies`` inside the functions that normalize a project
name. Importing it also runs ``otto.config``'s ``__init__`` on the way, which
is lazy bar one eager import, :mod:`otto.config.env` (stdlib only at import),
so that module loads too. The pydantic spec the config is built from is a
``TYPE_CHECKING`` annotation, so importing the predicate never drags the
settings models in.

The composition root's error types live here too: :class:`BootstrapError`,
its subclasses :class:`DependencyError` and :class:`ProjectScopeError`, and
:class:`BootstrapWarning`. ``ProjectScopeError`` is shared by bootstrap's D2
refusal and this module's scope checks, so it is defined where both can reach
it; :mod:`otto.bootstrap` re-exports all four under their public names.
:class:`DuplicateProjectError`, one more subclass, is deliberately not re-exported:
callers import it from this module.

So do the runtime settings types (spec 2026-10-11 placement-and-phases §3.6):
where a project keeps its settings file (``SETTINGS_FILENAME``,
``TOML_SETTINGS_PATH``), the context suffixes docker reads as an archive
(:data:`ARCHIVE_SUFFIXES`), and the frozen dataclasses the settings specs build
(:class:`DockerImage`, :class:`DockerCompose`, :class:`DockerUseCase`,
:class:`DockerSettings`, :class:`MonitorSettings`), with
:func:`declared_use_case_names`, the one reading of which use-cases they
declare. :mod:`otto.config.project` holds them in every project it loads, and
:mod:`otto.models.settings` builds them; this module is below both and already
loaded wherever the project loader is, so the loader reads them at module scope
(its annotations resolve at runtime) and the specs read them inside the
functions that build them, and neither loads more than it did. The four
``otto.config`` exports (:class:`DockerImage`, :class:`DockerCompose`,
:class:`DockerSettings`, :class:`MonitorSettings`) keep that public path; otto
imports the rest from this module directly.
"""

# Spec: docs/superpowers/specs/2026-08-18-project-lab-host-scoping-design.md §5.

import dataclasses
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from typing_extensions import final

from ..errors import OttoError, _UnquotedKeyError

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterable, Mapping, Sequence

    from ..host.host import Host
    from ..models.settings import ProjectScopeSpec
    from ..run_context import OttoContext
    from .project import Project


class BootstrapError(OttoError):
    """One user file failed to load during bootstrap registration."""

    def __init__(
        self, root: Any, *, source: str, cause: BaseException, reason: "str | None" = None
    ) -> None:
        """Frame *cause* as ``project root <root>: failed to load <source>``.

        The frame ends with *reason* when one is given, else with ``repr(cause)``.
        A caller passes *reason* for a cause whose ``repr`` is not fit to print:
        a settings file that fails validation, whose error spans several lines
        and repeats the rejected values.
        """
        said = reason if reason is not None else repr(cause)
        super().__init__(f"project root {root}: failed to load {source}: {said}")
        self.root = root
        self.source = source
        self.__cause__ = cause


class DependencyError(BootstrapError):
    """A project's declared dependencies cannot be satisfied (or a dependency was skipped)."""

    def __init__(self, root: Any, *, message: str) -> None:
        """Frame *message* as ``project root <root>: <message>``."""
        Exception.__init__(self, f"project root {root}: {message}")
        self.root = root
        self.source = "dependencies"


class ProjectScopeError(BootstrapError):
    """A project registered providers without declaring the labs it applies to (D2).

    Unlike its siblings this one is NOT contained: a project whose providers can
    never reach a host is a configuration mistake with no degraded mode worth
    offering, and the whole point of the check is that it is impossible to
    ignore. *message* is used verbatim — it already opens with the project's name,
    so the ``project root <root>:`` prefix its siblings add would say it twice.
    """

    def __init__(self, root: Any, *, message: str) -> None:
        """Carry *message* unframed; keep *root* on the exception for callers."""
        Exception.__init__(self, message)
        self.root = root
        self.source = "project_scope"


class DuplicateProjectError(BootstrapError):
    """Two or more ``OTTOPATH`` roots hold one project, so none of them loads.

    One is recorded for EACH root that claimed the name, so each is contained
    like any other per-root load failure: otto never guesses which copy is
    meant. Each is framed with its own root, as its siblings are, and names
    the project and every root that claimed it. *name* is the normalized name
    the roots share.
    """

    def __init__(self, root: Any, name: str, roots: "list[Path]") -> None:
        """Frame ``project root <root>: project <name> has N roots in OTTOPATH: …; keep one``."""
        listed = ", ".join(str(r) for r in roots)
        Exception.__init__(
            self,
            f"project root {root}: project {name!r} has {len(roots)} roots in OTTOPATH: "
            f"{listed}; keep one",
        )
        self.root = root
        self.name = name
        self.roots = list(roots)
        self.source = "OTTOPATH"


def refused_names(errors: "list[BootstrapError]") -> list[str]:
    """Return the normalized names discovery refused because two roots claimed them."""
    return sorted({e.name for e in errors if isinstance(e, DuplicateProjectError)})


@final
@dataclasses.dataclass(frozen=True)
class BootstrapWarning:
    """A non-fatal dependency finding: rendered at startup, never gates dispatch."""

    root: Any
    message: str


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

    A sibling of :class:`ProjectScopeError` rather than a subclass, and the
    difference is what each one is ABOUT. ``ProjectScopeError`` is a
    ``BootstrapError``: a project's ``[project]`` declaration cannot work, the
    fix is an edit to a ``settings.toml``, and it carries a ``root`` to
    name the file. This one is a bad *selection at the call* — a ``--hosts``
    regex or a ``pattern=`` argument that picked nothing out of a fleet that
    is otherwise fine — so there is no project to blame and no file to point at.
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
        *,
        base_size: int,
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

    def cli_message(self, *, walker: str = "this command") -> str:
        """Return the message for a reader who has no keyword argument to pass.

        ``str(self)`` advises a library caller to pass ``include_local=True``
        and the like; an otto command exposes no such switch, and neither does
        any coverage call, so that advice is unfollowable there. A selection
        its membership flags emptied says instead that *walker* does not walk
        those hosts. The plain no-match message is already actionable from a
        command line and comes back unchanged.

        Args:
            walker: Who did the walk, as the sentence's subject. The default
                addresses a command line; a path a library caller reaches as
                well names the walk itself (``"coverage"``), which reads true
                for both.
        """
        if not self.excluded_by:
            return str(self)
        what = " or ".join(_NOT_WALKED.get(flag, flag) for flag in self.excluded_by)
        if "include_local" not in self.excluded_by:
            remedy = _REACH_CONTAINER
        elif len(self.excluded_by) == 1:
            remedy = _NAME_A_LAB_HOST
        else:
            remedy = f"{_NAME_A_LAB_HOST}\n{_REACH_CONTAINER_ONLY}"
        return _HIDDEN_FROM_COMMAND.format(
            pattern=self.pattern,
            matched=self.matched_size,
            size=self.base_size,
            walker=walker,
            what=what,
            remedy=remedy,
        )


def selection_message(error: BaseException, *, walker: str = "this command") -> "str | None":
    """Return *error*'s CLI wording when it is, or directly wraps, an empty selection.

    A wrapper (``nothing to clean: <selection message>``) keeps its own text;
    only the wrapped library message is swapped for
    :meth:`EmptySelectionError.cli_message`, given *walker*. ``None`` for any
    other error, so a caller falls back to its own rendering.
    """
    cause = error.__cause__
    if isinstance(error, EmptySelectionError):
        return error.cli_message(walker=walker)
    if isinstance(cause, EmptySelectionError):
        return str(error).replace(str(cause), cause.cli_message(walker=walker))
    return None


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


_HIDDEN_FROM_COMMAND = """\
pattern {pattern!r} fullmatches {matched} of the {size} host(s) this run may walk,
but {walker} does not walk {what}, so nothing would be contacted.

{remedy}\
"""
"""The hidden-by-flag refusal for a reader with no keyword to pass: no otto
command (and no coverage call) takes the membership flags."""


_REACH_CONTAINER = """\
The regex is correct — do not widen it. To reach one of them directly, use
'otto host <id> <verb>'."""
"""The remedy when only container hosts were held out: the pattern named them
right, and a container is reached by its own id, never by a sweep."""


_NAME_A_LAB_HOST = """\
The built-in 'local' host is the machine otto runs on, never a lab host:
name a lab host in the pattern instead."""
"""The remedy when ``local`` was held out. Here the regex IS what to change:
no walk that holds ``local`` out ever reaches it, and ``otto host local`` runs
a verb on this machine without collecting or monitoring anything."""


_REACH_CONTAINER_ONLY = "To reach a container directly, use 'otto host <id> <verb>'."
"""The container half of the remedy when ``local`` was held out alongside one."""


_NOT_WALKED = {
    "include_containers": "container hosts",
    "include_local": "the built-in 'local' host",
}
"""What each membership flag holds out, named for a reader who never typed the flag."""


@dataclasses.dataclass(frozen=True)
class ProjectScopeConfig:
    """Compiled ``[project]`` declaration for one project.

    The runtime half of the two-type split the other settings sections use:
    :class:`~otto.models.settings.ProjectScopeSpec` validates the TOML, this
    holds the compiled patterns. Compiling once at settings parse means a bad
    regex fails loudly at bootstrap rather than being re-discovered — or worse,
    silently non-matching — on every fleet walk.
    """

    lab_patterns: "list[re.Pattern[str]]"
    """Labs this project applies to; a lab is applicable when ANY entry fullmatches.

    An empty list therefore matches no lab at all. That is deliberate: an
    unset ``lab_patterns`` is not "every lab" (D2 — match-all must be written
    out as ``[".*"]``), and the loud complaint about a provider-registering
    project that never declared one belongs to the bootstrap check, not here.
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


def project_targets(scope: "ProjectScopeConfig | None", lab_name: str, host_id: str) -> bool:
    """Report whether *scope* admits the host *host_id* of lab *lab_name*.

    The pattern rule alone, over a (lab, id) pair. A site that judges a HOST
    calls :func:`host_targeted`, which brings a container to its parent first.

    Both axes must pass: some ``lab_patterns`` entry fullmatches *lab_name*
    **and** some ``host_patterns`` entry fullmatches *host_id*.

    ``re.fullmatch``, never ``re.search`` (D6) — under ``search`` a declared
    ``bench`` would quietly pull in ``bench-overflow``, and a scoping mistake
    that *widens* the fleet is exactly the one no test notices.

    Args:
        scope: The project's compiled ``[project]`` declaration, or ``None`` when
            the project declared no ``[project]`` table. ``None`` admits
            everything — an undeclared project scopes nothing out, which is what
            the whole-lab fallback (§6) is built on.
        lab_name: A lab *component* name (``a+b`` merges keep both).
        host_id: The host's ``id``.

    Returns:
        True when the project targets that (lab, host) pair.

    >>> import re
    >>> from otto.config.scope import ProjectScopeConfig, project_targets
    >>> scope = ProjectScopeConfig([re.compile("bench")], [re.compile(".*")])
    >>> project_targets(scope, "bench", "sensor-1")
    True
    >>> project_targets(scope, "bench-overflow", "sensor-1")
    False
    >>> project_targets(None, "any-lab", "any-host")
    True
    """
    if scope is None:
        return True
    return applies_to_lab(scope, lab_name) and any(
        p.fullmatch(host_id) for p in scope.host_patterns
    )


def host_targeted(config: "ProjectScopeConfig | None", host: object) -> bool:
    """Report whether the declaration *config* targets *host*, a container by its parent.

    The one host-against-declaration predicate: every site that asks whether
    a project targets a host calls it. A container is judged by its parent
    (:func:`judged_host`), so it is in scope exactly
    when the host it runs on is, whatever its own id; any other host is
    judged by its own ``source_lab`` and ``id`` through :func:`project_targets`.

    ``None`` (no ``[project]`` table) admits before any attribute of *host* is
    read. That short-circuit is load-bearing, not an optimization: projects
    are routinely visible without declaring anything, and a world with zero
    declarations must not demand attributes (``source_lab``) that hosts built
    before this feature never carried. Only a declaration may raise the bar.

    Args:
        config: The project's compiled ``[project]`` declaration, or ``None``.
        host: One lab host, of any family.

    Returns:
        True when the project targets *host*.

    >>> import re
    >>> import types
    >>> from otto.config.scope import ProjectScopeConfig, host_targeted
    >>> config = ProjectScopeConfig([re.compile("bench")], [re.compile("sensor-1")])
    >>> sensor = types.SimpleNamespace(id="sensor-1", source_lab="bench")
    >>> on_sensor = types.SimpleNamespace(id="sensor-1.p.api", source_lab="bench", parent=sensor)
    >>> host_targeted(config, sensor), host_targeted(config, on_sensor)
    (True, True)
    >>> host_targeted(None, object())  # undeclared: nothing is read
    True
    """
    if config is None:
        return True
    judged = judged_host(host)
    return project_targets(config, judged.source_lab, judged.id)


def judged_host(host: object) -> Any:
    """Return the host *host* is judged by, its parent for a container, else *host* itself.

    The one "a container is judged by its parent" projection. Every judgment
    of a host reads it: :func:`host_targeted` matches the judged host's lab
    and id, :func:`host_outside` names its lab, and
    ``otto.host.builtin_hosts.is_runner_side`` asks whether it is the built-in
    runner. So a container is in scope, outside it, or on the runner exactly
    when its parent is.
    """
    return getattr(host, "parent", None) or host


def applies_to_lab(scope: "ProjectScopeConfig | None", lab_name: str) -> bool:
    """Report whether *scope* applies to the lab *lab_name* (the lab axis alone).

    The one place ``lab_patterns`` is matched: :func:`project_targets`,
    :func:`resolve_scopes`, :func:`applies_to_any` and every pre-lab seam call
    it, so the module's lab rule is not spelled twice. The resolver needs the
    lab axis without a host to pair it with (a lab can be applicable while
    holding no matching host, which is a distinct verdict). ``None`` applies
    everywhere, for the same reason it targets everything.

    >>> import re
    >>> from otto.config.scope import ProjectScopeConfig, applies_to_lab
    >>> scope = ProjectScopeConfig([re.compile("bench")], [])
    >>> applies_to_lab(scope, "bench"), applies_to_lab(scope, "bench-overflow")
    (True, False)
    """
    if scope is None:
        return True
    return any(p.fullmatch(lab_name) for p in scope.lab_patterns)


def applies_to_any(scope: "ProjectScopeConfig | None", lab_names: "Iterable[str]") -> bool:
    """Report whether *scope* applies to at least one of *lab_names* (lab axis).

    ``True`` for an undeclared (``None``) scope whatever the names; ``False`` for a
    declared scope and no names, since nothing was loaded for it to apply to.

    >>> import re
    >>> from otto.config.scope import ProjectScopeConfig, applies_to_any
    >>> scope = ProjectScopeConfig([re.compile("bench")], [])
    >>> applies_to_any(scope, ["other", "bench"]), applies_to_any(scope, [])
    (True, False)
    """
    if scope is None:
        return True
    return any(applies_to_lab(scope, name) for name in lab_names)


@dataclasses.dataclass(frozen=True)
class ProjectScope:
    """One project's resolved fleet of interest for one loaded lab (spec §5).

    A *verdict*, not a live view: :attr:`universe` is the host set as it stood
    at resolution, used for display (``status --full``) and for the D3
    abort/skip decision. Fleet iteration re-evaluates :func:`host_targeted`
    instead, so a container that joins the lab after resolution is scoped
    correctly rather than frozen out.

    The last four fields exist so a verdict can explain ITSELF. D3's errors
    have to say what was loaded, what was declared, and which file to edit; a
    :func:`require_current_scope` that had to be re-handed the projects and the
    lab to build that sentence would be one more argument every caller could
    get wrong — and the display surface wants the same three facts anyway.
    """

    project_name: str
    """The project this verdict is about (``Project.name``); the dict key too."""

    declared: bool
    """Whether the project has a ``[project]`` table at all.

    False is NOT "targets nothing" — it is the whole-lab fallback (§6), which
    is why it is carried separately from :attr:`excluded`. Product-less projects
    and every pre-``[project]`` project live here.
    """

    config: "ProjectScopeConfig | None"
    """The project's compiled declaration — what :func:`scoped_ids` re-asks LIVE.

    Carried on the verdict because the verdict is what a fleet walk is handed,
    and a walk must re-evaluate :func:`host_targeted` per host rather than read
    :attr:`universe` (see the class docstring). Recompiling from
    :attr:`host_patterns` at every walk would work and would also put the
    "unset means undeclared" rule in a second place; the compiled object is
    already immutable, so carrying it costs nothing and drifts from nothing.
    """

    applicable_labs: frozenset[str]
    """Loaded lab COMPONENTS this project applies to (``a+b`` contributes both).

    An undeclared project applies to all of them: it scoped nothing out.
    """

    universe: frozenset[str]
    """Host ids this project targeted AT RESOLVE TIME — display and abort data only.

    Fleet walks must NOT iterate this snapshot; they re-evaluate
    :func:`host_targeted` live so hosts that join after resolution (docker
    containers) scope correctly.
    """

    excluded: bool
    """Declared, yet no loaded lab applies — D3's skip (dependency) or abort (current).

    Distinct from an empty :attr:`universe`, deliberately. "Wrong lab loaded"
    and "no host here matches your host_patterns" have different fixes, and a
    single flag covering both would send half the readers to the wrong one.
    """

    root: str
    """Where the ``settings.toml`` to edit lives (the project root), and what rides the error."""

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
    projects: "list[Project]",
    component_names: "list[str]",
    hosts: "Mapping[str, Host]",
    exclude_ids: "frozenset[str]" = frozenset(),
) -> "dict[str, ProjectScope]":
    """Resolve every project's fleet of interest against one loaded lab.

    Pure and cheap: it reads exactly three things — each project's ``name`` and
    ``project_scope``, and what :func:`host_targeted` reads of each host (its
    ``source_lab`` and ``id``, a container's parent's) — and returns verdicts.
    A container already in *hosts* is in exactly the universes its parent is in.
    Nothing is raised here even when a verdict is fatal (:func:`require_current_scope`
    is where D3 fires), because resolution runs at context creation and the
    consequence belongs at project-layer entry.

    *exclude_ids* is how the built-in runner and the containers on it stay
    out of every universe, and the indirection is deliberate on two counts.
    Layering: this module must not import ``otto.host``, so it cannot tell the
    built-in runner from a lab entry that reuses its id. Correctness: the
    runner's ``source_lab`` is stamped with whichever component the CLI
    happened to list first, so a membership rule that keyed on that stamp
    would make ``-l a+b`` and ``-l b+a`` resolve differently. The one caller
    that passes them, ``otto.host.builtin_hosts.lab_scopes``, judges them by
    identity. ``include_local`` remains the ambient walks' own knob (§6).

    Args:
        projects: The projects to resolve (bootstrap's set; order is preserved in the
            returned mapping, which is keyed by name).
        component_names: The loaded lab's ``component_names`` — the component
            labs, never the composite ``a+b`` display name.
        hosts: The loaded lab's ``hosts`` mapping, ``{id: host}``.
        exclude_ids: Host ids no universe may contain, whatever the patterns
            say. Defaults to excluding nothing.

    Returns:
        ``{project name: ProjectScope}``.

    >>> import types
    >>> from otto.config.scope import ProjectScopeConfig, resolve_scopes
    >>> import re
    >>> project = types.SimpleNamespace(  # only .name/.project_scope/.root are read
    ...     name="sensors",
    ...     root="/projects/sensors",
    ...     project_scope=ProjectScopeConfig([re.compile("bench")], [re.compile("sensor-.*")]),
    ... )
    >>> hosts = {
    ...     "sensor-1": types.SimpleNamespace(id="sensor-1", source_lab="bench"),
    ...     "gw-1": types.SimpleNamespace(id="gw-1", source_lab="bench"),
    ... }
    >>> scope = resolve_scopes([project], ["bench"], hosts)["sensors"]
    >>> sorted(scope.universe), scope.excluded
    (['sensor-1'], False)
    """
    scopes: dict[str, ProjectScope] = {}
    for project in projects:
        config = project.project_scope
        applicable = frozenset(name for name in component_names if applies_to_lab(config, name))
        # An undeclared project's universe is the whole lab BY CONSTRUCTION:
        # host_targeted admits for no declaration before reading any host
        # attribute, which is what keeps this loop safe over hosts built
        # before scoping (see its docstring).
        universe = frozenset(
            host_id
            for host_id, host in hosts.items()
            if host_id not in exclude_ids and host_targeted(config, host)
        )
        scopes[project.name] = ProjectScope(
            project_name=project.name,
            declared=config is not None,
            config=config,
            applicable_labs=applicable,
            universe=universe,
            excluded=config is not None and not applicable,
            root=str(project.root),
            loaded_labs=tuple(component_names),
            lab_patterns=() if config is None else tuple(p.pattern for p in config.lab_patterns),
            host_patterns=() if config is None else tuple(p.pattern for p in config.host_patterns),
        )
    return scopes


_NO_APPLICABLE_LAB = """\
project '{name}' declares [project] lab_patterns that match none of the loaded
labs, so no host in this run belongs to its fleet of interest.

    loaded labs:  {loaded}
    lab_patterns: {patterns}

Load a lab this project applies to (otto -l <lab>), or widen lab_patterns in
{root}/.otto/settings.toml.\
"""
"""D3's abort for the current project. Both halves are printed because either one
alone reads as the other's fault: patterns without the loaded labs looks like a
typo in the regex, loaded labs without the patterns looks like the wrong -l."""

_EMPTY_UNIVERSE = """\
project '{name}' applies to loaded lab(s) {applicable}, but its [project]
host_patterns match no host there, so every fleet walk it drives would be
empty.

    host_patterns: {patterns}

Widen host_patterns in {root}/.otto/settings.toml (['.*'] is every host in
those labs), or load a lab that holds the hosts this project targets.\
"""
"""The other current-project abort. Named separately from the one above precisely
because the fix differs: the lab was RIGHT here, and telling this user to load
a different one would send them the wrong way."""


def unusable_scope(scope: "ProjectScope") -> bool:
    """Report whether *scope*'s declared fleet of interest cannot work — D3's one condition.

    THE TWIN OF ``_unusable_scope_message``, and it exists for the same
    reason: two sites act on this verdict — the current project's abort
    (:func:`require_current_scope`) and the orchestrator's dependency skip
    (``otto.project.orchestrator._applicable``) — and D3's asymmetry is
    about WHAT THEY DO, never about which condition they read. Spelling the
    condition out at both would let them drift, and a skip that fired on one
    unusable shape while the walk raised on the other is exactly the defect
    this function closes: the dependency's own fleet walk raises out of the
    walk, and one project's declaration takes down another project's run.

    ``declared`` is load-bearing. An undeclared project's universe is empty
    whenever the LAB is empty, and that is the whole-lab fallback (§6) rather
    than a failure — refusing there would break every product-less project and
    every pre-``[project]`` project.

    Args:
        scope: One resolved verdict.

    Returns:
        True when the project declared a ``[project]`` scope that admits no host
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
    caller passing ``("My_Project",)`` would otherwise have its explicit switch
    silently ignored, which is the failure mode this module exists to refuse.
    Normalizing HERE makes the invariant hold at the only place that reads it.

    Shared by :func:`active` and :func:`switched_off` rather than spelled at
    each, because the two must agree about what "the same project" means: a
    ``switched_off`` that said False where ``active`` said False-because-excluded
    would attribute the verdict to the wrong cause.

    The tuples hold one entry per ``-I``/``-E`` the user typed, so rebuilding
    the set per call is free and keeps the function pure.
    """
    from ..models.dependencies import normalize_name

    return frozenset(normalize_name(n) for n in names)


def scopes_of(ctx: "OttoContext") -> "dict[str, ProjectScope]":
    """Return *ctx*'s per-project scope verdicts by ``Project.name``; the one way to read them.

    Resolved once per context, over the context's one bootstrap result, and
    cached with any refusal (``otto.session.ProjectLoadError`` for a fatal load
    error, or the environment's own failure). Empty for the library sentinel
    lab and for a run with no project roots.
    """
    return ctx._resolve_scopes()  # noqa: SLF001 — the context computes and caches them; this is their one reader


def active(project_name: str, ctx: "OttoContext") -> bool:
    """Report whether *project_name* participates in this invocation — THE authority.

    Every enforcement point (bootstrap-error demotion via
    :func:`inactive_before_lab`'s projection, the orchestrator walks,
    instruction dispatch) consults this one predicate, so the resolution
    order is stated once: explicit switch > lab inference > default-on.
    Pure over ``(ctx.include_projects, ctx.exclude_projects, scopes_of(ctx))``;
    no I/O.

    A missing verdict is active on purpose: it covers a run with no project
    roots (``scopes_of(ctx)`` is empty), the undeclared project (whole-lab
    fallback, scoping spec §6), and the library context's sentinel lab alike
    — in every case there is no signal that would justify leaving the project
    out. A run whose projects cannot be read has no missing verdict to fall back
    on: reading ``scopes_of(ctx)`` raises its refusal, and so does this.

    Args:
        project_name: The project's declared ``Project.name``, spelled exactly as the
            project declares it. ``scopes_of(ctx)`` is keyed by that raw name, so a
            user-typed variant (different case, ``_`` for ``-``) finds no
            verdict and therefore fails OPEN — resolving ACTIVE. Callers
            holding a user-supplied spelling must map it back to a declared
            name before asking.
        ctx: The runtime context supplying the switches and the lab verdicts.

    Raises:
        otto.session.ProjectLoadError: :func:`scopes_of` refused, because a project failed
            to load in a way that stops a run. An environment failure
            propagates the same way, as raised.
    """
    from ..models.dependencies import normalize_name

    name = normalize_name(project_name)
    if name in _switched(ctx.exclude_projects):
        return False
    if name in _switched(ctx.include_projects):
        return True
    # RAW name, deliberately: ``scopes_of(ctx)`` is keyed by ``Project.name`` exactly as
    # written, so normalizing this key would miss the verdict of every project whose
    # name holds an ``_``, a ``.`` or a capital — and a missing verdict resolves
    # ACTIVE, i.e. it would fail OPEN into the silent widening D6 refuses. The
    # switches above normalize because they carry USER-typed names; this does not
    # because it carries a declared one.
    verdict = scopes_of(ctx).get(project_name)
    if verdict is None:
        return True
    return not unusable_scope(verdict)


def switched_off(project_name: str, ctx: "OttoContext") -> bool:
    """Report whether *project_name* was explicitly ``--exclude-projects``'d.

    Attribution only — every message that names the SWITCH as the reason
    reads this, while the combined verdict stays :func:`active`'s alone.
    Normalizes both sides through ``_switched``, the same way
    :func:`active` does, so the two cannot disagree about identity. Unlike
    :func:`active` this one reads no lab verdict, so *project_name* may be any
    spelling — normalization is the whole comparison.
    """
    from ..models.dependencies import normalize_name

    return normalize_name(project_name) in _switched(ctx.exclude_projects)


OUT_OF_SCOPE_REASON = "no declared project targets it"
"""The reason a lab host is outside the fleet of interest when it is in no declared universe."""

SWITCHED_OFF_REASON = "only switched-off projects target it"
"""The reason a lab host is outside the fleet of interest when only ``-E``'d projects declare it."""


def _not_switched_off(
    scopes: "list[ProjectScope]", exclude_projects: "tuple[str, ...]"
) -> "list[ProjectScope]":
    """Drop the verdicts of projects ``--exclude-projects`` switched off, in order.

    The fleet-side twin of :func:`switched_off`: it compares through the same
    ``_switched`` normalization, so the fleet and the activation verdict cannot
    disagree about which project ``-E My_Project`` named.
    """
    from ..models.dependencies import normalize_name

    switched = _switched(exclude_projects)
    return [scope for scope in scopes if normalize_name(scope.project_name) not in switched]


def inactive_before_lab(
    scope: "ProjectScopeConfig | None", lab_selection: "Sequence[str] | None"
) -> bool:
    """Report whether *scope* is inactive on the LAB AXIS alone, for seams that run pre-lab.

    The lab-axis projection of :func:`active`.
    Bootstrap-error demotion fires before the lab is built, so it cannot ask
    for a :class:`ProjectScope` verdict; what it CAN know is the ``-l``
    selection (already split into component names) and the project's declared
    ``lab_patterns``. This is deliberately the narrower test: a project that is
    inactive only because it is host-starved is NOT detected here, and its
    bootstrap errors stay fatal (plan deviation 1).
    """
    if not lab_selection or scope is None:
        return False
    return not applies_to_any(scope, lab_selection)


PreLabReason = Literal["excluded", "out_of_scope"]
# Why a project is inactive before the lab is built: switched off, or outside the ``-l`` labs.


def pre_lab_inactive_reason(
    project: "Project",
    *,
    labs: "Sequence[str] | None",
    include: "Sequence[str]",
    exclude: "Sequence[str]",
) -> PreLabReason | None:
    """Say why *project* is inactive before the lab is built, or ``None`` when it is active.

    THE pre-lab verdict: the switches (PEP 503-normalised, as ``_switched``
    does), an ``exclude`` beating an ``include``, then the lab axis
    (:func:`inactive_before_lab`) for a project no ``include`` forces on.
    *labs* is the ``-l`` selection already split into component names; empty
    or ``None`` means no lab axis. The host axis needs the built lab and is
    not judged here.
    """
    from ..models.dependencies import normalize_name

    name = normalize_name(project.name)
    if name in _switched(tuple(exclude)):
        return "excluded"
    if name not in _switched(tuple(include)) and inactive_before_lab(
        project.project_scope, labs or None
    ):
        return "out_of_scope"
    return None


InactiveKind = Literal["excluded", "out_of_lab_scope", "host_starved"]
# The three ways a project is inactive: switched off, wrong lab, or no host of interest.


@dataclasses.dataclass(frozen=True)
class InactiveReason:
    """The ONE record of why a project is inactive; every message renders from it.

    Built only by :func:`inactive_reason` (lab verdicts known) and
    :func:`pre_lab_reason_record` (an already-decided ``PreLabReason``), so
    the excluded / out-of-lab-scope / host-starved branch exists in one place
    and each surface (instruction refusal, skip warning, ``not searched``
    line) is only a renderer over these fields.
    """

    project: str
    """The project's declared name, as it is written."""

    normalized: str
    """The PEP 503-normalised name, which is what ``-I`` / ``-E`` take."""

    reason: InactiveKind
    """Why it is inactive."""

    loaded_labs: "tuple[str, ...]"
    """Every loaded component lab (empty for ``"excluded"``)."""

    lab_patterns: "tuple[str, ...]"
    """The declared ``lab_patterns`` (empty for ``"excluded"`` and when undeclared)."""

    host_patterns: "tuple[str, ...]"
    """The declared ``host_patterns`` (empty for ``"excluded"`` and when undeclared)."""

    applicable_labs: "tuple[str, ...]"
    """The loaded labs its scope applies to (empty for ``"excluded"`` and out of lab scope)."""


def inactive_reason(project_name: str, ctx: "OttoContext") -> "InactiveReason | None":
    """Say why *project_name* is inactive in *ctx*, or ``None`` when :func:`active` says it is.

    THE reason branch: the switch (:func:`switched_off`) first, then the lab
    verdict's ``excluded`` flag separates a wrong lab from a host-starved
    project. *project_name* is the declared name, as for :func:`active`.
    """
    from ..models.dependencies import normalize_name

    if active(project_name, ctx):
        return None
    normalized = normalize_name(project_name)
    if switched_off(project_name, ctx):
        return InactiveReason(
            project=project_name,
            normalized=normalized,
            reason="excluded",
            loaded_labs=(),
            lab_patterns=(),
            host_patterns=(),
            applicable_labs=(),
        )
    # Not switched off, yet inactive: `active()` reaches that verdict only by
    # finding an unusable ProjectScope under the declared name, so the lookup
    # cannot miss.
    scope = scopes_of(ctx)[project_name]
    return InactiveReason(
        project=project_name,
        normalized=normalized,
        reason="out_of_lab_scope" if scope.excluded else "host_starved",
        loaded_labs=scope.loaded_labs,
        lab_patterns=scope.lab_patterns,
        host_patterns=scope.host_patterns,
        applicable_labs=tuple(lab for lab in scope.loaded_labs if lab in scope.applicable_labs),
    )


def pre_lab_reason_record(
    project: "Project", reason: PreLabReason, *, labs: "Sequence[str]"
) -> InactiveReason:
    """Record an already-decided pre-lab *reason* for *project* as an :class:`InactiveReason`.

    Maps, never decides: *reason* came from :func:`pre_lab_inactive_reason`.
    ``"out_of_scope"`` becomes ``"out_of_lab_scope"`` and carries the ``-l``
    *labs* as the loaded ones; no lab applies, by that verdict.
    """
    from ..models.dependencies import normalize_name

    normalized = normalize_name(project.name)
    if reason == "excluded":
        return InactiveReason(
            project=project.name,
            normalized=normalized,
            reason="excluded",
            loaded_labs=(),
            lab_patterns=(),
            host_patterns=(),
            applicable_labs=(),
        )
    declared = project.project_scope
    return InactiveReason(
        project=project.name,
        normalized=normalized,
        reason="out_of_lab_scope",
        loaded_labs=tuple(labs),
        lab_patterns=tuple(p.pattern for p in declared.lab_patterns) if declared else (),
        host_patterns=tuple(p.pattern for p in declared.host_patterns) if declared else (),
        applicable_labs=tuple(lab for lab in labs if applies_to_lab(declared, lab)),
    )


def not_searched_line(reason: InactiveReason) -> str:
    """Render the one-line ``project X was not searched`` notice for *reason*."""
    name = reason.project
    labs = ", ".join(reason.loaded_labs)
    if reason.reason == "excluded":
        return (
            f"project {name} was not searched: it was switched off with -E {name}. "
            f"Remove -E {name} to search it."
        )
    if reason.reason == "out_of_lab_scope":
        return (
            f"project {name} was not searched: its lab_patterns "
            f"({', '.join(reason.lab_patterns)}) do not match the loaded lab(s) {labs}. "
            f"Use -I {name} to search it anyway."
        )
    return (
        f"project {name} was not searched: its host_patterns "
        f"({', '.join(reason.host_patterns)}) match no host in the loaded lab(s) {labs}. "
        f"Use -I {name} to search it anyway."
    )


@final
@dataclasses.dataclass(frozen=True, kw_only=True)
class ScopeSelection:
    """A run's switches before its lab is loaded: the ``-l`` labs and the ``-I`` / ``-E`` projects.

    A listing judges which projects take part by it, and
    :func:`otto.session.build_lab` places the declared containers by it.
    """

    labs: "tuple[str, ...]"
    """The ``-l`` lab names, already split into components."""

    include: "tuple[str, ...]"
    """``-I``: the project names as given; they are compared PEP 503-normalized."""

    exclude: "tuple[str, ...]"
    """``-E``: the project names as given; they are compared PEP 503-normalized."""


@dataclasses.dataclass(frozen=True)
class DroppedProject:
    """A project that does not take part, with the record of why."""

    project: "Project"
    reason: InactiveReason


ParticipationTier = Literal["lab", "pre_lab"]
# Which verdict judged: ``"lab"`` (the built lab's) or ``"pre_lab"`` (``-l`` names only).


@dataclasses.dataclass(frozen=True)
class Participation:
    """Which of a project list take part, input order kept in both lists."""

    kept: "list[Project]"
    dropped: "list[DroppedProject]"
    tier: ParticipationTier


def participating(
    projects: "Sequence[Project]",
    *,
    ctx: "OttoContext | None" = None,
    selection: "ScopeSelection | None" = None,
) -> Participation:
    """Split *projects* into those that take part and those that do not -- THE answer.

    Exactly one source of truth: *ctx* (a built lab: :func:`inactive_reason`,
    tier ``"lab"``) or *selection* (no lab yet: :func:`pre_lab_inactive_reason`
    over the ``-l`` names, tier ``"pre_lab"``). Passing both or neither is a
    :class:`TypeError`.
    """
    if (ctx is None) == (selection is None):
        raise TypeError("participating() takes exactly one of ctx= or selection=")
    kept: "list[Project]" = []
    dropped: "list[DroppedProject]" = []
    for project in projects:
        if ctx is not None:
            reason = inactive_reason(project.name, ctx)
        else:
            assert selection is not None  # noqa: S101 — narrowed by the guard above
            pre = pre_lab_inactive_reason(
                project,
                labs=selection.labs,
                include=selection.include,
                exclude=selection.exclude,
            )
            reason = (
                None if pre is None else pre_lab_reason_record(project, pre, labs=selection.labs)
            )
        if reason is None:
            kept.append(project)
        else:
            dropped.append(DroppedProject(project, reason))
    return Participation(kept, dropped, "lab" if ctx is not None else "pre_lab")


def _skip_message(reason: "InactiveReason", verb: str) -> str:
    """Render WHY a project is being left out of *verb*'s walk, over its ``InactiveReason``.

    TWO CONDITIONS, TWO TEXTS, each reachable only under its own -- the same
    split :func:`_unusable_scope_message` makes for the
    abort, and for the same reason: "no loaded lab applies to you" and "no host
    in the labs that do apply matches your host_patterns" have different fixes,
    and one text covering both sends half its readers to edit the wrong line.
    The out-of-lab-scope project's reader needs the loaded labs beside the
    declared ``lab_patterns``; the host-starved one's needs the
    ``host_patterns``, because its ``-l`` and its ``lab_patterns`` are already
    right and saying otherwise would send it to change them. A switched-off
    project has its own text (:func:`switched_off_message`).

    Args:
        reason: Why the project is inactive, from
            :func:`~otto.config.scope.inactive_reason`.
        verb: What is being skipped, so the reader knows which operation left
            the project out.

    Returns:
        The WARNING line, ready to log.
    """
    if reason.reason == "excluded":
        return switched_off_message(reason, verb)
    if reason.reason == "out_of_lab_scope":
        return (
            f"project {reason.project!r} is not applicable to the loaded lab(s) "
            f"[{', '.join(reason.loaded_labs) or '(none)'}] "
            f"(lab_patterns: {', '.join(reason.lab_patterns) or '(none)'}) "
            f"— skipping it for {verb}"
        )
    return (
        f"project {reason.project!r} applies to lab(s) "
        f"[{', '.join(sorted(reason.applicable_labs)) or '(none)'}] but its [project] "
        f"host_patterns ({', '.join(reason.host_patterns) or '(none)'}) match no host there "
        f"— skipping it for {verb}"
    )


def switched_off_message(reason: "InactiveReason", verb: str) -> str:
    """Render the warning for a project ``--exclude-projects`` switched off, for *verb*."""
    return (
        f"project {reason.project!r} switched off for this run "
        f"(--exclude-projects {reason.normalized}) — skipping it for {verb}"
    )


def announce_dropped(
    dropped: "Sequence[DroppedProject]", *, verb: str, log: "logging.Logger"
) -> None:
    """Log one WARNING per dropped project saying why *verb* skips it.

    *log* is the caller's own logger, which already carries the operator's
    handlers and level, so the lines keep their source.
    """
    for item in dropped:
        log.warning(_skip_message(item.reason, verb))


def _unusable_scope_message(scope: "ProjectScope") -> str:
    """Render the D3 message for a project whose fleet of interest cannot work.

    Extracted so the two sites that reach this condition — D3's project-layer
    entry check (:func:`require_current_scope`) and an owner-bound fleet walk
    that admits nothing (:func:`require_nonempty_fleet`) — cannot describe the
    same project differently. Which of the two templates applies is the verdict's
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
            name=scope.project_name,
            loaded=", ".join(scope.loaded_labs) or "(none)",
            patterns=", ".join(scope.lab_patterns) or "(none)",
            root=scope.root,
        )
    return _EMPTY_UNIVERSE.format(
        name=scope.project_name,
        applicable=", ".join(sorted(scope.applicable_labs)) or "(none)",
        patterns=", ".join(scope.host_patterns) or "(none)",
        root=scope.root,
    )


def require_current_scope(scopes: "dict[str, ProjectScope]", current_project_name: str) -> None:
    """Enforce D3 for the CURRENT project: raise when its fleet of interest is unusable.

    Call at project-layer entry (default instructions, a suite's ``ensure``
    marker steps, monitor fleet build) — not at context creation, so explicit
    ``otto host <id> <verb>`` targeting still works when a declaration is
    wrong. Which project is current (``bootstrap().projects[0]``, the driving
    project) is the caller's reading; this function is handed the name.

    A DEPENDENCY in the same *scopes* mapping is never enforced here: an
    excluded dependency is skipped loudly by the orchestrator and shown as
    not-applicable by ``status``, which is D3's deliberate asymmetry — one
    project's fleet declaration must not veto another's run.

    Args:
        scopes: The resolver's output.
        current_project_name: ``Project.name`` of the driving project. A name absent
            from *scopes* returns None: no verdict was resolved for it (a project
            bootstrap skipped, say), and that failure is already reported.

    Returns:
        None — always, when there is nothing to refuse.

    Raises:
        ProjectScopeError: The same class bootstrap's D2 check
            raises; the taxonomy has one "this project's scope declaration cannot
            work" error, not one per site. Message names the project, what was
            loaded, what was declared, and the file to edit.
    """
    scope = scopes.get(current_project_name)
    # The condition is :func:`unusable_scope`'s, never a copy of it — the
    # orchestrator's dependency skip reads the same predicate, and D3's
    # asymmetry is about the RESPONSE (abort here, skip there), not about which
    # verdicts qualify. Undeclared never qualifies: that is the whole-lab
    # fallback (§6), including when its universe is empty because the LAB is
    # empty, which is a lab problem and not this check's to diagnose.
    if scope is None or not unusable_scope(scope):
        return
    raise ProjectScopeError(scope.root, message=_unusable_scope_message(scope))


def require_driving_scope(ctx: "OttoContext") -> None:
    """Enforce D3 for *ctx*'s DRIVING project: :func:`require_current_scope` on the first project.

    The one answer to "which project's declaration must work for this run".
    The driving project is the first project the run parsed
    (``ctx.unscoped.projects[0]``, the first ``OTTOPATH`` entry), never the
    head of the dependency-ordered walk, which is a dependency in any lab that
    has one. The project orchestrator's entry and ``otto monitor --live``
    both call it.

    The verdicts are read first, so a project that failed to load refuses
    through :func:`scopes_of`, whichever project broke. A run with no
    projects has no driving project to enforce, and that is legal.

    Raises:
        ProjectScopeError: The driving project declared a ``[project]`` scope
            that admits no host here.
        otto.session.ProjectLoadError: A project failed to load in a way that
            stops a run, so the verdicts cannot be known.
    """
    scopes = scopes_of(ctx)
    projects = ctx.unscoped.projects
    if projects:
        require_current_scope(scopes, projects[0].name)


def union_admits(
    host: object, scopes: "dict[str, ProjectScope]", *, exclude_projects: "Sequence[str]"
) -> bool:
    """Report whether the declared union of *scopes* admits *host* (D7).

    The union across the DECLARED projects not switched off with
    ``--exclude-projects``, each judged by :func:`host_targeted`. It owns the
    whole-lab fallback (§6): when no project declared ``[project]`` it admits
    every host, before reading anything off it. "No project declared" is the
    condition, not "some project did not": an undeclared project's own
    verdict admits everything, so folding it into the union would silently
    restore the whole lab the moment a product-less project shared the run.
    Excluding every declaring project admits NOTHING: the fallback is for
    "nothing declared", and answering a request for less with the whole lab
    would be the widening this design refuses.

    Args:
        host: One lab host, of any family.
        scopes: :func:`resolve_scopes`' output for this run.
        exclude_projects: The run's ``--exclude-projects`` names, as typed;
            compared normalized, the way :func:`active` compares them.

    Returns:
        True when some active declaration targets *host*, or none declared.

    >>> import re
    >>> import types
    >>> from otto.config.scope import ProjectScopeConfig, resolve_scopes, union_admits
    >>> project = types.SimpleNamespace(
    ...     name="sensors",
    ...     root="/projects/sensors",
    ...     project_scope=ProjectScopeConfig([re.compile("bench")], [re.compile("sensor-.*")]),
    ... )
    >>> gw = types.SimpleNamespace(id="gw-1", source_lab="bench")
    >>> scopes = resolve_scopes([project], ["bench"], {"gw-1": gw})
    >>> union_admits(gw, scopes, exclude_projects=()), union_admits(gw, {}, exclude_projects=())
    (False, True)
    """
    declared = [scope for scope in scopes.values() if scope.declared]
    if not declared:
        return True
    return any(
        host_targeted(scope.config, host)
        for scope in _not_switched_off(declared, tuple(exclude_projects))
    )


def scoped_ids(
    hosts: "Mapping[str, Host]",
    scopes: "dict[str, ProjectScope]",
    owner: "str | None",
    *,
    exclude_projects: "tuple[str, ...]" = (),
    runner_ids: "frozenset[str]" = frozenset(),
) -> "set[str]":
    """Return the host ids a fleet walk may iterate — evaluated LIVE, not read off a snapshot.

    The ambient universe of spec §6, in one place so both fleet surfaces
    (:meth:`otto.context.OttoContext.all_hosts` and the module-level
    :func:`otto.fleet.all_hosts` that delegates to it) cannot disagree,
    nor can the pure query :func:`otto.fleet.fleet_of_interest`.
    Membership is re-derived by calling :func:`host_targeted` against *hosts*
    as they stand at the call, so a docker container registered after the
    context was created is scoped correctly, by its parent, instead of frozen
    out — which is precisely what reading :attr:`ProjectScope.universe` here
    would do.

    Two answers:

    * *owner* **names a project** — that project's admission alone
      (:func:`host_targeted` over its declaration). A known project that
      declared no ``[project]`` admits the whole lab: its own verdict admits
      everything. An owner that is not in *scopes* AT ALL is a different
      thing entirely and is REFUSED — see Raises.
    * *owner* **is None** — :func:`union_admits` per host: the union across
      the declared projects (D7), or the whole lab when none declared. It is
      the fleet that host-global operations (cleanup, toolchain, debug logs)
      walk. A project switched off with ``--exclude-projects`` contributes
      nothing to it.

    *runner_ids* are admitted in both answers, whatever any declaration says:
    the built-in runner and its containers are judged by identity, never by
    patterns over the runner's stamped ``source_lab`` (the first ``-l``
    component), so the component order cannot change a walk. This module
    cannot tell the runner from a lab entry reusing its id, so the caller
    names them; ``otto.host.builtin_hosts.admitted_ids`` is the one that
    does. Whether a walk wants them stays the walk's own ``include_local``
    and ``include_containers`` choice, applied after (§6).

    Args:
        hosts: The lab's live ``{id: host}`` mapping — read at call time.
        scopes: :func:`resolve_scopes`' output for this run.
        owner: ``Project.name`` whose universe bounds this walk, or ``None`` for
            the union. The project-scoped context view (spec §7,
            ``ctx.for_project(name=project)``) is what supplies a name; a plain context
            always passes ``None``.
        exclude_projects: The run's ``--exclude-projects`` names, as typed;
            compared normalized, the way :func:`active` compares them. Read by
            the union only: an owner-bound walk is that project's own, and a
            switched-off project is never walked.
        runner_ids: Ids admitted in every answer, by identity. Defaults to none.

    Returns:
        The admissible ids, as a set.

    Raises:
        ProjectScopeError: *owner* names a project that is not in
            *scopes*, while *scopes* holds verdicts. Silently falling back to
            the whole lab there would turn a caller's typo — or a project this
            run never parsed — into exactly the quiet WIDENING this
            design exists to prevent. An empty *scopes* is not that case: it
            means the run has no projects to declare anything (the library
            sentinel lab, or no project roots), and every owner is unknown
            for a reason that is not the caller's, so the fallback stands.
            Projects that could not be read never reach here as an empty
            *scopes*: :func:`scopes_of` refuses first.

    >>> import re
    >>> import types
    >>> from otto.config.scope import ProjectScope, ProjectScopeConfig, scoped_ids
    >>> config = ProjectScopeConfig([re.compile("bench")], [re.compile("sensor-.*")])
    >>> verdict = ProjectScope(
    ...     project_name="sensors",
    ...     declared=True,
    ...     config=config,
    ...     applicable_labs=frozenset({"bench"}),
    ...     universe=frozenset({"sensor-1"}),
    ...     excluded=False,
    ...     root="/projects/sensors",
    ...     loaded_labs=("bench",),
    ...     lab_patterns=("bench",),
    ...     host_patterns=("sensor-.*",),
    ... )
    >>> hosts = {
    ...     "sensor-1": types.SimpleNamespace(id="sensor-1", source_lab="bench"),
    ...     "gw-1": types.SimpleNamespace(id="gw-1", source_lab="bench"),
    ... }
    >>> sorted(scoped_ids(hosts, {"sensors": verdict}, None))
    ['sensor-1']
    >>> sorted(scoped_ids(hosts, {}, None))  # nothing declared — the whole lab
    ['gw-1', 'sensor-1']
    """
    if owner is not None and scopes and owner not in scopes:
        # Checked BEFORE any admission, so an unknown owner is refused
        # whether or not anything declared: the caller asked to be bounded by a
        # project that does not exist, and answering "the whole lab" to that is
        # the widening, not a degraded mode.
        raise ProjectScopeError(
            "",  # no project, so no settings.toml to send the reader to
            message=_UNKNOWN_OWNER.format(owner=owner, known=", ".join(sorted(scopes)) or "(none)"),
        )
    if owner is not None and scopes:
        config = scopes[owner].config  # present: the unknown-owner refusal above ran first
        return {
            host_id
            for host_id, host in hosts.items()
            if host_id in runner_ids or host_targeted(config, host)
        }
    return {
        host_id
        for host_id, host in hosts.items()
        if host_id in runner_ids or union_admits(host, scopes, exclude_projects=exclude_projects)
    }


_UNKNOWN_OWNER = """\
a fleet walk was bound to project '{owner}', which is not among this run's
resolved projects — so no [project] declaration can bound it.

    resolved projects: {known}

A walk bound to a project otto never resolved would fall back to the WHOLE lab,
which is the silent widening this scoping exists to prevent. Bind the walk to
one of the projects above, or use the plain context to walk their union."""
"""Not a user-facing config mistake but a caller one, which is why it names the
resolved set rather than a file to edit: whoever built the project-scoped view
passed a name otto does not know, and the resolved names are what tells them
whether it is a typo or a project bootstrap skipped."""


_EMPTY_FLEET = """\
no host in this run belongs to any project's fleet of interest, so every fleet
walk would be empty.

    loaded labs:  {loaded}
    declared by:  {projects}{settings}

Load a lab these projects apply to (otto -l <lab>), or widen [project]
lab_patterns / host_patterns in their .otto/settings.toml.\
"""
"""Spec §10 row 5. Deliberately fleet-shaped rather than project-shaped: the
per-project aborts in :func:`require_current_scope` fire for the CURRENT project
and can name one file, while this one fires when the union came out empty and
any of the contributing declarations could be the reason. ``{settings}`` is
:data:`_SETTINGS_LINE` over the files to widen, or nothing when there are none."""


_SETTINGS_LINE = "\n    settings:     {files}"
""":data:`_EMPTY_FLEET`'s ``settings:`` line, left out when no file would widen the scope:
with every declaring project switched off, an empty field would read as a fault."""


_EXCLUDED_FLEET = """\
no host is in play: every project whose [project] declaration admits a host in
the loaded labs was switched off for this run (--exclude-projects), so every
fleet walk would be empty.

    switched off: {projects}

Drop --exclude-projects for a project whose hosts this run should reach.\
"""
"""The union came out empty because of ``-E``, not because of a declaration —
:data:`_EMPTY_FLEET`'s "widen your patterns" would send the reader to edit a
file that is not the cause."""


def require_nonempty_fleet(
    scopes: "dict[str, ProjectScope]",
    admissible: "set[str]",
    owner: "str | None" = None,
    *,
    exclude_projects: "tuple[str, ...]" = (),
) -> None:
    """Refuse a fleet walk whose base set is empty while some project declared one (§10 row 5).

    Call at the fleet surfaces, over the walk's hosts in play: its
    :func:`scoped_ids` answer less the built-in runner and its containers,
    which are admitted by identity and so would make every declared scope
    look non-empty in a lab that holds the runner. The asymmetry with
    the undeclared case is the point: an empty walk under the whole-lab
    fallback means the LAB is empty, which is not this check's to diagnose and
    has always been silent; an empty walk under a declaration means the
    declarations and the loaded lab disagree, and a sweep that quietly touches
    nothing is the failure this design exists to make loud.

    *owner* decides WHICH failure gets reported, and getting that wrong is not
    cosmetic. An owner-bound walk that admits nothing while the union is
    healthy is ONE project's problem: the fleet-shaped message below would claim
    "every fleet walk would be empty" — false, the plain-context walk works —
    list every declaring project as a suspect, and point at the FIRST one's
    ``settings.toml``, sending the reader to edit a file that is not the cause.
    With *owner* set, the same condition is reported in D3's per-project framing
    instead, built from that project's own verdict.

    Args:
        scopes: :func:`resolve_scopes`' output for this run.
        admissible: The walk's hosts in play, for *owner* (see above).
        owner: The ``Project.name`` this walk was bound to, or ``None`` for a
            plain-context union walk. An owner that declared nothing falls
            through to the fleet-shaped message deliberately: its base set was
            the whole lab, so an empty one says the LAB is empty and says
            nothing about that project's declaration.
        exclude_projects: The same names :func:`scoped_ids` was given. For
            the plain-context union, a union emptied by ``-E`` — some
            switched-off project would have put a host in play — gets a message
            naming the switch, not a declaration that cannot work; otherwise
            the fleet-shaped message lists the projects still contributing.
            Every fleet-shaped message's ``settings:`` line names
            :func:`settings_files` for them, so a switched-off project's file is
            never offered as the fix; with no file to name, the line is left out.

    Returns:
        None — always, when there is nothing to refuse.

    Raises:
        ProjectScopeError: The base set is empty and at least
            one project declared ``[project]``. Same class as D3's per-project
            aborts: the taxonomy has one "a scope declaration cannot work"
            error, and this is that condition read at a fleet surface.
    """
    if admissible:
        return
    declared = [scope for scope in scopes.values() if scope.declared]
    if not declared:
        return
    if owner is not None:
        owned = scopes.get(owner)
        if owned is not None and owned.declared:
            raise ProjectScopeError(owned.root, message=_unusable_scope_message(owned))
    # Only the plain-context union reads the switch, as in :func:`scoped_ids`:
    # an UNDECLARED owner fell through to here because its base set was the
    # whole lab, so its empty walk says the lab is empty, whatever -E said.
    contributing = declared
    if owner is None:
        contributing = _not_switched_off(declared, exclude_projects)
        kept = {id(scope) for scope in contributing}
        switched = [scope for scope in declared if id(scope) not in kept]
        # The switch is the cause exactly when a switched-off project would have
        # put a host in play: with it, the union could not have been empty.
        # A lab-inactive declarer left behind is not the cause and must not be
        # named as one — that would send the reader to edit its settings.toml.
        if any(scope.universe for scope in switched):
            raise ProjectScopeError(
                "",  # the cause is a switch on this command line, not a settings.toml
                message=_EXCLUDED_FLEET.format(
                    projects=", ".join(scope.project_name for scope in switched)
                ),
            )
        # Switched-off projects that admitted nothing anyway did not empty it;
        # the declarations did, so name them all when none is left standing.
        contributing = contributing or declared
    files = settings_files(scopes, exclude_projects=exclude_projects)
    raise ProjectScopeError(
        contributing[0].root,
        message=_EMPTY_FLEET.format(
            loaded=", ".join(contributing[0].loaded_labs) or "(none)",
            projects=", ".join(
                f"{scope.project_name} (lab_patterns: {', '.join(scope.lab_patterns) or '(none)'})"
                for scope in contributing
            ),
            settings=_SETTINGS_LINE.format(files=", ".join(files)) if files else "",
        ),
    )


# ---------------------------------------------------------------------------
# Why a lab host is outside this run's scope, and the refusal that says so
# ---------------------------------------------------------------------------


def settings_files(
    scopes: "dict[str, ProjectScope]", *, exclude_projects: "Sequence[str]"
) -> list[str]:
    """Return the ``settings.toml`` files whose ``[project]`` patterns would widen this run.

    The one answer to "which file do I edit to reach a host": each declaring
    project's ``{root}/.otto/settings.toml``, in *scopes*' order, less the
    projects ``--exclude-projects`` switched off (their declarations put no
    host in play, so editing them would change nothing). An undeclared
    project scopes nothing out, so it has no file here. Every message that
    sends a reader to edit their declarations reads this list:
    :func:`host_outside`'s record, :func:`require_nonempty_fleet`'s
    ``settings:`` line, and the run's scope judge.

    Args:
        scopes: :func:`resolve_scopes`' output for this run.
        exclude_projects: The run's ``--exclude-projects`` names, as typed;
            compared normalized, the way :func:`active` compares them.

    Returns:
        The file paths, possibly empty.
    """
    declared = [scope for scope in scopes.values() if scope.declared]
    return [
        str(Path(scope.root) / TOML_SETTINGS_PATH)
        for scope in _not_switched_off(declared, tuple(exclude_projects))
    ]


@final
@dataclasses.dataclass(frozen=True)
class HostOutside:
    """Why one lab host is outside this run's scope: what :func:`host_outside` returns.

    A record, not a message: :func:`outside_message` renders it, and the
    fleet-of-interest log line reads its :attr:`reason`.
    """

    host_id: str
    """The id of the host that is outside."""

    lab: str
    """The lab the host is in, its own component lab, which ``lab_patterns`` match."""

    kind: Literal["out_of_scope", "switched_off"]
    """``"switched_off"`` when only ``-E``'d projects target the host, else ``"out_of_scope"``."""

    switched_off: "tuple[str, ...]"
    """The switched-off projects that target the host, in order; empty for ``"out_of_scope"``."""

    settings_files: "tuple[str, ...]"
    """The files whose patterns would widen this run to the host (:func:`settings_files`)."""

    @property
    def reason(self) -> str:
        """The short reason, :data:`OUT_OF_SCOPE_REASON` or :data:`SWITCHED_OFF_REASON`."""
        return SWITCHED_OFF_REASON if self.kind == "switched_off" else OUT_OF_SCOPE_REASON


def host_outside(
    host: object,
    scopes: "dict[str, ProjectScope]",
    *,
    lab_name: str,
    exclude_projects: "Sequence[str]",
) -> "HostOutside | None":
    """Report why the declared union leaves *host* out, or ``None`` when it admits it.

    Judged by :func:`union_admits`, so it answers ``None`` exactly where a
    fleet walk's union admits the host, the whole-lab fallback included. The
    built-in runner is admitted by identity, not by any declaration, which is
    the caller's to know: this module cannot tell the runner from a lab
    entry that reuses its id.

    Args:
        host: One lab host, of any family; a container is judged by its parent.
        scopes: :func:`resolve_scopes`' output for this run.
        lab_name: The loaded lab's name, named for a host that carries no
            component stamp of its own.
        exclude_projects: The run's ``--exclude-projects`` names, as typed.

    Returns:
        The record of why *host* is outside, or ``None``.
    """
    if union_admits(host, scopes, exclude_projects=exclude_projects):
        return None
    declared = [scope for scope in scopes.values() if scope.declared]
    kept = {id(scope) for scope in _not_switched_off(declared, tuple(exclude_projects))}
    switched = tuple(
        scope.project_name
        for scope in declared
        if id(scope) not in kept and host_targeted(scope.config, host)
    )
    named: Any = host
    return HostOutside(
        host_id=named.id,
        lab=getattr(judged_host(host), "source_lab", None) or lab_name,
        kind="switched_off" if switched else "out_of_scope",
        switched_off=switched,
        settings_files=tuple(settings_files(scopes, exclude_projects=exclude_projects)),
    )


def outside_message(outside: HostOutside) -> str:
    """Render *outside* as the one-line refusal a lookup of that host raises.

    It names the host, its lab and why it is outside, then the fix: drop the
    ``-E`` that switched off the projects targeting it, or add the host to a
    declaring project's patterns, naming each file to edit.
    """
    head = (
        f"host {outside.host_id!r} is in lab {outside.lab!r} "
        f"but outside this run's scope: {outside.reason}"
    )
    if outside.kind == "switched_off":
        names = ", ".join(outside.switched_off)
        drops = " or ".join(f"-E {name}" for name in outside.switched_off)
        return f"{head} ({names}). Drop {drops} to reach it."
    files = ", ".join(outside.settings_files)
    where = f" in {files}" if files else ""
    return (
        f"{head}. To reach it, add its id to [project] host_patterns "
        f"(or its lab to lab_patterns){where}."
    )


class HostOutOfScopeError(_UnquotedKeyError):
    """A lookup named a host that is in the lab but outside this run's scope.

    Raised by ``get_host`` (the run context's, ``otto.get_host`` and
    ``otto.lab.get_host``) for a host no active project's ``[project]``
    declaration targets. It is an :class:`~otto.errors.OttoError` and a
    :class:`KeyError`, as a lookup miss always was, so an existing
    ``except KeyError`` still catches it; an id that is not in the lab at all
    raises ``otto.host.targeting.UnknownHostError``, also a ``KeyError`` that
    is an ``OttoError``.

    ``str()`` is the message itself, without :class:`KeyError`'s quotes. It
    names why the host is outside and the fix: the ``settings.toml`` files
    to widen, or the ``-E`` to drop. :attr:`outside` carries the same facts
    as a record.
    """

    def __init__(self, message: str, *, outside: "HostOutside | None" = None) -> None:
        """Carry *message* as the error's text and *outside* as its record."""
        super().__init__(message)
        self.outside = outside
        """Why the host is outside, or ``None`` when the raiser had no record."""


# ---------------------------------------------------------------------------
# Where a project keeps its settings, and the runtime settings types
# ---------------------------------------------------------------------------
# The frozen dataclasses otto.models.settings' specs build (``to_runtime()``) and
# otto.config.project holds.
# The four otto.config exports (DockerImage, DockerCompose, DockerSettings,
# MonitorSettings) keep that public path.

SETTINGS_FILENAME = "settings.toml"
TOML_SETTINGS_PATH = Path(".otto") / SETTINGS_FILENAME


ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
"""Context file names docker reads as a tar archive on ``docker build -``."""


@final
@dataclasses.dataclass(frozen=True)
class DockerImage:
    """A Dockerfile-built image declared by a project."""

    name: str
    """The image repository name docker sees, verbatim; no tag."""

    dockerfile: Path
    """Absolute path to the Dockerfile."""

    context: Path
    """Absolute path to the build context directory."""

    target: str | None = None
    """Optional multi-stage build target."""

    build_args: tuple[tuple[str, str], ...] = ()
    """Frozen list of (name, value) build args. Tuples (not dicts) so the
    container is hashable and order is preserved."""

    dockerfile_in_archive: str = ""
    """For an archive ``context``: the Dockerfile's path inside the archive, as written."""

    @property
    def is_archive(self) -> bool:
        """True when ``context`` is a tar archive given to docker unopened."""
        return self.context.name.endswith(ARCHIVE_SUFFIXES)


@final
@dataclasses.dataclass(frozen=True)
class DockerCompose:
    """A docker-compose file contributed by a project."""

    path: Path
    """Absolute path to the compose YAML file."""

    services: tuple[str, ...] = ()
    """Service names declared in the compose file.

    Authoritative, not a hint: this is the list container hosts are registered
    from, and it is what lets tab-completion synthesize ids without parsing
    YAML on the completion fast path. The legacy per-project ``compose_up`` path
    additionally compares it with ``docker compose config --services`` once the
    stack is up and warns on drift; the use-case deploy path registers from the
    declaration alone.
    """

    name: str = ""
    """Handle use-case fragments reference; the spec fills the path stem when unset."""

    users: tuple[tuple[str, str], ...] = ()
    """Frozen, sorted (service, user) pairs from ``users = {...}``: the declared
    default access user per service. Tuples rather than a dict so the container
    stays hashable, matching ``build_args``. Values go to docker verbatim."""


@dataclasses.dataclass(frozen=True)
class DockerUseCase:
    """One ``[[docker.use_cases]]`` fragment (spec §3.1): the atom of participation.

    A use-case deploys on one parent (the ``--parent`` it is given, or the
    lab's default by the one rule), so a fragment says nothing about where.
    """

    name: str
    """Use-case this fragment belongs to; same name across projects = one use-case."""
    composes: "tuple[str, ...]"
    """Handles into this project's [[docker.composes]]."""
    provides: "str | None" = None
    """Capability this fragment offers to the provider competition (spec §4)."""
    priority: int = 0
    """Competition rank; higher wins; only meaningful with provides."""
    env: "dict[str, str]" = dataclasses.field(default_factory=dict)
    """Channel-1 static env; values may use ``${otto:...}`` fact refs (spec §6)."""
    pass_env: "tuple[str, ...]" = ()
    """Allowlisted variable names copied from the invoking user's shell."""


@final
@dataclasses.dataclass(frozen=True)
class DockerSettings:
    """Per-project docker configuration parsed from `[docker]` in `settings.toml`."""

    images: tuple[DockerImage, ...] = ()
    """Images this project knows how to build."""

    composes: tuple[DockerCompose, ...] = ()
    """Compose files this project contributes."""

    use_cases: tuple[DockerUseCase, ...] = ()
    """`[[docker.use_cases]]` fragments this project contributes."""


def declared_use_case_names(projects: "Iterable[Project]") -> list[str]:
    """Return every use-case name *projects* declare, each once, in declaration order.

    The one answer to "which use-cases are declared": fragments sharing a name,
    in one project or across several, are one use-case (spec §3.1), so a name
    counts once. Pure and settings-only (no lab, no host, no bootstrap), so the
    completion fast path may call it; a caller that wants another order sorts
    the result.
    """
    return list(
        dict.fromkeys(
            fragment.name for project in projects for fragment in project.docker_settings.use_cases
        )
    )


@final
@dataclasses.dataclass(frozen=True)
class MonitorSettings:
    """Per-project monitor configuration parsed from `[monitor]` in `settings.toml`.

    ``tls_cert``/``tls_key`` point at a PEM certificate/key on the machine that
    runs ``otto monitor`` (conventionally under ``~/.otto/tls/`` — the
    committed settings value is shared team-wide, so it must not name a
    machine-local absolute path). ``tls_key`` may stay ``None`` when the cert
    PEM bundles the private key.
    """

    tls_cert: Path | None = None
    tls_key: Path | None = None
