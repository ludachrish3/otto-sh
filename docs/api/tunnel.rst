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
their parsers. The verdicts themselves come from :doc:`otto.check <check>`.
``check_tunnel`` and ``TunnelCheckReport`` are importable from
``otto.tunnel``, and are documented once, under ``otto.tunnel.check``. See
the :doc:`check guide <../cli/tunnel/check>` for what the check proves and
how to read it.

.. automodule:: otto.tunnel
   :members:
   :exclude-members: CARRIERS, TunnelCarrier, build_carrier, register_carrier,
      Tunnel, TunnelHop, Direction, Role, ProcKey, make_tunnel_id,
      SENTINEL_PREFIX, ParsedSentinel, encode_sentinel, parse_sentinel,
      DiscoveredTunnel, TunnelDiscovery, TunnelNotMeasuredError,
      discover_tunnels, AddedTunnel, DryRunPlan,
      RemovedReport, add_tunnel, remove_tunnel, remove_all_tunnels,
      TunnelCheckReport, check_tunnel

.. automodule:: otto.tunnel.model
   :members:

.. automodule:: otto.tunnel.sentinel
   :members:

.. automodule:: otto.tunnel.carrier
   :members:

.. automodule:: otto.tunnel.socat
   :members:

.. automodule:: otto.tunnel.discovery
   :members:

.. automodule:: otto.tunnel.manage
   :members:

.. automodule:: otto.tunnel.check
   :members:

.. automodule:: otto.tunnel.check_probes
   :members:
