# Inventory backends

Otto reads the tool-agnostic half of a host — address, interfaces,
credentials, versions, location — through an **inventory backend**: a small
class that answers "what is true about the machine with this key?". Two ship
with otto, the `json` file and `netbox`
({doc}`../configuration/inventory`); anything else is a backend you
register from your own project, because your source of record is a CMDB, an asset
database, or a service nobody else has.

This page is the contract. The user-facing side — the partition rule, the
settings table, the adoption path — lives in
{doc}`../configuration/inventory`.

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
| `fingerprint()` | A value that changes whenever the records may have; `None` means "not cacheable". Answers from local state and never fetches or probes the network ({ref}`below <inventory-fingerprint-contract>`). Must not raise: the conformance helper counts a raise as a violation. |

Two rules that are easy to miss:

- **Construction does no I/O.** No socket, no file read, no environment
  variable that must be set — the paths and parameters are computed, and the
  first `lookup`/`list_keys` does the work. A lab with no referenced entry
  never touches the inventory at all, so a broken or unreachable inventory
  does not affect a run that does not need it.
- **A record may not carry a field outside `supplies`.** Otto's join copies
  exactly `supplies`, so a record stating more than the deployment declared is
  a partition violation, not a bonus. Four fields are exempt: `element_id`, a
  cross-checked fact rather than a filled one — opt-in, named in `supplies`
  like any other field, and checked against `element.id` (never copied) only
  when a deployment does that; otherwise a stated value is simply inert —
  `extra`, opaque and with no host field to collide with, and `groups` and
  `labels`, discovery data that never reaches a host.

Validate the `supplies` declaration with
{func}`~otto.inventory.protocol.check_supplies`, which is what both built-ins
call: `None` means "every fillable record field", a name that is not a record
field is refused, and a set without `"ip"` is refused.

```python
from otto.inventory import check_supplies


class MyInventory:
    def __init__(self, *, url, supplies=None):
        self.url = url
        self.supplies = check_supplies(supplies)
        self.label = f"mycmdb:{url.rstrip('/')}"
        self._records = None
```

## Selecting it in settings

An inventory backend is a *configured* backend, registered with the one
wrapper shape every configured seam shares
({ref}`configured-backends`): a config model that parses the `[inventory]`
table, and a factory that builds the backend from it. Register it under a
bare name from an `init` module (one of the modules in `init = [...]`), then
select it by that name:

```python
# my_inventory.py  (listed in init = [...])
from pydantic import ConfigDict

from otto.inventory import InventoryEnv, register_inventory_backend
from otto.models import OttoModel
from otto.registry import Configured
from my_company.cmdb import MyInventory


class MyInventoryConfig(OttoModel):
    model_config = ConfigDict(frozen=True)

    url: str
    supplies: list[str] | None = None


def my_inventory(c: Configured[MyInventoryConfig, InventoryEnv]) -> MyInventory:
    return MyInventory(url=c.config.url, supplies=c.config.supplies)


register_inventory_backend("mycmdb", config=MyInventoryConfig, factory=my_inventory)
```

```toml
[inventory]
backend = "mycmdb"
url = "https://cmdb.example.com"
cache_ttl = "24h"
```

Otto parses every key in the table except its own (`backend`,
`cache_ttl`) with `MyInventoryConfig`, so an unknown key or a bad value fails
when the table is read, naming the field, the backend and the settings file
(never quoting the rejected value); what a config model must be is stated
once, under {ref}`configured-backends`. The factory then receives the parsed
configuration as `c.config`.

For an HTTPS CMDB signed by an internal CA, build the client with
{func}`otto.tls.os_trust_session`, the same session otto's own NetBox
backend uses — see {doc}`https-clients`.

`c.env` is an {class}`~otto.inventory.InventoryEnv`. Its `anchor_dir` is
**the directory the declaration came from**: the repository root for a project
`[inventory]` override, and `~/.otto` — otto's home — for the user settings
file, which is where most declarations live. A validator on your config model
reads it as `info.context["env"].anchor_dir` to anchor a relative path-like
setting, as the built-in `json` backend's model does; do not read it as "the
project" — for the usual declaration there is no project involved. Its `origin` is
the settings file, for messages.

Otto decides which tables name the same inventory, and keys the snapshot
cache, by the *parsed* configuration (plus `cache_ttl`; the snapshot's key
leaves out `max_stale`, below), never by the declaring directory or file: a relative path that one project anchors and the
absolute path another project writes are one inventory.

A factory that raises, or that returns something without the protocol's
members, fails with {class}`~otto.inventory.InventoryConstructionError`
naming the settings file, the backend and the module that registered it.

{func}`~otto.inventory.register_inventory_backend` refuses a duplicate name
unless you pass `overwrite=True`, which is how you deliberately replace a
built-in. A name nobody registered raises when otto builds the inventory, and the
error lists the registered names. This is
the same named-registry mechanism otto uses for host sources, reservation
backends, term/transfer backends and host classes — see
{doc}`Extension points <../architecture/subsystems/extension-points>`.

## Credentials are layered

`[creds]` is a **core** table, not a backend's: otto wraps the selected
inventory in {class}`~otto.inventory.creds.CredsOverlay`, which merges the
configured creds store's entries UNDER the record's, by identity — login plus
scope ({ref}`cred-protocols`) — and the lab file's over both
({ref}`credentials-layered`). A backend therefore never has to see a
password — leave `creds` out of your records and the store supplies them — but
it may carry them when the source of record genuinely holds them; the record
then overrides the store for the logins it names. A backend whose
`supplies` includes `creds` is never snapshot-cached (below). Writing a store
of your own is {doc}`creds-backends`.

(inventory-discovery)=

## Declaring instances

A backend whose source of record knows which machines form one instance (a
rig, a pod) can say so by also implementing
{class}`~otto.inventory.discovery.SupportsDiscovery`: one method,
`inventory_discovery()`, that returns a
{class}`~otto.inventory.discovery.Discovery` holding every record and the
instances, each an {class}`~otto.models.inventory.InventoryInstance`.

```python
from otto.inventory import Discovery
from otto.models import InventoryInstance


class MyInventory:
    ...

    def inventory_discovery(self) -> Discovery:
        records = {key: self.lookup(key) for key in self.list_keys()}
        rigs = self._fetch_rigs()  # {"rig-7": {"dut": ["edge-17"], "tgen": ["ixia-9"]}}
        return Discovery(
            records=records,
            instances={name: InventoryInstance(roles=roles) for name, roles in rigs.items()},
        )
```

- The capability is optional and detected by the method's presence. otto
  reads every inventory through
  {func}`~otto.inventory.discovery.discover_inventory`, which calls
  `inventory_discovery()` when the backend has it and otherwise looks up
  every key `list_keys()` returns. The name is deliberately distinctive, so a
  `discover()` method of your own is never mistaken for it.
- `instances=None` (the default) means your backend has no notion of
  instances. `{}` means it has one and declares none. Return `{}` when the
  upstream has an instance map that happens to be empty.
- Members are **your own keys**, never another inventory's name or key. A
  member needs no record, so an instance whose machines have all gone is
  still reported.
- Each list in an instance is sorted for you. A key repeated in one list is
  refused, and so is a key in `members` that also plays a role, because
  `members` holds keys without one. One key may play several roles.
- `records` holds what `lookup` returns for each key: the same records, not a
  second view of them. The creds overlay merges the store's creds into each
  record exactly as its own `lookup` does, and passes instances through.

The built-in `json` backend has the capability: its `otto.inventory.v2`
document declares instances ({doc}`../configuration/inventory`).

(inventory-fingerprint-contract)=

## Opting into the snapshot cache

Whether otto may cache a backend is first the registration's to say:
`register_inventory_backend(..., snapshot_cache=False)` keeps otto from ever
wrapping it, and from asking its `fingerprint()` while it builds it. The
default is `True`; the built-in `json` backend states `False`, the built-in
`netbox` `True`.

`fingerprint()` is otto's freshness question. Otto asks it whenever it
validates or writes the shell-completion cache — on a TAB, on root
`otto --help`, and in the commands that record into it — so it must answer from
local state (a file's stat, a hash already on disk) and never fetch or probe the
network. The answer decides whether a remote backend gets a snapshot cache for
free:

- Return a **string** and you have opted out. You are saying you can report
  freshness yourself, from local state.
- Return **`None`** and otto wraps you in
  {class}`~otto.inventory.cache.SnapshotCache` whenever `cache_ttl` is greater
  than zero (and your registration allows it). A snapshot younger than the TTL is served without calling you at
  all; an older one triggers one `list_keys()` + `lookup()` sweep and an atomic
  rewrite; and when you raise, the error you raise decides whether the
  snapshot is served ({ref}`inventory-backend-failures`). The cache then
  supplies the fingerprint you could not — the snapshot's content hash — so
  completion caches normally.

The snapshot is written in the same stage-1 document shape the `json` backend
reads, so an operator can copy one out of otto's home and point a `json`
inventory at it. In that document a `_`-prefixed key is a comment, so a
record key or instance name starting with `_` cannot be stored: otto refuses
such a discovery with {class}`~otto.inventory.InventoryDataError` before
anything is served or written, rather than lose the name on the next read.

A snapshot never holds credentials, so a backend whose own `supplies` includes
`creds` is **refused** the cache rather than quietly losing them for the rest
of the TTL. The fix is on your side of the API: leave `creds` out of
`supplies` and let the deployment's `[creds]` store carry them instead. An
operator who cannot change the backend still has `cache_ttl = "0"`. The error
names your backend and the settings file that declared it.

(inventory-backend-failures)=

### Saying why a fetch failed

When the snapshot cache wraps you and a fetch raises, the class you raise
decides whether the snapshot on disk is served in its place. The decision is
otto's, made once, and the age of the snapshot is shown either way:

- {class}`~otto.inventory.InventoryDataError` for bad DATA from the source:
  missing output, output that does not parse, a record missing a field you
  need. A snapshot is never served for it.
- {class}`~otto.inventory.InventoryUnreachableError` for a TRANSIENT outage:
  a refused connection, a timeout. A snapshot is served only while its total
  age is at most the `max_stale` you pass: `InventoryUnreachableError(message,
  max_stale=...)`. Without one, it is never served.
- A plain {class}`~otto.inventory.InventoryError` keeps the behaviour every
  backend had before the typed errors: a snapshot of any age is served, with a
  warning. The built-in NetBox backend raises only this.

`max_stale` is your backend's setting, so add it to your config model as an
optional string and parse it with {func}`~otto.inventory.parse_max_stale`,
which reads the `cache_ttl` grammar and names `max_stale` in its errors:

```python
from datetime import timedelta

from pydantic import ConfigDict

from otto.inventory import InventoryUnreachableError, parse_max_stale
from otto.models import OttoModel
from my_company.cmdb import fetch_keys


class MyInventoryConfig(OttoModel):
    model_config = ConfigDict(frozen=True)

    url: str
    max_stale: str | None = None


class MyInventory:
    def __init__(self, *, url: str, max_stale: str | None) -> None:
        self.url = url
        self._max_stale: timedelta | None = parse_max_stale(text=max_stale)

    def list_keys(self) -> list[str]:
        try:
            return fetch_keys(self.url)
        except ConnectionError as e:
            raise InventoryUnreachableError(f"{self.url}: {e}", max_stale=self._max_stale) from e
```

Name the key exactly `max_stale`. Like `cache_ttl`, it is a key otto
interprets, whatever your backend does with it, and unlike `cache_ttl` it is
not part of the snapshot's identity: an operator who raises it during an
outage is served the snapshot already on disk instead of a fresh, empty one,
and any other key name would move the snapshot when it changes. Never give
`max_stale` another meaning in your configuration.

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

If your backend implements `inventory_discovery()`
({ref}`inventory-discovery`), the helper also checks it against the rest of
your backend: discovery's record keys must equal `list_keys()`, each record
must equal what `lookup` returns for its key, and `instances` must be `None`
or a dict of {class}`~otto.models.inventory.InventoryInstance` by name. A
discovery that reads its upstream by a different route than `lookup` and
drifts from it fails here, instead of a template seeing one machine and a lab
another.

## Migration note for custom lab repositories

The {doc}`lab-source backend <lab-source-backends>` protocol gained an
argument: `load_lab(name, *, preferences=None, inventory=None)`, and
`list_host_summaries(*, inventory=None)` alongside it. A backend that builds hosts
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
