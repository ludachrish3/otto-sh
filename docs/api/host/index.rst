host
====

The host package provides the core abstractions for running commands on
local and remote machines. The pages below are its extension namespaces:
the modules a backend or a custom host class imports from.

``run`` and ``exec`` return a :class:`~otto.result.CommandResult` per command (fields in
:doc:`../../architecture/utilities/results`); what its exit code, output and timeout
promise on each kind of host is on :ref:`Command contracts <command-contracts>`.

.. toctree::

   transfer
   options
   command_frame
   limitation
   exec_route
   login_proxy
   session_setup
   app_shell
   embedded_filesystem
   product
   dev_tool

.. automodule:: otto.host
