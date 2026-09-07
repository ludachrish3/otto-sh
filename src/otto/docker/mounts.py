"""Deriving a container's mount table from the docker daemon.

Everything that knows docker's JSON shape lives here; :mod:`otto.host.mount`
stays pure. The command is issued ONCE per stack rather than per container:
:func:`~otto.docker.compose.register_stack_hosts` already spends a round
trip per service resolving ids, and this must not double that.
"""

import json
import logging
import re
import shlex
from pathlib import Path

from ..host.host import Host, refuse_declined_fact
from ..host.mount import Mount

logger = logging.getLogger(__name__)

INSPECT_FORMAT = "{{.Id}}\t{{json .Mounts}}"
"""Emits one line per container -- the full id, a tab, then the Mounts array."""

_LINE = re.compile(r"^([0-9a-f]{64})\t(.*)$")
"""What a data line looks like. Everything else on the stream is docker's
own diagnostics -- otto merges stderr into stdout, so `error: no such
object: X` arrives interleaved with the rows and must be walked past
rather than parsed."""


def _mount_from_entry(entry: "dict[str, object]") -> "Mount | None":
    """Build one :class:`Mount` from a docker Mounts element, or drop it."""
    source = entry.get("Source") or ""
    destination = entry.get("Destination") or ""
    if not isinstance(source, str) or not isinstance(destination, str):
        return None
    if not source or not destination:
        # A tmpfs declared through the mount API reports an empty Source.
        # There is no parent-side path, so the honest answer to "is this
        # shared with the parent?" is no -- dropping it is what makes
        # mount_for() return None rather than a path that does not exist.
        return None
    name = entry.get("Name")
    return Mount(
        container_path=Path(destination),
        parent_path=Path(source),
        kind=str(entry.get("Type") or "bind"),
        name=name if isinstance(name, str) and name else None,
        read_only=entry.get("RW") is False,
    )


def parse_inspect_output(text: str) -> "dict[str, list[Mount]]":
    """Parse ``docker inspect`` output into full-id -> mounts.

    Tolerant by construction: a line that is not a data row is skipped, and
    so is a row whose payload will not parse. The alternative -- refusing
    the whole batch -- would throw away every good row because one
    container disappeared or one diagnostic landed mid-stream.
    """
    table: "dict[str, list[Mount]]" = {}
    for line in text.splitlines():
        matched = _LINE.match(line.rstrip())
        if not matched:
            continue
        container_id, payload = matched.group(1), matched.group(2)
        try:
            entries = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if entries is None:
            table[container_id] = []
            continue
        if not isinstance(entries, list):
            continue
        table[container_id] = [
            mount
            for mount in (_mount_from_entry(e) for e in entries if isinstance(e, dict))
            if mount is not None
        ]
    return table


async def inspect_mounts(parent: Host, container_ids: "list[str]") -> "dict[str, list[Mount]]":
    """Return mount tables for *container_ids*, keyed by the ids the CALLER passed.

    Keyed by the caller's ids, not docker's: the caller holds short ids from
    ``docker compose ps -q`` and would otherwise have to re-derive the
    mapping. An id that produced no row gets an empty list and a warning
    naming it.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run. The return type
            cannot carry "I did not look", and an empty table here would be
            indistinguishable from a stack that shares nothing.
    """
    if not container_ids:
        return {}
    joined = " ".join(shlex.quote(cid) for cid in container_ids)
    # --type container: ids come from `docker ps -q` so a container always
    # exists, but plain `inspect` falls back to images on a miss and
    # `{{json .Mounts}}` against an image errors rather than producing a
    # row -- pinning the type makes a miss "no such container" instead.
    result = await parent.exec(
        f"docker inspect --type container --format {shlex.quote(INSPECT_FORMAT)} {joined}"
    )
    refuse_declined_fact(result, asked=f"inspect_mounts({len(container_ids)} container(s))")

    # NOT gated on result.status: a batch naming one container that has since
    # been reaped exits non-zero while still printing every other row, and
    # discarding those would lose the whole stack's mapping over one absence.
    by_full_id = parse_inspect_output(result.value or "")

    table: "dict[str, list[Mount]]" = {}
    unanswered: "list[str]" = []
    for cid in container_ids:
        found = by_full_id.get(cid)
        if found is None:
            found = next(
                (mounts for full, mounts in by_full_id.items() if cid and full.startswith(cid)),
                None,
            )
        if found is None:
            unanswered.append(cid)
            table[cid] = []
        else:
            # Copy: `found` is the SAME list object `by_full_id` holds, and a
            # short id and its full-id twin would otherwise both point at it
            # -- Mount is frozen so this is latent today, but a future
            # in-place mutation by one caller must not reach the other.
            table[cid] = list(found)
    if unanswered:
        host_name: str = parent.id
        logger.warning(
            rf"\[docker] could not read mounts for "
            rf"{', '.join(c[:12] for c in unanswered)} on {host_name}; "
            rf"path translation will be unavailable for those containers"
        )
    return table
