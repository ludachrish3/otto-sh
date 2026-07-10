Link
====

The ``otto.link`` package models connectivity between lab hosts as one
``Link`` type regardless of where it came from: implicit SSH/telnet hop
edges and declared ``lab.json`` routes. ``otto.link.derive`` resolves those
edges at lab-load time. A link is a topology *edge* — the route that
exists; the live, host-resident tunnels built over it are
``otto.tunnel``'s concern (see the :doc:`tunnel guide <../guide/tunnel>`
and :doc:`API reference <tunnel>`).

.. automodule:: otto.link
   :members:
   :exclude-members: Link, LinkEndpoint, Provenance, make_link_id, make_static_link_id

.. automodule:: otto.link.model
   :members:

.. automodule:: otto.link.derive
   :members:

.. automodule:: otto.models.link
   :members:
