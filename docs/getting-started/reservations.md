# Reservations

Once more than one person shares the bed, otto can refuse to run against
hardware someone else holds. It never books anything: it asks a **backend**
who holds what, and compares that with what the run needs.

## What a run needs

Identifiers, declared at three levels in the lab file — the lab as a whole,
an element, a host — and required for whatever is in play. The labs table,
then the whole `bb1350` element, whose two `resources` lines sit on the
element and on its host:

```{literalinclude} ../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "labs"'
:end-before: '"_doc_end": "labs"'
```

```{literalinclude} ../examples/getting-started/lab_data/lab.json
:language: json
:start-after: '"_doc_begin": "bb1350"'
:end-before: '"_doc_end": "bb1350"'
```

Holding `bb-bench` is holding the bench; `bb1350-chassis` the element;
`bb1350-slot` one host in it. The fleet of interest from
{doc}`boards-of-interest` is what is in play — five guests — so the
requirement is the bench plus the chassis and slot identifiers of the guests
that declare them. The built-in `local` host is never in play.
{doc}`../guide/cli/reservation/index` defines the three levels and how the
required set is computed from the hosts in play;
{doc}`../guide/configuration/lab-config` is the field reference for
`resources`.

## The shipped backend

A JSON file. Selecting it is two tables in `.otto/settings.toml`. The example
keeps them in a separate file, `reservations.toml`, so that every page before
this one ran with no gate. To try it in your own project, paste both
tables at the end of `.otto/settings.toml` and create `reservations.json`
beside `.otto/`, with your own login name in place of `chris`:

```{literalinclude} ../examples/getting-started/reservations.toml
:language: toml
:start-after: "# doc: begin reservations"
:end-before: "# doc: end reservations"
```

```{literalinclude} ../examples/getting-started/reservations.json
:language: json
```

`otto reservation check` prints what the lab requires, whether the identity
holds each, and the verdict a gated command would reach. The identity is your
login name unless `--as-user` overrides it, and the `reservations.json` above
names `chris`, so the walkthrough passes `--as-user`. `alice` holds nothing:

```{literalinclude} ../examples/getting-started/captures/reservation-check-refused.txt
:language: text
```

`chris` holds all three:

```{literalinclude} ../examples/getting-started/captures/reservation-check-ok.txt
:language: text
```

{doc}`../guide/cli/reservation/index` covers identity (`otto reservation
whoami` shows yours), `-R` to skip the gate with a loud warning, and why a
backend that cannot answer fails the run rather than letting it through.

## A backend of your own

The JSON file is a stand-in for the scheduler your team already has. A
backend subclasses `ReservationBackendBase`, implements its three read-only
methods, and forwards the two constructor arguments otto passes (`url`,
`repo_dir`) to the base; this one reads a text file, and everything but the
file read and its `path` setting is what every backend looks like:

```{literalinclude} ../examples/getting-started/libs/gs_example/reservations.py
:language: python
:start-after: "# doc: begin team-backend"
:end-before: "# doc: end team-backend"
```

Registered by name from the `init` module, then selected by that name:

```{literalinclude} ../examples/getting-started/libs/gs_example/__init__.py
:language: python
:start-after: "# doc: begin register-backend"
:end-before: "# doc: end register-backend"
```

```{literalinclude} ../examples/getting-started/reservations.toml
:language: toml
:start-after: "# doc: begin team-backend-config"
:end-before: "# doc: end team-backend-config"
```

The base class catches a forgotten method at instantiation. What it cannot
check is meaning — that a failure raises rather than returning empty, that
identifiers match the lab file byte for byte — so otto also ships the
conformance test a backend must pass, and this page runs it on the backend
above every time the documentation builds (`GS_EXAMPLE` and the `sys.path`
line are as on {doc}`boards-of-interest`):

```{doctest}
>>> import sys
>>> sys.path.insert(0, str(GS_EXAMPLE / "libs"))
>>> from otto.testing import assert_reservation_backend_conforms
>>> from gs_example.reservations import TeamFileBackend
>>> assert_reservation_backend_conforms(
...     TeamFileBackend(repo_dir=GS_EXAMPLE, path="team-reservations.txt"),
...     known_user="chris",
...     known_resources=["bb-bench", "bb1350-chassis", "bb1350-slot"],
... )
```

The rules it checks — never mutate, return the full set, raise for every
failure, match identifiers byte for byte — and the optional capabilities
(reservation windows, username completion: implement the method and otto
detects it) are in {doc}`../library/reservation-backends`.
