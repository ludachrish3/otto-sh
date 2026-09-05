# Reservation backends

Otto reads reservation state through a **backend**: a small class that
answers "who holds this resource right now?" against whatever scheduler
your team already uses. The `json` backend ships with otto
({doc}`../guide/cli/reservation/json-backend`); anything else is a class you
register from your own repo.

When your team already has a scheduler (Jira, a web API, a database), write a
backend that talks to it instead of using the JSON file. A backend is a
subclass of [`ReservationBackendBase`](../api/reservations.rst) that
implements its three abstract read-only methods (`get_reserved_resources`,
`who_reserved`, `backend_name`). Otto never calls a write method; the
scheduler stays authoritative.

The base class is the recommended starting point, not a requirement: what
otto actually checks is the [`ReservationBackend`](../api/reservations.rst)
Protocol, satisfied by any class with the three methods. Inheriting buys you a
`TypeError` naming any method you forgot the moment the class is
instantiated, and a constructor that already accepts the two keyword
arguments otto passes — `url` (when the setting is present) and `repo_dir`
(always). Declare your own `[reservations.<name>]` settings as further keyword
parameters and forward the two otto-owned ones to `super().__init__`:

```python
from pathlib import Path

from otto.reservations import ReservationBackendBase, ReservationBackendError


class MyTeamBackend(ReservationBackendBase):
    def __init__(
        self, *, url: str | None = None, repo_dir: Path | None = None, api_key_env: str
    ) -> None:
        super().__init__(url=url, repo_dir=repo_dir)
        self._api_key_env = api_key_env

    def get_reserved_resources(self, username: str) -> set[str]: ...
    def who_reserved(self, resource: str) -> list[str]: ...
    def backend_name(self) -> str: ...
```

Otto ships a small, dependency-free reference implementation —
[`otto.examples.reservations.ExampleReservationBackend`](../api/examples.rst) —
that you can copy from `src/otto/examples/reservations.py` as a starting point.
It demonstrates a multi-holder `who_reserved`, a stable `backend_name`, and the
optional `list_usernames` completion capability:

```{doctest}
>>> from otto.examples.reservations import ExampleReservationBackend
>>> backend = ExampleReservationBackend()
>>> backend.backend_name()
'example'
>>> sorted(backend.get_reserved_resources("alice"))
['lab-a', 'shared']
>>> backend.who_reserved("shared")
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
`MyTeamBackend(url="https://jira.example.com", repo_dir=<repo root>, api_key_env="JIRA_API_KEY")`
— the `[reservations.<name>]` sub-table becomes keyword arguments, `url` is
passed when present, and `repo_dir` is always passed for resolving any
relative paths. Selecting an unregistered name raises an error listing the
registered backends. This is the same named-registry mechanism otto uses for
host sources, term/transfer backends, and host classes; an `init` module always
imports before the reservation check runs, so the name is registered in time.
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
...     ExampleReservationBackend(),
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
        MyTeamBackend(url="https://jira.example.com"),
        known_user="alice",
        known_resources=["rack3-psu"],
    )
```

## Contract rules for implementers

- **Never mutate.** Otto only reads from the scheduler. Writes, releases,
  extensions — all stay in the scheduler's own UI/API.
- **Return the user's full reserved set** from `get_reserved_resources`. Don't
  pre-filter against what otto "might need" — otto does that filtering itself,
  and doing it twice loses information for the error message.
- **`who_reserved` returns a `list[str]`.** Return every username currently
  holding the resource, in a deterministic order with duplicates removed. An
  **empty list** means no one holds it — there is no `None` sentinel, and a
  resource can have any number of concurrent holders.
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
- **Accept `repo_dir`.** Otto always passes `repo_dir=<repo root>` — use it to
  anchor any relative path-like settings your own backend accepts, the same
  way custom lab backends do.
- **Optionally implement `list_usernames()`** to power cached `--as-user`
  completion (see [Username tab-completion](../guide/cli/reservation/identity.md#username-tab-completion)).
- **Optionally implement `get_reservation_windows()`** if your scheduler knows
  *when* bookings start and end (see
  [Reservation windows](../guide/cli/reservation/windows.md)).

## Signalling optional capabilities

Implementing the method **is** the signal. Otto detects each capability with
`isinstance` against a `runtime_checkable` Protocol —
[`SupportsUsernameCompletion`](../api/reservations.rst) is "has a callable
`list_usernames`", [`SupportsReservationWindows`](../api/reservations.rst) is
"has a callable `get_reservation_windows`". There is no flag to set, nothing to
register, and nothing on the base class to override: add the method to your
subclass and the feature is on from the next run.

If you want the intent visible in the class header, name the capability
Protocol as an extra base — `class MyTeamBackend(ReservationBackendBase,
SupportsReservationWindows)`. That changes nothing at runtime; it lets a type
checker hold your signature to the contract.

## Using the reservation library in your own CLI

Everything above walks through `otto`'s own subcommands. The library itself —
`otto.reservations` — has no dependency on Typer, rich, or any other part of
otto's CLI, so a completely separate tool (a deploy script, a CI gate, your
own CLI) can run the exact same check without going through `otto` at all.
Four steps:

1. **Build** a backend from your tool's own settings with
   [`build_backend`](../api/reservations.rst). An unconfigured (or `"none"`)
   backend setting resolves to
   [`NullReservationBackend`](../api/reservations.rst) — a no-op, so this step
   needs no live scheduler to exercise in a test.
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
>>> run_check(demo, backend=ExampleReservationBackend(), identity=resolve_username("alice"))
alice: OK
0
>>> run_check(demo, backend=ExampleReservationBackend(), identity=resolve_username("carol"))
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
