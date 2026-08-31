"""Use-case deployment (spec §8): one merged compose stack per resolved host.

This is where Tasks 6-10's pure engine meets the parent host. Everything
above the first device touch — selection (§4), placement (§5), facts (§7),
the adapter call (§7), the env mapping (§6) and the rendered compose texts —
is settled from configuration, which is what lets ``--dry-run`` decline with
a resolved plan rather than a shrug.

The three verbs mirror the per-repo primitives one layer up:
:func:`deploy` / :func:`teardown` / :func:`deployed` are to a use-case what
``compose_up`` / ``compose_down`` / ``composed`` are to one repo's stack, and
they keep the same sharing contract (tear down only what this call brought
up, unless ``own=True``).
"""

import logging
import os
import shlex
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ..config import get_lab, get_ordered_repos, get_repos
from ..host.docker_host import DockerContainerHost
from ..host.errors import HostCommandError
from ..host.host import is_dry_run
from ..host.unix_host import UnixHost
from ..result import CommandNotRunError
from .adapter import AdapterResult, adapter_for
from .build import build_images
from .compose import (
    _stack_already_up,
    compose_down_project,
    register_stack_hosts,
    unregister_container_hosts,
    use_case_project,
)
from .resolve import (
    Selection,
    UseCaseResolutionError,
    assemble_env,
    build_facts,
    resolve_placement,
    select_fragments,
)
from .staging import (
    ComposeFileToStage,
    stage_use_case,
    use_case_compose_paths,
    use_case_env_file,
)

if TYPE_CHECKING:
    from ..config.lab import Lab
    from ..config.repo import DockerCompose, Repo
    from .resolve import SelectedFragment

logger = logging.getLogger(__name__)


@dataclass
class UseCaseStack:
    """What one :func:`deploy` produced, across every host it touched."""

    use_case: str
    """The deployed use-case's name."""

    selection: Selection
    """The provider competition's outcome — winners and displacements (§4).

    Required, and placed above the defaulted fields on purpose: a consumer
    rendering the selection report (the CLI, a suite) would otherwise have to
    narrow ``Selection | None`` on a value ``deploy`` always sets.
    """

    projects: "dict[str, str]" = field(default_factory=dict)
    """Parent host id -> the compose project its merged stack runs under.

    A dict rather than one string because the project is derived PER HOST
    from that host's own lab (spec §9): a use-case spanning two labs' hosts
    must not put both stacks in one project, since ``--remove-orphans``
    reaps within a project.
    """

    hosts: "dict[str, DockerContainerHost]" = field(default_factory=dict)
    """Service name -> container host, flattened across every parent."""

    by_host: "dict[str, dict[str, DockerContainerHost]]" = field(default_factory=dict)
    """Parent host id -> that parent's own ``{service: container host}``."""

    env: "dict[str, str]" = field(default_factory=dict)
    """The final env mapping, as one host saw it.

    Assembled PER HOST (``${otto:parent.*}`` differs between them), so on a
    multi-host deployment this carries the last host's mapping and no other.
    There is deliberately no per-host env view to consult instead --
    ``by_host`` maps parents to their CONTAINER HOSTS, not to env mappings.
    Nothing in otto renders this field; a caller that needs every host's
    mapping should deploy per host, or ask for the field to be added.
    """


# ---------------------------------------------------------------------------
# Resolution helpers (pure)
# ---------------------------------------------------------------------------


def _canonical_on(lab: "Lab", on: "str | None") -> "str | None":
    """Canonicalize an ``on=`` handle to a host id, refusing one the lab lacks.

    ``on`` is the one placement knob that bypasses roles, pins and scope
    entirely (spec §5 knob 1), so nothing downstream ever checks it: an
    unknown value would be handed to ``resolve_placement`` as the group key
    and only surface as a ``KeyError``-ish failure a host lookup later, with
    the user's typo nowhere in the message. Resolved through
    ``Lab.resolve_handle`` so ``on`` accepts the same typed handles every
    other host-taking verb does, not only canonical ids.
    """
    if on is None:
        return None
    host = lab.resolve_handle(on)
    if host is None:
        raise UseCaseResolutionError(
            f"on={on!r} matches no host in lab {lab.name!r} — a use-case cannot be "
            f"deployed onto a host this session does not have. Available hosts: "
            f"{sorted(lab.hosts)}"
        )
    return host.id


def _parent_for(lab: "Lab", host_id: str) -> UnixHost:
    """Return the docker-capable parent behind a resolved placement, or refuse.

    Only called for a host :func:`_acting_hosts` decided this call actually
    touches — a host whose services were all narrowed away by ``services=``
    is never handed here, so it never has to be a docker-capable unix host
    for THIS call to succeed. Deliberate: a use-case spanning several hosts
    must be narrowable to a subset without also requiring the excluded hosts
    to be valid docker parents.
    """
    host = lab.hosts.get(host_id)
    if not isinstance(host, UnixHost) or not host.docker_capable:
        kind = "absent from the lab" if host is None else f"a {type(host).__name__}"
        raise UseCaseResolutionError(
            f"placement resolved to host {host_id!r}, which is {kind} and not a "
            f"docker-capable unix host, so a use-case stack cannot be deployed onto "
            f'it. Mark it in lab.json ("docker_capable": true), or place the '
            f"fragment elsewhere."
        )
    return host


def _compose_entry(repo: "Repo", handle: str) -> "DockerCompose":
    """Resolve one of a fragment's ``composes`` handles to its repo's entry.

    Settings validation already refuses a fragment naming a handle its repo
    does not declare, so this is the backstop for a ``Repo`` assembled in
    code (a suite's fixture, an embedder) rather than parsed from
    ``settings.toml`` — where the same mistake would otherwise surface as a
    ``StopIteration`` deep inside the render.
    """
    for compose in repo.docker_settings.composes:
        if compose.name == handle:
            return compose
    known = sorted(c.name for c in repo.docker_settings.composes) or ["<none>"]
    raise UseCaseResolutionError(
        f"repo {repo.name!r}: a use-case fragment names compose handle {handle!r}, "
        f"which the repo does not define; declared handles: {known}"
    )


@dataclass
class _RepoUnit:
    """One participating repo's contribution on one host, in dependency order."""

    repo: "Repo"
    handles: "list[str]"


def _units(frags: "list[SelectedFragment]") -> "list[_RepoUnit]":
    """Group a host's fragments by repo, preserving the caller's order.

    Keyed by repo NAME, never by hashing the fragment dataclasses: they are
    not hashable by contract, and identity keying would split one repo's two
    fragments into two adapter invocations.
    """
    units: "dict[str, _RepoUnit]" = {}
    for sf in frags:
        unit = units.setdefault(sf.repo.name, _RepoUnit(repo=sf.repo, handles=[]))
        for handle in sf.fragment.composes:
            if handle not in unit.handles:
                unit.handles.append(handle)
    return list(units.values())


def _declared_services(frags: "list[SelectedFragment]", *, report: bool = False) -> "list[str]":
    """Every service the fragments' compose files declare, deduped, in order.

    A service declared by two different fragments is warned about rather than
    refused: the merge really does happen (``-f`` order decides the winner),
    and §4's provider competition — not the YAML merge — is the mechanism for
    REPLACING a service. A silent merge is the part that would be wrong.

    *report* says whether THIS pass is the one that talks. Three callers walk
    the same fragments for three different questions (validate a narrowing
    over the whole selection, decide one host's service set, probe in
    ``deployed``), so without it a collision warned once, twice or three times
    depending on whether ``services=`` was passed — log volume varying with an
    unrelated flag reads like a bug in the thing being logged about. Only
    ``deploy``'s per-host pass reports.
    """
    services: "list[str]" = []
    owner: "dict[str, str]" = {}
    for sf in frags:
        for handle in sf.fragment.composes:
            entry = _compose_entry(sf.repo, handle)
            for service in entry.services:
                where = f"{sf.repo.name}[{handle}]"
                if service in owner:
                    if report and owner[service] != where:
                        logger.warning(
                            rf"\[docker] service {service!r} is declared by both "
                            f"{owner[service]} and {where}; the later -f wins the "
                            f"merge — use provides/priority (spec §4) to replace it"
                        )
                    continue
                owner[service] = where
                services.append(service)
    return services


def _validated_services(
    selection: Selection, services: "Sequence[str] | None"
) -> "set[str] | None":
    """Check a ``services`` narrowing against the whole selection's declarations."""
    if services is None:
        return None
    declared = set(_declared_services(selection.fragments, report=False))
    unknown = sorted(set(services) - declared)
    if unknown:
        raise UseCaseResolutionError(
            f"use-case {selection.use_case!r}: no participating fragment declares "
            f"service(s) {unknown}; declared services: {sorted(declared)}"
        )
    return set(services)


# ---------------------------------------------------------------------------
# Env + files for one host (pure, but the adapter may write its scratch dir)
# ---------------------------------------------------------------------------


def _env_file_pairs(path: Path) -> "dict[str, str]":
    """Parse a caller ``--env-file``: ``K=V`` lines, ``#`` and blanks skipped.

    DELIBERATELY NARROWER THAN DOCKER'S OWN ``--env-file`` READER, and the
    difference is worth knowing before you hit it. Docker additionally accepts
    ``export K=V``, strips a matched pair of surrounding quotes from the value,
    and (in recent versions) trims whitespace around it. This parser does
    none of that: the key is stripped, the value is taken verbatim from the
    first ``=`` to end of line, and a line with no ``=`` is REFUSED rather
    than skipped.

    Verbatim is the conservative choice here because these values do not stay
    in a file otto merely read — they are merged into the mapping otto then
    WRITES as ``otto.env`` and splices into an ``env K=V`` shell prefix. A
    parser that quietly unquoted ``K="a b"`` to ``a b`` would change what the
    product's container receives, in a direction the caller never asked for
    and cannot see. If you want a quoted value, quote it where the shell can
    see it, or pass ``--env``.

    Refusing the ``=``-less line rather than skipping it is the same
    reasoning: in docker's reader such a line means "inherit ``K`` from the
    ambient environment", a behavior otto's ``pass_env`` allowlist owns
    explicitly (spec §6 channel 1b). Silently doing neither is the worst of
    the three.
    """
    out: "dict[str, str]" = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise UseCaseResolutionError(
                f"env file {path}: line {raw!r} is not a K=V assignment — an env "
                f"file otto merges must be plain assignments, one per line."
            )
        out[key.strip()] = value
    return out


@dataclass
class _HostPlan:
    """Everything one host needs, settled before the first device touch."""

    env: "dict[str, str]"
    files: "list[ComposeFileToStage]"
    extra_files: "dict[str, str]"


def _plan_host(
    *,
    selection: Selection,
    placed: "dict[str, list[SelectedFragment]]",
    lab: "Lab",
    host_id: str,
    frags: "list[SelectedFragment]",
    compose_project: str,
    use_case: str,
    caller_env: "Mapping[str, str] | None",
    env_files: "Sequence[Path] | None",
) -> _HostPlan:
    """Build one host's env mapping and rendered compose texts (spec §6, §7).

    The adapters run HERE, exactly once per repo, and their result feeds both
    halves: ``env`` merges over the fragment static tables, ``files``
    overrides the committed compose texts, ``extra_files`` rides along to
    staging. Splitting the env and the render into two passes would invoke
    each adapter twice — and an adapter is allowed to generate content into
    ``scratch_dir``, so a second call is not free and need not agree with
    the first.

    Merge order, later wins (§6): fragment static (fact refs resolved) +
    ``pass_env`` -> adapter env -> caller ``env_files`` -> caller ``env``.
    """
    units = _units(frags)
    files_fact = {
        handle: str(_compose_entry(unit.repo, handle).path)
        for unit in units
        for handle in unit.handles
    }
    # Not cleaned up on purpose: an adapter may generate a file here and
    # reference it from the text it returns, so the directory has to outlive
    # this call. It lives under the OS temp dir and is per-invocation.
    scratch_dir = tempfile.mkdtemp(prefix="otto-adapter-")
    facts = build_facts(
        selection,
        placed,
        lab,
        compose_project=compose_project,
        parent_id=host_id,
        files=files_fact,
        scratch_dir=scratch_dir,
    )

    assembly = assemble_env(frags, facts, pass_env_source=os.environ)
    if assembly.missing_pass_env:
        logger.warning(
            rf"\[docker] use-case {use_case!r} on {host_id}: pass_env names "
            f"{assembly.missing_pass_env} are not set in this shell; they are "
            f"absent from the deployment's environment"
        )
    env: "dict[str, str]" = dict(assembly.env)

    results: "dict[str, AdapterResult]" = {}
    for unit in units:
        adapter = adapter_for(unit.repo.name, use_case)
        if adapter is None:
            continue
        # A per-repo view: an adapter may only see the files it is
        # responsible for rendering, never a peer repo's (spec §7). Handing
        # over the union would invite a repo to render another's compose file
        # and make the merge order decide who won.
        repo_facts = dict(facts)
        repo_facts["files"] = {h: files_fact[h] for h in unit.handles}
        result = adapter(repo_facts)
        results[unit.repo.name] = result
        env.update(result.env)

    for path in env_files or ():
        env.update(_env_file_pairs(Path(path)))
    env.update(caller_env or {})

    rendered: "list[ComposeFileToStage]" = []
    extra: "dict[str, str]" = {}
    for unit in units:
        result = results.get(unit.repo.name)
        for name, text in (result.extra_files if result else {}).items():
            if name in extra and extra[name] != text:
                logger.warning(
                    rf"\[docker] two adapters generated different content for "
                    f"extra file {name!r}; {unit.repo.name}'s wins. They share one "
                    f"staging dir, so give the sidecars distinct relative names."
                )
            extra[name] = text
        for handle in unit.handles:
            entry = _compose_entry(unit.repo, handle)
            override = result.files.get(handle) if result else None
            text = override if override is not None else entry.path.read_text()
            rendered.append(
                ComposeFileToStage(handle=handle, text=text, source_dir=entry.path.parent)
            )
    return _HostPlan(env=env, files=rendered, extra_files=extra)


def _env_text(env: "Mapping[str, str]") -> str:
    """Render the mapping as an ``--env-file`` body: one ``K=V`` line per key.

    Sorted, so a redeploy that changed nothing stages a byte-identical file.
    A newline anywhere is refused rather than escaped: compose's env-file
    parser is line-based, so a value carrying one would silently become a
    second assignment (or corrupt the next).
    """
    lines: "list[str]" = []
    for key in sorted(env):
        value = env[key]
        if "\n" in key or "\n" in value or "=" in key or key.startswith("-"):
            raise UseCaseResolutionError(
                f"env entry {key!r} cannot be written to an env file: a key with "
                f"'=', a leading '-', or a newline in either half would be read "
                f"back as something other than the assignment you set. A leading "
                f"'-' is the sharp one — the same mapping is spliced into an "
                f"`env K=V ...` prefix, where `env -i` WIPES the environment "
                f"instead of adding to it."
            )
        lines.append(f"{key}={value}")
    return "".join(f"{line}\n" for line in lines)


def _up_command(
    compose_project: str,
    compose_paths: "Sequence[Path]",
    env_file: Path,
    env: "Mapping[str, str]",
    services: "Sequence[str]",
    *,
    narrowed: bool,
) -> str:
    """Build the one merged ``up`` command (spec §8 step 4).

    The mapping feeds BOTH sinks: ``--env-file`` (what compose interpolates
    the YAML with) and an ``env K=V`` prefix (what the compose PROCESS sees,
    which is what a ``${VAR}`` in a shell-ish field resolves against). One
    mapping, two sinks — never two mappings that could disagree.
    """
    parts: "list[str]" = []
    if env:
        parts.append("env")
        parts += [f"{k}={shlex.quote(env[k])}" for k in sorted(env)]
    parts += ["docker", "compose", "-p", shlex.quote(compose_project)]
    for path in compose_paths:
        parts += ["-f", shlex.quote(str(path))]
    parts += ["--env-file", shlex.quote(str(env_file)), "up", "-d", "--remove-orphans"]
    if narrowed:
        parts += [shlex.quote(s) for s in services]
    return " ".join(parts)


def _unit_services(unit: _RepoUnit) -> "set[str]":
    """Return the services one repo contributes to a host, from its winning handles."""
    return {
        service for handle in unit.handles for service in _compose_entry(unit.repo, handle).services
    }


@dataclass
class _ActingHost:
    """One host this call will actually touch, and what it will run there."""

    host_id: str
    fragments: "list[SelectedFragment]"
    services: "list[str]"


def _acting_hosts(
    placed: "dict[str, list[SelectedFragment]]",
    order: "Mapping[str, int]",
    services_filter: "set[str] | None",
    *,
    use_case: str,
    report: bool = False,
) -> "list[_ActingHost]":
    """Resolve which hosts this call touches, and each one's service set.

    ONE answer, shared by ``deploy`` and ``deployed``, because they must agree
    on it: ``deployed`` decides whether to tear down from an ownership probe,
    and probing a host ``deploy`` then SKIPS (its services were all narrowed
    away) lets an unrelated stack on that host make ``was_up`` true and
    suppress a teardown that was owed. Same reason ``teardown`` shares
    ``_resolve``: the two halves of a deployment must not disagree about what
    the deployment is.
    """
    acting: "list[_ActingHost]" = []
    for host_id, host_frags in placed.items():
        frags = _ordered(host_frags, order)
        declared = _declared_services(frags, report=report)
        wanted = [s for s in declared if services_filter is None or s in services_filter]
        if not wanted:
            if report:
                logger.info(
                    rf"\[docker] {host_id} declares none of the requested services; "
                    f"skipping it for use-case {use_case!r}"
                )
            continue
        acting.append(_ActingHost(host_id=host_id, fragments=frags, services=wanted))
    return acting


async def _build_for(units: "list[_RepoUnit]", parent: UnixHost) -> None:
    """Build every participating repo's images, once each, in the given order."""
    for unit in units:
        if not unit.repo.docker_settings.images:
            continue
        results = await build_images(unit.repo, parent, rebuild=False)
        for name, res in results.items():
            if not res.is_ok:
                # value, not msg: the captured build output is the diagnosis.
                raise HostCommandError(
                    f"build for image {name!r} of repo {unit.repo.name!r} failed "
                    f"before the use-case stack came up: {res.value}"
                )


# ---------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------


def _resolve(
    use_case: str,
    *,
    on: "str | None",
    provide: "Mapping[str, str] | None",
) -> "tuple[Lab, Selection, dict[str, list[SelectedFragment]], dict[str, int]]":
    """Run the pure prefix `deploy`, `teardown` and `deployed` all share.

    Shared so ``--on`` and ``--provide`` cannot mean one thing on the way up
    and another on the way down: a teardown that resolved differently would
    tear down a project nobody deployed and leave the real one running.
    """
    lab = get_lab()
    on_id = _canonical_on(lab, on)
    selection = select_fragments(use_case, get_repos(), provide=provide)
    placed = resolve_placement(selection, lab, on=on_id)
    order = {repo.name: i for i, repo in enumerate(get_ordered_repos())}
    return lab, selection, placed, order


def _ordered(
    frags: "list[SelectedFragment]", order: "Mapping[str, int]"
) -> "list[SelectedFragment]":
    """Sort a host's fragments into repo dependency order.

    This IS the ``-f`` order, and the ``-f`` order is what decides which
    fragment's key survives a merge: dependents come later so they override
    their dependencies, never the other way round. A repo missing from the
    order map (skipped by bootstrap) sorts last rather than first — it
    depends on nothing that is present.
    """
    return sorted(frags, key=lambda sf: order.get(sf.repo.name, len(order)))


async def deploy(
    use_case: str,
    *,
    services: "Sequence[str] | None" = None,
    env: "Mapping[str, str] | None" = None,
    env_files: "Sequence[Path] | None" = None,
    on: "str | None" = None,
    provide: "Mapping[str, str] | None" = None,
    build: bool = True,
    project_name: "str | None" = None,
) -> UseCaseStack:
    """Deploy *use_case*: one merged compose stack per resolved host (spec §8).

    Args:
        services: Narrow the deployment to these services. Every name must be
            declared by some participating fragment; each host runs only its
            own intersection, and a host left with none is skipped.
        env: Caller overrides, the last layer of the merge (§6).
        env_files: ``K=V`` files merged under *env* and over the adapters.
        on: Collapse every fragment onto this host (§5 knob 1). Accepts any
            typed handle ``Lab.resolve_handle`` understands.
        provide: ``capability -> repo`` overrides for the provider
            competition (§4).
        build: Build each participating repo's declared images first.
        project_name: Use this compose project on every host instead of
            deriving ``<lab>-<usecase>-<suffix>`` per host.

    Returns:
        The :class:`UseCaseStack` describing every registered container.

    Raises:
        ~otto.docker.resolve.UseCaseResolutionError: a configuration refusal
            — unknown use-case, provider tie, unresolvable role, an ``on`` or
            a service name nothing declares. Nothing was touched.
        ~otto.host.errors.HostCommandError: a build, an ``up`` or the
            container-id resolution failed. Whatever THIS call brought up has
            been torn down again first.
        ~otto.result.CommandNotRunError: this is a dry run. Same reasoning as
            ``compose_up``'s arm, one layer up: the return value is a stack of
            LIVE container hosts and an empty one is a real, different
            outcome. Armed below the pure phases AND below the adapter call
            and the file render -- spec §7 makes both plain-data (an adapter's
            only sanctioned effect is writing its own ``scratch_dir``), and
            running them is what lets the decline carry spec §12's promised
            EXACT compose command rather than a description of one. Still
            above the first build/stage/up, which are the first device
            touches.
    """
    lab, selection, placed, order = _resolve(use_case, on=on, provide=provide)
    services_filter = _validated_services(selection, services)

    # Read once: every host's branch below must agree about which run this is.
    dry = is_dry_run()
    # Above the dry branch on purpose, which is a CHANGE: this used to sit
    # below the decline and so never spoke during a preview. Displacements are
    # settled from configuration, they are the single most surprising thing a
    # deployment does, and a dry run is exactly when someone is checking which
    # provider won -- reporting them only on the live run would have made the
    # preview quieter than the thing it previews.
    _log_displacements(use_case, selection)
    stack = UseCaseStack(use_case=use_case, selection=selection)
    brought_up: "list[tuple[UnixHost, str]]" = []
    previews: "list[tuple[str, str]]" = []  # (host id, the command it would run)
    try:
        for acting in _acting_hosts(placed, order, services_filter, use_case=use_case, report=True):
            host_id, frags, wanted = acting.host_id, acting.fragments, acting.services
            parent = _parent_for(lab, host_id)
            proj = project_name or use_case_project(parent.source_lab, use_case)
            units = _units(frags)
            # Planned BEFORE the build, so a dry run reaches the render
            # without a build in front of it -- and so a live run discovers a
            # bad adapter or an unreadable compose file in seconds rather than
            # after minutes of image building.
            plan = _plan_host(
                selection=selection,
                placed=placed,
                lab=lab,
                host_id=host_id,
                frags=frags,
                compose_project=proj,
                use_case=use_case,
                caller_env=env,
                env_files=env_files,
            )
            if dry:
                # THE DECLINE'S POSITION: below the adapter + render (plain
                # data, spec §7), above build/stage/up (the first device
                # touches). The paths come from staging's own pure layout
                # functions, so the previewed `-f` set is the one staging
                # would produce, not a second guess at it.
                previews.append(
                    (
                        parent.id,
                        _up_command(
                            proj,
                            use_case_compose_paths(proj, plan.files),
                            use_case_env_file(proj),
                            plan.env,
                            wanted,
                            narrowed=services_filter is not None,
                        ),
                    )
                )
                continue
            if build:
                # Only the repos that still contribute a service being started.
                # A build is minutes, and `services=["api"]` is a request to
                # deploy `api` -- not a request to build the four images of the
                # three repos whose services were just excluded. Their compose
                # files still join the `-f` merge (compose needs the whole
                # document set to resolve the merge), they simply have nothing
                # running, so nothing of theirs needs an image.
                await _build_for([u for u in units if _unit_services(u) & set(wanted)], parent)
            staged = await stage_use_case(
                parent, proj, plan.files, _env_text(plan.env), extra_files=plan.extra_files
            )
            # `is True`, so an UNKNOWN probe answer counts as "we brought it
            # up" and the rollback below will clean up after us. The opposite
            # default would strand a stack nobody claims.
            was_up = await _stack_already_up(parent, proj) is True
            if not was_up:
                brought_up.append((parent, proj))
            logger.info(rf"\[docker] deploying {use_case} as {proj} on {parent.id}")
            result = await parent.exec(
                _up_command(
                    proj,
                    staged.compose_paths,
                    staged.env_file,
                    plan.env,
                    wanted,
                    narrowed=services_filter is not None,
                ),
                # Unbounded on purpose: this can pull images. See _compose_cmd.
                timeout=float("inf"),
            )
            if not result.is_ok:
                _refuse_failed_up(proj, parent.id, result.value)
            hosts = await register_stack_hosts(
                lab, parent, compose_project=proj, id_project=use_case, services=wanted
            )
            stack.projects[host_id] = proj
            stack.by_host[host_id] = hosts
            stack.hosts.update(hosts)
            stack.env = plan.env
    except BaseException:
        # Only what THIS call brought up, on every host it reached — the
        # multi-host generalization of compose_up's rollback. A stack that
        # was already running belongs to someone else and is left alone.
        await _rollback(lab, brought_up)
        raise
    if dry:
        raise CommandNotRunError(
            f"deploy({use_case})",
            ", ".join(sorted(placed)),
            f"{_plan(placed, selection)} {_command_preview(previews)}",
        )
    return stack


def _command_preview(previews: "list[tuple[str, str]]") -> str:
    """Render spec §12's exact per-host compose command(s) for a dry run.

    The adapters DID run to produce these (they are plain data, spec §7), and
    the sentence says so: a reader who knows an adapter can write into its own
    ``scratch_dir`` is owed the fact that one may have, and a reader who does
    not is owed the reason this command is exact rather than illustrative.
    """
    if not previews:
        return "No host was left with a service to start, so there is no compose command to show."
    commands = " ".join(f"On {host_id}, would run: {cmd}" for host_id, cmd in previews)
    return (
        f"The adapters ran (plain data; the only thing one may write is its own "
        f"scratch dir), so this is the command itself, not a description of it. "
        f"{commands}"
    )


def _log_displacements(use_case: str, selection: Selection) -> None:
    """Name every fragment the provider competition excluded (spec §4).

    Both priorities are printed and NEITHER is described as the higher one:
    a ``--provide cap=repo`` override narrows the field to one repo first, so
    the winner can legitimately carry a LOWER priority than the fragment it
    displaced. The loser can also be the winner's own repo (two fragments of
    one repo at different priorities). Saying "lower priority lost" would be
    a lie in both cases; naming who won and at what is true in all of them.
    """
    for d in selection.displaced:
        logger.info(
            rf"\[docker] use-case {use_case}: capability {d.capability!r} goes to "
            f"{d.winner_repo} (priority {d.winner_priority}); "
            f"{d.loser_repo} (priority {d.loser_priority}) stands down"
        )


def _refuse_failed_up(compose_project: str, parent_id: str, output: str) -> "NoReturn":
    """Raise the ``up`` failure. Its own function so the loop stays readable."""
    raise HostCommandError(
        f"docker compose up failed for {compose_project} on {parent_id}: {output}"
    )


def _plan(placed: "dict[str, list[SelectedFragment]]", selection: Selection) -> str:
    """Render the resolved plan a dry run declines with (spec §12).

    Winners (per host, with their compose handles), displacements, and the
    channel-1 env KEY names -- the SELECTION half of the pipeline, which every
    verb's dry run gets to run. Shared by all three so ``--dry-run docker
    down`` tells you as much as ``up`` does.

    Deliberately does not describe what is absent. ``deploy`` appends the
    exact per-host compose command (:func:`_command_preview`) because it
    reached the render; ``teardown`` renders nothing at all (a full teardown
    is ``-p <proj> down`` with no ``-f``) and ``deployed`` declines above
    ``deploy``. The clause that used to apologize here for a command it could
    not show described the old arm's position, not a property of a dry run.

    The displacement clause renders each record AS IT IS and calls neither
    priority the higher one, for :func:`_log_displacements`'s reason: a
    ``--provide`` override narrows the field to one repo before ranking, so
    the winner can carry a LOWER priority than what it displaced, and the
    loser can be the winner's own repo.
    """
    per_host = "; ".join(
        f"{host_id} <- "
        + ", ".join(f"{sf.repo.name}[{','.join(sf.fragment.composes)}]" for sf in frags)
        for host_id, frags in placed.items()
    )
    keys = sorted({key for frags in placed.values() for sf in frags for key in sf.fragment.env})
    env_note = f" Fragment env keys: {keys}." if keys else ""
    displaced = "; ".join(
        f"{d.capability} -> {d.winner_repo} (priority {d.winner_priority}), "
        f"{d.loser_repo} (priority {d.loser_priority}) stands down"
        for d in selection.displaced
    )
    displaced_note = f" Displaced: {displaced}." if displaced else ""
    return (
        f"Resolved plan: {per_host}.{displaced_note}{env_note} No image was built, "
        f"no file was staged and no container was started."
    )


async def _rollback(lab: "Lab", brought_up: "list[tuple[UnixHost, str]]") -> None:
    """Best-effort teardown of every stack this call started. Never masks.

    The caller is already propagating the real error, so a rollback that
    itself fails is reported and swallowed — but it IS reported: residue the
    user has to clean up by hand deserves to be named.

    ``remove_ids_under=None``: this call did not finish registering, and
    popping ids it never put in ``lab.hosts`` would unregister a peer's
    containers.
    """
    for parent, proj in brought_up:
        await _rollback_one(lab, parent, proj)


async def _rollback_one(lab: "Lab", parent: UnixHost, compose_project: str) -> None:
    """Roll one host's stack back, swallowing (but reporting) its own failure."""
    try:
        await compose_down_project(parent, compose_project, lab=lab, remove_ids_under=None)
    except Exception as e:  # noqa: BLE001 — a rollback may not mask the real error
        # .error, not .exception: the traceback the user needs belongs to the
        # error being propagated, not to the rollback that failed after it.
        logger.error(  # noqa: TRY400 — see above
            rf"\[docker] {compose_project} could not be rolled back on {parent.id} "
            f"and is still up: {e}"
        )


async def teardown(
    use_case: str,
    *,
    services: "Sequence[str] | None" = None,
    on: "str | None" = None,
    provide: "Mapping[str, str] | None" = None,
    stop_timeout: int = 1,
    project_name: "str | None" = None,
) -> None:
    """Tear *use_case* down and unregister its container hosts (spec §8).

    Full teardown is ``docker compose -p <proj> down`` with NO ``-f``: the
    project label is enough to find the stack, so DELETING a deployment never
    re-runs an adapter, re-renders a template or re-stages a file. Partial
    teardown (``services=[...]``) is ``stop`` then ``rm -f`` on just those
    services — stable across compose versions, and it leaves the rest of the
    stack and its network standing.

    *on* and *provide* are accepted (and resolved) exactly as :func:`deploy`
    resolves them, so the two verbs always agree on which hosts and which
    project a deployment lives in.

    Raises:
        ~otto.docker.resolve.UseCaseResolutionError: a configuration refusal,
            identical to :func:`deploy`'s.
        ~otto.result.CommandNotRunError: this is a dry run. Armed here rather
            than inherited, for ``compose_down``'s reason: the second half of
            this verb mutates ``lab.hosts``, which a dry run must leave alone.
    """
    lab, selection, placed, order = _resolve(use_case, on=on, provide=provide)
    services_filter = _validated_services(selection, services)

    if is_dry_run():
        raise CommandNotRunError(
            f"teardown({use_case})",
            ", ".join(sorted(placed)),
            f"{_plan(placed, selection)} No container was stopped and no host was "
            f"unregistered from the lab.",
        )

    for host_id, host_frags in placed.items():
        parent = _parent_for(lab, host_id)
        proj = project_name or use_case_project(parent.source_lab, use_case)
        prefix = f"{parent.id}.{use_case.lower()}."
        if services_filter is None:
            # No -f, and no render either: the project label is the whole
            # input, so a full teardown never re-reads a compose file (and so
            # cannot be blocked by one that has since been edited away).
            await compose_down_project(
                parent, proj, lab=lab, remove_ids_under=prefix, stop_timeout=stop_timeout
            )
            continue
        declared = _declared_services(_ordered(host_frags, order), report=False)
        wanted = [s for s in declared if s in services_filter]
        if not wanted:
            continue
        names = " ".join(shlex.quote(s) for s in wanted)
        quoted = shlex.quote(proj)
        for action in (f"stop -t {int(stop_timeout)} {names}", f"rm -f {names}"):
            result = await parent.exec(f"docker compose -p {quoted} {action}")
            if not result.is_ok:
                logger.error(
                    rf"\[docker] `{action.split()[0]}` failed for {proj} on "
                    f"{parent.id}: {result.value}"
                )
        await unregister_container_hosts(lab, prefix, services=wanted)


@asynccontextmanager
async def deployed(
    use_case: str, *, own: bool = False, **kw: "Any"
) -> "AsyncIterator[UseCaseStack]":
    """Context manager wrapping :func:`deploy` / :func:`teardown`.

    Keeps ``composed()``'s sharing contract, generalized to a deployment:
    a stack that was ALREADY up on entry is someone else's (a suite-level
    fixture, a peer instruction) and is left standing on exit. Ownership is
    stack-level and all-or-nothing, per spec §8 — compose keeps no ledger of
    "which containers did I add", and diffing before/after would fake one.

    ``**kw`` is forwarded to both verbs; keys only one of them takes
    (``env``, ``env_files``, ``build`` / ``stop_timeout``) are routed to the
    verb that takes them.

    Raises:
        ~otto.host.errors.HostCommandError: the already-up probe could not
            answer on some host. Read as "not up" it would tear down a peer's
            stack — exactly what this contract promises not to do — so it
            refuses and names ``own=True``.
        ~otto.result.CommandNotRunError: this is a dry run. Its own arm, for
            ``composed()``'s reason: the decline would otherwise come from
            one of two different callees depending on ``own``.
    """
    lab, selection, placed, order = _resolve(use_case, on=kw.get("on"), provide=kw.get("provide"))
    services_filter = _validated_services(selection, kw.get("services"))
    if is_dry_run():
        raise CommandNotRunError(
            f"deployed({use_case})",
            ", ".join(sorted(placed)),
            f"{_plan(placed, selection)} No stack was brought up, so none was torn down either.",
        )

    project_name = kw.get("project_name")
    was_up = False
    if not own:
        # The hosts `deploy` will ACT on, not every host in `placed`: a host
        # whose services were all narrowed away is one this call never
        # touches, and an unrelated stack sitting under the same project name
        # there would otherwise make `was_up` true and suppress a teardown
        # this context manager owed for the hosts it DID deploy to.
        for acting in _acting_hosts(placed, order, services_filter, use_case=use_case):
            parent = _parent_for(lab, acting.host_id)
            proj = project_name or use_case_project(parent.source_lab, use_case)
            probed = await _stack_already_up(parent, proj)
            if probed is None:
                raise HostCommandError(
                    f"cannot tell whether {proj} was already running on {parent.id}, "
                    f"so deployed({use_case!r}) cannot promise to leave a peer's "
                    f"stack alone; pass own=True to tear down unconditionally"
                )
            was_up = was_up or probed

    deploy_kw = {k: v for k, v in kw.items() if k != "stop_timeout"}
    stack = await deploy(use_case, **deploy_kw)
    try:
        yield stack
    finally:
        if own or not was_up:
            # Teardown is a compensating action: an interrupt landing while it
            # runs must not strand a half-torn deployment (chaos spec).
            # Imported here, not at module scope — otto.lifecycle is only
            # needed once a compensating action actually runs.
            from ..lifecycle import compensate

            teardown_kw = {
                k: v
                for k, v in kw.items()
                if k in ("services", "on", "provide", "stop_timeout", "project_name")
            }
            await compensate(
                teardown(use_case, **teardown_kw),
                what=f"docker use-case teardown {use_case}",
            )
