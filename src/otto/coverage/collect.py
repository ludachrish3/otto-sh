"""Composed coverage collection: fetch ``.gcda`` → write metadata → produce captures.

This is the single canonical collection workflow behind both ``otto test --cov``
(via :func:`otto.suite.run._post_run_coverage`) and ``otto cov get`` (via
``otto.cli.cov._do_get``, rewired in a later task). It replaces the copy that
used to live inline in the ``otto.cli.test`` coverage helpers.

Two public entry points:

* :func:`clean_remote_gcda` zeroes ``.gcda`` counters on the lab's remote hosts
  *before* a run and rebuilds host connections so the pytest session gets fresh
  ones on its own event loop. The ``--cov``/``--cov-clean`` gate stays with the
  caller.
* :func:`collect_coverage` runs the fetch → metadata → capture sequence *after*
  a run and returns a :class:`CollectResult`. It **fails loud**: a missing
  ``[coverage]`` section, no ``.gcda`` retrieved from any host, an
  ambiguous/unknown tier, or a merge/produce error all raise — the never-fail-a-
  successful-run swallow policy lives in the callers (see
  :func:`otto.suite.run._post_run_coverage`).

Import-weight note: this module never imports ``typer`` (nor the CLI) at load
time — every heavy dependency (config, host, fetcher, capture, tiers) is
imported lazily inside the function that needs it, so ``import
otto.coverage.collect`` stays cheap for library callers and the existing
``otto.config.*`` / ``otto.coverage.*`` patch points keep working.
"""

import dataclasses
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config.repo import Repo

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class CollectResult:
    """Outcome of a :func:`collect_coverage` run.

    ``cov_dir`` is the directory the coverage landed in; ``host_dirs`` maps each
    contributing host id to its per-host ``.gcda`` directory (Unix hosts fetched
    over the network plus embedded boards dumped over the console); and
    ``captures_written`` lists the ``capture.json`` files produced, one per
    board (empty when no ``[coverage]`` repo resolved a git root).
    """

    cov_dir: Path
    host_dirs: dict[str, Path]
    captures_written: list[Path]


async def clean_remote_gcda(repos: "list[Repo] | None" = None) -> None:
    """Delete ``.gcda`` files on the lab's remote hosts, then rebuild connections.

    The pre-run cleanup for ``otto test --cov --cov-clean``: zero every
    configured host's remote counters so stale data from a previous run cannot
    be mixed in, then rebuild all Unix host connections so the pytest session
    reconnects on its own event loop. The rebuild runs unconditionally (even
    when the clean itself is skipped for want of config) — matching the old
    ``_pre_run_cov_clean`` behavior. The ``if opts.cov and opts.cov_clean`` gate
    stays with the caller.
    """
    from ..config import all_hosts, get_repos
    from ..host import UnixHost
    from .config import get_cov_config
    from .fetcher.remote import GcdaFetcher

    if repos is None:
        repos = get_repos()

    cov_config = get_cov_config(repos)
    gcda_remote_dir = cov_config.get("gcda_remote_dir", "") if cov_config else ""

    if not cov_config:
        pass  # no [coverage] section — nothing to clean, but still rebuild below
    elif not gcda_remote_dir:
        logger.warning("coverage.gcda_remote_dir not configured — skipping pre-run cleanup")
    elif not any(all_hosts()):
        pass  # no hosts in the lab — nothing to clean
    else:
        fetcher = GcdaFetcher(Path("/tmp"))  # noqa: S108 — deliberate staging path
        await fetcher.clean_remote(gcda_remote_dir)

    # Rebuild host connections so pytest gets fresh ones on its own loop.
    # rebuild_connections() only exists on UnixHost; embedded targets don't
    # carry the same connection lifecycle so skip them.
    for host in all_hosts():
        if isinstance(host, UnixHost):
            host.rebuild_connections()


async def collect_coverage(
    cov_dir: Path,
    *,
    repos: "list[Repo] | None" = None,
    tier: str | None = None,
    ticket: str | None = None,
    note: str | None = None,
    tester: dict[str, str] | None = None,
    display_names: dict[str, str] | None = None,
    clean_after_fetch: bool = True,
) -> CollectResult:
    """Collect ``.gcda`` coverage from Unix and/or embedded hosts into ``cov_dir``.

    Unix hosts emit ``.gcda`` to a filesystem fetched by
    :class:`~otto.coverage.fetcher.remote.GcdaFetcher`; embedded (Zephyr LLEXT)
    hosts have no filesystem and instead dump theirs over the console, decoded
    by :func:`~otto.coverage.fetcher.embedded.collect_embedded_coverage`. Both
    land under the same *cov_dir* so the merge/report step treats them
    identically. A ``.otto_cov_meta.json`` sidecar records source roots and
    per-host toolchains, then a ``capture.json`` is produced per board against
    the resolved tier (``tier=None`` selects the lab's sole e2e-kind tier).

    Fails loud (never swallows):

    * no ``[coverage]`` section configured → :class:`ValueError`;
    * no ``.gcda`` retrieved from any matched host → :class:`ValueError`
      naming the hosts searched;
    * an ambiguous/unknown tier → :class:`ValueError` (from
      :func:`~otto.coverage.tiers.resolve_get_tier`);
    * a non-git sut, a polluted tree, an incompatible gcov, or a merge failure
      propagate as :class:`~otto.coverage.capture.gitio.GitUnavailableError`,
      :class:`~otto.coverage.errors.CoverageDataMismatchError`,
      :class:`~otto.coverage.errors.CoverageToolVersionError`, or
      :class:`RuntimeError`.

    Args:
        cov_dir: Destination directory for the collected coverage.
        repos: Repo list to resolve ``[coverage]`` from (defaults to
            :func:`otto.config.get_repos`).
        tier: Tier name to annotate onto each capture; ``None`` resolves the
            sole e2e-kind tier.
        ticket: Optional ticket reference annotated onto every capture.
        note: Optional free-text note annotated onto every capture.
        tester: Optional tester identity annotated onto each capture.
        display_names: Optional board-dir (host id) → display name map.
        clean_after_fetch: When ``True`` (default), zero the Unix hosts' remote
            ``.gcda`` counters immediately after a successful fetch — the
            ``otto test --cov`` semantics that keep the next run from mixing in
            stale data. When ``False``, skip that internal clean entirely so the
            caller can own the post-fetch clean itself (e.g. ``otto cov get``
            scopes its ``--clean`` to just the Unix host ids, never an embedded
            board on a mixed lab). Embedded counters are never cleaned here.

    Returns:
        A :class:`CollectResult` with the destination, per-host dirs, and the
        produced capture paths.
    """
    from ..config import all_hosts, get_repos
    from ..host import UnixHost
    from .config import get_cov_config
    from .fetcher.embedded import collect_embedded_coverage
    from .fetcher.remote import GcdaFetcher

    if repos is None:
        repos = get_repos()

    cov_config = get_cov_config(repos)
    if not cov_config:
        raise ValueError("No [coverage] section found in .otto/settings.toml")

    host_dirs: dict[str, Path] = {}

    # The set of hosts to collect coverage from is repo-declared: an optional
    # ``[coverage].hosts`` regex (matched against each host id) selects targets,
    # defaulting to every host in the lab. This is how a lab's SSH **hop** (e.g.
    # `basil` fronting `sprout_cov`) is kept out of the coverage set — it is
    # excluded by the pattern, not inferred from the fact that it emits no .gcda.
    hosts_pattern = cov_config.get("hosts")
    cov_pattern = re.compile(hosts_pattern) if hosts_pattern else None

    # Unix hosts compile the SUT and emit .gcda to a filesystem we fetch over
    # the network. EmbeddedHost/DockerContainerHost are skipped by the fetcher.
    cov_hosts = list(all_hosts(pattern=cov_pattern))
    unix_hosts = [h for h in cov_hosts if isinstance(h, UnixHost)]
    gcda_remote_dir = cov_config.get("gcda_remote_dir", "")

    # Unix hosts that actually produced .gcda (host id -> dir). Keying the meta
    # off *collected coverage* (rather than lab membership) is a safety net
    # behind the ``[coverage].hosts`` selector above: should an infrastructure
    # host slip through the pattern, producing no .gcda keeps it from being
    # mistaken for a Unix coverage target — which would otherwise flip the
    # source-root choice (breaking embedded .gcno discovery) and write a bogus
    # toolchain entry.
    unix_dirs: dict[str, Path] = {}
    if gcda_remote_dir and unix_hosts:
        # Hosts may carry stale connections from pytest's event loop; rebuild
        # their connection state so they reconnect on the current loop.
        for host in unix_hosts:
            host.rebuild_connections()
        fetcher = GcdaFetcher(cov_dir)
        unix_dirs = await fetcher.fetch_all(gcda_remote_dir)
        host_dirs.update(unix_dirs)
        if unix_dirs and clean_after_fetch:
            # The unscoped post-fetch clean that preserves `otto test --cov`
            # semantics: zero the remotes right after a successful fetch so the
            # next run cannot mix in stale counters. Callers that own their own
            # (scoped) post-fetch clean — `otto cov get --clean` must never zero
            # an embedded board on a mixed lab — pass clean_after_fetch=False.
            await fetcher.clean_remote(gcda_remote_dir)

    # Embedded (RTOS) hosts dump .gcda over the console (no filesystem).
    embedded_dirs = await collect_embedded_coverage(cov_config, cov_dir, pattern=cov_pattern)
    host_dirs.update(embedded_dirs)

    if not host_dirs:
        searched = ", ".join(sorted(h.id for h in cov_hosts))
        where = f"searched: {searched}" if searched else "no hosts matched [coverage].hosts"
        raise ValueError(f"no .gcda counters retrieved from any host ({where})")

    logger.info("Coverage data collected to %s (%d hosts)", cov_dir, len(host_dirs))

    await _write_metadata(
        repos=repos,
        cov_config=cov_config,
        unix_hosts=unix_hosts,
        unix_dirs=unix_dirs,
        cov_hosts=cov_hosts,
        embedded_dirs=embedded_dirs,
        cov_dir=cov_dir,
    )

    captures_written = await _produce_capture_tail(
        repos=repos,
        cov_config=cov_config,
        cov_dir=cov_dir,
        tier=tier,
        ticket=ticket,
        note=note,
        tester=tester,
        display_names=display_names,
    )

    return CollectResult(cov_dir=cov_dir, host_dirs=host_dirs, captures_written=captures_written)


async def _produce_capture_tail(
    *,
    repos: "list[Repo]",
    cov_config: dict[str, Any],
    cov_dir: Path,
    tier: str | None,
    ticket: str | None,
    note: str | None,
    tester: dict[str, str] | None,
    display_names: dict[str, str] | None,
) -> list[Path]:
    """Produce a ``capture.json`` per board (anchored to base_commit) against a tier.

    So a bare ``otto test --cov`` run always leaves behind capture artifacts
    (not just raw ``.gcda``) — the same production step ``otto cov get`` uses for
    a manual/on-demand pull. Unlike the old inline tail this does **not** swallow
    errors: an ambiguous/unknown tier (:class:`ValueError`), a non-git sut, a
    stamp mismatch, or a merge failure propagate to the caller, which decides
    whether to fail the run. Returns an empty list (no captures) when no
    ``[coverage]`` repo resolved a git root.
    """
    from .capture.produce import produce_captures
    from .config import get_cov_repo
    from .tiers import load_tiers, resolve_get_tier

    cov_repo = get_cov_repo(repos)
    if cov_repo is None:
        return []

    tiers = load_tiers(cov_config)
    resolved_tier = resolve_get_tier(tiers, tier)
    written = await produce_captures(
        cov_dir,
        tier=resolved_tier.name,
        repo_root=cov_repo.sut_dir,
        labs=[cov_repo.name],
        tester=tester,
        ticket=ticket,
        note=note,
        display_names=display_names,
    )
    logger.info("Coverage captures produced: %d board(s)", len(written))
    return written


async def _write_metadata(
    repos: "list[Repo]",
    cov_config: dict[str, Any],
    unix_hosts: list[Any],
    unix_dirs: dict[str, Path],
    cov_hosts: list[Any],
    embedded_dirs: dict[str, Path],
    cov_dir: Path,
) -> None:
    """Write ``.otto_cov_meta.json`` so ``otto cov report`` can find source roots and toolchains.

    Moved verbatim from the ``otto.cli.test`` coverage-metadata helper
    (library-extraction Task 15). Behavior is identical to the original.
    """
    import json

    from .config import get_cov_repo

    cov_repo = get_cov_repo(repos)
    if not cov_repo:
        return

    toolchains: dict[str, dict[str, str]] = {}
    for host in unix_hosts:
        # Only hosts that actually produced coverage — skip infrastructure hosts
        # (e.g. an SSH hop) that are in the lab solely for connectivity.
        if host.id not in unix_dirs:
            continue
        tc = host.toolchain
        toolchains[host.id] = {
            "sysroot": str(tc.sysroot),
            "lcov": str(tc.lcov),
            "gcov": str(tc.gcov),
        }

    sut_dir = str(cov_repo.sut_dir.resolve())

    # Embedded hosts now carry a per-host Toolchain (lab-data ``toolchain``),
    # exactly like Unix hosts: the bed declares the cross-gcov for binaries it
    # runs. Use it per host; fall back to scanning the build's .gcno only for a
    # host left at the default (unconfigured) toolchain.
    #
    # The build dir is the report's source root when there are no Unix hosts
    # (standalone-embedded). Multi-Zephyr-version labs declare per-version build
    # dirs under [coverage.embedded.builds.<version>]; each host's os_version
    # selects its own root, recorded in ``source_roots`` so the reporter can
    # resolve the correct .gcno tree per host. The single ``build_dir`` remains
    # supported as a legacy/fallback for single-version labs.
    embedded_cfg = cov_config.get("embedded") or {}
    embedded_build_dir = embedded_cfg.get("build_dir")  # single legacy/fallback
    embedded_builds = embedded_cfg.get("builds") or {}  # {"3.7": {"build_dir": ...}}

    def _resolve_build_dir(host: object) -> str | None:
        ver = getattr(host, "os_version", None)
        if ver and ver in embedded_builds:
            bd = embedded_builds[ver].get("build_dir")
            if bd:
                return bd
        return embedded_build_dir

    source_roots: dict[str, str] = {}
    if embedded_dirs and (embedded_build_dir or embedded_builds):
        from ..host.embedded_host import EmbeddedHost
        from ..host.toolchain import Toolchain
        from ..host.toolchain_discovery import discover_toolchain_from_gcno

        embedded_hosts = {h.id: h for h in cov_hosts if isinstance(h, EmbeddedHost)}
        # Cache .gcno-discovery per build dir so hosts sharing a build dir do
        # not re-trigger the (potentially slow) filesystem scan.
        discovery_cache: dict[str, Toolchain | None] = {}
        for host_id in embedded_dirs:
            host = embedded_hosts.get(host_id)
            host_build_dir = _resolve_build_dir(host) if host is not None else embedded_build_dir
            if host_build_dir:
                source_roots[host_id] = str(Path(host_build_dir).resolve())
            tc = host.toolchain if host is not None and host.toolchain != Toolchain() else None
            if tc is None:
                bd_key = host_build_dir or ""
                if bd_key not in discovery_cache:
                    if host_build_dir:
                        discovery_cache[bd_key] = discover_toolchain_from_gcno(Path(host_build_dir))
                    else:
                        discovery_cache[bd_key] = None
                tc = discovery_cache[bd_key]
            if tc is not None:
                toolchains[host_id] = {
                    "sysroot": str(tc.sysroot),
                    "lcov": str(tc.lcov),
                    "gcov": str(tc.gcov),
                }
        if not unix_dirs:
            # Use the single fallback if present; otherwise the first resolved root.
            if embedded_build_dir:
                sut_dir = str(Path(embedded_build_dir).resolve())
            elif source_roots:
                sut_dir = next(iter(source_roots.values()))

    meta: dict[str, object] = {
        "repo_name": cov_repo.name,
        "sut_dir": sut_dir,
        "toolchains": toolchains,
        "source_roots": source_roots,
    }
    (cov_dir / ".otto_cov_meta.json").write_text(json.dumps(meta, indent=2))
