result
======

:meth:`Host.run <otto.host.host.Host.run>` returns :class:`~otto.result.Results`, one
:class:`~otto.result.CommandResult` per command, and ``exec`` returns one ``CommandResult``.
What their fields mean is in :doc:`../architecture/utilities/results`; what otto promises
about them on each kind of host is on :ref:`the command contract page <command-contract>`.

.. automodule:: otto.result
