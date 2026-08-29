"""``otto inventory`` — read-only helpers over the configured host inventory (spec §11).

- ``lookup KEY`` / ``list`` — debug the join without editing a lab file.
- ``export PATH`` — write the inventory as a stage-1 JSON file (no creds).
- ``diff PATH [OTHER]`` — compare the inventory (or a second file) against a
  stage-1 file; exit 1 on any difference.
- ``refresh`` — force a fetch of a cached remote inventory.

Lab-free, and settings-only: §8 resolves the process inventory from the active
repos' ``[inventory]`` tables and the user settings file, and from nothing
else — no lab, no root option and no host reaches it. That is why the verbs
below take no ``typer.Context``: unlike ``otto reservation whoami``, which
reads ``--as-user``/``-R`` off the root options, there is nothing on the
context an inventory answer depends on.

IMPORT DISCIPLINE: everything from :mod:`otto.inventory` and
``otto.models`` is imported INSIDE the verb that uses it. This module is
reachable from ``otto --help`` (the root group resolves a spec's loader on
dispatch, and the completion cache walks the registry), and the budgeted
import surfaces in ``scripts/import_budget.py`` cap what a bare ``--help``
may pull in. The edges are real and declared in ``tach.toml``; only the
*timing* is deferred.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich import box, get_console
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from .invoke import fail

if TYPE_CHECKING:
    from ..inventory import Inventory
    from ..models.inventory import InventoryRecord

inventory_app = typer.Typer(
    name="inventory",
    no_args_is_help=True,
    help="Inspect, export and diff the configured host inventory.",
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@inventory_app.callback()
def inventory_callback(ctx: typer.Context) -> None:
    """Inspect, export and diff the configured host inventory.

    Every verb is read-only and touches no remote host, so this command
    creates no per-invocation output directory.
    """
    if ctx.resilient_parsing:
        return


def _row(text: str) -> None:
    """Print one detail row verbatim: no markup parsing, no width wrapping.

    Same treatment as ``otto.cli.link._row``, for the same reason: every field
    on these rows is user-supplied (an inventory key, a backend label, a
    filesystem path), and rich would read ``inventory[old].json`` as a style
    tag and print ``inventory.json`` — naming a file that does not exist.
    Folding is just as damaging here: a path broken mid-line is a path the
    reader cannot paste.
    """
    get_console().print(text, markup=False, soft_wrap=True)


def _no_inventory_message() -> str:
    """Return the §8 "nothing declared anywhere" error, naming BOTH places it could have been.

    The RESOLVED user file rather than the ``~/.otto/settings.toml`` spelling
    the load path uses: ``OTTO_HOME`` relocates it wholesale, and a message
    naming a file the reader does not have sends them to edit the wrong one.
    """
    from ..config.user_settings import user_settings_path

    return (
        "no inventory is configured; declare an [inventory] table in the user settings file "
        f"({user_settings_path()}) or in a project's .otto/settings.toml"
    )


_CANNOT_ANSWER = 2
"""``diff``'s exit code for "I could not compare", per ``diff(1)`` (ruling R28).

0 is "no differences", 1 is "differences", and 2 is everything that stopped the
comparison happening at all — a missing or unreadable file, no configured
inventory, a backend that is down. Every other verb keeps 1 for its failures:
they have no third outcome to distinguish, and 1 is what the group documents.
Without the split a typo'd path reads to a script exactly like a real
difference, which is the failure ``diff(1)`` gave the code away for.
"""


def _inventory(code: int = 1) -> "Inventory":
    """Resolve the process inventory the way bootstrap does; ``fail`` on none or on error.

    A BROKEN declaration is loud and distinct from "nothing declared": the
    former names the settings file and what is wrong with it, the latter names
    both files it could have been written in. Collapsing the two is how a
    typo'd ``[inventory]`` reads as "you never configured one".

    *code* is the exit code to fail with — 1 everywhere except ``diff``, which
    passes :data:`_CANNOT_ANSWER`.
    """
    from ..config import get_repos
    from ..inventory import InventoryError, build_inventory

    try:
        inventory = build_inventory(get_repos())
    except InventoryError as e:
        fail(
            f"[bold red]Inventory unavailable:[/bold red] {escape(str(e))}",
            code,
            soft_wrap=True,
        )
    if inventory is None:
        fail(_no_inventory_message(), code, soft_wrap=True)
    return inventory


def _records(inventory: "Inventory", code: int = 1) -> "dict[str, InventoryRecord]":
    """Every record this inventory holds; ``fail`` when the backend cannot answer.

    THE ONE CHOKE POINT for the three verbs that read the whole inventory, and
    therefore where the stale-snapshot notice is rendered: a verb that resolved
    records without reporting a served-stale snapshot is the defect, so the
    report belongs where the records come from rather than at three call sites
    one of which will eventually forget.
    """
    from ..inventory import InventoryError

    try:
        records = {key: inventory.lookup(key) for key in inventory.list_keys()}
    except InventoryError as e:
        fail(escape(str(e)), code, soft_wrap=True)
    _stale_row(inventory)
    return records


def _stale_row(inventory: "Inventory") -> None:
    """Report a snapshot served because the backend was unreachable, if that happened.

    The load path logs this; a ``lab_free`` CLI group never installs a log
    handler, and ``otto``'s ``NullHandler`` defeats ``logging.lastResort``, so
    without this the whole outage is invisible — ``list`` prints a table,
    ``export`` writes an artefact and ``diff`` reports "no differences", all
    from a snapshot that may be days old, all exiting 0.
    """
    from ..inventory import snapshot_cache_of

    cache = snapshot_cache_of(inventory)
    if cache is not None and cache.stale_notice is not None:
        _row(cache.stale_notice)


def _unwrapped(inventory: "Inventory") -> "Inventory":
    """Peel the creds overlay off, exposing whatever it wraps.

    ``construct_inventory`` puts the snapshot cache on first and the creds
    overlay OUTERMOST (a snapshot must never hold credentials), so the cache —
    when there is one — is exactly one layer in.
    """
    from ..inventory import CredsOverlay

    while isinstance(inventory, CredsOverlay):
        inventory = inventory.inner
    return inventory


def _backend_of(inventory: "Inventory") -> "Inventory":
    """Return the innermost object — the backend itself, past both core wrappers."""
    from ..inventory import SnapshotCache

    inner = _unwrapped(inventory)
    return inner.inner if isinstance(inner, SnapshotCache) else inner


def _not_cached_reason(inventory: "Inventory") -> str:
    """Why this inventory has no snapshot to refresh (spec §9.5)."""
    from ..inventory import JsonInventory

    if isinstance(_backend_of(inventory), JsonInventory):
        return (
            "the json backend reads its file on every command, so there is no snapshot to replace"
        )
    return (
        "a remote inventory is cached only when [inventory] sets cache_ttl greater than 0 and "
        "the backend reports no fingerprint of its own"
    )


def _cell(value: "Any") -> str:
    """One table cell: a string verbatim, anything else as canonical JSON.

    A string is passed through so ``lab-a`` reads as ``lab-a`` rather than
    ``"lab-a"``; everything else (numbers, booleans, structures) is JSON with
    sorted keys. Escaped either way — rich parses ``[...]`` in a cell as a
    style tag and deletes it, and every value here came from an inventory.
    """
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return escape(text)


@inventory_app.command("lookup")
def lookup(
    key: Annotated[str, typer.Argument(help="Inventory key to resolve.")],
) -> None:
    """Show the resolved record for KEY — creds as login names only, never passwords."""
    inventory = _inventory()
    from ..inventory import InventoryError

    try:
        record = inventory.lookup(key)
    except InventoryError as e:
        # Before the error, not after: "this key is not here" and "you are
        # reading a three-day-old snapshot" are the same sentence, and the
        # second is the answer to the first.
        _stale_row(inventory)
        fail(escape(str(e)), soft_wrap=True)
    _stale_row(inventory)

    # The provenance a referencing host carries as `host.inventory_ref`: the
    # key, the backend's label, and the record's opaque `extra` table.
    _row(f"key:      {key}")
    _row(f"backend:  {inventory.label}")

    table = Table(box=box.ROUNDED)
    table.add_column("field")
    table.add_column("value")
    # `exclude_unset`, never `exclude_defaults`: the two differ on a field
    # stated AT its default (`is_virtual: false`), and that is exactly what the
    # join reads to decide whether the inventory supplied it.
    stated = record.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    for name in sorted(stated):
        if name == "extra":
            continue  # rendered opaquely below, as its own table
        if name == "creds":
            # LOGINS ONLY. The record holds passwords, this prints to a
            # terminal and into whatever captured it.
            table.add_row("creds", escape(", ".join(c.login for c in record.creds)))
            continue
        table.add_row(name, _cell(stated[name]))
    rprint(table)

    if record.extra:
        extra = Table(title="extra (opaque to otto)", box=box.ROUNDED)
        extra.add_column("key")
        extra.add_column("value")
        for name in sorted(record.extra):
            extra.add_row(escape(name), _cell(record.extra[name]))
        rprint(extra)

    _row(f"supplies: {', '.join(sorted(inventory.supplies))}")


def _skip_rows(inventory: "Inventory") -> "list[str]":
    """Return what the backend passed over on its last fetch, if it says (spec §9.2).

    Empty when the inventory answered from a snapshot: nothing was selected
    this run, so there is nothing honest to report. Read through the two
    public views rather than the backend's private buckets, so a NetBox-shaped
    third-party backend can offer the same accounting.
    """
    backend = _backend_of(inventory)
    rows: list[str] = []
    addressless = getattr(backend, "addressless_device_names", [])
    if addressless:
        source = getattr(backend, "ip_source", "?")
        rows.append(
            f"skipped {len(addressless)} device(s) with no address at ip_source "
            f"{source!r}: {', '.join(addressless)}"
        )
    unnamed = getattr(backend, "unnamed_device_ids", [])
    if unnamed:
        rows.append(
            f"skipped {len(unnamed)} unnamed device(s): "
            f"{', '.join(f'id {i}' for i in sorted(unnamed))}"
        )
    return rows


@inventory_app.command("list")
def list_records() -> None:
    """List every inventory key with its address."""
    inventory = _inventory()
    records = _records(inventory)
    table = Table(box=box.ROUNDED)
    table.add_column("key")
    table.add_column("ip")
    for key in sorted(records):
        table.add_row(escape(key), escape(records[key].ip))
    rprint(table)
    _row(f"{len(records)} record(s) in {inventory.label}")
    for row in _skip_rows(inventory):
        _row(row)


@inventory_app.command("export")
def export(
    path: Annotated[Path, typer.Argument(help="Stage-1 JSON file to write.")],
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write the inventory as a stage-1 JSON file (sorted keys, no creds)."""
    inventory = _inventory()
    # Checked BEFORE the fetch: a refusal must leave the file untouched, and
    # an operator who mistyped a path should not pay a NetBox round trip to
    # find out.
    if path.exists() and not force:
        fail(f"{escape(str(path))} exists; pass --force to overwrite it", soft_wrap=True)
    from ..inventory import records_to_document
    from ..inventory.snapshot import write_document_atomically

    doc = records_to_document(_records(inventory))
    # The same writer the snapshot cache uses: write-then-rename, so a reader
    # never sees a half-written document, and mode 0600 — a stage-1 document
    # carries no credentials but does describe a whole estate.
    #
    # Every way the destination can refuse the write is an OSError — a
    # read-only directory, a path whose parent is a file, a full disk — and an
    # operator who mistyped a path is owed the path and the reason, not a
    # PermissionError traceback out of `mkstemp`.
    try:
        write_document_atomically(path, doc)
    except OSError as e:
        fail(f"{escape(str(path))}: {escape(str(e))}", soft_wrap=True)
    _row(f"wrote {len(doc)} record(s) from {inventory.label} to {path}")


def _document_records(path: Path) -> "dict[str, InventoryRecord]":
    """Parse one stage-1 file; ``fail`` naming the file and what is wrong with it.

    Only ``diff`` reads a file, so every failure here exits
    :data:`_CANNOT_ANSWER` — an unreadable file is "I could not compare", never
    "the two sides differ".
    """
    from ..inventory import InventoryError, document_to_records
    from ..models.inventory import FILLABLE_INVENTORY_FIELDS

    try:
        data = json.loads(path.read_text())
    # UnicodeDecodeError is a ValueError, NOT an OSError, and json.JSONDecodeError
    # never sees it: `read_text` decodes before json is called at all, so a
    # binary file (an image, a gzip, a UTF-16 export) escaped both arms and
    # reached the operator as a traceback.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        fail(f"{escape(str(path))}: {escape(str(e))}", _CANNOT_ANSWER, soft_wrap=True)
    try:
        # Every record field the FILE may carry, not this inventory's
        # `supplies`: an older export may hold a field this deployment no
        # longer supplies, and the diff is how you SEE that rather than a
        # parse error that hides it.
        return document_to_records(data, source=str(path), supplies=FILLABLE_INVENTORY_FIELDS)
    except InventoryError as e:
        fail(escape(str(e)), _CANNOT_ANSWER, soft_wrap=True)


_ABSENT = "absent"
_UNSTATED = "not stated"
_DIFF_LEGEND = (
    f"{_ABSENT!r} = the key is not in that side at all; "
    f"{_UNSTATED!r} = the record is there but says nothing about that field"
)
"""Both empty cells a bare rendering makes indistinguishable, told apart.

``diff_records`` reports a missing key as one row with ``field=None`` and a
missing FIELD as a row with that field named, but both arrive here as ``None``
on one side — so without this the reader cannot tell "this machine is not in
the file" from "the file says nothing about its site".
"""


@inventory_app.command("diff")
def diff(
    path: Annotated[Path, typer.Argument(help="Stage-1 JSON file to compare against.")],
    other: Annotated[
        Path | None,
        typer.Argument(
            help="Second stage-1 file; with it, both sides are files and the inventory is "
            "never read."
        ),
    ] = None,
) -> None:
    """Compare the inventory with a stage-1 file; exit 1 when anything differs.

    Three exit codes, ``diff(1)``'s: 0 no differences, 1 differences, 2 could
    not answer (see ``_CANNOT_ANSWER``).

    With a second PATH the comparison is between the two FILES — yesterday's
    export against today's — and the configured inventory is not resolved at
    all, because nothing in the answer depends on it.
    """
    from ..inventory import diff_records

    if other is not None:
        left_label, left = str(path), _document_records(path)
        right_label, right = str(other), _document_records(other)
    else:
        inventory = _inventory(_CANNOT_ANSWER)
        left_label, left = inventory.label, _records(inventory, _CANNOT_ANSWER)
        right_label, right = str(path), _document_records(path)

    differences = diff_records(left, right)
    if not differences:
        _row(f"no differences between {left_label} and {right_label}")
        return

    _row(f"left:  {left_label}")
    _row(f"right: {right_label}")
    table = Table(box=box.ROUNDED)
    for column in ("key", "field", "left", "right"):
        table.add_column(column)
    for difference in differences:
        # field=None is "this key is on one side only" — a whole-record row,
        # where an empty cell means ABSENT rather than unstated.
        empty = _ABSENT if difference.field is None else _UNSTATED
        table.add_row(
            escape(difference.key),
            escape(difference.field) if difference.field is not None else "(whole record)",
            escape(difference.left) if difference.left is not None else empty,
            escape(difference.right) if difference.right is not None else empty,
        )
    rprint(table)
    _row(_DIFF_LEGEND)
    raise typer.Exit(code=1)


@inventory_app.command("refresh")
def refresh() -> None:
    """Force a fetch of a cached remote inventory and rewrite its snapshot."""
    from datetime import datetime, timezone

    from ..inventory import InventoryError, snapshot_cache_of
    from ..inventory.cache import format_age

    inventory = _inventory()
    cache = snapshot_cache_of(inventory)
    if cache is None:
        # An error, not a cheerful no-op: the operator asked for a fetch and
        # did not get one, and `otto inventory refresh && <next step>` must
        # not proceed as though the inventory had just been re-read.
        fail(
            f"inventory {escape(inventory.label)} is not cached — nothing to refresh: "
            f"{_not_cached_reason(inventory)}",
            soft_wrap=True,
        )
    # Read immediately before the call, which takes its own clock reading on
    # its first line: subtracting the age it reports recovers the replaced
    # snapshot's timestamp to well under the minute this renders.
    before = datetime.now(timezone.utc)
    try:
        result = cache.refresh()
    except InventoryError as e:
        fail(escape(str(e)), soft_wrap=True)
    _row(f"refreshed {inventory.label}: {result.count} record(s)")
    if result.previous_age is None:
        _row("no previous snapshot")
    else:
        # LOCAL time, unlike the load path's stale-snapshot warning (stamped
        # UTC because it is a log line that may be read anywhere). This is an
        # operator watching their own terminal.
        replaced = (before - result.previous_age).astimezone()
        _row(
            f"replaced the snapshot fetched {replaced:%Y-%m-%d %H:%M %Z} "
            f"({format_age(result.previous_age)} old)"
        )
    _row(f"snapshot: {cache.snapshot_path}")
    for row in _skip_rows(inventory):
        _row(row)
