r"""Batched shell probes and removal over the products' ``cov_dir`` trees (#777).

The host-layer half of "cleanup owns coverage data": one command per host
and elevation, whatever the number of products, answering per product.

- :func:`probe_presence`: which ``cov_dir`` exists right now. The pre-run
  counter reset records it (:func:`otto.coverage.collect.clean_coverage`'s
  ``record_presence``), so a dir that existed at run start and is gone at
  the fetch is a loud loss, not a quiet "never exercised".
- :func:`coverage_left`: what coverage left under each ``cov_dir`` — its
  ``.gcda`` counters and the directories that are empty. Any of either
  means "not clean" (``is_clean``).
- :func:`remove_coverage`: cleanup's sweep. It removes only what
  :func:`~otto.host.product.gcda_remove_files` (``.gcda`` regular files)
  and :func:`~otto.host.product.gcda_remove_dirs` (dirs ``rmdir`` accepts)
  can, then probes again: ``-exec … {} \;`` carries no ``rm`` status, so
  the probe after is the only proof.
- :func:`warn_if_counters_discarded`: the one decision whether a cleanup's
  sweep discarded counters a coverage run is collecting, shared by a
  project's cleanup and a host's.

Every script opens with :func:`~otto.host.product.marker_cmd` and every
parser reads only what follows :func:`~otto.host.product.after_marker`: a
host session merges what sudo, PAM or a login shell print first into the
same stream, and none of that is ever the script's answer. After the mark,
the parse is fail-closed: a line it cannot attribute is an error, never a
leftover and never ignored, and a batch whose markers are not exactly the
expected sequence is an error for every product in it.

The probes run unelevated first; only a product whose kind's ``cov_dir`` is
root-owned (:attr:`~otto.host.product.Product.cov_dir_root_owned`) and whose
plain answer was an error is asked again through
:func:`~otto.host.privilege.run_sh_elevated`. Its removal always runs
elevated. Which products are probed is
:func:`~otto.host.product.cov_dir_products`'s decision, made by the caller;
every path is quoted by :func:`~otto.host.shell_quote.path_operand`.
"""

import logging
import re
import shlex
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Literal, TypeAlias

from ..result import Result
from ..utils import Status
from .product import (
    LEFTOVERS_SHOWN,
    GcdaFindError,
    after_marker,
    classify_gcda_listing,
    cov_dir_of,
    enoent_for_start,
    failure_detail,
    gcda_list_cmd,
    gcda_remove_dirs,
    gcda_remove_files,
    marker_cmd,
    path_under,
)

if TYPE_CHECKING:
    from ..invocation import RunPolicy
    from ..result import CommandResult
    from .host import Host
    from .product import Product

logger = logging.getLogger(__name__)

_PRESENCE_MARK = "otto-cov-presence"
"""The marker :func:`probe_presence`'s script prints before its answer."""

_PROBE_MARK = "otto-cov-probe"
"""The marker the leftover probe's script prints before its answer."""

_REMOVE_MARK = "otto-cov-remove"
"""The marker the removal's script prints before its answer."""

_PRESENCE_LINE = re.compile(r"^otto-cov (?P<index>\d+) (?P<word>present|absent)$")

EMPTY_DIR_TEST = (
    'entries=$(ls -A "$1") || { printf "otto-ls-failed %s\\n" "$1"; exit 1; }; '
    'if [ -z "$entries" ]; then printf "%s\\n" "$1"; fi'
)
"""Print ``$1`` when it is an empty directory; ``otto-ls-failed $1`` when it cannot be listed.

Run once per directory by ``find -exec sh -c … sh {} \\;``, which carries no
status of its own, so a failed ``ls`` is reported by the marker line, which
the probe's parser reads as an error."""

_INCOMPLETE = "removal batch output was incomplete"

# One product's probe answer: absent, what is left (empty: present and clean), or an error.
Answer: TypeAlias = "Literal['absent'] | list[str] | GcdaFindError"


# ── the presence probe ────────────────────────────────────────────────────────


def _presence_script(dirs: Sequence[str]) -> str:
    """``test -d`` per dir, answering ``otto-cov <i> present|absent``; no path is echoed."""
    from .shell_quote import path_operand  # function-local: off the host import path

    lines = [marker_cmd(_PRESENCE_MARK)]
    lines.extend(
        f"if test -d {path_operand(d)}; then echo 'otto-cov {i} present'; "
        f"else echo 'otto-cov {i} absent'; fi"
        for i, d in enumerate(dirs)
    )
    return "\n".join(lines)


def _parse_presence(output: str, count: int) -> list[bool] | None:
    """Each index's presence, or ``None`` unless every index answered exactly once."""
    body = after_marker(output, _PRESENCE_MARK)
    if body is None:
        return None
    answers: dict[int, bool] = {}
    for line in body.splitlines():
        if not line:
            continue
        match = _PRESENCE_LINE.match(line)
        if match is None:
            return None
        index = int(match["index"])
        if index >= count or index in answers:
            return None
        answers[index] = match["word"] == "present"
    if len(answers) != count:
        return None
    return [answers[i] for i in range(count)]


async def probe_presence(host: "Host", products: "Sequence[Product]") -> frozenset[str] | None:
    """Return the names of *products* whose ``cov_dir`` exists on *host* now.

    One unelevated ``host.exec`` for all of them (nothing is sent for none).
    ``None`` means no evidence: the batch failed, was declined by a dry run,
    or did not answer every product exactly once. A dir behind an unreadable
    ancestor reads as absent — the evidence only ever makes a loss louder,
    so a false "absent" costs a warning, never a wrong verdict.
    """
    if not products:
        return frozenset()
    dirs = [cov_dir_of(p) for p in products]
    result = await host.exec(_presence_script(dirs), timeout=60)
    if result.status is Status.NotRun or not result.is_ok:
        return None
    answers = _parse_presence(result.value, len(dirs))
    if answers is None:
        return None
    return frozenset(p.name for p, present in zip(products, answers, strict=True) if present)


# ── framing shared by the probe and the removal ──────────────────────────────


def _sections(
    body: str, count: int, *, mid: bool
) -> "list[tuple[list[str], list[str], list[int]]] | None":
    """Split a framed batch body into per-index sections, or ``None`` if it is not exactly framed.

    Each index must appear once, in order, as ``otto-cov-begin <i>``, (with
    *mid*) ``otto-cov-mid <i>``, then ``otto-cov-end <i> <status>...``. A
    line outside a begin/end pair, a missing, duplicated or out-of-order
    marker, or a status that is not an integer fails the whole batch: no
    partial answer is trusted. Returns, per index, the lines before the
    mid marker, the lines after it (always empty without *mid*), and the
    statuses on the end line.
    """
    sections: list[tuple[list[str], list[str], list[int]]] = []
    first: list[str] = []
    second: list[str] = []
    state = "outside"
    for line in body.splitlines():
        index = len(sections)
        if state == "outside":
            if not line:
                continue
            if line != f"otto-cov-begin {index}":
                return None
            first, second, state = list[str](), list[str](), "first"
        elif line == f"otto-cov-mid {index}" and mid and state == "first":
            state = "second"
        elif line.startswith(f"otto-cov-end {index} ") and state == ("second" if mid else "first"):
            words = line.split()[2:]
            if not words or not all(re.fullmatch(r"-?\d+", w) for w in words):
                return None
            sections.append((first, second, [int(w) for w in words]))
            state = "outside"
        elif line.startswith(("otto-cov-begin ", "otto-cov-mid ", "otto-cov-end ")):
            return None
        elif line:
            (first if state == "first" else second).append(line)
    if state != "outside" or len(sections) != count:
        return None
    return sections


# ── the leftover probe ────────────────────────────────────────────────────────


def _probe_script(dirs: Sequence[str]) -> str:
    """Build the leftover probe for *dirs*: per index, the counters, then the empty dirs.

    No ``test -d`` guard: an unreadable ancestor makes ``test -d`` false, and
    a guarded probe would call a dir nobody could look into clean. Each
    ``find``'s output is its own section and its status is kept, so a later
    product's success never hides an earlier one's failure.
    """
    from .shell_quote import path_operand  # function-local: off the host import path

    empty_dir_test = shlex.quote(EMPTY_DIR_TEST)
    lines = [marker_cmd(_PROBE_MARK), "LC_ALL=C; export LC_ALL"]
    for i, d in enumerate(dirs):
        lines += [
            f"echo 'otto-cov-begin {i}'",
            f"{gcda_list_cmd(d)} 2>&1; r=$?",
            f"echo 'otto-cov-mid {i}'",
            (
                f"find {path_operand(d)} -depth -type d "
                f"-exec sh -c {empty_dir_test} sh {{}} \\; 2>&1; s=$?"
            ),
            f'echo "otto-cov-end {i} $r $s"',
        ]
    return "\n".join(lines)


def _classify(files: list[str], dirs: list[str], r: int, s: int, cov_dir: str) -> Answer:
    """One index's answer from its two sections and statuses (see :func:`_parse_probe`).

    The files section is :func:`~otto.host.product.classify_gcda_listing`'s
    to read, the reset's own reading; only the empty-dirs rules are here.
    *cov_dir* itself listed empty is not a leftover: the removal tries its
    ``rmdir``, and one the session cannot remove (its parent is root's, as
    ``/var/cov`` is) is kept, empty, and clean. An empty dir under it is
    still a leftover.
    """
    text = "\n".join(files + dirs)
    error = GcdaFindError(text or f"exit {r}/{s}, no output")
    if any(line.startswith("otto-ls-failed ") for line in dirs):
        return error
    counters = classify_gcda_listing(cov_dir, files, r)
    if counters == "absent":
        gone = s != 0 and len(dirs) == 1 and enoent_for_start(dirs[0], cov_dir)
        return "absent" if gone else error
    if isinstance(counters, GcdaFindError) or s != 0:
        return error
    if all(d == cov_dir or path_under(d, cov_dir) for d in dirs):
        return counters + [d for d in dirs if d != cov_dir]
    return error


def _parse_probe(output: str, dirs: Sequence[str], retcode: int = 0) -> list[Answer]:
    """Each of *dirs*' answer to :func:`_probe_script`, fail-closed.

    Only what follows the batch mark is read; no mark makes every index a
    :class:`~otto.host.product.GcdaFindError` carrying the whole output. Per
    index, with its files section and its empty-dirs section:

    - an ``otto-ls-failed`` line: an error;
    - both ``find`` exited 0: the lines are what is left (empty: present and
      clean), the dir itself excepted, provided every files line is a
      ``.gcda`` under the dir and every dirs line the dir or a path under it;
      any other line is an error;
    - both failed, and each section is exactly the one
      :func:`~otto.host.product.enoent_for_start` line: ``"absent"``;
    - anything else (one failed, a silent failure, a descendant's ENOENT, a
      permission error): an error.
    """
    body = after_marker(output, _PROBE_MARK)
    sections = None if body is None else _sections(body, len(dirs), mid=True)
    if sections is None:
        error = GcdaFindError(failure_detail(output, retcode))
        return [error for _ in dirs]
    answers: list[Answer] = []
    for d, (files, empties, statuses) in zip(dirs, sections, strict=True):
        if len(statuses) != 2:  # noqa: PLR2004 — the files find's status, then the dirs find's
            answers.append(GcdaFindError(failure_detail(output, retcode)))
            continue
        answers.append(_classify(files, empties, statuses[0], statuses[1], d))
    return answers


def _command_value(cmd: "CommandResult") -> tuple[str, int]:
    return cmd.value or "", cmd.retcode


async def _probe(host: "Host", products: "Sequence[Product]") -> "dict[str, Answer]":
    """Ask *host* what coverage left under each of *products*' ``cov_dir``: one answer each.

    One unelevated batch first. A product whose answer is an error and whose
    kind's ``cov_dir`` is root-owned is asked again, with the others like it,
    in one elevated batch, whose answers replace the plain ones. Nothing is
    sent for no products. Under a dry run it refuses, rather than invent an
    answer (:func:`~otto.host.host.refuse_declined_fact`).
    """
    from .host import refuse_declined_fact
    from .privilege import run_sh_elevated  # function-local: patched in tests at its home

    if not products:
        return {}
    dirs = [cov_dir_of(p) for p in products]
    plain = await host.exec(_probe_script(dirs), timeout=120)
    refuse_declined_fact(plain, asked=f"{host.id}.coverage_dirs_clean()")
    output, retcode = _command_value(plain)
    answers = dict(
        zip((p.name for p in products), _parse_probe(output, dirs, retcode), strict=True)
    )
    again = [
        p
        for p in products
        if isinstance(answers[p.name], GcdaFindError) and type(p).cov_dir_root_owned
    ]
    if again:
        again_dirs = [cov_dir_of(p) for p in again]
        elevated = await run_sh_elevated(host, _probe_script(again_dirs), timeout=120)
        refuse_declined_fact(elevated, asked=f"{host.id}.coverage_dirs_clean()")
        output, retcode = _command_value(elevated.only)
        answers.update(
            zip((p.name for p in again), _parse_probe(output, again_dirs, retcode), strict=True)
        )
    logger.debug(
        "coverage probe on %s: %d product(s) answered unelevated, %d re-probed elevated",
        host.id,
        len(products) - len(again),
        len(again),
    )
    return answers


async def coverage_left(host: "Host", products: "Sequence[Product]") -> dict[str, list[str]]:
    """Return what coverage left under each of *products*' ``cov_dir`` on *host*.

    The answer is ``{name: paths}``.

    Only products with something left appear: an absent ``cov_dir``, or one
    holding only non-coverage files, is clean. The paths are the ``.gcda``
    counters and the empty directories.

    Raises:
        HostCommandError: a product's probe could not tell (an unreadable
            dir, a permission error the elevated re-probe did not cure, a
            malformed answer); every such product is named, with its output.
        CommandNotRunError: a dry run declined the probe.
    """
    from .errors import HostCommandError

    answers = await _probe(host, products)
    errors = [
        f"{host.id}: cannot tell whether {p.name}'s cov_dir {cov_dir_of(p)} is clean: "
        f"{answer.detail}"
        for p in products
        if isinstance(answer := answers[p.name], GcdaFindError)
    ]
    if errors:
        raise HostCommandError("; ".join(errors))
    return {name: left for name, left in answers.items() if isinstance(left, list) and left}


# ── the removal ───────────────────────────────────────────────────────────────


def _remove_script(dirs: Sequence[str]) -> str:
    r"""Build the removal for *dirs*: per index, the counters (``find``'s status kept), the dirs.

    The files pass's status is ``find``'s traversal status, not ``rm``'s
    (``\;`` carries none): it reports a start path or subtree ``find``
    could not walk. The dirs pass fails for every non-empty dir by design,
    so its status and output are dropped. Whether the dir was kept is the
    probe's answer afterwards, never this one's.
    """
    lines = [marker_cmd(_REMOVE_MARK), "LC_ALL=C; export LC_ALL"]
    for i, d in enumerate(dirs):
        lines += [
            f"echo 'otto-cov-begin {i}'",
            f"{gcda_remove_files(d)} 2>&1; f=$?",
            f"{gcda_remove_dirs(d)} 2>/dev/null",
            f'echo "otto-cov-end {i} $f"',
        ]
    return "\n".join(lines)


def _parse_removal(output: str, dirs: Sequence[str], retcode: int = 0) -> list[str | None]:
    """Each of *dirs*' removal error, or ``None``; framed and fail-closed like the probe.

    ``None`` when ``find`` exited 0, or when it failed with exactly the one
    line saying the start path does not exist (an absent ``cov_dir`` is a
    no-op). Otherwise the section's text (``exit <f>, no output`` when
    silent). A batch that is not exactly framed answers every index with
    ``removal batch output was incomplete: <output>``.
    """
    body = after_marker(output, _REMOVE_MARK)
    sections = None if body is None else _sections(body, len(dirs), mid=False)
    incomplete = f"{_INCOMPLETE}: {failure_detail(output, retcode)}"
    if sections is None:
        return [incomplete for _ in dirs]
    errors: list[str | None] = []
    for d, (lines, _unused, statuses) in zip(dirs, sections, strict=True):
        if len(statuses) != 1:
            errors.append(incomplete)
            continue
        status = statuses[0]
        if status == 0 or (len(lines) == 1 and enoent_for_start(lines[0], d)):
            errors.append(None)
        else:
            errors.append(failure_detail("\n".join(lines), status))
    return errors


async def _remove_batch(
    host: "Host", group: str, products: "Sequence[Product]"
) -> "dict[str, str | None] | None":
    """Run one removal batch; each product's error, or ``None`` when the session declined it."""
    from .privilege import run_sh_elevated  # function-local: patched in tests at its home

    dirs = [cov_dir_of(p) for p in products]
    script = _remove_script(dirs)
    if group == "elevated":
        results = await run_sh_elevated(host, script, timeout=600)
        if results.status is Status.NotRun:
            return None
        cmd = results.only
    else:
        cmd = await host.exec(script, timeout=600)
        if cmd.status is Status.NotRun:
            return None
    output, retcode = _command_value(cmd)
    if cmd.timed_out or retcode == -1:
        detail = "timed out" if cmd.timed_out else failure_detail(output, retcode)
        return {p.name: f"{group} removal batch failed: {detail}" for p in products}
    parsed = _parse_removal(output, dirs, retcode)
    return {
        p.name: f"{group} removal batch failed: {error}"
        if error is not None and error.startswith(_INCOMPLETE)
        else error
        for p, error in zip(products, parsed, strict=True)
    }


async def remove_coverage(host: "Host", products: "Sequence[Product]") -> Result:
    """Remove the ``.gcda`` counters under each of *products*' ``cov_dir``, and the dirs left empty.

    One plain batch for the products whose ``cov_dir`` the session user
    owns, one elevated batch (:func:`~otto.host.privilege.run_sh_elevated`)
    for the root-owned kinds; nothing is sent for no products. Then, always,
    the probe again: the result names, per product in declaration order,
    its removal error (if any) beside what is still there or why the probe
    could not tell. A ``cov_dir`` that still holds other files, or that is
    empty but the session cannot remove, is kept, and logged at INFO once
    the probe established it. A dry run declines with
    :attr:`~otto.utils.Status.NotRun`: no probe, no "kept" line.
    """
    if not products:
        return Result(Status.Success)
    plain = [p for p in products if not type(p).cov_dir_root_owned]
    root_owned = [p for p in products if type(p).cov_dir_root_owned]
    logger.debug(
        "coverage removal on %s: %d plain, %d elevated", host.id, len(plain), len(root_owned)
    )
    removal: dict[str, str | None] = {}
    for group, members in (("plain", plain), ("elevated", root_owned)):
        if not members:
            continue
        errors = await _remove_batch(host, group, members)
        if errors is None:
            return Result(Status.NotRun)
        removal.update(errors)
    answers = await _probe(host, products)
    problems: list[str] = []
    for p in products:
        d = cov_dir_of(p)
        if (error := removal.get(p.name)) is not None:
            problems.append(f"{p.name}: {error}")
        answer = answers[p.name]
        if isinstance(answer, GcdaFindError):
            problems.append(f"{p.name}: cannot tell whether {d} is clean: {answer.detail}")
        elif answer == "absent":
            continue
        elif answer:
            problems.append(f"{p.name}: left {', '.join(answer[:LEFTOVERS_SHOWN])}")
        else:
            logger.info(
                "cov_dir %s kept: holds non-coverage files, or the session cannot remove it", d
            )
    if not problems:
        return Result(Status.Success)
    return Result(
        Status.Error, msg=f"{host.id}: cleanup left coverage data: " + "; ".join(problems)
    )


# ── the cleanup-during-collection warning ─────────────────────────────────────

_DISCARD_WARNING = (
    "cleanup removed coverage counters during a coverage run, discarding those "
    'collected so far; mark coverage suites ensure("uninstalled") to keep them'
)


def warn_if_counters_discarded(
    policy: "RunPolicy", swept: "Iterable[tuple[Host, Sequence[Product], Result | BaseException]]"
) -> bool:
    """Log the WARNING once per invocation when a sweep discarded counters a run is collecting.

    *swept* holds one entry per host the sweep walked: the host, the products
    it was scoped to, and its outcome.

    The one decision for every cleanup entry point
    (:meth:`otto.project.ProjectActions.cleanup` and
    :meth:`otto.host.host.BaseHost.cleanup`), so the same sweep outcome warns
    the same way through either. Three gates, in this order:

    - *policy*'s :attr:`~otto.context.RunPolicy.coverage` is collecting;
    - the sweep ran on some host with an eligible product
      (:func:`~otto.host.product.cov_dir_products` of its scoped products):
      its outcome is an exception (a crash mid-sweep may have removed some
      counters) or a Result that is neither a dry run's decline (``NotRun``)
      nor a stopped container passed over (``Skipped``);
    - the invocation's latch is still unclaimed
      (:meth:`~otto.invocation.CoverageCollection.claim_warning`).

    Eligibility is read from the outcomes before the latch is claimed, so a
    cleanup that removed nothing leaves it for a later one that does.
    Returns whether it warned.
    """
    from .product import cov_dir_products

    state = policy.coverage
    if not state.collecting:
        return False
    ran = any(
        cov_dir_products(host, products)
        and (
            isinstance(outcome, BaseException)
            or outcome.status not in (Status.NotRun, Status.Skipped)
        )
        for host, products, outcome in swept
    )
    if not (ran and state.claim_warning()):
        return False
    logger.warning(_DISCARD_WARNING)
    return True
