"""Composed coverage collection: fetch ``.gcda`` → write metadata → produce captures → clean.

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
* :func:`collect_coverage` runs the fetch → metadata → capture → clean sequence
  *after* a run and returns a :class:`CollectResult`. It **fails loud**: a missing
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
    from ..config.project import Project
    from ..host.product import Product
    from ..host.toolchain import Toolchain
    from ..result import Result
    from .capture.produce import ProduceResult, ProductKey
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
    boards dumped over the console); ``produce`` is every pair's capture
    outcome, and ``captures_written`` the ``capture.json`` files among them.
    """

    cov_dir: Path
    product_dirs: dict[tuple[str, str], Path]
    produce: "ProduceResult"
    """Every product's capture outcome (empty when no ``[coverage]`` project resolved)."""
    clean: "CleanReport | None" = None
    """The clean after collection, when ``clean_after_fetch`` ran one."""

    @property
    def captures_written(self) -> list[Path]:
        """List the ``capture.json`` files produced, one per captured pair."""
        return self.produce.written


async def clean_coverage(
    projects: "list[Project] | None" = None,
    *,
    host_ids: "list[str] | None" = None,
    pairs: "list[ProductKey] | None" = None,
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

    *host_ids* narrows the walk to those hosts. *pairs* narrows it further,
    to exactly the named products on the named hosts — the scope a
    collection cleans after capture, so a product that failed to capture
    keeps its counters. A named product the host does not carry as an
    instrumented product is reported failed, never skipped. An empty *pairs*
    is nothing to clean: an empty report, no host touched.

    Raises:
        CoverageConfigError: no ``[coverage]`` section, or a malformed
            ``hosts`` selector.
        NoCoverageHostsError: the selector matched no host, or matched only
            the built-in ``local`` host, which no coverage walk includes; or
            ``host_ids`` or *pairs* narrowed an otherwise non-empty match down
            to nothing.
    """
    from ..config.coverage_settings import get_cov_config, load_hosts_pattern
    from ..config.scope import EmptySelectionError
    from ..declared import current_projects
    from ..errors import is_containable
    from ..fleet import all_hosts, do_for_all_hosts
    from ..result import Result
    from ..utils import Status
    from .errors import NoCoverageHostsError
    from .instrumentation import instrumented_products
    from .reports import CleanReport

    if projects is None:
        # The composition root: importing it installs the initiator that composes
        # the run's projects when no run is installed (spec 2026-10-11
        # placement-and-phases §3.12).
        from .. import bootstrap

        del bootstrap
        projects = current_projects()
    cov_config = get_cov_config(projects)
    if not cov_config:
        raise CoverageConfigError("No [coverage] section found in .otto/settings.toml")
    if pairs is not None and not pairs:
        return CleanReport(hosts={})

    pattern = load_hosts_pattern(cov_config)
    try:
        hosts = list(all_hosts(pattern=pattern, include_containers=True))
    except EmptySelectionError as e:
        # The flag-free wording: no coverage call takes the membership flags,
        # so the library advice to pass one cannot be followed from here. It
        # names coverage as the walker, not "this command": a library caller
        # of clean_coverage reads it as well as `otto cov clean`.
        raise NoCoverageHostsError(f"nothing to clean: {e.cli_message(walker='coverage')}") from e
    if not hosts:
        # The base set itself was empty (a lab with no hosts at all) —
        # `all_hosts` stays silent here rather than raising (see its
        # docstring), so this is the only way that case reaches us.
        raise NoCoverageHostsError("no host in the lab to walk — nothing to clean")
    # {host_id: product names to reset}, or None to reset every product.
    named: dict[str, set[str]] | None = None
    if pairs is not None:
        named = {}
        for key in pairs:
            named.setdefault(key.host, set()).add(key.product)
        host_ids = sorted(named if host_ids is None else set(host_ids) & set(named))
    if host_ids is not None:
        wanted = set(host_ids)
        hosts = [h for h in hosts if h.id in wanted]
        if not hosts:
            raise NoCoverageHostsError(
                f"host_ids={sorted(wanted)!r} matched none of the hosts "
                "[coverage].hosts selected — nothing to clean"
            )

    scoped = re.compile("^(?:" + "|".join(re.escape(h.id) for h in hosts) + ")$")
    outcomes = await do_for_all_hosts(
        _reset_products, named, pattern=scoped, include_containers=True
    )

    def _all_failed(host: "Any", reason: str) -> "dict[str, Result]":
        """Every product on *host* this clean covers, reported failed with *reason*.

        That is every instrumented product, or just the ones *pairs* named.

        The shape a host-level failure (a raised exception, or no outcome at
        all) takes: Chris's ruling leaves no room for a silent "ok" default,
        so a host this walk could not get a real per-product answer from is
        reported as fully failed rather than dropped or left empty.
        """
        names = (
            sorted(named[host.id])
            if named is not None
            else [p.name for p in instrumented_products(host)]
        )
        return {name: Result(Status.Error, msg=reason) for name in names} or {
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


async def _reset_products(
    host: "Any", named: "dict[str, set[str]] | None" = None
) -> "dict[str, Result]":
    """Reset *host*'s instrumented products, in declaration order.

    With *named*, only the products it names for this host; a named product
    the host does not carry instrumented is reported failed.
    """
    from .. import layout
    from ..result import Result
    from ..utils import Status
    from .instrumentation import instrumented_products

    wanted = None if named is None else named.get(host.id, set())
    results = {}
    for product in instrumented_products(host):
        if wanted is not None and product.name not in wanted:
            continue
        # Before the hook runs, not after: a name that is not a single safe
        # path segment must never reach a host.
        layout.validate_product_name(product.name)
        results[product.name] = await product.reset_coverage(host)
    for name in sorted((wanted or set()) - set(results)):
        results[name] = Result(
            Status.Error, msg=f"{name!r} is not an instrumented product on {host.id!r}"
        )
    return results


async def collect_coverage(
    cov_dir: Path,
    *,
    projects: "list[Project] | None" = None,
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
    roots, per-host toolchains, the resolved tier and the commit the counters
    were collected at; then a ``capture.json`` is produced per (host, product)
    against that tier (``tier=None`` selects the lab's sole e2e-kind tier),
    and only the pairs that captured are cleaned.

    Fails loud (never swallows):

    * no ``[coverage]`` section configured →
      :class:`~otto.config.coverage_settings.CoverageConfigError` (a :class:`ValueError`);
    * no ``.gcda`` retrieved from any matched product →
      :class:`~otto.coverage.errors.NoCoverageDataError` (a :class:`ValueError`)
      naming every ``host:product:cov_dir`` triple searched;
    * a reused *cov_dir* holding products not yet captured that another
      collection left at another tier or commit (or with no metadata saying
      which) → :class:`~otto.coverage.errors.CoverageInputError`
      (``field="output_dir"``), before any host is fetched;
    * an ambiguous/unknown tier name → :class:`ValueError` (from
      :func:`~otto.coverage.tiers.resolve_get_tier`) — only reachable when
      *tier* is a name (or ``None``); a resolved :class:`~otto.coverage.tiers.TierConfig`
      passed directly skips resolution entirely;
    * a project root outside git, a polluted tree, an incompatible gcov, or a merge failure
      propagate as :class:`~otto.coverage.capture.gitio.GitUnavailableError`,
      :class:`~otto.coverage.errors.CoverageDataMismatchError`,
      :class:`~otto.coverage.errors.CoverageToolVersionError`, or
      :class:`RuntimeError`;
    * one or more products failing to capture →
      :class:`~otto.coverage.errors.CoverageCaptureError`, raised after every
      product was attempted and the ones that captured were cleaned (when
      *clean_after_fetch*). Its ``result`` holds every outcome and its
      ``clean`` that clean's report; a failed product's counters stay on
      the host, so the next collection captures them.

    Args:
        cov_dir: Destination directory for the collected coverage.
        projects: Project list to resolve ``[coverage]`` from (defaults to the
            run's projects, :func:`otto.declared.current_projects`).
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
            on exactly the (host, product) pairs that captured (embedded
            boards included) once production is done — the ``otto test
            --cov`` semantics that keep the next run from mixing in stale
            data, while a product that failed keeps its counters. When
            ``False``, skip that internal clean entirely so the caller can
            own the post-fetch clean itself (e.g. ``otto cov get``, whose
            ``--clean`` is optional). The report lands on the returned
            :class:`CollectResult`'s ``clean`` field, or on the raised
            :class:`~otto.coverage.errors.CoverageCaptureError`'s (``None``
            when this is ``False`` or nothing captured); a failed reset is
            never swallowed here — it is the caller's to act on.

    Returns:
        A :class:`CollectResult` with the destination, per-product dirs, and
        every product's capture outcome.
    """
    from ..config.coverage_settings import get_cov_config, get_cov_project, load_hosts_pattern
    from ..declared import current_projects
    from ..fleet import all_hosts
    from ..host.embedded_host import EmbeddedHost
    from ..host.local_host import LocalHost
    from .capture import gitio
    from .capture.produce import ProduceResult, ProductKey, produce_captures
    from .errors import CoverageCaptureError
    from .fetcher.embedded import collect_embedded_coverage
    from .fetcher.remote import GcdaFetcher
    from .instrumentation import instrumented_products

    if projects is None:
        # The composition root: importing it installs the initiator that composes
        # the run's projects when no run is installed (spec 2026-10-11
        # placement-and-phases §3.12).
        from .. import bootstrap

        del bootstrap
        projects = current_projects()

    cov_config = get_cov_config(projects)
    if not cov_config:
        raise CoverageConfigError("No [coverage] section found in .otto/settings.toml")

    # The set of hosts to collect coverage from is project-declared: an optional
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

    cov_project = get_cov_project(projects)
    if cov_project is not None and _pending_products(cov_dir):
        # Before any host is fetched: a reused directory's uncaptured counters
        # must not be handed this collection's provenance.
        _refuse_foreign_pending(
            cov_dir,
            tier=_resolve_tier(cov_config, tier).name,
            base_commit=gitio.head_commit(cov_project.root),
        )

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

    logger.info("Coverage data collected to %s (%d product dir(s))", cov_dir, len(product_dirs))

    produced = ProduceResult([])
    failure: CoverageCaptureError | None = None
    if cov_project is not None:
        resolved_tier = _resolve_tier(cov_config, tier)
        # The commit the counters were collected at: recorded beside them,
        # and the one every capture below is anchored to.
        base_commit = gitio.head_commit(cov_project.root)
        await _write_metadata(
            cov_project=cov_project,
            cov_config=cov_config,
            fetch_hosts=fetch_hosts,
            fetched_host_ids={host_id for host_id, _product in fetched},
            cov_hosts=cov_hosts,
            embedded_host_ids={host_id for host_id, _product in embedded_dirs},
            cov_dir=cov_dir,
            tier=resolved_tier.name,
            base_commit=base_commit,
        )

        # Only the products that yielded counters, read off the hosts as declared:
        # the fetch already decided which are instrumented, so no artifact is
        # scanned again here.
        # Keyed by host as well as name: two hosts may carry different builds
        # under one product name, and each host's counters pair against its own.
        products: "dict[ProductKey, Product]" = {}
        for h in searched_hosts:
            for p in h.products:
                if (h.id, p.name) in product_dirs:
                    products.setdefault(ProductKey(h.id, p.name), p)
        try:
            produced = await produce_captures(
                cov_dir,
                tier=resolved_tier.name,
                project_root=cov_project.root,
                labs=[cov_project.name],
                products=products,
                base_commit=base_commit,
                # Only this call's fetched pairs: a reused cov_dir may hold
                # leaves another collection left, which this one never fetched
                # and so must neither capture nor clean.
                only=[ProductKey(h, p) for h, p in sorted(product_dirs)],
                tester=tester,
                ticket=ticket,
                note=note,
                display_names=display_names,
            )
        except CoverageCaptureError as e:
            produced, failure = e.result, e
        logger.info("Coverage captures produced: %d board(s)", len(produced.written))

    clean_report = None
    if clean_after_fetch and produced.captured:
        # Exactly the pairs that captured, embedded boards included: a product
        # that failed keeps its counters on the host for the next collection.
        # A failed reset is reported, never logged and dropped — the caller
        # (otto test) fails the run on it.
        try:
            clean_report = await clean_coverage(projects, pairs=produced.captured)
        except Exception:
            if failure is not None:
                # The clean's own exception propagates; this keeps the capture
                # failure it would otherwise bury in __context__ visible.
                logger.error(str(failure))  # noqa: TRY400 — the clean's traceback follows; this is its companion line
            raise

    if failure is not None:
        failure.clean = clean_report
        raise failure

    return CollectResult(
        cov_dir=cov_dir,
        product_dirs=product_dirs,
        produce=produced,
        clean=clean_report,
    )


def _pending_products(cov_dir: Path) -> list[str]:
    """List *cov_dir*'s ``host/product`` dirs holding ``.gcda`` but no capture marker."""
    from .tree import iter_product_dirs

    return [
        f"{host_id}/{product}"
        for host_id, product, leaf in iter_product_dirs(cov_dir)
        if not (leaf / "capture.json").is_file()
        and not (leaf / "capture.failed").is_file()
        and next(leaf.rglob("*.gcda"), None) is not None
    ]


def _refuse_foreign_pending(cov_dir: Path, *, tier: str, base_commit: str) -> None:
    """Refuse a reused *cov_dir* whose uncaptured counters another collection left.

    The directory's ``.otto_cov_meta.json`` is shared by every product under
    it, and a product not yet captured is captured later with the tier and
    commit that file records. A collection at another tier or commit would
    rewrite it and so hand the earlier counters a provenance they do not
    have. A product already captured (or failed) carries its own, so only
    pending ones count; metadata that is missing or unreadable is as
    foreign as metadata that differs.

    Raises:
        CoverageInputError: *cov_dir* holds pending products and its metadata
            records another tier or commit, or none (``field="output_dir"``).
            Raised before any host is fetched and before the metadata is
            touched.
    """
    import json

    from .errors import CoverageInputError

    pending = _pending_products(cov_dir)
    if not pending:
        return
    try:
        meta = json.loads((cov_dir / ".otto_cov_meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    recorded_tier, recorded_commit = meta.get("tier"), meta.get("base_commit")
    if recorded_tier == tier and recorded_commit == base_commit:
        return
    if isinstance(recorded_tier, str) and isinstance(recorded_commit, str):
        was = f"collected under tier {recorded_tier!r} at commit {recorded_commit[:12]}"
        remedy = (
            f"report that directory first ('otto cov report {cov_dir.parent}' at commit "
            f"{recorded_commit[:12]}), or collect into a fresh output directory"
        )
    else:
        was = "whose tier and commit no metadata records"
        remedy = "collect into a fresh output directory"
    raise CoverageInputError(
        f"{cov_dir} holds counters not yet captured ({', '.join(pending)}) {was}; this "
        f"collection is tier {tier!r} at commit {base_commit[:12]}, and reusing the "
        f"directory would capture them under the wrong one — {remedy}",
        field="output_dir",
    )


def _resolve_tier(cov_config: dict[str, Any], tier: "str | TierConfig | None") -> "TierConfig":
    """Resolve the tier a collection annotates its captures with.

    A :class:`~otto.coverage.tiers.TierConfig` is used as is: a caller like
    ``otto cov get`` that resolved its own tier up front (to validate the
    manual-tier ``--ticket`` requirement) must not pay for — or risk
    diverging from — a second resolution by name here.

    Raises:
        ValueError: An ambiguous or unknown tier name (from
            :func:`~otto.coverage.tiers.resolve_get_tier`).
    """
    from .tiers import TierConfig, load_tiers, resolve_get_tier

    if isinstance(tier, TierConfig):
        return tier
    return resolve_get_tier(load_tiers(cov_config), tier)


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
    *,
    cov_project: "Project",
    cov_config: dict[str, Any],
    fetch_hosts: list[Any],
    fetched_host_ids: set[str],
    cov_hosts: list[Any],
    embedded_host_ids: set[str],
    cov_dir: Path,
    tier: str,
    base_commit: str,
) -> None:
    """Write ``.otto_cov_meta.json`` so ``otto cov report`` can find source roots and toolchains.

    The metadata is per *host*, not per product: a host's toolchain and source
    root are shared by every product it runs, so the ``(host, product)`` pairs
    that produced counters arrive here reduced to their host ids. It also
    records the *tier* the collection captured under and the *base_commit*
    the counters were collected at.
    """
    import json

    from ..host.toolchain import Toolchain
    from ..utils import anchor_path

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

    root = str(cov_project.root.resolve())

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

        Applies the documented path-resolution convention
        (:doc:`/configuration/settings`, "Path resolution"): ``~`` expansion
        and project-root anchoring for a still-relative value.
        """
        return str(anchor_path(Path(raw), cov_project.root))

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
        # sorted(), not set order: the root fallback below reads the FIRST
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
                root = str(Path(embedded_build_dir).resolve())
            elif source_roots:
                root = next(iter(source_roots.values()))

    meta: dict[str, object] = {
        "project_name": cov_project.name,
        "project_root": root,
        "toolchains": toolchains,
        "source_roots": source_roots,
        "tier": tier,
        "base_commit": base_commit,
    }
    (cov_dir / ".otto_cov_meta.json").write_text(json.dumps(meta, indent=2))
