"""``SnapshotCache`` — a TTL snapshot of a remote inventory under ``~/.otto`` (spec §9.5).

A snapshot younger than the TTL is served WITHOUT contacting the backend, so
the ordinary otto invocation costs one file read; an older one triggers a fetch
and an atomic rewrite; a backend that fails with a snapshot on disk serves the
snapshot and WARNS with its age, unless :func:`stale_refusal` refuses it: a
data error never falls back, an unreachable backend only within its
``max_stale``, and a plain :class:`~otto.inventory.errors.InventoryError`
(NetBox's) at any age. A lab that loaded yesterday should load today, and the
warning is what keeps the staleness visible rather than silent.

The cache also gives a remote backend the ``fingerprint()`` it could not
produce on its own — the snapshot's content hash — which is what lets shell
completion cache normally against NetBox (§11). That method runs whenever the
completion cache is validated or written — on every TAB the console-script shim
hands over, on root ``otto --help``, and in the commands that record into the
cache (``otto.config.completion_cache`` resolves the inventory and calls this
twice per such invocation) — so it answers from the two files on disk and
never, ever fetches — nor lies about what they hold.

Construction does no I/O, the rule for every inventory object: the paths are
computed and nothing is opened until the first ``lookup``/``list_keys``.
"""

import contextlib
import dataclasses
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from typing_extensions import final

from ..host.inventory_ref import DEFAULT_INVENTORY
from ..models.base import secret_safe_json_default
from ..models.inventory import InventoryRecord
from .creds import below_creds_overlay
from .discovery import Discovery, discover_inventory
from .errors import (
    InventoryDataError,
    InventoryError,
    InventoryKeyError,
    InventoryUnreachableError,
)
from .protocol import Inventory
from .snapshot import (
    discovery_to_document,
    document_hash,
    document_to_discovery,
    write_document_atomically,
)

logger = logging.getLogger(__name__)

_SLUG_CHARS = 16
"""Characters of the identity digest used as a filename. Sixteen hex characters
is 64 bits: legible in an ``ls``, and far past collision for a handful of
inventories per user."""

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_HOURS_PER_DAY = 24
_HOURS_UNTIL_DAYS = 48
"""Where :func:`format_age` stops counting hours and starts counting days."""

_warned_snapshots: set[str] = set()
"""Snapshots whose staleness — or write failure — this PROCESS has already reported.

Keyed by path rather than by cache object because one otto command builds the
inventory several times over — bootstrap resolves it for the load, and the
completion-cache writer resolves it again to enumerate hosts, each building
its own :class:`SnapshotCache` — and an operator should see "your NetBox is
down" (or "cannot write the snapshot") once per command, not once per
resolution. Cleared for a snapshot by a successful fetch or write
(:meth:`SnapshotCache.refresh` included), so the next outage warns again.

Two kinds of entry share this one set so :func:`reset_stale_warnings` stays
the single reset point: the stale-snapshot warning keys on the bare path
(``str(snapshot_path)``, see :meth:`SnapshotCache._warn_stale`), the
write-failure warning keys on ``f"write:{snapshot_path}"``
(:meth:`SnapshotCache._fetch_and_write`) — a prefix no real path can collide
with, since snapshot paths are otto-chosen hash filenames.

Bounded by the number of configured inventories a process resolves — two
entries per distinct snapshot path at most, and §8 gives a process exactly one
inventory — so this never grows with the number of lookups.
:func:`reset_stale_warnings` clears it; the test suite does so between tests.
"""


def reset_stale_warnings() -> None:
    """Forget which snapshots this process has already warned about.

    Exists for the test suite (an autouse fixture in the root ``conftest``
    calls it), which must not let one test's outage silence another's. A
    long-running host process that wants the warning repeated after it has
    dealt with an outage should call :meth:`SnapshotCache.refresh`, which
    clears the entry for that snapshot on success.
    """
    _warned_snapshots.clear()


def _utc_now() -> datetime:
    """Return now, in UTC. Injectable so a test pins an age without sleeping."""
    return datetime.now(timezone.utc)


def format_age(age: timedelta) -> str:
    """``45m``, ``31h``, ``3d 7h`` — how the stale-snapshot warning states an age.

    Three arms, coarsening as the number grows. Under an hour, minutes. Under
    two days, HOURS — ``31h``, not ``1d 7h``, because ``cache_ttl`` is written
    in hours and the operator reading the warning is comparing the two. Beyond
    that, days and hours, because nobody counts a week in hours.
    """
    total = max(int(age.total_seconds()), 0)
    hours, remainder = divmod(total, _SECONDS_PER_HOUR)
    if hours < 1:
        return f"{remainder // _SECONDS_PER_MINUTE}m"
    if hours < _HOURS_UNTIL_DAYS:
        return f"{hours}h"
    days, spare_hours = divmod(hours, _HOURS_PER_DAY)
    return f"{days}d {spare_hours}h"


SNAPSHOT_POLICY_KEYS: frozenset[str] = frozenset({"max_stale"})
"""Backend keys that are POLICY rather than data, and so never part of a snapshot's identity.

``max_stale`` says how long an existing snapshot may be served during an
outage; it does not change what a fetch returns. Keyed into the slug, raising
it during an outage (the moment an operator reaches for it) would look for a
new, empty snapshot and orphan the one that could have been served.
"""


def snapshot_slug_material(
    backend: str, label: str, normalized: "Mapping[str, Any]", *, cache_ttl: timedelta
) -> str:
    """Return the identity a snapshot is keyed by (§9.5).

    The inputs are the ones :meth:`~otto.inventory.config.CompiledInventory.same_as`
    compares: the backend, its NORMALIZED configuration (what its config
    model dumped, a :class:`~otto.registry.FrozenMap` thawed) and
    ``cache_ttl`` (``|ttl=<whole seconds>``). Never where the table was
    declared: two projects naming one inventory share its snapshot.

    Two kinds of key are excluded from the configuration, each so an
    unchanged table keeps its snapshot:

    - DATA keys at their default, left out by the backend's config model
      itself (NetBox's ``virtual_machines``, ``vm_filter`` and
      ``vm_ip_source``), so a table written before the key existed keeps the
      slug it had; set away from the default, they are data and separate two
      configurations.
    - POLICY keys (:data:`SNAPSHOT_POLICY_KEYS`, ``max_stale``) at EVERY
      value, dropped here: they decide whether a snapshot may be served, not
      what it holds. A configuration without ``max_stale`` keeps today's slug,
      and changing ``max_stale`` keeps the snapshot.
      :meth:`~otto.inventory.config.CompiledInventory.same_as` still compares
      them, so two declarations of one inventory must agree on them.

    The backend's own ``label`` stands in for a key the backend adopted
    verbatim, because a backend NORMALISES its identity and a configuration
    does not: ``url = "https://nb/"`` and ``url = "https://nb"`` are one
    NetBox, and two spellings of one inventory must not keep two snapshots —
    each refreshing on its own schedule, neither ever seeing the other's
    fetch. Such a key is dropped rather than re-added raw; the match is exact
    (``<backend>:<value>``, trailing slashes aside) so a coincidentally-similar
    value cannot silently merge two configurations.

    Everything else stays: filter, ip_source, custom-field mappings and token
    variable all separate two configurations of the SAME server, which §9.5
    requires them to.
    """
    thaw = getattr(normalized, "thaw_json", None)
    config: dict[str, Any] = thaw() if callable(thaw) else dict(normalized)
    adopted = {
        key
        for key, value in config.items()
        if isinstance(value, str) and label == f"{backend}:{value.rstrip('/')}"
    }
    residue = {
        key: value
        for key, value in config.items()
        if key not in adopted and key not in SNAPSHOT_POLICY_KEYS
    }
    ttl = int(cache_ttl.total_seconds())
    from ..config.home import secret_digest_key  # function-local, as config.py's use is

    hook = secret_safe_json_default(secret_digest_key)
    dumped = json.dumps(residue, sort_keys=True, default=hook)
    return f"{backend}|{label}|{dumped}|ttl={ttl}"


def _snapshot_phrase(*, fetched_at: datetime, age: timedelta) -> str:
    """``from 2026-08-27 09:14 UTC, 31h old``: how every stale message states a snapshot's age.

    One phrase for the stale notice ("using cached snapshot from …") and for
    every refusal ("the cached snapshot (from …) is not served"), so an
    operator reads the same fetch time and age whichever way the cache decided.
    """
    return f"from {fetched_at:%Y-%m-%d %H:%M UTC}, {format_age(age)} old"


def refresh_remedy(*, inventory_name: str = DEFAULT_INVENTORY) -> str:
    """Return the command that replaces *inventory_name*'s snapshot, quoted for a message.

    The one remedy every stale notice and refusal names: ```otto inventory
    refresh``` for ``default``, with ``--inventory <name>`` for any other.
    """
    suffix = "" if inventory_name == DEFAULT_INVENTORY else f" --inventory {inventory_name}"
    return f"`otto inventory refresh{suffix}`"


def stale_refusal(
    *,
    error: InventoryError,
    label: str,
    fetched_at: datetime,
    age: timedelta,
    inventory_name: str = DEFAULT_INVENTORY,
) -> "InventoryError | None":
    """Decide whether an expired snapshot may stand in for a failed fetch (lab templating [D2]).

    The one place the decision is made. *error* is what the backend raised;
    *fetched_at* and *age* describe the snapshot on disk. Returns ``None`` to
    serve the snapshot (the caller then records the stale notice), or a NEW
    error for the caller to raise ``from`` *error*:

    - :class:`~otto.inventory.errors.InventoryDataError`: always refused, a
      new ``InventoryDataError``. The source answered wrongly, and data nobody
      can vouch for must not run a lab.
    - :class:`~otto.inventory.errors.InventoryUnreachableError`: served while
      its ``max_stale`` is set and *age* is at most it (inclusive), otherwise
      refused, a new ``InventoryUnreachableError`` keeping ``max_stale``.
      *age* is the snapshot's total age, not its age past the TTL.
    - Any other :class:`~otto.inventory.errors.InventoryError` (NetBox's, and
      every backend written before the typed errors): served at any age, as
      before.

    A refusal names *label* and the snapshot (``_snapshot_phrase``), so
    the age is shown whichever way the decision goes, and the command that
    refreshes *inventory_name* (:func:`refresh_remedy`). The refusal is of the
    D2 base class, never a backend's subclass, whose constructor this cannot
    know.
    """
    phrase = _snapshot_phrase(fetched_at=fetched_at, age=age)
    remedy = refresh_remedy(inventory_name=inventory_name)
    if isinstance(error, InventoryDataError):
        return InventoryDataError(
            f"inventory '{label}' returned unusable data ({error}); the cached snapshot "
            f"({phrase}) is not served for a data error — fix the source, then run {remedy}"
        )
    if not isinstance(error, InventoryUnreachableError):
        return None
    limit = error.max_stale
    if limit is not None and age <= limit:
        return None
    why = (
        "no max_stale is set in its inventory table"
        if limit is None
        else f"{format_age(age)} is past the max_stale of {format_age(limit)} in its "
        "inventory table"
    )
    return InventoryUnreachableError(
        f"inventory '{label}' unreachable ({error}); the cached snapshot ({phrase}) is not "
        f"served, because {why} — run {remedy} once the inventory is reachable",
        max_stale=limit,
    )


@final
@dataclass(frozen=True)
class RefreshResult:
    """What ``otto inventory refresh`` reports: the replaced snapshot's age, and the new size."""

    previous_age: "timedelta | None"
    """Age of the snapshot this refresh replaced, or ``None`` if there was none."""

    count: int
    """Records in the freshly fetched snapshot."""


def content_digest(path: Path) -> str:
    """sha256 of a file's RAW BYTES — what ties a meta to the snapshot beside it.

    Distinct from :func:`~otto.inventory.snapshot.document_hash`, which hashes
    the CANONICAL form and is the fingerprint's actual value: two files that
    differ only in indentation share a document hash, and a meta must be able
    to tell them apart to know whether it is describing this file or the one
    that used to be here.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class _SnapshotMeta:
    """The sidecar's facts, all ``None`` when there is no readable meta file.

    ``content_sha256`` is the snapshot file's raw-byte digest as it stood when
    this meta was written. It exists so :meth:`SnapshotCache.fingerprint` can
    tell a meta that DESCRIBES the snapshot beside it from one that merely sits
    next to it — see that method for what goes wrong without it.
    """

    fetched_at: "datetime | None"
    sha256: "str | None"
    content_sha256: "str | None"

    def describes(self, digest: str) -> bool:
        """Whether this meta was written for a snapshot with exactly this content."""
        return self.sha256 is not None and self.content_sha256 == digest


_NO_META = _SnapshotMeta(fetched_at=None, sha256=None, content_sha256=None)
"""The answer for every way a meta file can be unreadable: "there is no snapshot"."""


@dataclass(frozen=True)
class _StoredSnapshot:
    """A snapshot that is on disk AND readable, with its age at the moment it was read."""

    discovery: Discovery
    fetched_at: datetime
    age: timedelta


@final
class SnapshotCache:
    """Wrap *inner* so a process pays one fetch per TTL window.

    Parameters
    ----------
    inner : Inventory
        The backend to cache. Its ``label`` and ``supplies`` are adopted
        unchanged — a cache is not a different inventory, it is the same one
        answering from a file.
    ttl : timedelta
        How long a snapshot is served without contacting *inner*.
    cache_dir : Path
        Directory the snapshot and its meta file live in
        (:func:`~otto.config.home.snapshot_cache_dir` for the real one).
    slug_material : str
        The configuration's identity, hashed into the filename
        (:func:`snapshot_slug_material`).
    clock : Callable[[], datetime] | None
        Injectable "now", so a test pins an age instead of sleeping. Must
        return an AWARE datetime; the default is UTC.
    inventory_name : str
        The inventory's name, for the refresh command a stale notice or
        refusal names (:func:`refresh_remedy`); ``default`` when omitted.
    """

    def __init__(
        self,
        inner: Inventory,
        *,
        ttl: timedelta,
        cache_dir: Path,
        slug_material: str,
        clock: "Callable[[], datetime] | None" = None,
        inventory_name: str = DEFAULT_INVENTORY,
    ) -> None:
        self.inner = inner
        self._inventory_name = inventory_name
        self.label = inner.label
        self.supplies = frozenset(inner.supplies)
        self._ttl = ttl
        self._clock = clock if clock is not None else _utc_now
        slug = hashlib.sha256(slug_material.encode()).hexdigest()[:_SLUG_CHARS]
        self.snapshot_path = Path(cache_dir) / f"{slug}.json"
        self.meta_path = Path(cache_dir) / f"{slug}.meta.json"
        self._discovery: "Discovery | None" = None
        self._hash: "str | None" = None
        self.stale_notice: "str | None" = None
        """Set when this object served a snapshot because the backend was unreachable.

        The same sentence ``_warn_stale`` logs, kept as READABLE STATE so a
        caller can report the outage in its own output instead of hoping a log
        line was heard. Since root capture (spec 2026-08-30 §3.1) the CLI's
        console handler is up from the root callback, so the log line is no
        longer swallowed on ``lab_free`` groups — but the DEDUP is what makes
        this state load-bearing anyway: ``_warn_stale`` logs once per snapshot
        per PROCESS, and the first resolution can happen in ``entry()``'s
        completion-cache write, before any handler exists. The verb's own
        resolution is then the deduped one, and ``list`` / ``export`` / ``diff``
        would answer from a stale snapshot in silence. ``export`` would write a
        stale artefact and ``diff`` report "no differences" against a stale left
        side, which is the one thing the transition gate exists to prevent.

        ``None`` until it happens, and cleared again by a successful fetch
        (:meth:`refresh` included). Read-only to callers.
        """

    # -- snapshot files ----------------------------------------------------
    def _meta(self) -> _SnapshotMeta:
        """Read the sidecar; every way it can be unreadable means "no snapshot"."""
        try:
            data = json.loads(self.meta_path.read_text())
            fetched_at = datetime.fromisoformat(data["fetched_at"])
            sha256 = data.get("sha256")
            content = data.get("content_sha256")
        except (OSError, ValueError, KeyError, TypeError):
            # ValueError covers json.JSONDecodeError and a malformed timestamp;
            # KeyError/TypeError a meta file that is valid JSON of the wrong shape.
            return _NO_META
        if fetched_at.tzinfo is None:
            # `fromisoformat` accepts a NAIVE timestamp, which every reader
            # here then subtracts from an aware "now" — a TypeError raised
            # outside `_stored`'s guard, failing every load until somebody
            # deletes the file by hand. A timestamp otto cannot measure an age
            # against describes no snapshot, exactly like a malformed one.
            return _NO_META
        return _SnapshotMeta(
            fetched_at=fetched_at,
            sha256=sha256 if isinstance(sha256, str) else None,
            # A meta written by an older otto carries no content digest, and so
            # reads as "describes no snapshot": one refetch, then it is repaired.
            content_sha256=content if isinstance(content, str) else None,
        )

    def _write_meta(self, fetched_at: datetime, digest: str, content: str) -> None:
        """Write the sidecar. BOTH digests are the caller's, from ONE read of the bytes.

        Deliberately does no I/O on the snapshot. Reading it here to compute
        *content* would make the meta's two digests come from two separate
        reads, and a concurrent writer's rename landing between them yields a
        meta whose ``sha256`` and ``content_sha256`` describe DIFFERENT
        content: :meth:`_SnapshotMeta.describes` then passes and
        :meth:`fingerprint` hands §11 a hash for records that are not there,
        for a whole TTL. Every caller has the bytes already — one has just
        parsed them, the other has just written them — so there is nothing to
        buy by re-reading and a correctness hole to pay for.
        """
        write_document_atomically(
            self.meta_path,
            {
                "fetched_at": fetched_at.isoformat(),
                "sha256": digest,
                "content_sha256": content,
            },
        )

    def _stored(self, now: datetime) -> "_StoredSnapshot | None":
        """Return the snapshot on disk, or ``None`` when there is not a readable one.

        A corrupt, truncated or unparseable snapshot reads as ABSENT rather
        than as an error. Everything under otto's home (``~/.otto``) is derived
        state otto must be able to rebuild without complaint, and a
        file left behind by a killed process should cost one refetch — not
        every command from now until somebody deletes it by hand.

        This is also where a meta that has fallen out of step with its snapshot
        is REPAIRED, so the first real resolution after a torn write costs one
        meta rewrite and :meth:`fingerprint` starts answering again. Both
        digests come from the ONE read below — see :meth:`_write_meta` for why
        that matters. A snapshot already past its TTL is not repaired: the
        caller is about to refetch and rewrite both files, so the work would be
        thrown away unread.
        """
        meta = self._meta()
        if meta.fetched_at is None or not self.snapshot_path.is_file():
            return None
        try:
            raw = self.snapshot_path.read_bytes()
            data: Any = json.loads(raw)
            discovery = document_to_discovery(
                data=data, source=str(self.snapshot_path), supplies=self.supplies
            )
        except (OSError, ValueError, InventoryError) as e:
            logger.debug("%s: unusable snapshot (%s); refetching", self.snapshot_path, e)
            return None
        self._hash = document_hash(data)
        content = hashlib.sha256(raw).hexdigest()
        age = now - meta.fetched_at
        if age < self._ttl and (meta.sha256 != self._hash or not meta.describes(content)):
            # Best-effort, like every write under otto's home: the records are
            # already parsed and about to be served, and a READ must not fail
            # because a repair could not be written (an unwritable cache
            # directory, a full filesystem). The next resolution tries again;
            # until then `fingerprint()` answers `None`, which is honest.
            with contextlib.suppress(OSError):
                self._write_meta(meta.fetched_at, self._hash, content)
        return _StoredSnapshot(discovery=discovery, fetched_at=meta.fetched_at, age=age)

    def _fetch_and_write(self, *, write_required: bool = False) -> Discovery:
        """Discover *inner* whole, write the snapshot, then the meta beside it.

        Discovery, not a key walk of its own: a backend that declares instances
        is asked through its capability, so the snapshot keeps them, and every
        other backend is walked exactly as before
        (:func:`~otto.inventory.discovery.discover_inventory`), its snapshot
        the same flat document byte for byte
        (:func:`~otto.inventory.snapshot.discovery_to_document`).

        Snapshot first: a meta file naming a snapshot that does not exist yet
        would let a concurrent reader believe in one, whereas a snapshot with
        no meta reads as "no snapshot" and simply refetches.

        The meta's content digest is taken from the bytes ``write_document_
        atomically`` reports having written, never by reading the path back —
        see :meth:`_write_meta`.

        A FAILED WRITE IS NOT A FAILED LOOKUP. Everything under otto's home is
        derived state (§9.5), so on the read path an unwritable cache
        directory warns and the fetched records are served anyway — otherwise
        the lab load fails ("Lab file …: PermissionError: …") and every
        ``otto inventory`` verb tracebacks over a directory otto only uses to
        go faster. ``self._hash`` is left as the fetched document's digest, so
        THIS process's ``fingerprint()`` still describes the records it is
        serving; a later process, finding nothing on disk, gets ``None``.

        *write_required* flips that for :meth:`refresh`, where the user asked
        for the write and a warning they cannot see is not an answer.
        """
        discovery = discover_inventory(inventory=self.inner)
        doc = discovery_to_document(discovery=discovery)
        self._hash = document_hash(doc)
        write_key = f"write:{self.snapshot_path}"
        try:
            written = write_document_atomically(self.snapshot_path, doc)
            self._write_meta(self._clock(), self._hash, hashlib.sha256(written).hexdigest())
        except OSError as e:
            if write_required:
                raise InventoryError(
                    f"could not write inventory snapshot {self.snapshot_path}: {e}"
                ) from e
            # Deduped through `_warned_snapshots`, same as `_warn_stale` below:
            # otherwise every otto command logs this once per SnapshotCache it
            # builds (bootstrap's resolution, the completion-cache writer's) —
            # once per process is the promise.
            if write_key not in _warned_snapshots:
                _warned_snapshots.add(write_key)
                logger.warning(
                    "could not write inventory snapshot %s: %s; serving the fetched records "
                    "without caching",
                    self.snapshot_path,
                    e,
                )
        else:
            # The write succeeded, so a later failure is a new outage worth a
            # fresh warning.
            _warned_snapshots.discard(write_key)
        # The fetch itself succeeded, whatever the cache write did: the outage
        # (if there was one) is over, so the memo and the notice both clear.
        _warned_snapshots.discard(str(self.snapshot_path))
        # These records came off the wire, so nothing about them is stale — and
        # a notice left standing would have `otto inventory refresh` reporting
        # the outage it just cleared.
        self.stale_notice = None
        return discovery

    def _warn_stale(self, stored: _StoredSnapshot, error: InventoryError) -> None:
        """Record an unreachable backend on this object, and log it once per process.

        TWO SINKS, ONE SENTENCE, AND ONLY THE LOG IS DEDUPED. The dedup exists
        so an operator sees "your NetBox is down" once per command however many
        times the process resolves the inventory — but ``stale_notice`` is
        per-object state a caller READS, and suppressing it on the second
        resolution would hand that caller ``None`` for a snapshot it is in fact
        serving stale. That is the silent-staleness bug one layer down: the
        second resolution is the one the CLI verb holds.
        """
        phrase = _snapshot_phrase(fetched_at=stored.fetched_at, age=stored.age)
        remedy = refresh_remedy(inventory_name=self._inventory_name)
        message = (
            f"inventory '{self.label}' unreachable ({error}): using cached snapshot {phrase} — run "
            f"{remedy} to replace it once the inventory is reachable"
        )
        self.stale_notice = message
        key = str(self.snapshot_path)
        if key in _warned_snapshots:
            return
        _warned_snapshots.add(key)
        logger.warning(message)

    def _ensure(self) -> Discovery:
        """Return this process's discovery: fresh snapshot, a fetch, or stale snapshot + warning.

        A failed fetch falls back to the stored snapshot only when
        :func:`stale_refusal` allows it; a refusal is raised from the
        backend's error, and nothing is memoized, so the next call tries again.
        """
        if self._discovery is not None:
            return self._discovery
        now = self._clock()
        stored = self._stored(now)
        if stored is not None and stored.age < self._ttl:
            self._discovery = stored.discovery
            return self._discovery
        try:
            self._discovery = self._fetch_and_write()
        except InventoryError as e:
            if stored is None:
                raise  # nothing to fall back on: the caller's error is the real one
            # The age when the fetch gave up, not when it began: a slow
            # timeout must not serve a snapshot that has meanwhile passed
            # its max_stale. The decision and the notice read this one age.
            stored = dataclasses.replace(stored, age=self._clock() - stored.fetched_at)
            refusal = stale_refusal(
                error=e,
                label=self.label,
                fetched_at=stored.fetched_at,
                age=stored.age,
                inventory_name=self._inventory_name,
            )
            if refusal is not None:
                raise refusal from e
            self._warn_stale(stored, e)
            self._discovery = stored.discovery
        return self._discovery

    # -- protocol ----------------------------------------------------------
    def inventory_discovery(self) -> Discovery:
        """Return every record and the instances, resolving the snapshot on the first call.

        The same answer the backend gives (its instances, or ``None`` when it
        has no notion of them), whether it came off the wire or out of the
        snapshot.
        """
        return self._ensure()

    def lookup(self, key: str) -> InventoryRecord:
        """Return the record for *key*, resolving the snapshot on the first call."""
        records = self._ensure().records
        try:
            return records[key]
        except KeyError:
            raise InventoryKeyError(key, label=self.label) from None

    def list_keys(self) -> list[str]:
        """Every key the snapshot holds, sorted; resolves it on the first call."""
        return sorted(self._ensure().records)

    def fingerprint(self) -> "str | None":
        """Return the stored snapshot's content hash, or ``None`` when it cannot be trusted.

        NEVER fetches, and never even parses the snapshot: this runs on every
        otto command through the completion cache (§11), so a network probe
        here would put the inventory service on the path of ``otto --help``.

        It must also never LIE, which is why the meta's hash alone is not
        enough. The snapshot and its meta are two files written one after the
        other, and the pair can be torn: a crash between the two writes, two
        processes interleaving their renames, or an operator hand-copying a
        snapshot in — which the document format positively invites, being the
        same stage-1 file the ``json`` backend reads. A meta left describing
        the PREVIOUS content would hand §11 a digest that says "unchanged"
        while the records underneath it are different, and completion would
        serve the stale answer for a whole TTL.

        So the meta also records the snapshot's RAW-BYTE digest, and this
        returns the fingerprint only when the file still hashes to it.
        Otherwise ``None`` — the honest answer, which
        ``otto.config.completion_cache`` reads as "not cacheable" and which the
        next real resolution repairs, as long as the snapshot is still inside
        its TTL (past it, the refetch rewrites both files). ``None`` before the
        first fetch is the same answer for the same reason.

        Hashing rather than comparing ``st_size``/``st_mtime_ns``, which is
        cheaper and was tried first: MEASURED on this project's dev filesystem,
        46 of 50 back-to-back same-size rewrites shared an ``st_mtime_ns``, so
        the stat pair is blind to exactly the case that matters — a snapshot
        replaced in place by a same-length document while a second process was
        mid-write. The read it costs was measured too: 0.76 ms for a 1.7 MB,
        5000-device snapshot, against a stat's 0.001 ms. Under a millisecond,
        twice per command, to make a cache key that cannot lie.
        """
        if self._hash is not None:
            return self._hash
        meta = self._meta()
        try:
            digest = content_digest(self.snapshot_path)
        except OSError:
            return None
        return meta.sha256 if meta.describes(digest) else None

    def refresh(self) -> RefreshResult:
        """Fetch unconditionally and rewrite the snapshot (``otto inventory refresh``).

        Always contacts the backend, whatever the TTL says and whatever this
        object has already resolved — the verb exists precisely for the moment
        the operator knows better than the TTL.

        Unlike the read path, a cache write that fails here is an
        :class:`~otto.inventory.errors.InventoryError`: this verb's whole
        product is the replaced snapshot, and ``otto inventory refresh && …``
        must not proceed as though one had been written.
        """
        meta = self._meta()
        previous = self._clock() - meta.fetched_at if meta.fetched_at is not None else None
        self._discovery = self._fetch_and_write(write_required=True)
        return RefreshResult(previous_age=previous, count=len(self._discovery.records))


def snapshot_cache_of(*, inventory: Inventory) -> "SnapshotCache | None":
    """Return the :class:`SnapshotCache` inside *inventory*, or ``None`` if it is uncached.

    The cache, when there is one, sits directly below the creds overlays
    (:func:`~otto.inventory.creds.below_creds_overlay`, the one peel).

    Public because ``SnapshotCache.stale_notice`` has to reach surfaces that
    report staleness in their own output — ``otto inventory``'s table row and
    ``otto init``'s prompt — rather than leaving it to a log line that may
    already have been spent. ``_warn_stale`` logs once per snapshot per
    process, and the first resolution can be ``entry()``'s completion-cache
    write, which runs before the root callback installs a console handler; both
    verbs would then report success against a snapshot days old.
    """
    inner = below_creds_overlay(inventory=inventory)
    return inner if isinstance(inner, SnapshotCache) else None
