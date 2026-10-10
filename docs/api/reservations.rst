reservations
============

The reservations package gates every live-lab subcommand on whether the
effective user actually holds the resources the selected lab needs.
It is pluggable: the check itself is fixed, but the "who has what
reserved?" query is answered by a
:class:`~otto.reservations.protocol.ReservationBackend`
implementation — shipped ones, or your own backend selected by registered name
in ``.otto/settings.toml``.

For narrative setup, configuration, and writing a custom backend, see
the :doc:`CLI reference <../cli/reservation/index>`.

The backend contract
--------------------

Third-party backends satisfy the :class:`~otto.reservations.protocol.ReservationBackend`
Protocol.  The contract is deliberately small — two read-only
methods, no write methods of any kind.  Otto never mutates scheduler
state.  The recommended way to satisfy it is to inherit
:class:`~otto.reservations.ReservationBackendBase`, which declares the two
methods as abstract, adds the cached ``reservations`` member every consumer
reads, and keeps the ``url``, ``repo_dir`` and ``username`` your factory hands
it.

``fetch_reservations`` answers in :class:`~otto.reservations.protocol.Reservation`
records (``backend_name`` returns a plain ``str``), so every backend reports
*when* a booking runs.  The rules those times obey — the window predicate, and
what a ``None`` bound means on the query versus on a row — are stated once,
under "The query window" in :doc:`../cookbook/extending/reservation-backends`.  Porting a
backend written for otto 0.10 is covered in the same page's "Migrating from
0.10".

Two optional capabilities sit alongside the contract: a backend that can
enumerate its users implements ``list_usernames``, and one that can answer the
inverted "who holds this resource, and until when?" query implements
``holders``.  Both are detected structurally with ``isinstance`` — implement
the method or don't.  Omitting ``holders`` degrades only the refusal message,
which then reports the holders as unknown;
:doc:`../cookbook/extending/reservation-backends` is the implementer's guide to both.

Extension points for implementers
---------------------------------

A reservation backend is a *configured* backend.  It is registered from an
``init`` module with
:func:`~otto.reservations.register_reservation_backend`, which takes a config
model and a factory (``register_reservation_backend(name, *, config, factory,
overwrite=False)``), and selected by that name with ``backend = "<name>"`` in
the ``[reservations]`` table.  Otto parses the ``[reservations.<name>]``
sub-table with the config model, then calls the factory with a
:class:`~otto.registry.Configured` carrying that parsed configuration and a
:class:`~otto.reservations.ReservationEnv` (``url``, ``repo_dir``,
``username`` and ``origin``).  The factory returns the backend.  Any failure on
that path raises :class:`~otto.reservations.ReservationConstructionError`.
:data:`~otto.reservations.RESERVATION_BACKENDS` is the registry itself.

"Selecting it in settings" in :doc:`../cookbook/extending/reservation-backends`
is the one home for that contract: the config model, the factory, the
environment and the errors.

See :doc:`../getting-started/reservations` for a worked example — a small
backend, its ``init`` module registration, its ``[reservations]`` table, and
the conformance test that proves it — and
:doc:`../cookbook/extending/reservation-backends` for the full implementer's contract.

The package
-----------

.. automodule:: otto.reservations
