Tunnel
======

The ``otto.tunnel`` package builds and tears down **host-resident
bidirectional tunnels** — one end-to-end forwarding path per ``add_tunnel``
call, realized as mirrored chains of tagged processes riding the topology
edges modelled by ``otto.link``. What each process actually executes is a
pluggable **carrier** (``otto.tunnel.carrier``'s ``TunnelCarrier`` contract +
``CARRIERS`` registry); ``socat`` remains the first-party default.
``otto.tunnel.manage`` and ``otto.tunnel.discovery`` are the callable
library API behind ``otto tunnel add`` / ``list`` / ``remove``;
``otto.tunnel.socat`` is the socat carrier plus the pure command-builder
layer it wraps, and ``otto.tunnel.sentinel`` is the argv-tag codec that
makes every running process self-describing. For CLI usage, multi-hop
chains, docker endpoints, and host requirements, see the
:doc:`CLI reference <../cli/tunnel/index>`.

``otto tunnel check`` builds on all of it: ``otto.tunnel.check`` is
``check_tunnel`` and its ``TunnelCheckReport``, and
``otto.tunnel.check_probes`` the probe scripts, the tagged echo listeners and
their parsers. The verdicts themselves come from :doc:`otto.check <internals/check>`.
``check_tunnel`` and ``TunnelCheckReport`` are documented below, with the rest of the
package. See
the :doc:`check guide <../cli/tunnel/check>` for what the check proves and
how to read it.

.. automodule:: otto.tunnel
   :members:
