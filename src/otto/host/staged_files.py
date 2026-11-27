"""Cleanup's removal of the files otto staged, and the question that matches it (#837).

The host-layer half of "cleanup owns the traces otto placed": the file a
product or dev tool was staged as (``<stage_dir>/<artifact basename>``), and a
declared stage directory left empty. One command per host whatever the number
of entries, then, for the question, one probe.

- :func:`staged_entries`: which entries are covered. The removal, the question
  and the plan all ask it, so they cannot disagree.
- :func:`remove_staged`: ``rm -f`` of each path (:func:`~otto.host.shell_quote.rm_f_command`),
  then one :func:`~otto.host.shell_quote.rmdir_if_empty` over the DECLARED stage
  directories, deepest first. otto keeps no record of what it staged, so the
  file is named by its declaration alone: on a host where the entry was never
  installed, a file of the same name in the same directory is removed, exactly
  as an install would have overwritten it.
- :func:`staged_files_left`: which of those paths still have a directory
  entry. Directories are never asked: removing one is opportunistic.

Both scripts open with :func:`~otto.host.product.marker_cmd` and both parsers
read only what follows :func:`~otto.host.product.after_marker`, as the
coverage sweep's do (:mod:`otto.host.cov_sweep`), and a line the parser cannot
attribute is an error. The question reuses the climb that
:func:`~otto.host.product.cov_dir_absent_test` makes, so a path behind an
unsearchable parent reads as "cannot tell", never as absent.

The commands run unelevated, except on a container host: its transfer lands
the file as root, so the removal and the probe run as root there too.
"""

import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ..result import Result
from ..utils import Status
from .exit_status import gave_no_exit_status
from .product import (
    StagedEntry,
    _creates_stage_dir,
    _declared_or_default,
    after_marker,
    cov_dir_absent_test,
    failure_detail,
    has_stage_destination,
    is_top_level_dir,
    marker_cmd,
    places_staged_file,
    staged_file,
    staged_path,
)
from .shell_quote import path_operand, rm_f_command, rmdir_if_empty

if TYPE_CHECKING:
    from ..result import CommandResult
    from .dev_tool import DevTool
    from .host import Host
    from .product import Product

logger = logging.getLogger(__name__)

_REMOVE_MARK = "otto-staged-removal"
"""The marker the removal's script prints before its answer."""

_PROBE_MARK = "otto-staged-probe"
"""The marker the question's script prints before its answer."""

_COMMAND_TIMEOUT = 120.0

_RM_LINE = re.compile(r"^otto-staged-rm (?P<index>\d+) (?P<status>\d+)$")
_DIR_LINE = re.compile(r"^otto-staged-dir (?P<index>\d+) kept$")
_PROBE_LINE = re.compile(r"^otto-staged (?P<index>\d+) (?P<word>absent|present|unknown)$")

_KEPT = "not empty, sticky, or the session cannot remove it"


def staged_entries(host: "Host", items: "Iterable[Product | DevTool]") -> list[StagedEntry]:
    """Those of *items* whose staged file cleanup removes on *host*, in the order given.

    ONE rule, asked by the removal, the question and the plan. An entry is
    covered when all of these hold:

    - its ``stages_artifact`` is true (``shell``, a tarball ``docker_image``,
      ``kmod``, the kernel-module dev tools, a provider's shell-class product);
    - it has an ``artifact`` and a ``stage_key``
      (:func:`~otto.host.product.places_staged_file`, which
      :func:`~otto.host.product.check_stage_collisions` asks too);
    - it is not a :class:`~otto.host.declared_product.DeclaredProduct` whose
      ``stage`` a ``class =`` subclass replaced: that code may place the file
      anywhere. The built-in kinds override ``stage`` themselves and are covered.

    An entry whose staged file on the local host IS its artifact (the stage
    directory is the artifact's own directory) is left out too: the copy
    refuses to copy a file onto itself, so nothing was staged, and removing
    the path would delete the user's build output. Only a directory known
    without touching the host is compared (``_is_its_own_artifact``).

    A host that makes no stage directories (``_creates_stage_dir``
    is false: an embedded target) is covered by none. An entry with no staging
    destination at all on *host* is left out too
    (:func:`~otto.host.product.has_stage_destination`): it was never staged there.
    """
    from .declared_product import DeclaredProduct  # function-local: off the host import path

    if not _creates_stage_dir(host):
        return []
    covered: list[StagedEntry] = []
    for item in items:
        if not places_staged_file(item):
            continue
        entry = item
        # SLF001: the package's own question about its own base class
        if isinstance(item, DeclaredProduct) and item._overridden("stage") is not None:  # noqa: SLF001
            continue
        if not has_stage_destination(entry, host) or _is_its_own_artifact(entry, host):
            continue
        covered.append(entry)
    return covered


def _is_its_own_artifact(entry: StagedEntry, host: "Host") -> bool:
    """Whether *entry*'s staged file on the local *host* is the artifact file itself.

    The local host stages by copying on its own filesystem, so a stage
    directory that is the artifact's directory names the artifact. Decided from
    the declaration and ``default_dest_dir`` alone; a login-home destination is
    not compared, since only a connection can say what it is.
    """
    from .local_host import LocalHost  # function-local: off the host import path

    if not isinstance(host, LocalHost):
        return False
    directory = _declared_or_default(entry.stage_dir, host, f"entry {entry.name!r}")
    if directory is None:
        return False
    return staged_file(directory, entry.artifact).resolve() == entry.artifact.resolve()


def declared_stage_dirs(items: "Iterable[StagedEntry]") -> list[str]:
    """Return the DECLARED stage directories of *items*, distinct, deepest first.

    Never the host's ``default_dest_dir`` and never the login home: an entry
    that declares no ``stage_dir`` contributes none. Never a top-level
    directory (:func:`~otto.host.product.is_top_level_dir`: ``/tmp``, ``/opt``):
    it is shared by the system, and a root ``rmdir`` of an empty one would
    recreate it plain on the next staging. Deepest first so a child is tried
    before its parent; ties break by name so the order is stable. The sticky
    guard is the command's (:func:`~otto.host.shell_quote.rmdir_if_empty`).
    """
    declared = {
        str(PurePosixPath(item.stage_dir))
        for item in items
        if str(item.stage_dir) not in ("", ".") and not is_top_level_dir(str(item.stage_dir))
    }
    return sorted(declared, key=lambda d: (-len(PurePosixPath(d).parts), d))


async def staged_paths(host: "Host", items: "Iterable[StagedEntry]") -> list[Path]:
    """Where each of *items*' staged file is on *host*, in order (:func:`staged_path`).

    Raises:
        ~otto.host.errors.HostCommandError: the login home an entry falls back
            on could not be discovered, or two entries stage one file into the
            same directory once it is known
            (:func:`~otto.host.product.check_stage_collisions`): a failure,
            never "nothing staged".
    """
    from .errors import HostCommandError

    try:
        return [await staged_path(item, host) for item in items]
    except (HostCommandError, ValueError) as error:
        # ValueError is a refusal the lab or an entry earned (two entries stage
        # one file into the discovered home). Both ends of cleanup say it the
        # one way they say a home that could not be read: a failure to tell
        # where the files are, never "nothing staged", and an OttoError the
        # CLI prints as one line.
        raise HostCommandError(
            f"{host.id}: cannot tell where the staged files are: {error}"
        ) from error


# ── the commands ─────────────────────────────────────────────────────────────


def removal_script(paths: Sequence[Path], dirs: Sequence[str]) -> str:
    """Build cleanup's removal: ``rm -f`` per path, then one quiet ``rmdir`` over *dirs*.

    Each ``rm`` keeps its own status and whatever it printed, on its
    ``otto-staged-rm <i> <status>`` line; ``rm -f`` succeeds for an absent
    path, so a non-zero status means a file that exists and could not be
    removed. The ``rmdir`` is :func:`~otto.host.shell_quote.rmdir_if_empty`
    over *dirs* in the order given (children first); afterwards each directory
    that still exists is reported as ``otto-staged-dir <j> kept``, with no
    reason: the caller does not classify why a directory stayed. Nothing but
    an index is ever echoed back, so no path can forge an answer.
    """
    lines = [marker_cmd(_REMOVE_MARK)]
    lines += [f'{rm_f_command(p)} 2>&1; echo "otto-staged-rm {i} $?"' for i, p in enumerate(paths)]
    if dirs:
        lines.append(rmdir_if_empty(*dirs, skip_sticky=True))
        for j, d in enumerate(dirs):
            q = path_operand(d)
            lines.append(f"if test -e {q} || test -L {q}; then echo 'otto-staged-dir {j} kept'; fi")
    return "\n".join(lines)


def probe_script(paths: Sequence[Path]) -> str:
    """Build the question: per path, ``absent``, ``present`` or ``unknown``.

    ``absent`` is :func:`~otto.host.product.cov_dir_absent_test`'s: the path
    is provably not there (it climbs to the nearest ancestor that exists, which
    must be searchable) and a final symlink counts as there, dangling or not,
    since ``rm -f`` removes it. ``present`` is a directory entry that can be
    seen. Anything else, an unsearchable parent above all, is ``unknown``.
    """
    lines = [marker_cmd(_PROBE_MARK)]
    for i, p in enumerate(paths):
        q = path_operand(p)
        lines.append(
            f"if {cov_dir_absent_test(str(p))}; then echo 'otto-staged {i} absent'; "
            f"elif test -e {q} || test -L {q}; then echo 'otto-staged {i} present'; "
            f"else echo 'otto-staged {i} unknown'; fi"
        )
    return "\n".join(lines)


def _parse_removal(
    output: str, count: int, dirs: int
) -> "tuple[list[str | None], list[int]] | None":
    """Read the removal's answer: each path's error (``None`` for removed) and the dirs kept.

    ``None`` unless the output after the marker is exactly one ``rm`` line per
    path, in order, each preceded by whatever ``rm`` printed (its reason), then
    the dirs kept, ascending. Text outside that shape is not attributable and
    fails the whole batch.
    """
    body = after_marker(output, _REMOVE_MARK)
    if body is None:
        return None
    errors: list[str | None] = []
    kept: list[int] = []
    said: list[str] = []
    for line in body.splitlines():
        if not line:
            continue
        if len(errors) < count:
            match = _RM_LINE.match(line)
            if match is None:
                said.append(line)
                continue
            if int(match["index"]) != len(errors):
                return None
            status = int(match["status"])
            reason = "\n".join(said).strip() or f"rm exited {status}"
            errors.append(None if status == 0 else reason)
            said = []
            continue
        match = _DIR_LINE.match(line)
        if (
            match is None
            or int(match["index"]) >= dirs
            or (kept and int(match["index"]) <= kept[-1])
        ):
            return None
        kept.append(int(match["index"]))
    return (errors, kept) if len(errors) == count else None


def _parse_probe(output: str, count: int) -> "list[str] | None":
    """Return each path's probe word, or ``None`` unless every path answered exactly once."""
    body = after_marker(output, _PROBE_MARK)
    if body is None:
        return None
    words: list[str] = []
    for line in body.splitlines():
        if not line:
            continue
        match = _PROBE_LINE.match(line)
        if match is None or int(match["index"]) != len(words):
            return None
        words.append(match["word"])
    return words if len(words) == count else None


async def _exec(host: "Host", script: str) -> "CommandResult":
    """Run *script* on *host*: unelevated, and as root on a container.

    A container's transfer lands the file as root (its own ``mkdir``,
    ``chmod`` and ``chown`` run with ``user=ROOT_LOGIN``), so the removal and
    the probe run as root there too; otherwise a root-owned stage directory
    would refuse the ``rm`` on every cleanup and the host would never read
    clean. Staging is a plain ``put`` as the login user on every other host.
    """
    from .docker_host import DockerContainerHost  # function-local: off the host import path
    from .host import exec_own  # function-local: off the host import path
    from .identity import ROOT_LOGIN

    if isinstance(host, DockerContainerHost):
        return await exec_own(host, script, timeout=_COMMAND_TIMEOUT, user=ROOT_LOGIN)
    return await exec_own(host, script, timeout=_COMMAND_TIMEOUT)


# ── the removal and the question ─────────────────────────────────────────────


async def remove_staged(host: "Host", items: "Iterable[Product | DevTool]") -> Result:
    """Remove each covered entry's staged file on *host*, then its declared dirs that are empty.

    One command (:func:`removal_script`), nothing sent when no entry is
    covered (:func:`staged_entries`). A file that exists and cannot be removed
    is an Error naming its path and the shell's reason; a path that is absent
    is success. A declared directory that is absent, not empty, or that the
    session cannot remove is kept, logged at INFO once, and never a failure.
    A login home that could not be discovered is an Error, never "nothing was
    staged". A dry run declines with :attr:`~otto.utils.Status.NotRun`, sending
    nothing.
    """
    from .errors import HostCommandError
    from .host import is_dry_run  # function-local: off the host import path

    covered = staged_entries(host, items)
    if not covered:
        return Result(status=Status.Success)
    if is_dry_run():
        return Result(status=Status.NotRun)
    try:
        paths = await staged_paths(host, covered)
    except HostCommandError as error:
        return Result(status=Status.Error, msg=str(error))
    dirs = declared_stage_dirs(covered)
    cmd = await _exec(host, removal_script(paths, dirs))
    if cmd.status is Status.NotRun:
        return Result(status=Status.NotRun)
    output, retcode = cmd.value or "", cmd.retcode
    parsed = None if gave_no_exit_status(cmd) else _parse_removal(output, len(paths), len(dirs))
    if parsed is None:
        detail = "timed out" if cmd.timed_out else failure_detail(output, retcode)
        return Result(
            status=Status.Error,
            msg=f"{host.id}: staged-file removal output was incomplete: {detail}",
        )
    errors, kept = parsed
    for index in kept:
        logger.info("stage directory %s kept: %s", dirs[index], _KEPT)
    problems = [f"{p}: {e}" for p, e in zip(paths, errors, strict=True) if e is not None]
    if not problems:
        return Result(status=Status.Success)
    return Result(
        status=Status.Error,
        msg=f"{host.id}: cleanup left staged files: " + "; ".join(problems),
    )


async def staged_files_left(host: "Host", items: "Iterable[Product | DevTool]") -> list[Path]:
    """Return the covered staged files of *items* that still have a directory entry on *host*.

    Empty means every one is confirmed absent. Nothing is sent when no entry
    is covered.

    Raises:
        ~otto.result.CommandNotRunError: a dry run, which never answers.
        ~otto.host.errors.HostCommandError: a path could not be looked at (an
            unsearchable parent), the probe did not answer every path, or the
            login home could not be discovered or two entries collide in it.
    """
    from ..result import CommandNotRunError
    from .errors import HostCommandError
    from .host import is_dry_run  # function-local: off the host import path

    covered = staged_entries(host, items)
    if not covered:
        return []
    if is_dry_run():
        raise CommandNotRunError(f"{host.id}.staged_files_absent()", host=str(host.id))
    paths = await staged_paths(host, covered)
    cmd = await _exec(host, probe_script(paths))
    words = None if cmd.timed_out else _parse_probe(cmd.value or "", len(paths))
    if words is None:
        raise HostCommandError(
            f"{host.id}: cannot tell whether the staged files are gone: "
            f"{'timed out' if cmd.timed_out else failure_detail(cmd.value or '', cmd.retcode)}"
        )
    unknown = [p for p, word in zip(paths, words, strict=True) if word == "unknown"]
    if unknown:
        raise HostCommandError(
            f"{host.id}: cannot tell whether {', '.join(map(str, unknown))} is gone: "
            "a directory above it cannot be searched"
        )
    return [p for p, word in zip(paths, words, strict=True) if word == "present"]
