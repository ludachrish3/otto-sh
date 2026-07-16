Link
====

The ``otto.link`` package models connectivity between lab hosts as one
``Link`` type regardless of where it came from: implicit SSH/telnet hop
edges and declared ``lab.json`` routes. ``otto.link.derive`` resolves those
edges at lab-load time. A link is a topology *edge* — the route that
exists; the live, host-resident tunnels built over it are
``otto.tunnel``'s concern (see the :doc:`tunnel guide <../guide/network/tunnel>`
and :doc:`API reference <tunnel>`).

Impairment builds on that same edge model: ``otto.link.params`` is the typed
parameter set and its unit/merge rules, ``otto.link.placement`` resolves
*where* one direction's netem lands (endpoint or in-path middlebox),
``otto.link.impairer`` is the pluggable ``LinkImpairer`` registry
(``otto.link.netem`` the first-party NetEm registrant), ``otto.link.manage``
is the merge-read-modify-verify orchestration behind ``otto link
impair``/``repair``/``list``, and ``otto.link.sentinel`` tags the detached
``--expire`` timer processes. See the :doc:`link guide <../guide/network/link>` for
CLI usage, the in-path model, and the Python API.

.. automodule:: otto.link
   :members:
   :exclude-members: AppliedPlacement, DirectionState, FlowDirection, IMPAIRERS,
      ImpairReport, ImpairmentParams, Link, LinkEndpoint, LinkImpairer,
      LinkState, NetEmImpairer, Placement, Provenance, RepairReport,
      ScopedState, Selector, build_impairer, find_link, impair_link,
      make_link_id, make_static_link_id, parse_percent, parse_rate,
      parse_time_ms, read_link_states, register_impairer, repair_all,
      repair_link

.. automodule:: otto.link.model
   :members:

.. automodule:: otto.link.derive
   :members:

.. automodule:: otto.link.params
   :members:

.. automodule:: otto.link.impairer
   :members:

.. automodule:: otto.link.netem
   :members:

.. automodule:: otto.link.placement
   :members:

.. automodule:: otto.link.manage
   :members:

.. automodule:: otto.link.sentinel
   :members:

.. automodule:: otto.models.link
   :members:
