# Reservation backends

Otto reads reservation state through a **backend**: a small class that
answers "who holds this resource right now, and until when?" against whatever
scheduler your team already uses. The `json` backend ships with otto
({doc}`../guide/cli/reservation/json-backend`); anything else is a class you
register from your own repo.

When your team already has a scheduler (Jira, a web API, a database), write a
backend that talks to it instead of using the JSON file. A backend is a
subclass of [`ReservationBackendBase`](../api/reservations.rst) that
implements its two abstract read-only methods — `fetch_reservations` and
`backend_name`. Otto never calls a write method; the scheduler stays
authoritative.

```python
def fetch_reservations(
    self, username: str, start: datetime | None = None, end: datetime | None = None
) -> list[Reservation]: ...
def backend_name(self) -> str: ...
```

Both bounds default to *this instant*, so the unbounded call is "what does
this user hold right now?". The rest of the rules the returned times must obey
— timezone-awareness, one row per `(user, resource)`, and the rest — are in
[Contract rules for implementers](#contract-rules-for-implementers) below.
{doc}`../guide/cli/reservation/windows` is the other half: what otto *does*
with the times once a backend reports them.

```{warning}
**`None` means the OPPOSITE thing on the two sides of this call.** Getting it
backwards is the one mistake here that fails *open* — otto admits a user whose
booking has already lapsed — and nothing downstream catches it: the check gate
does not re-filter by `end`. The conformance helper checks both directions, but only
the dropping one is provable against any fixture; see
[How the window predicate is checked](#how-the-window-predicate-is-checked).

| `None` on… | means | so the unbounded call is |
|---|---|---|
| the **query** parameters `start` / `end` | **this instant** — substitute `now` for the missing bound | the **tightest** query there is: "what is active right now?" |
| a returned **`Reservation`**'s `start` / `end` | **unbounded** — held since before you know, or open-ended and never expiring | a row that matches every window |

Reading the query's `start=None` as "-infinity" is the failure: you then return
every booking that ever existed, expired ones included.
```

## The query window

Substitute `now` for each missing *query* bound, then return a row **iff**:

```python
(row.start is None or row.start <= end) and (row.end is None or row.end > start)
```

That is **overlap**, not containment: a booking that began before `start` and
ends after `end` is active during the window and must come back. `>` on the
second clause is what drops rows that have already lapsed at the instant asked
about — dropping lapsed rows is this comparison, not a separate pass. That
strict `>` is deliberately not the inclusive `<=` in
[`Reservation.is_active`](../api/reservations.rst), which asks about a single
instant rather than a range; its docstring names the one row the two disagree
about and why. A `None` on a *row* satisfies its clause unconditionally, which is what
"unbounded" means.

A backend whose rows carry both bounds writes it out once — this is the body
of the `fetch_reservations` left as `...` in the skeleton below:

```python
@override
def fetch_reservations(
    self, username: str, start: datetime | None = None, end: datetime | None = None
) -> list[Reservation]:
    now = datetime.now(timezone.utc)
    window_start = start if start is not None else now
    window_end = end if end is not None else now
    return [
        Reservation(user=username, resource=b.resource, start=b.begins, end=b.ends)
        for b in self._bookings_for(username)
        if (b.begins is None or b.begins <= window_end)
        and (b.ends is None or b.ends > window_start)
    ]
```

The shipped JSON backend is the one-sided case of exactly this: its file format
records no start, so `b.begins` is always `None`, the first clause is always
true, and only `b.ends > window_start` can exclude an entry.

## Writing the class

The base class is the recommended starting point, not a requirement: what
otto actually checks is the [`ReservationBackend`](../api/reservations.rst)
Protocol, satisfied by any class with the two methods. Inheriting buys you a
`TypeError` naming any method you forgot the moment the class is
instantiated, the cached `reservations` member every consumer reads (and the
conformance helper requires), and a constructor that already accepts the three
keyword arguments otto passes — `url` (when the setting is present),
`repo_dir` (always) and `username` (always). Declare your own
`[reservations.<name>]` settings as further keyword parameters and forward the
otto-owned ones to `super().__init__`:

```python
from datetime import datetime
from pathlib import Path

from typing_extensions import override

from otto.reservations import Reservation, ReservationBackendBase


class MyTeamBackend(ReservationBackendBase):
    def __init__(
        self,
        *,
        url: str | None = None,
        repo_dir: Path | None = None,
        username: str | None = None,
        api_key_env: str,
    ) -> None:
        super().__init__(url=url, repo_dir=repo_dir, username=username)
        self._api_key_env = api_key_env

    @override
    def fetch_reservations(
        self, username: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[Reservation]: ...

    @override
    def backend_name(self) -> str: ...
```

`@override` on both required methods is not decoration: it is also what keeps
ruff's `ARG002` quiet about a `start` / `end` your implementation may not read
— see [A note on `@override` in the samples](#a-note-on-override-in-the-samples).

## One query per run

`reservations` is a `cached_property` on the base — the resolved user's
bookings, active right now. It is queried **lazily, on first access, and never
again**, so one otto run makes one scheduler query and every consumer reads
the same list.

Nothing fetches at construction time. That is deliberate on both sides:

- The base constructor runs on the `super().__init__()` line, before your
  subclass has assigned its own state, so an eager fetch would read
  `self._path` or `self._session` before they exist.
- Merely *building* a backend must not contact the scheduler. Otto builds one
  for commands that turn out to require no reservation at all, and those runs
  must survive a scheduler outage.

A backend that genuinely wants to fail fast may pre-seed the cache with
`self.reservations = self.fetch_reservations(self.username)` after
`super().__init__()` — assigning to a `cached_property`'s name seeds the
instance `__dict__`. This is available but **not recommended**, for the two
reasons above.

The cached list is handed to every caller as the same object. `Reservation`
is frozen, so rows are safe; treat the list itself as read-only, because
mutating it corrupts the cache for the rest of the run.

## The optional `holders` capability

`fetch_reservations` answers "what does user X hold?". The *inverted* query —
"who holds resource Y, and until when?" — is the optional
[`SupportsResourceHolders`](../api/reservations.rst) capability:

```python
def holders(self, resource: str) -> list[Reservation]: ...
```

Implement it if your scheduler can answer it, by enumerating its schedule or
by a targeted lookup, whichever it supports. Otto uses it in exactly one
place: the refusal message, which then names who is holding the resource you
were denied and when it frees up —

```text
  rack3-psu  host bench-07  (held by: alice until 15:30)
  rack4-psu  host bench-08  (held by: dana until 16:00, sam)
```

A scheduler that answers only per-user queries omits the method and is
**fully supported**: the gate, `otto reservation check`, remote-path
completion and the expiry warning all work unchanged. The only thing that
degrades is that one message, which then reads `(held by: unknown — this
backend cannot report other users)`. Otto checks for the capability before
calling it and never infers absence from an empty result: an empty list is a
definite `nobody`, and telling a locked-out engineer that the rack they
cannot get is held by nobody would be a confident lie.

`holders` is *not* cached. It is called once per missing resource on a path
that is already raising, so a backend that implements it by enumerating will
scan once per missing resource in a refusal.

Otto ships a small, dependency-free reference implementation —
[`otto.examples.reservations.ExampleReservationBackend`](../api/examples.rst) —
that you can copy from `src/otto/examples/reservations.py` as a starting point.
It demonstrates a multi-holder `holders`, a stable `backend_name`, and the
optional `list_usernames` completion capability:

```{doctest}
>>> from otto.examples.reservations import ExampleReservationBackend
>>> backend = ExampleReservationBackend(username="alice")
>>> backend.backend_name()
'example'
>>> [r.resource for r in backend.reservations]
['lab-a', 'shared']
>>> sorted(h.user for h in backend.holders("shared"))
['alice', 'bob']
>>> backend.list_usernames()
['alice', 'bob']
```

## Selecting it in settings

Register the backend under a bare name from an `init` module (one of the modules
in `init = [...]`), then select it by that name:

```python
# my_team_backend.py  (listed in init = [...])
from otto.reservations import register_reservation_backend
from my_company.jira_backend import MyTeamBackend

register_reservation_backend("my-team-jira", MyTeamBackend)
```

```toml
[reservations]
backend = "my-team-jira"
url = "https://jira.example.com"

[reservations.my-team-jira]
api_key_env = "JIRA_API_KEY"
```

Otto constructs the backend as
`MyTeamBackend(url="https://jira.example.com", repo_dir=<repo root>, username=<resolved identity>, api_key_env="JIRA_API_KEY")`
— the `[reservations.<name>]` sub-table becomes keyword arguments, `url` is
passed when present, `repo_dir` is always passed for resolving any
relative paths, and `username` is always passed because `reservations` is a
zero-argument member and has to know whose bookings to fetch. Selecting an
unregistered name raises an error listing the registered backends. This is the
same named-registry mechanism otto uses for host sources, term/transfer
backends, and host classes; an `init` module always imports before the
reservation check runs, so the name is registered in time.
See {doc}`Extension points <../architecture/subsystems/extension-points>` for
the registry machinery behind this and every other seam otto can be extended
at.

## Verify your backend

Otto ships a conformance helper that checks a backend against the full contract
and reports every violation at once (a single `AssertionError` listing each
failed rule). The shipped sample conforms:

```{doctest}
>>> from otto.testing import assert_reservation_backend_conforms
>>> from otto.examples.reservations import ExampleReservationBackend
>>> assert_reservation_backend_conforms(
...     ExampleReservationBackend(username="alice"),
...     known_user="alice",
...     known_resources=["lab-a", "shared"],
... )
```

Call it from your own suite. Passing `known_user` / `known_resources` (resources
that user is known to hold) enables the round-trip consistency rules against your
own fixtures:

```python
from otto.testing import assert_reservation_backend_conforms
from my_team_backend import MyTeamBackend


def test_my_backend_conforms():
    assert_reservation_backend_conforms(
        MyTeamBackend(url="https://jira.example.com", username="alice"),
        known_user="alice",
        known_resources=["rack3-psu"],
    )
```

### How the window predicate is checked

The [window predicate](#the-query-window) gets its own rule, and it has to be a
**differential** one: otto never writes reservations, so the helper cannot
stage a booking that straddles a boundary. Instead it asks unbounded, then
re-asks for a narrow window bracketing right now, and requires every resource
from the first answer back in the second — which a correct backend satisfies
and a containment-reading one does not.

A backend whose user currently holds nothing is **reported as skipped** for
this rule rather than passing silently, so pass `known_user=` for someone who
does hold something.

The other direction — returning **too much** — gets its own rule, because the
differential above cannot see it: it computes "unbounded minus narrow", so a
backend that over-returns in *both* calls subtracts to nothing. That is the
misread of `start=None` as "-infinity", which pulls in every booking that ever
existed, expired ones included. So the helper reads the second clause of the
[predicate](#the-query-window) back against the unbounded answer: every row it
returns must satisfy `row.end is None or row.end > now`. A row whose `end` has
passed fails, naming the resource and the instant it ended. `end is None` is
open-ended and always passes — it is not a missing value to treat as expired.

Two things to know about that rule before you lean on it:

- **It forgives one second.** Your scheduler's clock is not the helper's, and
  the round trip costs time; a booking that lapsed microseconds before the
  answer came back is a race, not a defect. One second is also exactly the
  bracket the differential rule uses, so a row still legitimately returned for
  `[now - 1s, now + 1s]` is never failed as lapsed.
- **It is vacuous on a fixture with nothing lapsed.** Conformance runs against
  *your* data, and the helper will not fabricate a row your backend never
  returned. If every booking you hand it is open-ended — as both of otto's own
  samples are — the rule cannot fire, and a green run is not evidence that your
  filter works. Point it at a fixture containing an expired booking if you want
  that evidence.

### Assert the capability you mean to keep

`holders` is optional, and by default the helper honours that: when the backend
does not implement it, **the holder rules are skipped, not failed**. That is
correct for a per-user-only scheduler and a trap for everyone else. Implement
`holders`, ship, then refactor it away during a cleanup, and
`assert_reservation_backend_conforms` keeps passing while your users start
seeing `held by: unknown` in every refusal.

So if your backend *is* meant to answer the inverted query, say so with
`expect_holders=True`. Absence of the capability then stops being a skipped rule
and becomes a named conformance failure:

```python
def test_my_backend_conforms():
    assert_reservation_backend_conforms(
        MyTeamBackend(url="https://jira.example.com", username="alice"),
        known_user="alice",
        known_resources=["rack3-psu"],
        expect_holders=True,
    )
```

The default is `False` and must stay that way: a backend without `holders` is
fully conforming, so the kwarg asserts *your* capability set rather than
tightening the contract. Otto passes it for its own documentation example — see
`test_the_example_reservation_backend_conforms` in
`tests/unit/docs/test_getting_started_example.py`.

{doc}`Lab source backends <lab-source-backends>` carry the same knob,
`expect_host_summaries=`, for their own optional capability.

## Contract rules for implementers

- **Never mutate.** Otto only reads from the scheduler. Writes, releases,
  extensions — all stay in the scheduler's own UI/API.
- **Return the user's full set** from `fetch_reservations`. Don't pre-filter
  against what otto "might need" — otto does that filtering itself, and doing
  it twice loses information for the error message.
- **One row per `(user, resource)` window.** A booking covering three racks is
  three `Reservation`s. On rows from `fetch_reservations(username)`, `user`
  equals the username you were asked about; the conformance helper checks it.
- **Honour the window predicate**, written out in
  [The query window](#the-query-window): overlap rather than containment, and
  `None` meaning *this instant* on a query bound but *unbounded* on a row.
  This is the one rule here that fails open.
- **Times are timezone-aware when present.** A naive `datetime` is a contract
  violation, not a UTC guess.
- **A bound you do not know is `None`, never a sentinel date.** `start is
  None` means the booking has been held since before this backend knows;
  `end is None` means it is open-ended and never expires. The Unix epoch and a
  far-future year are fabricated instants that no reader downstream can tell
  from real ones, and otto's expiry logic would then have to special-case the
  lie.
- **Raise [`ReservationBackendError`](../api/reservations.rst)** for *every*
  failure mode that prevents a definitive answer: network errors, timeouts,
  credential failures, malformed responses, missing data files. Do not swallow,
  do not return empty. The CLI surfaces this specific exception as a fail-closed
  startup error with an `-R` hint — swallowing it means otto proceeds as if the
  user holds nothing, the opposite of fail-closed.
- **String-match byte-for-byte.** The strings you return must match the
  identifiers `required_resources` computes — lab, element and host levels
  alike. Normalize inside your backend, not in otto.
- **`backend_name()` should be stable.** It shows up in diagnostics and skip
  warnings; changing it between versions breaks log-history searches.
- **`url` is optional on both sides.** Accept `url: str | None = None` and use
  it, or hardcode your endpoint and omit it — otto passes `url=` only when the
  setting is present.
- **Accept `repo_dir` and `username`.** Otto always passes both — `repo_dir` to
  anchor any relative path-like settings your own backend accepts, the same
  way custom lab backends do, and `username` as the identity `reservations`
  queries for.
- **Optionally implement `list_usernames()`** to power cached `--as-user`
  completion (see [Username tab-completion](../guide/cli/reservation/identity.md#username-tab-completion)).
- **Optionally implement `holders()`** if your scheduler can answer the
  inverted query — see [above](#the-optional-holders-capability).

## Signalling optional capabilities

Implementing the method **is** the signal. Otto detects each capability with
`isinstance` against a `runtime_checkable` Protocol —
[`SupportsUsernameCompletion`](../api/reservations.rst) is "has a callable
`list_usernames`", [`SupportsResourceHolders`](../api/reservations.rst) is
"has a callable `holders`". There is no flag to set, nothing to
register, and nothing on the base class to override: add the method to your
subclass and the feature is on from the next run. Failing to implement the
method is the declaration that you cannot.

If you want the intent visible in the class header, name the capability
Protocol as an extra base — `class MyTeamBackend(ReservationBackendBase,
SupportsResourceHolders)`. That changes nothing at runtime; it lets a type
checker hold your signature to the contract.

### A note on `@override` in the samples

Otto's own samples decorate `fetch_reservations` and `backend_name` with
`typing_extensions.override` — and deliberately **not** `holders`. Both facts
are load-bearing, not style:

- `@override` on the two required methods says "this implements the base
  class's abstract method", and it doubles as the reason ruff's `ARG002`
  (unused argument) stays quiet about the `start` and `end` parameters a
  time-less sample never reads. Without the decorator you would need a
  `# noqa`, which is how `otto.reservations.null_backend`'s `holders` spells
  the same idea visibly.
- `holders` carries no `@override` because it overrides nothing: it is not on
  the base class, and defining it *is* the capability signal.

## Migrating from 0.10

otto 0.11.0 replaced the three-method, string-returning contract with the
two-method `Reservation` one. It is a hard cutover: there is no shim and no
deprecation period.

**Port `who_reserved` to `holders` first.** It is the one removal that does
not fail loudly. Every other change below stops the backend at startup with a
message naming what to do; a backend that ports `fetch_reservations` and never
notices this one starts clean and silently reports `held by: unknown` in
refusals it used to answer. Otto warns once per process when it sees a backend
defining `who_reserved` but not `holders`, but the warning is the safety net,
not the plan.

| 0.10 | 0.11 |
|------|------|
| `who_reserved(resource) -> list[str]` | `holders(resource) -> list[Reservation]` — now **optional**, and returns rows carrying `user` and `end` |
| `get_reserved_resources(username) -> set[str]` | `fetch_reservations(username, start=None, end=None) -> list[Reservation]` |
| `get_reservation_windows(username)`, the `SupportsReservationWindows` capability, `ReservationWindow` | deleted — every backend now reports times, in `Reservation` |
| epoch `start` / far-future `end` sentinels | `None` on either bound |
| `__init__(self, *, url=None, repo_dir=None)` | `__init__(self, *, url=None, repo_dir=None, username=None)`, forwarded to `super().__init__` |
| a query per consumer | the cached `reservations` member, queried once per run |

The checklist:

1. Rename `who_reserved` to `holders` and return `Reservation` rows instead of
   usernames. Add a test asserting
   `isinstance(backend, SupportsResourceHolders)` — the conformance helper
   will not catch its absence.
2. Collapse `get_reserved_resources` and `get_reservation_windows` into one
   `fetch_reservations`, honouring [the window predicate](#the-query-window).
3. **Inherit `ReservationBackendBase` now, or provide `reservations`
   yourself.** A 0.10 backend that satisfied the contract *structurally* — the
   right methods, no base class — has no `super().__init__` to add `username`
   to in step 4 and no `reservations` member at all. Nothing refuses it at
   startup: it fails later, at gate time, with "has no 'reservations'
   attribute". Inheriting the base is the short way; a backend that cannot
   inherit must expose `reservations` as the invoking user's active rows
   itself.
4. Add `username` to the constructor and forward it to `super().__init__`.
5. Replace any sentinel dates with `None`.
6. Re-run `assert_reservation_backend_conforms` plus your own capability
   assertion.

## Using the reservation library in your own CLI

Everything above walks through `otto`'s own subcommands. The library itself —
`otto.reservations` — has no dependency on Typer, rich, or any other part of
otto's CLI, so a completely separate tool (a deploy script, a CI gate, your
own CLI) can run the exact same check without going through `otto` at all.
Four steps:

1. **Build** a backend from your tool's own settings with
   [`build_backend`](../api/reservations.rst), passing the identity as
   `username=`. An empty settings dict (no
   `[reservations]` table at all) or `backend = "none"` resolves to
   [`NullReservationBackend`](../api/reservations.rst) — a no-op, so this step
   needs no live scheduler to exercise in a test. A dict with keys but no
   `backend` is refused: a present table is a specified checker and must name
   its backend.
2. **Resolve** the effective identity with
   [`resolve_username`](../api/reservations.rst).
3. **Construct** a [`ReservationGate`](../api/reservations.rst) from the
   backend and identity and call `.evaluate()`.
4. **Present** the result yourself. `evaluate()` returns a
   `ReservationGateResult` whose `warning` is plain text — the library never
   touches your terminal. `MissingReservationError` and
   `ReservationBackendError` (the same two exceptions from
   [Fail-closed behavior](../guide/cli/reservation/index.md#fail-closed-behavior))
   are what you catch;
   exit codes, logging, and styling are entirely your call — `otto`'s own CLI
   wraps `warning` in rich markup, nothing here requires you to do the same.

The identity you resolve in step 2 and the `username=` you pass in step 1 must
be the same one: `reservations` caches rows for the username the backend was
*constructed* with, so a mismatch would produce a refusal blaming a user whose
bookings were never fetched. `check_reservations` raises `RuntimeError` rather
than let that happen.

| Exception                 | Raised by                                                      | Means                                                                       |
|---------------------------|-----------------------------------------------------------------|------------------------------------------------------------------------------|
| `MissingReservationError` | `evaluate()` / `check_reservations()`                          | The identity doesn't hold every required resource.                         |
| `ReservationBackendError` | `build_backend()` (construction) or `evaluate()` (query time)  | The backend itself couldn't answer — network, credentials, malformed data. |

A complete, runnable example ships as
[`otto.examples.reservations_cli`](../api/examples.rst)
(`src/otto/examples/reservations_cli.py`) — copy it as a starting point. Its
`run_check()` is steps 3-4, kept separate from the Typer command so it is
directly testable against the Null backend or the
[`ExampleReservationBackend`](../api/examples.rst) sample, no real scheduler
or CLI invocation required:

```{doctest}
>>> from otto.config.lab import Lab
>>> from otto.examples.reservations import ExampleReservationBackend
>>> from otto.reservations import resolve_username
>>> from otto.examples.reservations_cli import run_check
>>> demo = Lab(name="demo", resources={"lab-a"})
>>> run_check(
...     demo,
...     backend=ExampleReservationBackend(username="alice"),
...     identity=resolve_username("alice"),
... )
alice: OK
0
>>> run_check(
...     demo,
...     backend=ExampleReservationBackend(username="carol"),
...     identity=resolve_username("carol"),
... )
carol: User 'carol' does not hold all resources required by lab 'demo'. Missing:
  lab-a  lab demo  (held by: alice)
1
```

Run the full example as a standalone CLI — with no `--backend` flag it falls
back to the Null backend, so this needs no scheduler either:

```bash
python -m otto.examples.reservations_cli --resource rack1
```

If you're also writing a custom backend for your tool (rather than reusing
`json` or `none`), see [Verify your backend](#verify-your-backend) above —
`otto.testing.assert_reservation_backend_conforms` checks the same contract
whether the backend ends up wired into `otto`, your own CLI, or both.
