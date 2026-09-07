# Reservation object API — one query, one object, every backend reports time

**Date:** 2026-09-07
**Status:** Designed (this session); awaiting implementation plan
**Depends on:** nothing unlanded. Builds on the shipped reservation
subsystem — the `ReservationBackend` protocol (`src/otto/reservations/protocol.py`),
`ReservationBackendBase` (`a44f9aff`), the gate in
`src/otto/reservations/check.py`, and the conformance helper in
`src/otto/testing/conformance.py`.
**Breaking:** yes — targeted at 0.11.0, which is where breaking belongs.
**On citations:** this spec names files and symbols, not line numbers. Two
review passes found the line numbers in earlier revisions to be the single
largest source of error — they were estimated rather than read, and they rot
against the tree regardless. Grep for the symbol.

## 1. Goal

A reservation backend should answer one question, once, and return objects
that say when each booking ends:

```python
backend.reservations                # -> list[Reservation], mine, active now
backend.holders("rack3-psu")        # -> [Reservation(user="bob", end=15:30)]
```

Today that takes three methods, two of which return bare strings, and the
start/end times live behind an *optional* capability
(`SupportsReservationWindows`) that a backend may decline to implement. The
consequence is that otto cannot reliably tell a user their reservation is
about to expire — the data is only there if the backend opted in.

### In scope

- A `Reservation` record replacing `ReservationWindow` (§3).
- A two-method backend contract built on it, with a cached `reservations`
  data member (§4).
- A `SupportsResourceHolders` capability for the inverted query, so
  backends that can only query per user stay fully supported (§4.5).
- Deleting `get_reserved_resources`, `who_reserved` and the
  `SupportsReservationWindows` capability outright (§4, §8).
- An expiry warning at the gate (§6).
- Conformance, consumer, and documentation updates (§5, §7, §9).

### Out of scope

- `SupportsUsernameCompletion` — unchanged.
- Otto writing reservations. Otto remains strictly a consumer; the
  scheduler stays authoritative.
- Making the warning threshold configurable. It is a module constant.
- Any back-compatibility shim for old-style backends (§8).

## 2. What is actually wrong today

The working note that prompted this — `todo/reservation-backend-interface-improvements.md`,
an untracked local file not present in the repository at this commit —
names `_holdings()` and its `list[tuple[str, str]]` return as the defect. That
method is **not** part of otto's API: it is a private helper inside the
getting-started docs example
(`docs/examples/getting-started/libs/gs_example/reservations.py`), called by
nothing in `src/otto/`. Its tuple is worth deleting, but doing so is a docs
edit, not a breaking change, and it is not the reason to spend 0.11.0's
breaking-change budget here.

The real defects in the shipped contract are:

1. **Time is optional.** `get_reserved_resources` returns `set[str]`. A
  backend that knows when a booking ends may say so via
  `get_reservation_windows`, or may not. Every consumer must therefore
  handle "I have no idea when this expires", and the expiry warning
  (§6) would fire for some users and silently never fire for others.
2. **Sentinel dates stand in for missing knowledge.** The JSON backend uses
  the epoch for "start unknown" and `9999-01-01` for "never expires". A
  warning that reasons about `end` has to special-case a lie.
3. **Two shapes for one question.** `get_reserved_resources(user)` and
  `get_reservation_windows(user)` are the same query at two fidelities,
  and the conformance helper has to assert they agree.
4. **The inverted query is string-only.** `who_reserved(resource)` returns
  usernames with no times, so "who holds this and until when?" needs two
  calls and cannot be answered atomically.

## 3. The `Reservation` record

`ReservationWindow` is **replaced** by `Reservation` in
`src/otto/reservations/protocol.py` — same module, same frozen-dataclass
style, plus the holder and nullable times. No alias is kept.

```python
@dataclass(frozen=True)
class Reservation:
 user: str
 resource: str
 start: datetime | None = None
 end: datetime | None = None

 def expires_within(
 self, delta: timedelta, *, now: datetime | None = None
    ) -> bool: ...
```

Rules, all enforced by the conformance helper (§7):

- One row per `(user, resource)` window. A booking covering three racks
  yields three `Reservation`s — the same granularity `ReservationWindow`
  has today.
- `user` and `resource` are non-empty `str`.
- `start` and `end`, when not `None`, are timezone-aware, and
  `start <= end` when both are present.
- `start is None` means "held since before this backend knows".
  `end is None` means **open-ended: never expires**. There are no sentinel
  dates; `_EPOCH` and `_FAR_FUTURE` are deleted from the JSON backend.
- `expires_within` returns `False` when `end is None`. `now` defaults to
  `datetime.now(timezone.utc)`; the parameter exists so the warning's tests
  can freeze the clock without patching module globals.

`user` is load-bearing, not decoration: `holders(resource)` returns
`Reservation`s belonging to *other* people (§4.5), so `user` is the answer
to the inverted "who holds this?" query. On rows from
`fetch_reservations(username)` it necessarily equals the queried username,
and the conformance helper asserts exactly that.

## 4. The backend contract

### 4.1 Shape

```python
class ReservationBackendBase(ABC):
 def __init__(
 self,
        *,
 url: str | None = None,
 repo_dir: Path | None = None,
 username: str | None = None,
    ) -> None:
        ...

    @cached_property
 def reservations(self) -> list[Reservation]:
        """The caller's reservations, active right now. Queried once."""
 return self.fetch_reservations(self.username)

    @abstractmethod
 def fetch_reservations(
 self,
 username: str,
 start: datetime | None = None,
 end: datetime | None = None,
    ) -> list[Reservation]: ...

    @abstractmethod
 def backend_name(self) -> str: ...
```

The optional capability, alongside the untouched
`SupportsUsernameCompletion`:

```python
@runtime_checkable
class SupportsResourceHolders(Protocol):
    """Answers the inverted query: who holds *resource*, and until when?"""

 def holders(self, resource: str) -> list[Reservation]: ...
```

Required methods drop from three to two. `get_reserved_resources`,
`who_reserved`, and the entire `SupportsReservationWindows` protocol are
deleted. `username` is a required `str`: there is no sentinel value
meaning "everyone", and therefore one query shape rather than two
overloaded onto one signature.

**Nothing calls `fetch_reservations` at construction time.** It runs on
first access to `reservations` and never again. The implementer's rule is
unchanged from today: call `super().__init__(...)` forwarding otto's
kwargs, then set up your own state — there is no fetch to remember. An
author who deliberately wants to fail fast may pre-seed with
`self.reservations = self.fetch_reservations(self.username)` after
`super().__init__()` (assigning to a `cached_property`'s name seeds the
instance `__dict__`); this is documented as available but not recommended,
for the reasons in §4.3.

### 4.2 `fetch_reservations` semantics

- **`username` is required.** Every call names exactly one user. A
  scheduler that can only answer per-user queries satisfies this method as
  written; nothing about the required contract asks it to enumerate (§4.5).
- **`None` bounds are infinite, on both sides of the comparison.** A row's
  `start is None` reads as "since forever" and its `end is None` as "never
  ends"; a query's `start`/`end` default to this instant. This is not a
  corner case: the JSON file format records only `expires`, so **every** row
  from otto's reference backend has `start=None`, and the predicate below is
  meaningless without this rule.
- **The window predicate is OVERLAP.** A reservation is returned when it
  overlaps `[start, end]` — *not* when it is contained in it, and *not*
  when it begins within it. A booking running yesterday 22:00 →
  tomorrow 10:00 is active right now and MUST be returned for a window of
  "now". Getting this wrong makes the gate fail open: otto sees a held rack
  as free and admits a second user onto someone else's hardware. §7 makes
  this a named conformance test rather than a docstring aspiration.
- **Both bounds default to this instant.** `fetch_reservations(user)` with
  no window returns that user's reservations active now. This is the
  *tightest* bound,
  not the widest — it is the smallest result set that answers both of
  otto's questions, and it avoids the "whose midnight?" ambiguity a
  day-shaped default carries for a lab spanning timezones. A caller wanting
  a day's schedule passes explicit bounds.
- **Expired rows are omitted** for any window that ends at or before now.
- **Order is not significant.** Otto sorts where it displays.
- **Every failure raises `ReservationBackendError`** — network, I/O,
  credentials, malformed data. Never return an empty list to signal
  failure: empty means "nobody holds anything", which the gate turns into
  a refusal that blames the user.

### 4.3 Why `reservations` is a lazy `cached_property`

Three constraints converge on this and rule out the alternatives:

**It cannot be populated in `__init__`.** The base's constructor runs before
the subclass finishes building itself. Both shipped backends demonstrate the
failure:

```python
# json_backend.py — the fetch would happen on the super() line...
def __init__(self, url=None, *, path: Path) -> None:
 super().__init__(url=url)
 self._path = Path(path)      # ...but _path does not exist yet
```

The getting-started example has the identical shape, as would any
third-party backend holding a session, token, or connection. Eager
population raises `AttributeError` inside *the implementer's* code.

**It must not be decorated with `functools.cache`.** `@cache` on a method
in a class body is flagged by ruff `B019` (verified: otto's `.ruff.toml`
selects `ALL` and does not ignore it) because the cache lives on the class
function and keyed on `self`, pinning every instance alive forever.
`cached_property` stores into the instance `__dict__` and dies with the
instance. It is legal precisely because moving `username`/`start`/`end`
onto `fetch_reservations` leaves `reservations` with no parameters.

**Laziness preserves behaviour otto deliberately built.** From
`src/otto/cli/reservation.py`: *"Nor is the backend queried —
check_reservations returns on an empty requirement before it ever asks, and
a table that asked first would fail this command on a backend outage where
it used to succeed."* Construction must stay query-free; a lab requiring no
reservations must not start failing on scheduler outages.

### 4.4 Why `reservations` is NOT in the Protocol

`ReservationBackend` (the `runtime_checkable` Protocol) declares
`fetch_reservations` and `backend_name` — methods only. So does the
`SupportsResourceHolders` capability Protocol (§4.5).

`isinstance()` against a `runtime_checkable` Protocol **evaluates** a
non-method member. Verified on 3.10: an `isinstance` check against a
Protocol declaring `reservations` as a property triggered the underlying
fetch; a methods-only Protocol, and a concrete-class `isinstance`, evaluate
nothing.

Otto runs five Protocol `isinstance` checks today: three in the conformance
helper, plus `SupportsReservationWindows` in `cli/remote_completion.py` and
`SupportsUsernameCompletion` in `config/completion_cache.py`. After this
change the set is the conformance helper, `SupportsUsernameCompletion` in
completion, the new `SupportsResourceHolders` check in the refusal path
(§4.5), and the new `ReservationBackend` check in `build_backend` (§8).

Two places that *look* like hazard sites are not: `build_backend` performs no
Protocol check at all today — its only `isinstance` tests that a settings
value is a `str` — and `is_null_backend` tests a **concrete class**,
`NullReservationBackend`, which evaluates no members.

Every one of those sites is a reason the Protocol stays methods-only. Adding
`reservations` to it would put a scheduler query behind an `isinstance`,
including on the completion path, where it would run on every TAB.

The member is therefore guaranteed by `ReservationBackendBase` and
**enforced by the conformance helper** (§7), which asserts its presence and
type for inheriting and structural backends alike. Inheriting from the base
becomes the strongly recommended path; a structural backend must provide
`reservations` itself to pass conformance.

### 4.5 Backends that can only query per user

Some schedulers answer only "what does user X hold?" — there is no
enumerate-everything call. Such a backend implements `fetch_reservations`
and `backend_name`, omits `holders`, and is fully supported: gate, `otto
reservation check` table, remote-path completion, and the expiry warning
all work. Only the refusal message degrades.

**Nothing in the required contract asks a backend to enumerate.** That is
the property that makes this work, and it came from noticing why an
all-users query was ever proposed: the only reader of *other users' reservations* in
the codebase is the inverted "who holds this?" query, at one call site —
the refusal message in `check.py`. (`list_usernames` also enumerates
across users, but it returns names for `--as-user` completion, never
reservations, and §1 puts it out of scope unchanged.) Enumeration was a *means* to that end.
Making the end itself the capability removes the means entirely, along
with the `username=None` sentinel, the flag that would have declared
support for it, and the exception that would have reported its absence.

So the capability is `holders(resource) -> list[Reservation]`, detected
with `isinstance(backend, SupportsResourceHolders)` — the same structural
idiom as `list_usernames` / `SupportsUsernameCompletion`, and safe against
the §4.4 evaluation hazard because the Protocol declares only a method. A
backend that can serve the inverted query implements it, by enumerating or
by a targeted lookup, whichever its scheduler supports. One that cannot
omits it. There is nothing to declare and nothing to forget: **failing to
implement the method is the declaration.**

`base.py`'s stated principle — *"Optional capabilities stay structural. A
backend signals one by implementing the method… there is no flag to set"*
— therefore stands unamended. Its capability list still names **two**:
`SupportsReservationWindows` is replaced by `SupportsResourceHolders`, while
`SupportsUsernameCompletion` is untouched.

**The username reaches the backend at construction.** `reservations` is a
zero-argument property, so it needs to know whose reservations to fetch,
and otto holds the resolved identity while the backend does not.
`build_backend` gains a `username` parameter and
`ReservationBackendBase.__init__` accepts and stores it alongside
`repo_dir`. `reservations` is then unconditionally
`fetch_reservations(self.username)` — no flag, no `isinstance`, no branch
on the verdict path.

Two wiring details this is **not** a no-op for:

- `build_reservation_gate` currently resolves identity *after* building the
  backend (`reservations/__init__.py` then ). The order must
  invert — there is no backend to build until we know who is asking.
- The `_factory` closure stored on the gate as `backend_factory`
  (`reservations/__init__.py`) is the path `otto reservation
  whoami`/`check` use to build a backend on demand
  (`cli/reservation.py`). It must close over the username too, or
  those commands get a backend that cannot answer `reservations`.

`username` is `str | None` on the constructor, because
`config/completion_cache.py` builds a backend with no identity purely
to call `list_usernames`. Accessing `reservations` on such an instance
raises `ReservationBackendError` naming the missing username rather than
querying for `None`; that call site never touches the member.

**Degradation must not be silent.** The old `who_reserved` returned
`list[str]`, and an empty list means "nobody holds it" — the message
prints the word `nobody`. A single-user backend that answered the inverted
query with an empty list would tell a locked-out engineer that the rack
they cannot get is held by nobody: a confident lie at the moment they most
need the truth. Absence of the capability is therefore checked before the
call, never inferred from an empty result, and the message says `unknown`.

**`holders` returns objects, not usernames.** The inverted query is
symmetric with the forward one, which keeps `Reservation.user` load-bearing
and lets the refusal say when the resource frees up:

```text
 rack3-psu host bench-07 (held by: alice until 15:30)
 rack4-psu host bench-08 (held by: dana until 16:00, sam)
```

Multiple holders join with `", "`, preserving today's documented
multi-holder form (`docs/guide/cli/reservation/index.md` shows
`held by: dana, sam`). A holder whose booking is open-ended (`end is None`)
contributes a bare name with no `until` clause — there is no release time to
promise. Holders sort by `(user, resource)` so the line is stable.

That is the fact a blocked engineer actually needs — not only whom to
chase, but whether it is worth waiting. A backend without the capability
yields `(held by: unknown — this backend cannot report other users)`.

**`check_reservations` keeps its `username` parameter, and it must agree
with the backend's.** The signature stays
`check_reservations(lab, username, backend, ...)`, while
`backend.reservations` caches rows for the username the backend was
*constructed* with. If the two disagreed,
`{r.resource for r in backend.reservations if r.user == username}` would
silently yield an empty held set — a refusal blaming a user whose
reservations were never fetched. Every caller passes
`gate.identity.username` today, so they do agree; the function nonetheless
raises `RuntimeError` when `backend.username` is set and differs, as a
construction invariant in the same spirit as the existing "identity must be
resolved before evaluate() runs".

## 5. Consumer changes

| Site | Change |
|---|---|
| `reservations/check.py` | Held set becomes `{r.resource for r in backend.reservations if r.user == username}` — unconditional, no capability branch (§4.5). The `ReservationGateResult` carries the fetched list so the warning (§6) reuses one query. |
| `reservations/check.py` | The refusal loop tests `isinstance(backend, SupportsResourceHolders)`, calls `holders(resource)` when present, and writes `(held by: unknown — this backend cannot report other users)` when absent. Holder rows carry `end`, so the message can say `until 15:30`. |
| `reservations/__init__.py` | `build_backend` gains a `username` parameter. **Not** simply "beside `repo_dir`": the built-in branches pass no `repo_dir` at all today ( is `cls()`, is `cls(url=url, path=path)`; only the custom branch at forwards it), and `JsonReservationBackend.__init__` does not accept it. So all three branches change explicitly, and the JSON backend's constructor grows `username`. Also adds the Protocol check described in §8. |
| `reservations/__init__.py` | `build_reservation_gate` resolves identity **before** building the backend (today it is after), and the `_factory` closure captures the username so `backend_factory` builds a usable backend for `whoami`/`check`. |
| `cli/remote_completion.py` | The `isinstance(..., SupportsReservationWindows)` branch and its flat-TTL fallback both disappear. One path; every backend now reports times. |
| `config/remote_completion_cache.py` | Parameter becomes `reservations: list[Reservation]`. **Three** `None`-sensitive sites, not one, and only the first is behaviour-preserving: (a) `_valid_until` — dropping a `None` edge keeps the flat TTL, which the `9999` sentinel already achieved by accident; (b) `store_reservation_windows` calls `w.start.isoformat()`, which raises `AttributeError` on `None`; (c) `cached_reservation_ok` counts a window active only when `start is not None and end is not None`, so an open-ended row would read as **not held** and completion would refuse. `SCHEMA_VERSION` bumps to 2 so old entries are discarded rather than misread. |
| `config/remote_completion_cache.py` | `store_reservation_set` and the `resource_set` branch of `cached_reservation_ok` become dead — the flat-TTL fallback existed only for backends that could not report windows, and there are none now. Both are deleted; the schema bump handles existing files. |
| `cli/remote_completion.py` | `active = {w.resource for w in windows if w.start <= now <= w.end}` raises `TypeError` on a `None` bound; the comparison becomes `None`-aware alongside the capability branch's removal. |
| `cli/reservation.py` (`check` table) | Held set becomes `set() if null else {r.resource for r in backend.reservations if r.user == username}` — the **same predicate as the verdict's** in `reservations/check.py`, filter included. The two are one release apart in the same refusal: the table renders held/not-held directly above a verdict that names the unheld resources, so a backend returning a foreign user's row under a filter present on one side and absent on the other prints "yes" above a refusal calling that same resource missing. The null-backend short-circuit is **kept**. It is deliberate and documented in `null_backend.py`: the null backend holds nothing, so querying it renders every requirement unheld directly above an OK verdict, and a second spelling of "what none means" is how the table and the verdict drift apart. Note this is the `check` command; `whoami` is a different function and is untouched. |
| `cli/reservation.py` (`whoami`) | **Unchanged.** An earlier draft added a `reservations:` block here. Cut: `whoami` queries no backend today — it calls `backend_name()` and nothing else — and `docs/guide/cli/reservation/whoami.md` advertises it as the fast, lab-free way to confirm which backend a repo resolves to, one that "never contacts a host". Adding a scheduler query would make it fail on an outage, trading a documented property for a feature nobody asked for. `otto reservation check` already reports held state. |
| `config/completion_cache.py` | Passes `username=None` to `build_backend`; it only wants `list_usernames`, which is unchanged. |
| `reservations/json_backend.py` | `get_reserved_resources` + `get_reservation_windows` collapse into one `fetch_reservations` honouring the window with overlap semantics. `_EPOCH` and `_FAR_FUTURE` deleted; a missing `expires` key becomes `end=None`. `who_reserved` becomes `holders`, returning `Reservation`s, and the class declares `SupportsResourceHolders`. |
| `reservations/null_backend.py` | `fetch_reservations` returns `[]`. |
| `examples/reservations.py` | Rewritten against the new contract; its doctests change. |
| `docs/examples/.../gs_example/reservations.py` | `_holdings()` and its tuple deleted. Parses `<user> <resource>` lines into `Reservation(user=..., resource=...)` with both times `None` — the truth about a file format that records no times. |

## 6. The expiry warning

```python
EXPIRY_WARNING_WINDOW = timedelta(minutes=5)   # module constant, not a setting
```

A reservation warns when it (a) belongs to the resolved username, (b) names
a resource in `required_resources()`, and (c) satisfies
`expires_within(EXPIRY_WARNING_WINDOW)`. Each produces one bold-yellow log
line naming the resource and the minutes remaining.

- `end is None` never warns.
- Scope is **only resources this run needs** — the ones that would reject
  the next command. Racks held but untouched are not mentioned; that is
  noise on every gated command.
- It is a warning, never a refusal. An expiring reservation is still a
  valid one.

### 6.1 Where it fires

The warning needs the held rows, so it can only run where a query is already
happening. Three constraints fix its placement:

**It cannot live in `check_reservations`.** That function has three call
sites — `ReservationGate.evaluate`, the `otto reservation check` command, and
the out-of-fleet named-host check in `cli/host.py`. Lab-level resources are
required on every one of them, so a single implementation inside
`check_reservations` warns twice about the same booking on any run that names
an out-of-fleet host.

**But `host.py` must still warn.** An earlier draft claimed it need not,
because "the gate already warned". That is wrong. `required_resource_origins`
adds element- and host-level resources only for the *selected* hosts, and
`host.py` passes precisely the hosts that are **not** in
`get_hosts_in_play()` — the set the gate used. So an out-of-fleet host's own
resources were never in the gate's `needed` set, and are exactly the ones the
command is about to use.

**It must not create a query where otto deliberately avoids one.** Both
`check_reservations` and `otto reservation check` return before touching the
backend when the backend is null or the requirement is empty — the property
§4.3 quotes and preserves, so that a lab needing no reservation survives a
scheduler outage.

So: `warn_expiring_reservations(reservations, needed, *, now=None)` is called
from all three sites, each **after** that site's existing short-circuits and
reusing the rows it has already fetched. It never triggers a query of its own.
Duplicate suppression is a module-level set of `(resource, end)` pairs already
warned in this process, so a booking is announced once per run however many
sites observe it. The set is process-scoped state and is cleared by a fixture
in tests.

`--skip-reservation-check` suppresses the warning at the gate and at
`host.py`, both of which return early under `-R`, but **not** in `otto
reservation check`, which is registered `lab_free=True, gate=False` and builds
its backend through `backend_factory` even under `-R`. That asymmetry is
correct: `-R` means "do not block me", and the command whose entire job is
reporting reservation status should still say the booking is lapsing.

Firing from a `lab_free=True` verb is safe, but not because the gate only runs
on lab-bound verbs — it does not. It is safe because `otto.cli.main` installs
the console log handler unconditionally in the root callback for every
subcommand. Note the one place that is *not* true: shell completion returns
before the handler is installed, so anything logged on the completion path is
invisible (§8).

## 7. Conformance



`assert_reservation_backend_conforms` carries the weight of this change,
because it is the only thing a third-party backend runs against. The
`SupportsReservationWindows` block folds into the required path.

Assertions, each with a named failure message:

1. `fetch_reservations` and `backend_name` are callable.
2. `reservations` exists and is a `list` of `Reservation`.

   **There is no construction-time rule here.** An earlier draft asserted
  `"reservations" not in vars(backend)` to prove the base did not fetch
  during `__init__`. That is unusable: the helper receives an
  already-constructed instance, and a pre-seeded cache (§4.1, explicitly
  permitted) is indistinguishable from an eager base. The laziness of
  `ReservationBackendBase.reservations` is otto's own invariant, tested in
  otto's unit tests for the base class, not asserted against third parties.

   When `getattr(backend, "username", None)` is a non-empty `str`, every row
  must belong to that user. A backend built without an identity — which
  `config/completion_cache.py` does deliberately, to call `list_usernames` —
  has `username is None`; accessing `reservations` on it raises
  `ReservationBackendError` per §4.5, so the helper **catches that and
  records a skipped rule**, never letting it escape as anything but an
  `ExpectCollector` entry.
3. Rows: `user` and `resource` are non-empty `str`; `start`/`end` are `None`
  or timezone-aware; `start <= end` when both are present.
4. `fetch_reservations(username=u)` returns only rows with `user == u`.
5. **Overlap:** the fail-open rule, and it gets its own test.

   The helper cannot *create* a straddling booking — otto never writes
  reservations, and the helper is handed only `known_user` and
  `known_resources`. So the rule is checked differentially against the
  backend's own data: every resource returned by the unbounded call must
  still be returned for a narrow window bracketing now (`now - 1s` to
  `now + 1s`). A booking active right now overlaps any window containing
  now, so containment semantics drop rows here and overlap semantics do not
   — no fixture required. A backend whose unbounded call returns nothing is
  skipped for this rule, and the helper says so rather than passing
  silently.
6. **Only when `isinstance(backend, SupportsResourceHolders)`:** `holders(r)`
  returns `Reservation`s, obeys the same row rules as (3), and agrees with
  `fetch_reservations` in both directions — a resource in user *u*'s rows
  must yield a holder row for *u*, and vice versa. A backend without the
  capability is skipped here, not failed.
7. `SupportsUsernameCompletion` — unchanged.

Per the repo's standing rule that a guard must inject the hostile
condition, every one of these gets a test that feeds `assert_..._conforms`
a **violating** double and asserts the specific message — not merely a
conforming backend that passes. `ExpectCollector` dumps local variables
into its failure text, so each test's `match=` anchors on the exact
distinctive phrase of the rule under test, or it will pass for the wrong
reason.

Other tests:

- JSON backend: round-trip including expiry skipping, `end=None` for a
  missing `expires`, and a window query whose result depends on overlap.
- Per-user-only backend: an end-to-end test with a double that omits
  `holders` — the gate reaches the same verdict as a capable equivalent,
  and the refusal message says `unknown` rather than `nobody`. Its
  counterpart with the capability present asserts the holder and the
  `until <time>` fragment.
- Warning: frozen clock; fires at 4m59s, does not fire at 5m01s, does not
  fire for a resource the lab does not require, does not fire for
  `end=None`, and — at the gate — does not fire under
  `--skip-reservation-check`. A separate test asserts it DOES fire from
  `otto reservation check` under `-R`, per §6.1.
- Double-fire: a run that names an out-of-fleet host warns once, not twice
  (§6.1).
- `remote_completion_cache`: `end is None` produces no boundary
  invalidation.
- Doctests in `src/otto/examples/reservations.py` and the getting-started
  example. Both are also exercised by `make docs`, whose
  `doctest_global_setup` differs from `--doctest-modules`.

**Blast radius.** Sixteen existing test files reference the removed API and
must be migrated:
`tests/unit/reservations/{test_base,test_check,test_gate,test_protocol,test_registry,test_build_backend,test_json_backend,test_null_backend}.py`,
`tests/unit/cli/{test_reservation,test_remote_completion,test_host}.py`,
`tests/unit/config/{test_remote_completion_cache,test_completion_cache_usernames}.py`,
`tests/unit/testing/test_conformance.py`,
`tests/unit/examples/{test_reservations,test_reservations_cli}.py`.
`tests/unit/cli/test_host.py` is the non-obvious one — it exercises the
out-of-fleet named-host check of §6.1.

## 8. Migration

Hard cutover. `get_reserved_resources`, `get_reservation_windows`,
`ReservationWindow`, and `SupportsReservationWindows` are gone in 0.11.0
with no shim and no deprecation period. `who_reserved` is gone as a
*required* method: its replacement, `holders`, is optional and returns
`Reservation`s (§4.5).

That asymmetry is the one migration hazard. Every other removal fails
loudly at startup; a backend that ports `fetch_reservations` but never
notices `who_reserved` became `holders` starts up clean and silently
prints `unknown` in refusals it used to answer. The porting checklist
therefore leads with it.

Constructors change shape too: `ReservationBackendBase.__init__` gains
`username`, which subclasses forward via `super().__init__()` like
`repo_dir` (§4.5). Breaking a constructor is cheap only while the contract
above it is already breaking, which is why it happens now rather than
later.

**`build_backend` gains a Protocol check it does not have today.** This is
new work, not a refinement of existing behaviour. Today a structural backend
defining only the old methods is constructed and returned successfully, then
fails at first use with a bare `AttributeError`; only a backend *inheriting*
`ReservationBackendBase` fails early, at instantiation, with Python's own
`TypeError` naming the missing abstract methods. So `build_backend` adds
`isinstance(backend, ReservationBackend)` after construction and, on
rejection, inspects the instance for a `get_reserved_resources` or
`get_reservation_windows` attribute; finding one, it raises a message naming
the removed method, naming `fetch_reservations`, and pointing at the
migration section of `docs/library/reservation-backends.md`.

The check raises `ReservationBackendError`, not `ValueError`: the CLI's
reservation command catches only the former, so a `ValueError` would reach
the user as a raw traceback instead of the migration message this is for.

An **inheriting** old-style backend fails earlier and differently — Python
raises `TypeError` from `cls(...)` for the unimplemented abstract methods,
which `build_backend` does not translate and the CLI does not catch. That
too becomes a traceback, so `build_backend` wraps construction and re-raises
a `TypeError` naming an abstract method as the same `ReservationBackendError`
with the same migration pointer.

The claim "every other removal fails loudly at startup" is therefore true
only once both of those exist, and only for the removed *required* methods. A leftover `who_reserved` cannot be caught this way — the backend is
otherwise valid — so otto additionally warns when a backend defines
`who_reserved` but not `holders`, a reliable signature of a half-finished
port. Two placement constraints: `build_backend` runs on every gate
resolution, so the warning must fire **once per process**, not once per
command; and it must not be emitted on the shell-completion path, where the
console handler is not yet installed and the line would be swallowed
(§6.1). It therefore lives behind the same process-scoped guard as the
expiry warning and is skipped when completion is active.

## 9. Documentation

- `docs/library/reservation-backends.md` — the implementer's page.
  Rewritten around the two-method contract, with a migration section and an
  section on `SupportsResourceHolders` (§4.5) — what implementing
  `holders` buys, and that omitting it degrades only the refusal message.
- `src/otto/reservations/base.py` module docstring — its capability list
  gains `holders` / `SupportsResourceHolders` beside `list_usernames`. Its
  "there is no flag to set" principle stands unamended (§4.5).
- `docs/api/reservations.rst` — more than autodoc entries. Drop
  `SupportsReservationWindows` and `ReservationWindow`, add `Reservation`
  and `SupportsResourceHolders` — and rewrite the surrounding prose, which
  becomes false: "three read-only methods", "declares the three
  methods as abstract", "Two optional capabilities", "the
  three methods also works", and the factory-call description at
  `docs/library/reservation-backends.md` carries the same
  "three methods" claim.
- `docs/guide/cli/reservation/check.md` and
  `docs/guide/cli/reservation/index.md` — both print the refusal line
  (`(held by: dana)`, `held by: dana, sam`) whose format §4.5 changes.
- `src/otto/examples/reservations_cli.py` — a fourth `build_backend`
  call site (`build_backend({"backend": backend_name}, repo_dir=Path.cwd())`)
  that must pass a username.
- `docs/guide/cli/reservation/windows.md` — **rewritten**, not patched. It
  documents an optional capability that ceases to exist; it becomes a page
  about reservation times, which every backend now reports.
- `docs/architecture/subsystems/reservations.md` — the capability list
  loses one entry.
- Release notes for 0.11.0 carry the breaking change. `CHANGELOG.md` is
  cliff-generated from commit subjects and is never hand-edited.

## 10. Risks and open items

- **`ty` and the `cached_property` / Protocol interaction.** A concrete
  `cached_property` on an ABC consumed through a methods-only Protocol may
  draw a complaint. Fallback if it does: keep the member but annotate it
  explicitly; this is an implementation-time check, not a design decision.
- **The `build_backend` Protocol check is new behaviour.** Adding
  `isinstance(backend, ReservationBackend)` where none existed means a
  backend that otto used to construct successfully (and fail later) is now
  refused at startup. That is the intent, but it is a behaviour change in
  its own right, not a side effect of the contract edit — see §8.
- **`holders` is uncached.** Unlike `reservations` it is called per
  missing resource, on a path that is already raising. A backend that
  implements it by enumerating will scan once per resource in the refusal;
  the docs say so, and the refusal loop is bounded by the missing set.
- **`reservations` is a per-run snapshot, and that is a move toward
  fail-open.** `cached_property` pins the held set at first access for the
  life of the backend. The same gate backend serves the preamble check and,
  later in the same run, the out-of-fleet host check; under today's API each
  site issued a fresh query. On a long `otto test`, a reservation that
  expires mid-run still reads as held. That is the intended trade — one
  query per run was the goal — but it is a real weakening of a gate, and the
  expiry warning (§6) is the mitigation: the user is told the booking is
  about to lapse rather than silently running past it.
- **The cached list is shared.** `cached_property` hands every caller the
  same list object. `Reservation` is frozen, so rows are safe, but a
  consumer that mutates the list corrupts the cache for the rest of the
  run. The contract states that callers treat it as read-only; this is
  documented, not enforced by copying.
- **A backend without `holders` loses the holder list in refusals**, the
  one place the new contract gives an implementer less than a fully
  capable one. It is a message, never a verdict (§4.5).
- **`holders` is easy to leave unimplemented by accident.** Structural
  detection cannot tell "cannot" from "did not get round to it", so an
  otherwise-capable backend silently prints `unknown`. The migration doc
  must list it as a thing to port, since today's `who_reserved` is
  required and its replacement is not.
