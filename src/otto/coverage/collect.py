"""Composed coverage collection: fetch ``.gcda`` → write metadata → produce captures.

This is the single canonical collection workflow behind both ``otto test --cov``
(via ``otto.suite.run._post_run_coverage``) and ``otto cov get`` (via
:func:`otto.coverage.get.get_coverage`). It replaces the copy that
used to live inline in the ``otto.cli.test`` coverage helpers.

Two public entry points:

* :func:`clean_coverage` is the one clear path for zeroing counters: every
  instrumented product's own :meth:`~otto.host.product.Product.reset_coverage`
  hook, on every coverage host the lab admits. ``otto cov clean``, the
  ``otto test --cov --cov-clean`` pre-run clean, the clean after an
  ``otto test --cov`` collection and ``otto cov get --clean`` all come through
  it — a failed reset is a result in its report, never a log line.
* :func:`collect_coverage` runs the fetch → metadata → capture sequence *after*
  a run and returns a :class:`CollectResult`. It **fails loud**: a missing
  ``[coverage]`` section, no ``.gcda`` retrieved from any product, an
  ambiguous/unknown tier, or a merge/produce error all raise — the never-fail-a-
  successful-run swallow policy lives in the callers (see
  ``otto.suite.run._post_run_coverage``).

Products, not hosts, are the unit of collection: each host's instrumented
products name their own ``cov_dir``, and every stage below keys off the
``(host_id, product)`` pair that produced the counters.

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

from ..config.coverage_settings import CoverageConfigError
from .errors import NoCoverageDataError

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..host.toolchain import Toolchain
    from ..result import Result
    from .reports import CleanReport
    from .tiers import TierConfig

logger = logging.getLogger(__name__)


def _searched_where(host: "Any", product: "Any") -> str:
    """Where *product*'s counters were looked for, as the fail-loud names it.

    An embedded board has no filesystem — it dumps its ``.gcda`` over the serial
    console (:mod:`otto.coverage.fetcher.embedded`). ``cov_dir_of`` would print
    the ``/tmp/<name>`` default there, a path that exists on no board, and send
    the reader hunting for a directory instead of at the console transcript.
    """
    from ..host.embedded_host import EmbeddedHost
    from ..host.product import cov_dir_of

    return "console" if isinstance(host, EmbeddedHost) else cov_dir_of(product)


@dataclasses.dataclass(frozen=True)
class CollectResult:
    """Outcome of a :func:`collect_coverage` run.

    ``product_dirs`` maps each contributing ``(host_id, product)`` pair to its
    staging dir (Unix and container hosts fetched over the network, embedded
    boards dumped over the console); ``captures_written`` lists the
    ``capture.json`` files produced, one per pair.
    """

    cov_dir: Path
    product_dirs: dict[tuple[str, str], Path]
    captures_written: list[Path]
    clean: "CleanReport | None" = None
    """The clean after collection, when ``clean_after_fetch`` ran one."""


async def clean_coverage(
    repos: "list[Repo] | None" = None,
    *,
    host_ids: "list[str] | None" = None,
) -> "CleanReport":
    """Zero every instrumented product's counters on the lab's coverage hosts.

    The one clear path: ``otto cov clean``, ``otto test --cov --cov-clean``,
    the clean after an ``otto test --cov`` collection and ``otto cov get
    --clean`` all come through here. Every host ``[coverage].hosts`` matches
    (containers included, the otto runner excluded) has each instrumented
    product's own :meth:`~otto.host.product.Product.reset_coverage` called:
    Unix and container products delete their ``.gcda``, kernel modules write
    their sysfs reset, embedded extensions call their ``reset_fn``.

    A failed reset is a result in the returned report, never a log line:
    callers decide, and every caller in otto treats ``not report.ok`` as a
    failure. A host whose reset raises (a dropped connection) reports every
    one of its products failed, naming the exception.

    Raises:
        CoverageConfigError: no ``[coverage]`` section, or a malformed
            ``hosts`` selector.
        NoCoverageHostsError: the selector matched no host (the otto runner
            is never in the base set ``all_hosts`` walks, so this is the only
            way the walk comes back empty), or ``host_ids`` narrowed an
            otherwise non-empty match down to nothing.
    """
    from ..config import all_hosts, get_repos
    from ..config.coverage_settings import get_cov_config, load_hosts_pattern
    from ..config.fleet import do_for_all_hosts
    from ..config.scope import EmptySelectionError
    from ..errors import is_containable
    from ..result import Result
    from ..utils import Status
    from .errors import NoCoverageHostsError
    from .instrumentation import instrumented_products
    from .reports import CleanReport

    if repos is None:
        repos = get_repos()
    cov_config = get_cov_config(repos)
    if not cov_config:
        raise CoverageConfigError("No [coverage] section found in .otto/settings.toml")

    pattern = load_hosts_pattern(cov_config)
    try:
        hosts = list(all_hosts(pattern=pattern, include_containers=True))
    except EmptySelectionError as e:
        raise NoCoverageHostsError(f"nothing to clean: {e}") from e
    if not hosts:
        # The base set itself was empty (a lab with no hosts at all) —
        # `all_hosts` stays silent here rather than raising (see its
        # docstring), so this is the only way that case reaches us.
        raise NoCoverageHostsError("no host in the lab to walk — nothing to clean")
    if host_ids is not None:
        wanted = set(host_ids)
        hosts = [h for h in hosts if h.id in wanted]
        if not hosts:
            raise NoCoverageHostsError(
                f"host_ids={sorted(wanted)!r} matched none of the hosts "
                "[coverage].hosts selected — nothing to clean"
            )

    scoped = re.compile("^(?:" + "|".join(re.escape(h.id) for h in hosts) + ")$")
    outcomes = await do_for_all_hosts(_reset_products, pattern=scoped, include_containers=True)

    def _all_failed(host: "Any", reason: str) -> "dict[str, Result]":
        """Every instrumented product on *host*, reported failed with *reason*.

        The shape a host-level failure (a raised exception, or no outcome at
        all) takes: Chris's ruling leaves no room for a silent "ok" default,
        so a host this walk could not get a real per-product answer from is
        reported as fully failed rather than dropped or left empty.
        """
        return {p.name: Result(Status.Error, msg=reason) for p in instrumented_products(host)} or {
            "(host)": Result(Status.Error, msg=reason)
        }

    by_host: dict[str, dict[str, Result]] = {}
    for host in hosts:
        if host.id not in outcomes:
            # A gap between this scoped re-derivation and the walk
            # `do_for_all_hosts` just ran — the host was supposed to be
            # dispatched to but reported nothing back at all.
            by_host[host.id] = _all_failed(host, f"no result returned for host {host.id!r}")
            continue
        outcome = outcomes[host.id]
        if isinstance(outcome, BaseException):
            if not is_containable(outcome):
                raise outcome
            by_host[host.id] = _all_failed(host, f"{type(outcome).__name__}: {outcome}")
        else:
            by_host[host.id] = outcome
    return CleanReport(hosts=by_host)


async def _reset_products(host: "Any") -> "dict[str, Result]":
    """Reset every instrumented product on *host*, in declaration order."""
    from .. import layout
    from .instrumentation import instrumented_products

    results = {}
    for product in instrumented_products(host):
        # Before the hook runs, not after: a name that is not a single safe
        # path segment must never reach a host.
        layout.validate_product_name(product.name)
        results[product.name] = await product.reset_coverage(host)
    return results


async def collect_coverage(
    cov_dir: Path,
    *,
    repos: "list[Repo] | None" = None,
    tier: "str | TierConfig | None" = None,
    ticket: str | None = None,
    note: str | None = None,
    tester: dict[str, str] | None = None,
    display_names: dict[str, str] | None = None,
    clean_after_fetch: bool = True,
) -> CollectResult:
    """Collect each host's instrumented products' ``.gcda`` into ``cov_dir``.

    Every matched host is walked product by product. A product on a Unix or
    container host writes ``.gcda`` under its own ``cov_dir`` on a filesystem
    fetched by :class:`~otto.coverage.fetcher.remote.GcdaFetcher`; an embedded
    (Zephyr LLEXT) board has no filesystem and dumps its products' counters
    over the console instead, decoded by
    :func:`~otto.coverage.fetcher.embedded.collect_embedded_coverage`. Both
    stage into ``<cov_dir>/<host_id>/<product>/`` so the merge/report step
    treats them identically. A ``.otto_cov_meta.json`` sidecar records source
    roots and per-host toolchains, then a ``capture.json`` is produced per
    board against the resolved tier (``tier=None`` selects the lab's sole
    e2e-kind tier).

    Fails loud (never swallows):

    * no ``[coverage]`` section configured →
      :class:`~otto.config.coverage_settings.CoverageConfigError` (a :class:`ValueError`);
    * no ``.gcda`` retrieved from any matched product →
      :class:`~otto.coverage.errors.NoCoverageDataError` (a :class:`ValueError`)
      naming every ``host:product:cov_dir`` triple searched;
    * an ambiguous/unknown tier name → :class:`ValueError` (from
      :func:`~otto.coverage.tiers.resolve_get_tier`) — only reachable when
      *tier* is a name (or ``None``); a resolved :class:`~otto.coverage.tiers.TierConfig`
      passed directly skips resolution entirely;
    * a non-git sut, a polluted tree, an incompatible gcov, or a merge failure
      propagate as :class:`~otto.coverage.capture.gitio.GitUnavailableError`,
      :class:`~otto.coverage.errors.CoverageDataMismatchError`,
      :class:`~otto.coverage.errors.CoverageToolVersionError`, or
      :class:`RuntimeError`.

    Args:
        cov_dir: Destination directory for the collected coverage.
        repos: Repo list to resolve ``[coverage]`` from (defaults to
            :func:`otto.config.get_repos <otto.config.bootstrapped.get_repos>`).
        tier: Tier name to annotate onto each capture; ``None`` resolves the
            sole e2e-kind tier. A caller that has already resolved a
            :class:`~otto.coverage.tiers.TierConfig` (e.g. ``otto cov get``,
            which validates the manual-tier ``--ticket`` requirement against
            it before calling in) can pass the object directly instead of its
            name — this skips ``resolve_get_tier`` entirely rather than
            re-resolving what the caller already resolved.
        ticket: Optional ticket reference annotated onto every capture.
        note: Optional free-text note annotated onto every capture.
        tester: Optional tester identity annotated onto each capture.
        display_names: Optional board-dir (host id) → display name map.
        clean_after_fetch: When ``True`` (default), call :func:`clean_coverage`
            on every host that contributed a product (embedded boards
            included) immediately after a successful collection — the
            ``otto test --cov`` semantics that keep the next run from mixing
            in stale data. When ``False``, skip that internal clean entirely
            so the caller can own the post-fetch clean itself (e.g.
            ``otto cov get`` scopes its ``--clean`` to just the hosts it
            fetched). The report lands on the returned
            :class:`CollectResult`'s ``clean`` field (``None`` when this is
            ``False``); a failed reset is never swallowed here — it is the
            caller's to act on.

    Returns:
        A :class:`CollectResult` with the destination, per-product dirs, and
        the produced capture paths.
    """
    from ..config import all_hosts, get_repos
    from ..config.coverage_settings import get_cov_config, load_hosts_pattern
    from ..host.embedded_host import EmbeddedHost
    from ..host.local_host import LocalHost
    from .fetcher.embedded import collect_embedded_coverage
    from .fetcher.remote import GcdaFetcher
    from .instrumentation import instrumented_products

    if repos is None:
        repos = get_repos()

    cov_config = get_cov_config(repos)
    if not cov_config:
        raise CoverageConfigError("No [coverage] section found in .otto/settings.toml")

    # The set of hosts to collect coverage from is repo-declared: an optional
    # ``[coverage].hosts`` regex (matched against each host id) selects targets,
    # defaulting to every host in the lab. This is how a lab's SSH **hop** (e.g.
    # `test4` fronting `zephyr37_llext`) is kept out of the coverage set — it is
    # excluded by the pattern, not inferred from the fact that it emits no .gcda.
    cov_pattern = load_hosts_pattern(cov_config)
    cov_hosts = list(all_hosts(pattern=cov_pattern, include_containers=True))
    # Every host some stage actually looks at, in declaration order. The runner
    # is not a SUT, so no stage searches it and the failure message below must
    # not claim it did.
    searched_hosts = [h for h in cov_hosts if not isinstance(h, LocalHost)]
    # Of those, the ones with a filesystem to fetch over the network; an
    # embedded board has none and dumps over the console instead.
    fetch_hosts = [h for h in searched_hosts if not isinstance(h, EmbeddedHost)]

    # {(host_id, product): staging dir}. Keying the meta off *collected
    # coverage* (rather than lab membership) is a safety net behind the
    # ``[coverage].hosts`` selector above: should an infrastructure host slip
    # through the pattern, producing no .gcda keeps it from being mistaken for
    # a fetched coverage target — which would otherwise flip the source-root
    # choice (breaking embedded .gcno discovery) and write a bogus toolchain
    # entry.
    product_dirs: dict[tuple[str, str], Path] = {}
    fetched: dict[tuple[str, str], Path] = {}
    if fetch_hosts:
        # No reconnect step: a host whose connection belonged to one of
        # pytest's closed loops drops it on first use here and reconnects on
        # this loop (BaseHost._claim_loop).
        fetcher = GcdaFetcher(cov_dir, pattern=cov_pattern)
        fetched = await fetcher.fetch_all()
        product_dirs.update(fetched)

    # Embedded (RTOS) boards dump their products' .gcda over the console.
    embedded_dirs = await collect_embedded_coverage(cov_dir, pattern=cov_pattern)
    product_dirs.update(embedded_dirs)

    if not product_dirs:
        searched = [
            f"{h.id}:{p.name}:{_searched_where(h, p)}"
            for h in searched_hosts
            for p in instrumented_products(h)
        ]
        if searched:
            where = "searched: " + ", ".join(searched)
        elif searched_hosts:
            where = "no instrumented products on any host: " + ", ".join(
                sorted(h.id for h in searched_hosts)
            )
        else:
            where = "no hosts matched [coverage].hosts"
        raise NoCoverageDataError(f"no .gcda counters retrieved from any product ({where})")

    clean_report = None
    if clean_after_fetch:
        # Every host that contributed a product, embedded boards included:
        # each has a real reset now. A failed reset is reported, never logged
        # and dropped — the caller (otto test) fails the run on it.
        clean_report = await clean_coverage(
            repos, host_ids=sorted({host_id for host_id, _product in product_dirs})
        )

    logger.info("Coverage data collected to %s (%d product dir(s))", cov_dir, len(product_dirs))

    await _write_metadata(
        repos=repos,
        cov_config=cov_config,
        fetch_hosts=fetch_hosts,
        fetched_host_ids={host_id for host_id, _product in fetched},
        cov_hosts=cov_hosts,
        embedded_host_ids={host_id for host_id, _product in embedded_dirs},
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

    return CollectResult(
        cov_dir=cov_dir,
        product_dirs=product_dirs,
        captures_written=captures_written,
        clean=clean_report,
    )


async def _produce_capture_tail(
    *,
    repos: "list[Repo]",
    cov_config: dict[str, Any],
    cov_dir: Path,
    tier: "str | TierConfig | None",
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

    *tier* skips :func:`~otto.coverage.tiers.resolve_get_tier` entirely when
    it is already a :class:`~otto.coverage.tiers.TierConfig` (a caller like
    ``otto cov get`` that resolved its own tier up front, e.g. to validate the
    manual-tier ``--ticket`` requirement, must not pay for — or risk
    diverging from — a second resolution by name here).
    """
    from ..config.coverage_settings import get_cov_repo
    from .capture.produce import produce_captures
    from .tiers import TierConfig, load_tiers, resolve_get_tier

    cov_repo = get_cov_repo(repos)
    if cov_repo is None:
        return []

    if isinstance(tier, TierConfig):
        resolved_tier = tier
    else:
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


def _toolchain_meta(tc: "Toolchain") -> dict[str, Any]:
    """One host's toolchain as the cov metadata carries it.

    The three coverage paths always; ``lcov_args`` only when the host declares
    some, so a host that declares none reads back exactly as it did before the
    field existed. A deferred ``otto cov report`` builds its per-host capture
    commands from this, which is why the arguments have to travel with it.
    """
    meta: dict[str, Any] = {
        "sysroot": str(tc.sysroot),
        "lcov": str(tc.lcov),
        "gcov": str(tc.gcov),
    }
    if tc.lcov_args:
        meta["lcov_args"] = list(tc.lcov_args)
    return meta


async def _write_metadata(
    repos: "list[Repo]",
    cov_config: dict[str, Any],
    fetch_hosts: list[Any],
    fetched_host_ids: set[str],
    cov_hosts: list[Any],
    embedded_host_ids: set[str],
    cov_dir: Path,
) -> None:
    """Write ``.otto_cov_meta.json`` so ``otto cov report`` can find source roots and toolchains.

    The metadata is per *host*, not per product: a host's toolchain and source
    root are shared by every product it runs, so the ``(host, product)`` pairs
    that produced counters arrive here reduced to their host ids.
    """
    import json

    from ..config.coverage_settings import get_cov_repo
    from ..host.toolchain import Toolchain
    from ..utils import anchor_path

    cov_repo = get_cov_repo(repos)
    if not cov_repo:
        return

    toolchains: dict[str, dict[str, Any]] = {}
    for host in fetch_hosts:
        # Only hosts that actually produced coverage — skip infrastructure hosts
        # (e.g. an SSH hop) that are in the lab solely for connectivity.
        if host.id not in fetched_host_ids:
            continue
        # Only what was configured. A host at the default toolchain gets no
        # entry: the reporter reads its counters with the gcov the data's own
        # stamp names, and a recorded default would say "the runner's gcov"
        # about counters another gcc, clang, or a container's image wrote.
        if host.toolchain == Toolchain():
            continue
        tc = host.toolchain
        toolchains[host.id] = _toolchain_meta(tc)

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

    def _anchor_build_dir(raw: str) -> str:
        """Anchor a raw ``build_dir`` value read from the config passthrough dict.

        Applies the documented path-resolution convention (docs/configuration/
        settings.md ``### Path resolution``): ``~`` expansion and
        repo-root anchoring for a still-relative value.
        """
        return str(anchor_path(Path(raw), cov_repo.sut_dir))

    embedded_build_dir = embedded_cfg.get("build_dir")  # single legacy/fallback
    if embedded_build_dir:
        embedded_build_dir = _anchor_build_dir(embedded_build_dir)
    embedded_builds = embedded_cfg.get("builds") or {}  # {"3.7": {"build_dir": ...}}

    def _resolve_build_dir(host: object) -> str | None:
        ver = getattr(host, "os_version", None)
        if ver and ver in embedded_builds:
            bd = embedded_builds[ver].get("build_dir")
            if bd:
                return _anchor_build_dir(bd)
        return embedded_build_dir

    source_roots: dict[str, str] = {}
    if embedded_host_ids and (embedded_build_dir or embedded_builds):
        from ..host.embedded_host import EmbeddedHost
        from ..host.toolchain_discovery import discover_toolchain_from_gcno

        embedded_hosts = {h.id: h for h in cov_hosts if isinstance(h, EmbeddedHost)}
        # Cache .gcno-discovery per build dir so hosts sharing a build dir do
        # not re-trigger the (potentially slow) filesystem scan.
        discovery_cache: dict[str, Toolchain | None] = {}
        # sorted(), not set order: the sut_dir fallback below reads the FIRST
        # resolved root, and a set's iteration order is not stable across runs.
        for host_id in sorted(embedded_host_ids):
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
                toolchains[host_id] = _toolchain_meta(tc)
        if not fetched_host_ids:
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
