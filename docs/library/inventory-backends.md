# Inventory backends

Otto reads the tool-agnostic half of a host — address, interfaces,
credentials, versions, location — through an **inventory backend**: a small
class that answers "what is true about the machine with this key?". Two ship
with otto, the `json` file and `netbox`
({doc}`../guide/configuration/inventory`); anything else is a class you
register from your own repo, because your source of record is a CMDB, an asset
database, or a service nobody else has.

This page is the contract. The user-facing side — the partition rule, the
settings table, the adoption path — lives in
{doc}`../guide/configuration/inventory`.

## The protocol

A backend satisfies {class}`~otto.inventory.protocol.Inventory`: two
attributes and three methods, all read-only. Otto never writes to an
inventory.

| Member | Contract |
| ------ | -------- |
| `label` | Non-empty string naming this inventory in provenance and errors, e.g. `json:/home/me/lab/inventory.json`. Normalise it: it is the identity a snapshot is keyed by, so two spellings of one URL must produce one label. |
| `supplies` | A `frozenset` of the record fields this instance supplies. Always contains `"ip"`. Fixed at construction from configuration — never discovered from records. |
| `lookup(key)` | The {class}`~otto.models.inventory.InventoryRecord` for `key`. Raises {class}`~otto.inventory.errors.InventoryKeyError` when this inventory does not hold it, and {class}`~otto.inventory.errors.InventoryError` for anything that stopped it answering. Must be idempotent — an equal record on a second call. |
| `list_keys()` | Every key this inventory holds, sorted. Everything it lists must resolve. |
| `fingerprint()` | A value that changes whenever the records may have; `None` means "not cacheable". |

Two rules that are easy to miss:

- **Construction does no I/O.** No socket, no file read, no environment
  variable that must be set — the paths and parameters are computed, and the
  first `lookup`/`list_keys` does the work. A lab with no referenced entry
  never touches the inventory at all, which is what keeps a broken or
  unreachable inventory from breaking a run that did not need it.
- **A record may not carry a field outside `supplies`.** Otto's join copies
  exactly `supplies`, so a record stating more than the deployment declared is
  a partition violation, not a bonus. The two identity/opaque fields —
  `element_id` and `extra` — are exempt: one is asserted rather than filled,
  the other has no host field to collide with.

Validate the `supplies` declaration with
{func}`~otto.inventory.protocol.check_supplies`, which is what both built-ins
call: `None` means "every fillable record field", a name that is not a record
field is refused, and a set without `"ip"` is refused — a reference that yields
no address is pointless.

```python
from otto.inventory import check_supplies


class MyInventory:
    def __init__(self, repo_dir=None, *, url, supplies=None):
        self.url = url
        self.supplies = check_supplies(supplies)
        self.label = f"mycmdb:{url.rstrip('/')}"
        self._records = None
```

## Selecting it in settings

Register the backend under a bare name from an `init` module (one of the
modules in `init = [...]`), then select it by that name:

```python
# my_inventory.py  (listed in init = [...])
from otto.inventory import register_inventory_backend
from my_company.cmdb import MyInventory

register_inventory_backend("mycmdb", MyInventory)
```

```toml
[inventory]
backend = "mycmdb"
url = "https://cmdb.example.com"
creds_file = "~/.otto/creds.json"
cache_ttl = "24h"
```

Otto constructs it as
`MyInventory(repo_dir=<declaring directory>, url="https://cmdb.example.com")` —
every key in the table except otto's own (`backend`, `creds_file`, `cache_ttl`)
becomes a keyword argument. `repo_dir` is always passed, and it is **the
directory the declaration came from**: the repository root for a project
`[inventory]` override, and `~/.otto` — otto's home — for the user settings
file, which is where most declarations live. Anchor your own relative
path-like settings to it, and do not read it as "the repo" — for the usual
declaration there is no repo involved.

Reject a kwarg you do not understand with a `TypeError` or `ValueError`: otto
wraps it into an error naming the settings file and the backend, which the raw
`unexpected keyword argument 'urll'` does not.

{func}`~otto.inventory.register_inventory_backend` refuses a duplicate name
unless you pass `overwrite=True`, which is how you deliberately replace a
built-in. {func}`~otto.inventory.get_inventory_backend_class` resolves a name,
and an unregistered one raises an error listing the registered names. This is
the same named-registry mechanism otto uses for host sources, reservation
backends, term/transfer backends and host classes — see
{doc}`Extension points <../architecture/subsystems/extension-points>`.

## Credentials are not your problem

`creds_file` is a **core** `[inventory]` key, not a backend's. Otto wraps the
selected backend in {class}`~otto.inventory.creds.CredsOverlay`, which supplies
`creds` from that file for every lookup, so a backend never has to see a
password — and a backend record that carries `creds` while `creds_file` is
configured is an error naming the key.

Carry credentials in your own records only if your source of record genuinely
holds them and the deployment does not configure `creds_file`. If it does, the
overlay is authoritative; one home per field, and no reader ever chooses
between two sources.

## Opting into the snapshot cache

`fingerprint()` is otto's freshness question, and the answer decides whether a
remote backend gets a snapshot cache for free:

- Return a **string** and you have opted out. You are saying you can report
  freshness yourself — cheaply, and without a network round trip. Otto calls
  this on every command through the shell-completion cache, so it must never
  fetch.
- Return **`None`** and otto wraps you in
  {class}`~otto.inventory.cache.SnapshotCache` whenever `cache_ttl` is greater
  than zero. A snapshot younger than the TTL is served without calling you at
  all; an older one triggers one `list_keys()` + `lookup()` sweep and an atomic
  rewrite; and when you raise, a snapshot of any age is served with a warning
  naming its age. The cache then supplies the fingerprint you could not — the
  snapshot's content hash — so completion caches normally.

The snapshot is written in the same stage-1 document shape the `json` backend
reads, which is why an operator can copy one out of otto's home and point a
`json` inventory at it.

A snapshot never holds credentials — that is what makes it shareable — so a
backend whose own `supplies` includes `creds` is **refused** the cache rather
than quietly losing them for the rest of the TTL. The fix is on your side of
the API: leave `creds` out of `supplies` and let the deployment's
`creds_file` carry them instead (which is where credentials belong anyway).
An operator who cannot change the backend still has `cache_ttl = "0"`. The
error names your backend and the settings file that declared it.

## Verify your backend

Otto ships a conformance helper that checks a backend against the whole
contract and reports every violation at once. The shipped `json` backend
conforms, which is what makes it the worked example — read
{class}`~otto.inventory.json_backend.JsonInventory` alongside this page:

```{doctest}
>>> import json, tempfile
>>> from pathlib import Path
>>> from otto.inventory import JsonInventory
>>> from otto.testing import assert_inventory_conforms
>>> with tempfile.TemporaryDirectory() as tmp:
...     path = Path(tmp) / "inventory.json"
...     _ = path.write_text(json.dumps({"dut1": {"ip": "10.0.0.5"}}))
...     assert_inventory_conforms(JsonInventory(path), expected_keys=["dut1"])
```

Call it from your own suite. `expected_keys` names keys your fixture is known
to hold: each must resolve **and** appear in `list_keys()`, which catches the
backend that answers a lookup for a key it never lists.

```python
from otto.testing import assert_inventory_conforms
from my_company.cmdb import MyInventory


def test_my_inventory_conforms():
    assert_inventory_conforms(
        MyInventory(url="https://cmdb.test"),
        expected_keys=["dut1", "dut2"],
    )
```

Pass a `repository` and a `lab` as well and the helper adds a **positive
control**: the named lab must FAIL to load without the inventory and LOAD with
it, and at least one built host must carry a referenced `inventory_ref`. That
is the check a backend cannot pass by accident — see below for why it matters
most to people writing *lab* backends.

```python
def test_the_lab_resolves_through_the_inventory(tmp_path):
    assert_inventory_conforms(
        MyInventory(url="https://cmdb.test"),
        expected_keys=["dut1"],
        repository=my_lab_repository(tmp_path),
        lab="rig",
    )
```

## Migration note for custom lab repositories

The {doc}`lab-source backend <lab-source-backends>` protocol gained an
argument: `load_lab(name, preferences=None, inventory=None)`, and
`list_host_summaries(inventory=None)` alongside it. A backend that builds hosts
through otto's factory only has to forward it; a backend that builds them
itself must resolve `inventory` references, or entries that reference the
inventory will never get their addresses.

Update the signature even if you do not support references yet, because the two
call paths fail differently:

- `load_lab` fails **loudly** — otto passes `inventory=` and Python raises
  `TypeError: load_lab() got an unexpected keyword argument`.
- The completion and discovery paths fail **silently**, and at a coarser
  granularity than you would guess. Summarising is best-effort by contract — it
  must never crash a shell over one bad record — so the `TypeError` is caught
  and logged at debug level, and your hosts simply stop appearing in tab
  completion. If your backend implements `list_host_summaries`, the raise comes
  from that call and the composite skips **the whole source**: every lab it
  declares, not just one. If it implements only `load_lab`, otto's summary
  fallback skips each lab as it loads. Either way the hosts are gone and
  nothing says why above debug.

The conformance helper's positive control is the guard for exactly this: a
backend that ignores `inventory=` loads the lab it was supposed to fail on, and
fails the assertion.
