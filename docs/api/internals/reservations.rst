reservations internals
======================

.. automodule:: otto.reservations.protocol
   :ignore-module-all:

Exceptions
----------

Two exceptions classify the two failure modes a caller cares about:
*the user doesn't hold something* versus *we couldn't ask*.  They are
surfaced differently in the CLI — see
:ref:`skip-flag-hint-policy` below.

.. _the-check:

The check
---------

.. automodule:: otto.reservations.check
   :ignore-module-all:

The report
~~~~~~~~~~

.. automodule:: otto.reservations.report
   :ignore-module-all:

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
   :ignore-module-all:

Bundled backends
----------------

JSON backend
~~~~~~~~~~~~

Reference implementation and test double — also a perfectly usable
production backend for small teams that don't have a scheduler yet.
See the :doc:`CLI reference </cli/reservation/index>` for the file format.

.. automodule:: otto.reservations.json_backend
   :ignore-module-all:

Null backend
~~~~~~~~~~~~

Selected when no ``[reservations]`` section is configured at all, or when
``backend = "none"`` is set.
:func:`~otto.reservations.report.build_report` recognizes this
type and makes no query.

.. automodule:: otto.reservations.null_backend
   :ignore-module-all:

Backend factory
---------------


Backend registry
----------------

.. automodule:: otto.reservations.registry
   :ignore-module-all:
