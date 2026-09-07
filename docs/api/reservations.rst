reservations
============

The reservations package gates every live-lab subcommand on whether the
effective user actually holds the resources the selected lab needs.
It is pluggable: the check itself is fixed, but the "who has what
reserved?" query is answered by a
:class:`~otto.reservations.protocol.ReservationBackend`
implementation — shipped ones, or your own class selected by registered name in
``.otto/settings.toml``.

For narrative setup, configuration, and writing a custom backend, see
the :doc:`user guide <../guide/cli/reservation/index>`.

Package summary
---------------

.. automodule:: otto.reservations
   :no-members:

The backend contract
--------------------

Third-party backends satisfy the :class:`~otto.reservations.protocol.ReservationBackend`
Protocol.  The contract is deliberately small — two read-only
methods, no write methods of any kind.  Otto never mutates scheduler
state.  The recommended way to satisfy it is to inherit
:class:`~otto.reservations.ReservationBackendBase`, which declares the two
methods as abstract, adds the cached ``reservations`` member every consumer
reads, and spells out the constructor the factory calls.

.. autoclass:: otto.reservations.ReservationBackendBase
   :members:

``fetch_reservations`` answers in :class:`~otto.reservations.protocol.Reservation`
records (``backend_name`` returns a plain ``str``), so every backend reports
*when* a booking runs.  The rules those times obey — the window predicate, and
what a ``None`` bound means on the query versus on a row — are stated once,
under "The query window" in :doc:`../library/reservation-backends`.  Porting a
backend written for otto 0.10 is covered in the same page's "Migrating from
0.10".

.. autoclass:: otto.reservations.Reservation
   :no-index:

Two optional capabilities sit alongside the contract: a backend that can
enumerate its users implements ``list_usernames``, and one that can answer the
inverted "who holds this resource, and until when?" query implements
``holders``.  Both are detected structurally with ``isinstance`` — implement
the method or don't.  Omitting ``holders`` degrades only the refusal message,
which then reports the holders as unknown;
:doc:`../library/reservation-backends` is the implementer's guide to both.

.. autoclass:: otto.reservations.SupportsUsernameCompletion

.. autoclass:: otto.reservations.SupportsResourceHolders

.. automodule:: otto.reservations.protocol

Exceptions
----------

Two exceptions classify the two failure modes a caller cares about:
*the user doesn't hold something* versus *we couldn't ask*.  They are
surfaced differently in the CLI — see
:ref:`skip-flag-hint-policy` below.

.. autoexception:: otto.reservations.check.ReservationBackendError
   :no-index:

.. autoexception:: otto.reservations.check.MissingReservationError
   :no-index:

The check
---------

.. automodule:: otto.reservations.check

.. _skip-flag-hint-policy:

Skip-flag hint policy
~~~~~~~~~~~~~~~~~~~~~

Only :class:`~otto.reservations.check.ReservationBackendError` surfaces
a suggestion to pass ``--skip-reservation-check`` / ``-R`` — because
with a broken backend the user has no other way to proceed.
:class:`~otto.reservations.check.MissingReservationError` deliberately
does *not* mention the flag, since offering it on every contention
failure trains users to reach for the bypass instead of fixing the
underlying reservation.

Identity resolution
-------------------

.. automodule:: otto.reservations.identity

Bundled backends
----------------

JSON backend
~~~~~~~~~~~~

Reference implementation and test double — also a perfectly usable
production backend for small teams that don't have a scheduler yet.
See the :doc:`user guide <../guide/cli/reservation/index>` for the file format.

.. automodule:: otto.reservations.json_backend

Null backend
~~~~~~~~~~~~

Selected when no ``[reservations]`` section is configured at all, or when
``backend = "none"`` is set.
:func:`~otto.reservations.check.check_reservations` recognizes this
type and becomes a no-op.

.. automodule:: otto.reservations.null_backend

Backend factory
---------------

.. autofunction:: otto.reservations.build_backend

.. autofunction:: otto.reservations.register_reservation_backend

.. autofunction:: otto.reservations.build_reservation_gate

.. automodule:: otto.reservations.registry

Extension points for implementers
---------------------------------

A custom backend needs three pieces:

1. **A class** that satisfies :class:`~otto.reservations.protocol.ReservationBackend`.
   Inherit :class:`~otto.reservations.ReservationBackendBase` and implement its two
   abstract methods.  Protocol satisfaction is structural, so a class that merely has
   the two methods also works — but the base also supplies the cached
   ``reservations`` member the conformance helper requires, which a structural
   backend must then provide itself.
2. **An init module** that registers the class under a bare name::

      from otto.reservations import register_reservation_backend
      register_reservation_backend("my-team-jira", MyBackend)

   The init module must be importable (add its containing directory to ``libs = [...]``
   in ``.otto/settings.toml``, or install it into the same environment) and listed under
   ``[init]`` in ``.otto/settings.toml``.
3. **A ``[reservations]`` entry** selecting the registered name::

      [reservations]
      backend = "my-team-jira"

   Optional per-backend kwargs go in a ``[reservations.my-team-jira]`` sub-table and
   are passed to the constructor alongside the optional ``url`` setting.

The factory always passes ``repo_dir=`` and ``username=``, adds ``url=`` when the
setting is present, and passes every ``[reservations.<name>]`` key as a further
keyword argument — ``Class(url=url, repo_dir=..., username=..., **kwargs_from_settings)``.
Accept or omit ``url`` as fits your deployment; forward the otto-owned arguments
to ``super().__init__``.

See :doc:`../getting-started/reservations` for a worked example — a small
backend, its ``init`` module registration, its ``[reservations]`` table, and
the conformance test that proves it — and
:doc:`../library/reservation-backends` for the full implementer's contract.
