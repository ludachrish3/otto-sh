"""``otto inventory`` — read-only helpers over the configured host inventory (spec §11).

- ``lookup KEY`` / ``list`` — debug the join without editing a lab file.
- ``export PATH`` — write the inventory as a stage-1 JSON file (no creds).
- ``diff PATH [OTHER]`` — compare the inventory (or a second file) against a
  stage-1 file; exit 1 on any difference.
- ``refresh`` — force a fetch of a cached remote inventory.

Each verb reads one inventory: ``default`` (today's) unless ``--inventory
NAME`` names an ``[[inventories]]`` entry (#1176).

Lab-free, and settings-only: §8 resolves the process inventory from the
participating projects' ``[inventory]`` tables and the user settings file, and
from nothing else — no lab and no host reaches it. Which projects participate
is judged before any lab, from ``-l`` (or ``OTTO_LAB``), ``-I`` and ``-E``
alone, so each verb takes the ``typer.Context`` those root options are read
from.

IMPORT DISCIPLINE: everything from :mod:`otto.inventory` and
``otto.models`` is imported INSIDE the verb that uses it. This module is
reachable from ``otto --help`` (the root group resolves a spec's loader on
dispatch, and the completion cache walks the registry), and the
file-operation ceilings in ``scripts/import_budget.py`` bound what a bare
``--help`` costs. The edges are real and declared in ``tach.toml``; only the
*timing* is deferred.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

import typer
from rich import box
from rich import print as rprint

from ..host.inventory_ref import DEFAULT_INVENTORY
from .completers import completion_source
from .invoke import fail, scope_selection

if TYPE_CHECKING:
    from rich.text import Text

    from ..inventory.discovery import Discovery
    from ..inventory.protocol import Inventory
    from ..inventory.snapshot import RecordDifference

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
    """Print one detail row literally, through ``otto.console.print_literal``.

    Every field on these rows is user-supplied: an inventory key, a backend
    label, a filesystem path.
    """
    from ..console import print_literal

    print_literal(text)


def _no_inventory_message() -> str:
    """Return the §8 "nothing declared anywhere" error, naming BOTH places it could have been.

    The RESOLVED user file rather than the ``~/.otto/settings.toml`` spelling
    the load path uses: ``OTTO_HOME`` relocates it wholesale, and a message
    naming a file the reader does not have sends them to edit the wrong one.

    Deliberately distinct from :func:`otto.inventory.view.no_default_message`:
    that one answers a host's reference and names its KEY; this one answers a
    verb asked for ``default`` with nothing configured, so it has no key to
    name and names the resolved user file instead.
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


def _inventory(ctx: typer.Context, code: int = 1, *, name: str = DEFAULT_INVENTORY) -> "Inventory":
    """Resolve the inventory named *name* the way bootstrap does; ``fail`` on none or on error.

    Only the participating projects' ``[inventory]`` tables are read: a
    project ``-E`` switched off, or whose ``lab_patterns`` do not match the
    ``-l`` (or ``OTTO_LAB``) labs, contributes none, and ``-I`` brings one back.

    ``default`` is today's resolution, unchanged: a ``default`` declared
    nowhere fails with :func:`_no_inventory_message`. Any other name is
    selected from the process view by
    :meth:`~otto.inventory.view.InventoryView.select_name`, which builds that
    name alone (:func:`~otto.inventory.config.deferred_inventory_view`), so a
    broken ``default`` never stops ``--inventory physical``; an undeclared
    name fails listing the declared ones.

    A BROKEN declaration is loud and distinct from "nothing declared": the
    former names the settings file and what is wrong with it, the latter names
    both files it could have been written in. Collapsing the two is how a
    typo'd ``[inventory]`` reads as "you never configured one".

    *code* is the exit code to fail with — 1 everywhere except ``diff``, which
    passes :data:`_CANNOT_ANSWER`.
    """
    # The composition root: importing it installs the initiator that composes
    # the projects this reads (spec 2026-10-11 placement-and-phases §3.12).
    from .. import bootstrap

    del bootstrap
    from ..config.scope import participating
    from ..declared import get_projects
    from ..inventory.config import build_inventory, deferred_inventory_view
    from ..inventory.errors import InventoryError

    projects = participating(get_projects(), selection=scope_selection(ctx)).kept
    try:
        if name == DEFAULT_INVENTORY:
            inventory = build_inventory(projects=projects)
        else:
            inventory = deferred_inventory_view(projects=projects).select_name(name=name)
    except InventoryError as e:
        from rich.text import Text

        fail(
            Text.assemble(("Inventory unavailable:", "bold red"), " ", str(e)),
            code,
            soft_wrap=True,
        )
    if inventory is None:
        fail(_no_inventory_message(), code, soft_wrap=True)
    return inventory


@completion_source(kind="payload", key="inventory_names", sort=True)
def _inventory_name_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--inventory``: every declared inventory name.

    The ``names`` cache's ``inventory_names`` when present, which the shim
    answers a warm TAB from; else
    :func:`~otto.config.completion_cache.collect_inventory_names` over
    discovery's projects (:func:`otto.config.project.discover`, never
    ``get_projects()``, which runs user init code). Either way the names come
    from settings alone: a TAB never constructs an inventory, loads a lab or
    fetches.
    """
    try:
        from ..config.completion_cache import collect_inventory_names
        from ..config.project import discover
        from ..registry import get_completion_names

        cached = get_completion_names()
        if cached is not None and isinstance(cached.get("inventory_names"), list):
            names = [str(n) for n in cached["inventory_names"]]
        else:
            names = collect_inventory_names(discover().projects)
    except Exception:  # noqa: BLE001 — completion must never crash the shell
        return []
    return sorted(n for n in names if n.startswith(incomplete))


_InventoryName = Annotated[
    str,
    typer.Option(
        "--inventory",
        metavar="NAME",
        help="The inventory to read: `default`, or an `\\[\\[inventories]]` entry's name.",
        autocompletion=_inventory_name_completer,
    ),
]
"""The ``--inventory NAME`` option every verb takes: one definition, one completion source."""


def _discovery(inventory: "Inventory", code: int = 1) -> "Discovery":
    """Every record this inventory holds, and its instances; ``fail`` when it cannot answer.

    THE ONE CHOKE POINT for the three verbs that read the whole inventory, and
    therefore where the stale-snapshot notice is rendered: a verb that resolved
    records without reporting a served-stale snapshot is the defect, so the
    report belongs where the records come from rather than at three call sites
    one of which will eventually forget.
    """
    from ..inventory.discovery import discover_inventory
    from ..inventory.errors import InventoryError

    try:
        found = discover_inventory(inventory=inventory)
    except InventoryError as e:
        fail(e, code, soft_wrap=True)
    _stale_row(inventory)
    return found


def _stale_row(inventory: "Inventory") -> None:
    """Report a snapshot served because the backend was unreachable, if that happened.

    The load path logs this, but only ONCE per snapshot per process, and the
    resolution that spends the warning can be ``entry()``'s completion-cache
    write — which runs before the root callback installs a console handler.
    Without this row the whole outage is then invisible: ``list`` prints a
    table, ``export`` writes an artefact and ``diff`` reports "no differences",
    all from a snapshot that may be days old, all exiting 0.
    """
    from ..inventory.cache import snapshot_cache_of

    cache = snapshot_cache_of(inventory=inventory)
    if cache is not None and cache.stale_notice is not None:
        _row(cache.stale_notice)


def _creds_row(inventory: "Inventory") -> None:
    """Print the ``creds:`` row naming the store when the overlay is configured.

    Spec 2026-09-06 creds-store §7.3.
    """
    from ..inventory.creds import CredsOverlay

    if isinstance(inventory, CredsOverlay):
        _row(f"creds:    {inventory.store.label}")


def _backend_of(inventory: "Inventory") -> "Inventory":
    """Return the innermost object — the backend itself, past both core wrappers."""
    from ..inventory.cache import SnapshotCache
    from ..inventory.creds import below_creds_overlay

    inner = below_creds_overlay(inventory=inventory)
    return inner.inner if isinstance(inner, SnapshotCache) else inner


def _not_cached_reason(inventory: "Inventory") -> str:
    """Why this inventory has no snapshot to refresh (spec §9.5)."""
    from ..inventory.json_backend import JsonInventory

    if isinstance(_backend_of(inventory), JsonInventory):
        return (
            "the json backend reads its file on every command, so there is no snapshot to replace"
        )
    return (
        "a remote inventory is cached only when [inventory] sets cache_ttl greater than 0 and "
        "the backend reports no fingerprint of its own"
    )


def _cell(value: "Any") -> "Text":
    """One table cell: a string verbatim, anything else as canonical JSON.

    A string is passed through so ``lab-a`` reads as ``lab-a`` rather than
    ``"lab-a"``; everything else (numbers, booleans, structures) is JSON with
    sorted keys. Literal either way (``otto.console.literal_cell``): every
    value here came from an inventory, and its column is an ``id_column``.
    """
    from ..console import literal_cell

    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return literal_cell(text)


@inventory_app.command("lookup")
def lookup(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Inventory key to resolve.")],
    inventory_name: _InventoryName = DEFAULT_INVENTORY,
) -> None:
    """Show the resolved record for KEY — creds as login names only, never passwords."""
    from rich.table import Table

    from ..console import id_column

    inventory = _inventory(ctx, name=inventory_name)
    from ..inventory.errors import InventoryError

    try:
        record = inventory.lookup(key)
    except InventoryError as e:
        # Before the error, not after: "this key is not here" and "you are
        # reading a three-day-old snapshot" are the same sentence, and the
        # second is the answer to the first.
        _stale_row(inventory)
        fail(e, soft_wrap=True)
    _stale_row(inventory)

    # The provenance a referencing host carries as `host.inventory_ref`: the
    # key, the backend's label, and the record's opaque `extra` table.
    _row(f"key:      {key}")
    _row(f"backend:  {inventory.label}")
    _creds_row(inventory)

    table = Table(box=box.ROUNDED)
    id_column(table, "field")
    id_column(table, "value")
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
            table.add_row(_cell("creds"), _cell(", ".join(c.login for c in record.creds)))
            continue
        table.add_row(_cell(name), _cell(stated[name]))
    rprint(table)

    if record.extra:
        extra = Table(title="extra (opaque to otto)", box=box.ROUNDED)
        id_column(extra, "key")
        id_column(extra, "value")
        for name in sorted(record.extra):
            extra.add_row(_cell(name), _cell(record.extra[name]))
        rprint(extra)

    _row(f"supplies: {', '.join(sorted(inventory.supplies))}")


def _skip_rows(inventory: "Inventory") -> "list[str]":
    """Return what the backend passed over on its last fetch, if it says (spec §9.2).

    Empty when the inventory answered from a snapshot: nothing was selected
    this run, so there is nothing honest to report. Read through the public
    views (:data:`_SKIP_VIEWS`, three per object kind) rather than the
    backend's private buckets, so a NetBox-shaped third-party backend can
    offer the same accounting.
    """
    backend = _backend_of(inventory)
    rows: list[str] = []
    for views in _SKIP_VIEWS:
        addressless = getattr(backend, views.addressless, [])
        if addressless:
            source = getattr(backend, views.ip_source, "?")
            rows.append(
                f"skipped {len(addressless)} {views.noun}(s) with no address at "
                f"{views.ip_source} {source!r}: {', '.join(addressless)}"
            )
        unnamed = getattr(backend, views.unnamed, [])
        if unnamed:
            rows.append(
                f"skipped {len(unnamed)} unnamed {views.noun}(s): "
                f"{', '.join(f'id {i}' for i in sorted(unnamed))}"
            )
    return rows


class _SkipViews(NamedTuple):
    """Where a NetBox-shaped backend reports what it skipped of one object kind."""

    noun: str
    """The kind, as the rows name it."""
    addressless: str
    """The attribute naming its selected objects that have no address."""
    unnamed: str
    """The attribute listing the ids of its selected objects that have no name."""
    ip_source: str
    """The attribute (and setting) its addresses are read from."""


_SKIP_VIEWS: "list[_SkipViews]" = [
    _SkipViews("device", "addressless_device_names", "unnamed_device_ids", "ip_source"),
    _SkipViews(
        "virtual machine",
        "addressless_virtual_machine_names",
        "unnamed_virtual_machine_ids",
        "vm_ip_source",
    ),
]
"""Every object kind whose skips ``otto inventory list`` reports, in report order."""


@inventory_app.command("list")
def list_records(ctx: typer.Context, inventory_name: _InventoryName = DEFAULT_INVENTORY) -> None:
    """List every inventory key with its address."""
    from rich.table import Table

    from ..console import id_column

    inventory = _inventory(ctx, name=inventory_name)
    records = _discovery(inventory).records
    table = Table(box=box.ROUNDED)
    id_column(table, "key")
    id_column(table, "ip")
    for key in sorted(records):
        table.add_row(_cell(key), _cell(records[key].ip))
    rprint(table)
    _row(f"{len(records)} record(s) in {inventory.label}")
    _creds_row(inventory)
    for row in _skip_rows(inventory):
        _row(row)


@inventory_app.command("export")
def export(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Stage-1 JSON file to write.")],
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
    inventory_name: _InventoryName = DEFAULT_INVENTORY,
) -> None:
    """Write the inventory as a stage-1 JSON file (sorted keys, no creds).

    An inventory that declares instances is written in the
    `otto.inventory.v2` format, records and instances; any other is written
    as the flat document.
    """
    inventory = _inventory(ctx, name=inventory_name)
    # Checked BEFORE the fetch: a refusal must leave the file untouched, and
    # an operator who mistyped a path should not pay a NetBox round trip to
    # find out.
    if path.exists() and not force:
        fail(f"{path} exists; pass --force to overwrite it", soft_wrap=True)
    from ..inventory.snapshot import discovery_to_document, write_document_atomically

    found = _discovery(inventory)
    doc = discovery_to_document(discovery=found)
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
        fail(f"{path}: {e}", soft_wrap=True)
    wrote = f"{len(found.records)} record(s)"
    if found.instances is not None:
        wrote += f" and {len(found.instances)} instance(s)"
    _row(f"wrote {wrote} from {inventory.label} to {path}")


def _document_discovery(path: Path) -> "Discovery":
    """Parse one stage-1 file, either format; ``fail`` naming the file and what is wrong.

    Only ``diff`` reads a file, so every failure here exits
    :data:`_CANNOT_ANSWER` — an unreadable file is "I could not compare", never
    "the two sides differ".
    """
    from ..inventory.errors import InventoryError
    from ..inventory.snapshot import document_to_discovery
    from ..models.inventory import FILLABLE_INVENTORY_FIELDS

    try:
        data = json.loads(path.read_text())
    # UnicodeDecodeError is a ValueError, NOT an OSError, and json.JSONDecodeError
    # never sees it: `read_text` decodes before json is called at all, so a
    # binary file (an image, a gzip, a UTF-16 export) escaped both arms and
    # reached the operator as a traceback.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        fail(f"{path}: {e}", _CANNOT_ANSWER, soft_wrap=True)
    try:
        # Every record field the FILE may carry, not this inventory's
        # `supplies`: an older export may hold a field this deployment no
        # longer supplies, and the diff is how you SEE that rather than a
        # parse error that hides it.
        return document_to_discovery(
            data=data, source=str(path), supplies=FILLABLE_INVENTORY_FIELDS
        )
    except InventoryError as e:
        fail(e, _CANNOT_ANSWER, soft_wrap=True)


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


def _difference_table(
    differences: "list[RecordDifference]", *, key_column: str, whole: str
) -> None:
    """Render one table of diff rows: *key_column*, field, left, right.

    *whole* names the field cell of a ``field=None`` row (one side only),
    where an empty cell means ABSENT rather than unstated. A ``whole_map``
    row is about the whole instance map, so its field cell says so; the flag
    decides it, never the key, which a record or instance may share.
    """
    from rich.table import Table

    from ..console import id_column

    table = Table(box=box.ROUNDED)
    for column in (key_column, "field", "left", "right"):
        id_column(table, column)
    for difference in differences:
        empty = _ABSENT if difference.field is None else _UNSTATED
        field = difference.field
        if field is None:
            field = "(whole map)" if difference.whole_map else whole
        table.add_row(
            _cell(difference.key),
            _cell(field),
            _cell(difference.left if difference.left is not None else empty),
            _cell(difference.right if difference.right is not None else empty),
        )
    rprint(table)


@inventory_app.command("diff")
def diff(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Stage-1 JSON file to compare against.")],
    other: Annotated[
        Path | None,
        typer.Argument(
            help="Second stage-1 file; with it, both sides are files and the inventory is "
            "never read."
        ),
    ] = None,
    inventory_name: _InventoryName = DEFAULT_INVENTORY,
) -> None:
    """Compare the inventory with a stage-1 file; exit 1 when anything differs.

    Exit codes follow `diff(1)`: 0 no differences, 1 differences, 2 could not
    answer.

    Records are compared field by field and, when either side declares
    instances, instances by `members` and `roles` in a second table.

    With a second file (`other`) the comparison is between the two files
    (yesterday's export against today's) and the configured inventory is not
    read at all, because nothing in the answer depends on it; `--inventory`
    is then ignored.
    """
    # Developer note: the "could not answer" exit status is `_CANNOT_ANSWER`.
    from ..inventory.snapshot import diff_instances, diff_records

    if other is not None:
        left_label, left = str(path), _document_discovery(path)
        right_label, right = str(other), _document_discovery(other)
    else:
        inventory = _inventory(ctx, _CANNOT_ANSWER, name=inventory_name)
        left_label, left = inventory.label, _discovery(inventory, _CANNOT_ANSWER)
        right_label, right = str(path), _document_discovery(path)

    records = diff_records(left=left.records, right=right.records)
    instances = diff_instances(left=left.instances, right=right.instances)
    if not records and not instances:
        _row(f"no differences between {left_label} and {right_label}")
        return

    _row(f"left:  {left_label}")
    _row(f"right: {right_label}")
    if records:
        _difference_table(records, key_column="key", whole="(whole record)")
    if instances:
        _difference_table(instances, key_column="instance", whole="(whole instance)")
    _row(_DIFF_LEGEND)
    raise typer.Exit(code=1)


@inventory_app.command("refresh")
def refresh(ctx: typer.Context, inventory_name: _InventoryName = DEFAULT_INVENTORY) -> None:
    """Force a fetch of a cached remote inventory and rewrite its snapshot."""
    from datetime import datetime, timezone

    from ..inventory.cache import format_age, snapshot_cache_of
    from ..inventory.errors import InventoryError

    inventory = _inventory(ctx, name=inventory_name)
    cache = snapshot_cache_of(inventory=inventory)
    if cache is None:
        # An error, not a cheerful no-op: the operator asked for a fetch and
        # did not get one, and `otto inventory refresh && <next step>` must
        # not proceed as though the inventory had just been re-read.
        fail(
            f"inventory {inventory.label} is not cached — nothing to refresh: "
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
        fail(e, soft_wrap=True)
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
