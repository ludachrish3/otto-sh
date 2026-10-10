"""Use-case resolution (spec §4-§6): selection, the one parent, env — pure functions.

No device touches anywhere in this module, so everything here runs under
``--dry-run`` and powers ``otto docker use-cases``. Refusals raise
:class:`UseCaseResolutionError` (an :class:`~otto.errors.OttoError` that
keeps ``ValueError`` as its stdlib root): they are configuration errors,
settled before anything is staged or started.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, cast

from ..config.scope import repo_targets, scope_for_repo
from ..errors import OttoError

if TYPE_CHECKING:
    from ..config.lab import Lab
    from ..config.repo import DockerUseCase, Repo
    from ..host.unix_host import UnixHost


class UseCaseResolutionError(OttoError, ValueError):
    """A use-case cannot be resolved from configuration; nothing was touched."""


@dataclass
class SelectedFragment:
    """One participating fragment and the repo that declared it."""

    repo: "Repo"
    fragment: "DockerUseCase"


@dataclass
class Displacement:
    """A provider fragment the competition excluded (spec §4)."""

    capability: str
    loser_repo: str
    loser_priority: int
    winner_repo: str
    winner_priority: int

    def describe(self) -> str:
        """Return the one sentence every surface uses to report this displacement.

        Names who won, at what priority, and who stands down, and calls NEITHER
        priority the higher one: ``--provide cap=repo`` narrows the field to one
        repo before ranking, so the winner can legitimately carry a LOWER
        priority than the fragment it displaced, and the loser can be the
        winner's own repo (two fragments of one repo at different priorities).
        "Lower priority lost" would be false in both cases; this sentence is
        true in all of them. The log line, the CLI reports and the dry-run
        plans each add their own lead-in and never re-word this.
        """
        return (
            f"{self.capability} goes to {self.winner_repo} (priority {self.winner_priority}); "
            f"{self.loser_repo} (priority {self.loser_priority}) stands down"
        )


@dataclass
class Selection:
    """The competition's outcome for one use-case over the active repos."""

    use_case: str
    fragments: "list[SelectedFragment]"
    displaced: "list[Displacement]" = field(default_factory=list)


def declared_use_cases(repos: "list[Repo]") -> "dict[str, list[SelectedFragment]]":
    """Group every declared fragment by use-case name, in repos order."""
    out: dict[str, list[SelectedFragment]] = {}
    for repo in repos:
        for frag in repo.docker_settings.use_cases:
            out.setdefault(frag.name, []).append(SelectedFragment(repo, frag))
    return out


def is_declared_use_case(name: str) -> bool:
    """Whether any of the run's repos declares a docker use-case called *name*.

    Answers with or without a context, through the run's repos
    (:func:`otto.config.fleet.current_repos`); the container host's auto-start
    asks it, so the host layer never reads the composition root.
    """
    from ..config.fleet import current_repos  # function-scope: read when called

    return name in declared_use_cases(current_repos())


def select_fragments(
    use_case: str,
    repos: "list[Repo]",
    *,
    provide: "Mapping[str, str] | None" = None,
) -> Selection:
    """Run the provider competition (spec §4) and return the participants.

    Unconditional fragments always participate. Fragments sharing a
    ``provides`` capability compete: highest priority wins, its composes join,
    every loser is excluded whole. Exact cross-repo ties are refused with the
    ``--provide`` knob named; a same-repo tie is refused with no knob (fix the
    settings). *provide* maps capability -> repo name and must name a candidate.
    """
    declared = declared_use_cases(repos)
    candidates = declared.get(use_case)
    if not candidates:
        known = ", ".join(sorted(declared)) or "<none>"
        raise UseCaseResolutionError(
            f"no active repo declares use-case {use_case!r}; declared: {known}"
        )

    provide_map: dict[str, str] = dict(provide) if provide is not None else {}
    unconditional = [sf for sf in candidates if sf.fragment.provides is None]
    by_capability: dict[str, list[SelectedFragment]] = {}
    for sf in candidates:
        if sf.fragment.provides is not None:
            by_capability.setdefault(sf.fragment.provides, []).append(sf)

    unknown_caps = sorted(set(provide_map) - set(by_capability))
    if unknown_caps:
        names = ", ".join(unknown_caps)
        provided = ", ".join(sorted(by_capability)) or "<none>"
        raise UseCaseResolutionError(
            f"--provide names capabilit{'y' if len(unknown_caps) == 1 else 'ies'} "
            f"{names} that no fragment of use-case {use_case!r} provides; "
            f"provided: {provided}"
        )

    winners: list[SelectedFragment] = []
    displaced: list[Displacement] = []
    for capability, contenders in by_capability.items():
        winner = _pick_winner(use_case, capability, contenders, provide_map.get(capability))
        winners.append(winner)
        displaced.extend(
            Displacement(
                capability=capability,
                loser_repo=sf.repo.name,
                loser_priority=sf.fragment.priority,
                winner_repo=winner.repo.name,
                winner_priority=winner.fragment.priority,
            )
            for sf in contenders
            if sf is not winner
        )

    # Participation order = declaration order over repos (candidates order),
    # so -f merge order later derives deterministically from repo order.
    chosen = set(map(id, unconditional)) | set(map(id, winners))
    fragments = [sf for sf in candidates if id(sf) in chosen]
    return Selection(use_case=use_case, fragments=fragments, displaced=displaced)


def _pick_winner(
    use_case: str,
    capability: str,
    contenders: "list[SelectedFragment]",
    override_repo: "str | None",
) -> SelectedFragment:
    """Pick the winning fragment for `capability`, honoring `override_repo` if given.

    ``--provide`` picks a REPO, not a fragment: it narrows the field to that
    repo's own candidates and then applies the same highest-priority rule as
    the unforced path. It cannot resolve a tie *inside* the repo it names —
    that is still a same-repo config error, just discovered a different way.
    """
    if override_repo is None:
        return _highest_priority(use_case, capability, contenders, override_repo=None)

    picked = [sf for sf in contenders if sf.repo.name == override_repo]
    if not picked:
        raise UseCaseResolutionError(
            f"--provide {capability}={override_repo}: {override_repo!r} is not a "
            f"candidate provider of {capability!r} in use-case {use_case!r}; "
            f"candidates: {sorted(sf.repo.name for sf in contenders)}"
        )
    return _highest_priority(use_case, capability, picked, override_repo=override_repo)


def _highest_priority(
    use_case: str,
    capability: str,
    contenders: "list[SelectedFragment]",
    *,
    override_repo: "str | None",
) -> SelectedFragment:
    """Resolve `contenders` to its single top-priority fragment, or refuse a tie.

    `contenders` is either every candidate for `capability` (no override) or
    one repo's own fragments for it (`override_repo` narrowed the field to
    that repo already). Either way, ties at the top priority are a config
    error resolved only by editing settings.
    """
    top = max(sf.fragment.priority for sf in contenders)
    tied = [sf for sf in contenders if sf.fragment.priority == top]
    if len(tied) == 1:
        return tied[0]

    if override_repo is not None:
        # `contenders` here is already narrowed to override_repo's own
        # fragments, so this tie is always a same-repo tie — --provide named
        # a repo, it did not (and cannot) pick among that repo's own ties.
        raise UseCaseResolutionError(
            f"--provide {capability}={override_repo} does not break a tie: repo "
            f"{override_repo!r} still declares {len(tied)} fragments providing "
            f"{capability!r} at priority {top}; raise one fragment's priority instead."
        )

    tied_repos = sorted(sf.repo.name for sf in tied)
    if len(set(tied_repos)) == 1:
        raise UseCaseResolutionError(
            f"use-case {use_case!r}: repo {tied_repos[0]!r} declares "
            f"{len(tied)} fragments providing {capability!r} at priority {top} — a "
            f"same repo tie has no knob; keep one fragment per capability per repo."
        )
    raise UseCaseResolutionError(
        f"use-case {use_case!r}: capability {capability!r} is tied at priority "
        f"{top} between repos {tied_repos}. Raise one fragment's priority, or pass "
        f"--provide {capability}=<repo> for this invocation."
    )


def place(selection: Selection, parent: "UnixHost") -> "dict[str, list[SelectedFragment]]":
    """Every selected fragment on *parent* (spec §2): a use-case is one host."""
    return {parent.id: list(selection.fragments)}


_FACT_REF = re.compile(r"\$\{otto:([^}]+)\}")
_OTTO_PREFIX = re.compile(r"\$\{otto:")


def resolve_fact_refs(env: "Mapping[str, str]", facts: "Mapping[str, object]") -> "dict[str, str]":
    """Substitute ``${otto:...}`` fact refs (spec §6). Non-otto ``${...}`` is untouched.

    The syntax exists ONLY in settings.toml values — product compose files use
    compose-native interpolation over the product-named variables this
    produces (the executable decoupling test, spec §6). A value that merely
    *looks* like an otto ref but is malformed (empty or unterminated path) is
    refused rather than shipped verbatim into the product's environment.
    """

    def _sub(m: "re.Match[str]") -> str:
        return _lookup_fact(m.group(1), facts)

    out: dict[str, str] = {}
    for key, value in env.items():
        substituted = _FACT_REF.sub(_sub, value)
        if _OTTO_PREFIX.search(substituted):
            raise UseCaseResolutionError(
                f"malformed otto fact ref in {key!r} ({value!r}) — expected "
                f'"${{otto:<path>}}" with a non-empty path and a closing brace.'
            )
        out[key] = substituted
    return out


_PAIR = 2  # "<namespace>.<attr>" — parent.id, parent.addr
_TRIPLE = 3  # "<namespace>.<key>.<attr>" — host.<id>.addr


def _lookup_fact(path: str, facts: "Mapping[str, object]") -> str:
    parts = path.split(".")
    if parts[0] == "role":
        raise UseCaseResolutionError(
            f"${{otto:{path}}}: role facts are gone; use ${{otto:parent.<fact>}} — a use-case "
            f"deploys on one parent"
        )
    try:
        if parts in (["use_case"], ["compose_project"]):
            return str(facts[parts[0]])
        if parts[0] == "parent" and len(parts) == _PAIR and parts[1] in ("id", "addr"):
            parent = cast("Mapping[str, str]", facts["parent"])
            return str(parent[parts[1]])
        if parts[0] == "host" and len(parts) == _TRIPLE and parts[2] == "addr":
            hosts_by_id = cast("Mapping[str, Mapping[str, str]]", facts["hosts"])
            return str(hosts_by_id[parts[1]][parts[2]])
    except KeyError:
        pass
    hosts = sorted(cast("Mapping[str, object]", facts.get("hosts", {})))
    raise UseCaseResolutionError(
        f"unknown fact ref ${{otto:{path}}}. Known forms: use_case, compose_project, "
        f"parent.id|addr, host.<id>.addr (hosts: {hosts})."
    )


@dataclass
class EnvAssembly:
    """Channels 1a+1b of the env mapping (spec §6); adapter/caller merge above."""

    env: "dict[str, str]"
    missing_pass_env: "list[str]"


def assemble_env(
    fragments: "list[SelectedFragment]",
    facts: "Mapping[str, object]",
    *,
    pass_env_source: "Mapping[str, str]",
) -> EnvAssembly:
    """Fragment static env (fact refs resolved), then pass_env allowlists."""
    env: dict[str, str] = {}
    missing: list[str] = []
    seen_missing: "set[str]" = set()
    for sf in fragments:
        env.update(resolve_fact_refs(sf.fragment.env, facts))
    for sf in fragments:
        for name in sf.fragment.pass_env:
            if name in pass_env_source:
                env[name] = pass_env_source[name]
            elif name not in seen_missing:
                seen_missing.add(name)
                missing.append(name)
    return EnvAssembly(env=env, missing_pass_env=missing)


class ParentFact(TypedDict):
    """The parent host's id + address, as carried in facts (spec §7)."""

    id: str
    addr: str


class HostFact(TypedDict):
    """One host's address, as carried in facts (spec §7)."""

    addr: str


class Facts(TypedDict):
    """The plain-data facts mapping handed to adapters and fact refs (spec §7)."""

    use_case: str
    compose_project: str
    parent: ParentFact
    hosts: "dict[str, HostFact]"
    files: "dict[str, str]"
    scratch_dir: str


def build_facts(
    selection: Selection,
    lab: "Lab",
    *,
    compose_project: str,
    parent_id: str,
    files: "dict[str, str]",
    scratch_dir: str,
) -> Facts:
    """Build the plain-data facts dict handed to adapters and fact refs (spec §7).

    A parent or host with no resolvable address is a configuration error
    refused here (pure, before anything is staged) rather than silently
    guessed at — spec §12.
    """
    from ..host.unix_host import UnixHost  # function-scope: import-budget

    def _addr(hid: str, *, context: str) -> str:
        h = lab.hosts.get(hid)
        if h is None:
            raise UseCaseResolutionError(
                f"use-case {selection.use_case!r}: {context} names host {hid!r}, "
                f"which is not in the active lab — an address cannot be "
                f"fabricated for a host that does not exist."
            )
        ip = getattr(h, "ip", None)
        if not ip:
            raise UseCaseResolutionError(
                f"use-case {selection.use_case!r}: {context} host {hid!r} has no "
                f"configured address — an address cannot be fabricated for it."
            )
        return str(ip)

    # Union of the participating repos' scoped universes: a fact dict serves
    # every participating repo at once, so no single repo's scope can narrow
    # it alone (spec §7: "the owning repo's scoped universe").
    #
    # Deliberately NOT filtered by `docker_capable`, unlike the parent rule.
    # The parent rule asks "where can this stack RUN"; these facts
    # answer "what may a deployed service be told the address of", and the
    # answer includes hosts that will never run a container — the bench DUT a
    # container is meant to talk to is the motivating case, and spec §7 defines
    # `hosts` as the scoped universe with no capability qualifier. The scope
    # clause stays: a host outside every participating repo's universe is not
    # this deployment's business.
    repo_names = {sf.repo.name for sf in selection.fragments}
    scopes = [scope_for_repo(name) for name in repo_names]
    hosts: "dict[str, HostFact]" = {
        hid: {"addr": _addr(hid, context="host")}
        for hid, h in lab.hosts.items()
        if isinstance(h, UnixHost)
        and any(repo_targets(scope, h.source_lab, hid) for scope in scopes)
    }
    return {
        "use_case": selection.use_case,
        "compose_project": compose_project,
        "parent": {"id": parent_id, "addr": _addr(parent_id, context="parent_id")},
        "hosts": hosts,
        "files": dict(files),
        "scratch_dir": scratch_dir,
    }
