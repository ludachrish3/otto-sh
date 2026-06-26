reservations
============

The reservations package gates every live-lab subcommand on whether the
effective user actually holds the resources the selected lab needs.
It is pluggable: the check itself is fixed, but the "who has what
reserved?" query is answered by a :class:`~otto.reservations.protocol.ReservationBackend`
implementation — shipped ones, or your own class selected by registered name in ``.otto/settings.toml``.

For narrative setup, configuration, and writing a custom backend, see
the :doc:`user guide <../guide/reservations>`.

Package summary
---------------

.. automodule:: otto.reservations
   :no-members:

The backend contract
--------------------

Third-party backends implement the :class:`~otto.reservations.protocol.ReservationBackend`
Protocol.  The contract is deliberately small — three read-only
methods, no write methods of any kind.  Otto never mutates scheduler
state.

.. autoclass:: otto.reservations.SupportsUsernameCompletion

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
See the :doc:`user guide <../guide/reservations>` for the file format.

.. automodule:: otto.reservations.json_backend

Null backend
~~~~~~~~~~~~

Default when no ``[reservations]`` section is configured, or when
``backend = "none"`` is set.
:func:`~otto.reservations.check.check_reservations` recognizes this
type and becomes a no-op.

.. automodule:: otto.reservations.null_backend

Backend factory
---------------

.. autofunction:: otto.reservations.build_backend

.. autofunction:: otto.reservations.register_reservation_backend

.. autofunction:: otto.reservations.build_reservation_state

.. automodule:: otto.reservations.registry

Extension points for implementers
---------------------------------

A custom backend needs three pieces:

1. **A class** that satisfies :class:`~otto.reservations.protocol.ReservationBackend`.
   Protocol satisfaction is structural — no explicit inheritance is required (and none is
   recommended).
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

The factory calls the class as ``Class(url=url, **kwargs_from_settings)`` when ``url``
is set in settings, otherwise ``Class(**kwargs_from_settings)``.  Accept or omit ``url``
as fits your deployment.

See the :doc:`user guide <../guide/reservations>` for a worked example
with request handling, credential loading, and package layout.
