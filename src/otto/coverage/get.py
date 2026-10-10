"""``otto cov get`` as a library call: retrieve the lab's coverage now.

Every rule ``otto cov get`` applies lives here, so ``import otto`` callers get
the CLI's behaviour exactly: the tier and ticket rules, the instrumentation
refusal, the repository preflight, the destination, the committed manual
store and the scoped clean. Collection itself is
:func:`~otto.coverage.collect.collect_coverage`, the engine ``otto test --cov``
shares, called unchanged.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from .errors import CoverageInputError

if TYPE_CHECKING:
    from ..config.repo import Repo
    from .reports import GetReport


def _resolve_tester(name: str | None, email: str | None, sut_dir: Path) -> dict[str, str]:
    """Resolve tester identity for a manual capture (spec decision 15).

    ``name`` defaults to :func:`getpass.getuser`; ``email`` defaults to
    ``git config user.email`` read *in the SUT repo* (so the identity comes
    from the repo being tested, not whatever repo the process CWD happens to
    be in) and is omitted entirely (not annotated empty) when the key is
    unset or ``git config`` itself fails. Caller-supplied values always win
    over both defaults.

    Note ``config_value`` runs WITHOUT ``--local``, so a *sut_dir* that is not
    a repo still reads ``~/.gitconfig`` and answers rc 0 — the
    NotAGitRepoError arm below is defensive, not a path a non-repo takes.
    Reading the ambient identity there is the intended behaviour (it is the
    human running the capture), so this does not want narrowing.

    A MISSING git propagates instead: "otto cannot run git" is an
    environment error, not evidence that the tester has no email, and
    swallowing it here is what let it be reported as the latter. In the
    ``cov get`` flow it is unreachable anyway — the ``head_commit`` preflight
    has already proven git runs — so the top-level caller is the right place
    for the case where it is not.
    """
    import getpass

    from .capture.gitio import GitCommandFailedError, NotAGitRepoError, config_value

    resolved_name = name or getpass.getuser()
    resolved_email = email
    if not resolved_email:
        try:
            resolved_email = config_value(sut_dir, "user.email")
        except (NotAGitRepoError, GitCommandFailedError):
            resolved_email = None

    tester: dict[str, str] = {"name": resolved_name}
    if resolved_email:
        tester["email"] = resolved_email
    return tester


async def get_coverage(
    output_dir: Path | None = None,
    *,
    tier: str | None = None,
    ticket: str | None = None,
    note: str | None = None,
    tester_name: str | None = None,
    tester_email: str | None = None,
    clean: bool = False,
    repos: "list[Repo] | None" = None,
) -> "GetReport":
    """Fetch the lab's coverage now and produce one capture per (host, product).

    Refuses, in this order and before any host is touched: no ``[coverage]``
    section or a malformed ``hosts`` selector
    (:class:`~otto.config.coverage_settings.CoverageConfigError`); a selector
    matching no host (:class:`~otto.config.scope.EmptySelectionError`, raised
    by the host walk itself); an
    unknown or ambiguous *tier*, or a manual-kind tier with no *ticket*
    (:class:`~otto.coverage.errors.CoverageInputError` naming the field);
    nothing instrumented
    (:class:`~otto.coverage.errors.CoverageNotInstrumentedError`); a SUT that
    is not a git checkout
    (:class:`~otto.coverage.capture.gitio.GitUnavailableError`); no
    *output_dir* and no context to take one from (``CoverageInputError``,
    ``field="output_dir"``). After collection, no capture produced is
    :class:`~otto.coverage.errors.NoCoverageDataError`.

    A manual-kind tier's captures are also written into the repo's committed
    manual store, attributed to *tester_name*/*tester_email* (defaults: the
    current user, and ``git config user.email`` in the SUT repo). With
    *clean*, the counters of every host that contributed a product are
    cleared afterwards; a failed reset does not raise — the captures are
    written — it makes the report not ok.
    """
    from ..config.coverage_settings import (
        CoverageConfigError,
        get_cov_config,
        get_cov_repo,
        load_hosts_pattern,
    )
    from ..config.fleet import all_hosts, current_repos
    from ..context import try_get_context
    from . import collect
    from .capture.gitio import head_commit
    from .capture.model import Capture
    from .capture.store_dir import write_manual_capture
    from .errors import NoCoverageDataError
    from .instrumentation import decide_coverage, detect
    from .reports import GetReport
    from .tiers import load_tiers, resolve_get_tier

    if repos is None:
        repos = current_repos()
    cov_config = get_cov_config(repos)
    cov_repo = get_cov_repo(repos)
    if not cov_config or cov_repo is None:
        raise CoverageConfigError("No [coverage] section found in .otto/settings.toml")
    hosts = list(all_hosts(pattern=load_hosts_pattern(cov_config), include_containers=True))

    resolved = resolve_get_tier(load_tiers(cov_config), tier)
    if resolved.kind == "manual" and not ticket:
        raise CoverageInputError(
            f"tier {resolved.name!r} is a manual-kind tier and requires a ticket", field="ticket"
        )
    decide_coverage(True, detect(hosts), has_cov_config=True, command="otto cov get")
    head_commit(cov_repo.sut_dir)

    if output_dir is None:
        ctx = try_get_context()
        output_dir = ctx.output_dir if ctx is not None else None
        if output_dir is None:
            raise CoverageInputError(
                "no output directory was given and none is set for this invocation",
                field="output_dir",
            )
    cov_dir = output_dir.resolve() / "cov"

    tester = (
        _resolve_tester(tester_name, tester_email, cov_repo.sut_dir)
        if resolved.kind == "manual"
        else None
    )
    result = await collect.collect_coverage(
        cov_dir,
        repos=repos,
        tier=resolved,
        ticket=ticket,
        note=note,
        tester=tester,
        display_names={h.id: h.name for h in hosts},
        clean_after_fetch=False,
    )
    if not result.captures_written:
        searched = ", ".join(f"{h}:{p}" for h, p in sorted(result.product_dirs))
        where = f"searched: {searched}" if searched else "no products produced captures"
        raise NoCoverageDataError(f"no .gcda counters retrieved from any product ({where})")

    manual = []
    if resolved.kind == "manual":
        manual = [
            write_manual_capture(Capture.load(path), cov_repo.sut_dir)
            for path in result.captures_written
        ]

    clean_report = None
    if clean:
        contributed = sorted({host_id for host_id, _product in result.product_dirs})
        clean_report = await collect.clean_coverage(repos, host_ids=contributed)

    return GetReport(
        cov_dir=cov_dir,
        tier=resolved.name,
        captures=list(result.captures_written),
        manual_captures=manual,
        clean=clean_report,
    )
