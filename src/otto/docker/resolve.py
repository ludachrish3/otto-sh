"""Use-case resolution (spec §4-§6): selection, placement, env — pure functions.

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
    """A provider fragment excluded by a higher-priority winner (spec §4)."""

    capability: str
    loser_repo: str
    loser_priority: int
    winner_repo: str
    winner_priority: int


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

    provide = dict(provide or {})
    unconditional = [sf for sf in candidates if sf.fragment.provides is None]
    by_capability: dict[str, list[SelectedFragment]] = {}
    for sf in candidates:
        if sf.fragment.provides is not None:
            by_capability.setdefault(sf.fragment.provides, []).append(sf)

    unknown_caps = sorted(set(provide) - set(by_capability))
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
        winner = _pick_winner(use_case, capability, contenders, provide.get(capability))
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


_PLACEMENT_KNOBS = (
    "Disambiguate with --on <host> for this invocation, or a committed "
    'placement pin on the fragment (placement = { <role> = "<host>" }).'
)


def resolve_placement(
    selection: Selection, lab: "Lab", *, on: "str | None" = None
) -> "dict[str, list[SelectedFragment]]":
    """Resolve each fragment's host (spec §5); group fragments by host id.

    *on* must be a canonical host id and collapses every fragment to it.
    """
    from ..host.unix_host import UnixHost  # function-scope: import-budget

    placed: dict[str, list[SelectedFragment]] = {}
    for sf in selection.fragments:
        host_id = on if on is not None else _place_fragment(sf, lab, UnixHost)
        placed.setdefault(host_id, []).append(sf)
    return placed


def _place_fragment(sf: SelectedFragment, lab: "Lab", unix_cls: "type[UnixHost]") -> str:
    """Resolve one fragment's host per spec §5 knobs 2-4.

    Knob 1, ``--on``, is handled by the caller before this is ever reached.
    Knob 2 (the committed pin) is consulted first, but only when it is even
    *addressable* — a ``placement`` table whose only usable key is the
    fragment's own ``role`` (anything else can never fire for this fragment,
    in any lab, since ``role`` is fixed per fragment instance; that shape is
    refused as config debris, not resolved). When the pin is addressable but
    :func:`_validate_pin` reports it does not target *this* lab session
    (``None``), that is legitimate multi-lab config, not an error: fall
    through to knobs 3-4 exactly as if no pin had been declared.
    """
    frag, repo_name, uc = sf.fragment, sf.repo.name, sf.fragment.name
    where = f"use-case {uc!r} fragment of repo {repo_name!r}"

    if frag.placement and (frag.role is None or frag.role not in frag.placement):
        raise UseCaseResolutionError(
            f"{where}: declares a placement pin for role(s) {sorted(frag.placement)} "
            f"but its own role is {frag.role!r} — the pin can never apply to this "
            f"fragment in any lab. Fix the key to match the role, add the missing "
            f"role, or drop the unused pin."
        )
    if frag.role is not None and frag.role in frag.placement:
        pinned = _validate_pin(frag.placement[frag.role], where, lab, unix_cls)
        if pinned is not None:
            return pinned
        # Pin is well-formed but addressed to a lab this session isn't running —
        # not this call's problem, fall through to role/fallback resolution.

    scope = scope_for_repo(repo_name)
    in_scope = [
        (hid, h)
        for hid, h in lab.hosts.items()
        if isinstance(h, unix_cls) and h.docker_capable and repo_targets(scope, h.source_lab, hid)
    ]
    if frag.role is not None:
        matches = [hid for hid, h in in_scope if frag.role in h.roles]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise UseCaseResolutionError(
                f"{where}: no docker-capable host in the repo's scope carries role "
                f'{frag.role!r}. Tag a host in lab.json ("roles": ["{frag.role}"]). '
                f"{_PLACEMENT_KNOBS}"
            )
        raise UseCaseResolutionError(
            f"{where}: role {frag.role!r} is ambiguous — carried by {sorted(matches)}. "
            f"{_PLACEMENT_KNOBS}"
        )

    if len(in_scope) == 1:
        return in_scope[0][0]
    ids = sorted(hid for hid, _ in in_scope)
    raise UseCaseResolutionError(
        f"{where} declares no role and the repo's scope holds "
        f"{len(in_scope)} docker-capable host(s) {ids} — give the fragment a role. "
        f"{_PLACEMENT_KNOBS}"
    )


def _validate_pin(pin: str, where: str, lab: "Lab", unix_cls: "type[UnixHost]") -> "str | None":
    """Validate a committed ``placement`` pin, or report it as not-applicable-here.

    Deliberately does **not** consult scope: a committed pin is the repo
    author explicitly reaching for a host, the same trust ``--on`` gets
    (spec §5).

    Returns the resolved host id, or ``None`` when the pin is well-formed but
    plainly addressed to a lab session other than *lab* (a lab-qualified pin
    naming a lab that is neither the active lab nor a host present here) —
    that is legitimate multi-lab config (the schema explicitly supports
    lab-qualified values "for multi-lab sessions") and the caller falls
    through to the next knob rather than treating it as a refusal.

    Raises when the pin's *shape* could never resolve in any lab (an empty
    lab or host component), or when it names a host that exists right here
    under a conflicting lab tag or lacks docker capability — those are real
    conflicts with what this session can see, not an absent lab.
    """
    lab_part, sep, host_part = pin.partition(":")
    want_lab, host_id = (lab_part, host_part) if sep else (None, pin)
    if sep and (not lab_part or not host_part):
        raise UseCaseResolutionError(
            f"{where}: placement pin {pin!r} is malformed — a lab-qualified pin "
            f'needs both parts non-empty ("<lab>:<host>"); got lab={lab_part!r} '
            f"host={host_part!r}"
        )
    host = lab.hosts.get(host_id)
    if host is None:
        if want_lab is not None and want_lab != lab.name:
            return None
        raise UseCaseResolutionError(
            f"{where}: placement pin {pin!r} names no host in the active lab "
            f"{lab.name!r}; available: {sorted(lab.hosts)}"
        )
    if want_lab is not None and host.source_lab != want_lab:
        raise UseCaseResolutionError(
            f"{where}: placement pin {pin!r} is lab-qualified but {host_id!r} "
            f"belongs to lab {host.source_lab!r}, not {want_lab!r}"
        )
    if not isinstance(host, unix_cls) or not host.docker_capable:
        raise UseCaseResolutionError(
            f"{where}: placement pin {pin!r} must name a docker-capable unix host"
        )
    return host_id


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
_TRIPLE = 3  # "<namespace>.<key>.<attr>" — role.<r>.addr, host.<id>.addr


def _lookup_fact(path: str, facts: "Mapping[str, object]") -> str:
    parts = path.split(".")
    try:
        if parts in (["use_case"], ["compose_project"]):
            return str(facts[parts[0]])
        if parts[0] == "parent" and len(parts) == _PAIR and parts[1] in ("id", "addr"):
            parent = cast("Mapping[str, str]", facts["parent"])
            return str(parent[parts[1]])
        if parts[0] == "role" and len(parts) == _TRIPLE and parts[2] in ("host_id", "addr"):
            roles_by_id = cast("Mapping[str, Mapping[str, str]]", facts["roles"])
            return str(roles_by_id[parts[1]][parts[2]])
        if parts[0] == "host" and len(parts) == _TRIPLE and parts[2] == "addr":
            hosts_by_id = cast("Mapping[str, Mapping[str, str]]", facts["hosts"])
            return str(hosts_by_id[parts[1]][parts[2]])
    except KeyError:
        pass
    roles = sorted(cast("Mapping[str, object]", facts.get("roles", {})))
    hosts = sorted(cast("Mapping[str, object]", facts.get("hosts", {})))
    raise UseCaseResolutionError(
        f"unknown fact ref ${{otto:{path}}}. Known forms: use_case, compose_project, "
        f"parent.id|addr, role.<role>.host_id|addr (roles: {roles}), "
        f"host.<id>.addr (hosts: {hosts})."
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


class RoleFact(TypedDict):
    """One role's resolved host id + address, as carried in facts (spec §7)."""

    host_id: str
    addr: str


class HostFact(TypedDict):
    """One host's address, as carried in facts (spec §7)."""

    addr: str


class Facts(TypedDict):
    """The plain-data facts mapping handed to adapters and fact refs (spec §7)."""

    use_case: str
    compose_project: str
    parent: ParentFact
    roles: "dict[str, RoleFact]"
    hosts: "dict[str, HostFact]"
    files: "dict[str, str]"
    scratch_dir: str


def build_facts(
    selection: Selection,
    placed: "dict[str, list[SelectedFragment]]",
    lab: "Lab",
    *,
    compose_project: str,
    parent_id: str,
    files: "dict[str, str]",
    scratch_dir: str,
) -> Facts:
    """Build the plain-data facts dict handed to adapters and fact refs (spec §7).

    A role that resolves to more than one host across fragments, or a host
    with no resolvable address, is a configuration error refused here (pure,
    before anything is staged) rather than silently guessed at — spec §2.4/§12.
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

    roles: "dict[str, RoleFact]" = {}
    for host_id, frags in placed.items():
        for sf in frags:
            role = sf.fragment.role
            if role is None:
                continue
            entry: RoleFact = {
                "host_id": host_id,
                "addr": _addr(host_id, context=f"role {role!r}"),
            }
            prev = roles.setdefault(role, entry)
            if prev != entry:
                raise UseCaseResolutionError(
                    f"use-case {selection.use_case!r}: role {role!r} resolves to "
                    f"multiple hosts across fragments ({prev['host_id']!r} and "
                    f"{host_id!r}) — facts cannot carry one address for it. Give "
                    f"the fragments distinct roles, or pin them to one host "
                    f'(placement = {{ {role} = "<host>" }}), or use --on.'
                )

    # Union of the participating repos' scoped universes — same clause
    # `_place_fragment` applies per repo, but a fact dict serves every
    # participating repo at once, so no single repo's scope can narrow it
    # alone (spec §7: "the owning repo's scoped universe").
    #
    # Deliberately NOT filtered by `docker_capable`, unlike placement's own
    # in-scope list. Placement asks "where can this stack RUN"; these facts
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
        "roles": roles,
        "hosts": hosts,
        "files": dict(files),
        "scratch_dir": scratch_dir,
    }
