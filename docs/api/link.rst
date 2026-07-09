Link
====

The ``otto.link`` package models connectivity between lab hosts as one
``Link`` type regardless of where it came from: implicit SSH/telnet hop
edges, declared ``lab.json`` routes, and the live host-resident tunnels
``add_link`` creates. ``otto.link.manage`` and ``otto.link.discovery`` are
the callable library API behind ``otto link add`` / ``list`` / ``remove``;
``otto.link.socat`` is the pure command-builder layer they spawn. For CLI
usage, tunnel construction, and host requirements, see the
:doc:`user guide <../guide/link>`.

.. automodule:: otto.link
   :members:
   :exclude-members: Link, LinkEndpoint, Provenance, make_link_id, all_links,
      discover_dynamic_links, AddedTunnel, RemovedReport, add_link,
      remove_all_links, remove_link

.. automodule:: otto.link.model
   :members:

.. automodule:: otto.link.derive
   :members:

.. automodule:: otto.link.sentinel
   :members:

.. automodule:: otto.link.discovery
   :members:

.. automodule:: otto.link.manage
   :members:

.. automodule:: otto.link.socat
   :members:

.. automodule:: otto.models.link
   :members:
