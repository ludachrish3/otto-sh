Link
====

The ``otto.link`` package models connectivity between lab hosts as one
``Link`` type regardless of where it came from: implicit SSH/telnet hop
edges and declared ``lab.json`` routes. ``otto.link.derive`` resolves those
edges at lab-load time. A link is a topology *edge* — the route that
exists; the live, host-resident tunnels built over it are
``otto.tunnel``'s concern (see the :doc:`tunnel guide <../cli/tunnel/index>`
and :doc:`API reference <tunnel>`).

Impairment builds on that same edge model: ``otto.link.params`` is the typed
parameter set and its unit/merge rules, ``otto.link.placement`` resolves
*where* one direction's netem lands (endpoint or in-path middlebox),
``otto.link.impairer`` is the pluggable ``LinkImpairer`` registry
(``otto.link.netem`` the first-party NetEm registrant), ``otto.link.manage``
is the merge-read-modify-verify orchestration behind ``otto link
impair``/``repair``/``list``, and ``otto.link.sentinel`` tags the detached
``--expire`` timer processes.

``otto link check`` builds on all of it: ``otto.link.check`` is
``check_link`` and its report, ``otto.link.check_live`` the ``--live``
cycle, ``otto.link.sandbox`` the throwaway namespace it tests in,
``otto.link.probes`` the probe commands and their parsers, and
``otto.link.judge`` the rule that turns each measurement into a verdict. The
verdicts themselves come from :doc:`otto.check <check>`. See the
:doc:`link guide <../cli/link/index>` for CLI usage, the in-path model, and
the Python API.

.. automodule:: otto.link
   :members:
   :exclude-members: AppliedPlacement, DirectionState, DryRunPlan, FlowDirection,
      IMPAIRERS,
      ImpairReport, ImpairmentParams, Link, LinkCheckReport, LinkCommandFailedError,
      LinkEndpoint, LinkHostUnreachableError, LinkImpairer, LinkNotMeasuredError,
      LinkState, NetEmImpairer, Placement,
      Provenance, RepairAllReport, RepairReport, ScopedState, Selector,
      build_impairer, check_link, find_link, impair_link, make_link_id,
      make_static_link_id,
      parse_percent, parse_rate, parse_time_ms, read_link_states,
      register_impairer, repair_all, repair_link

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

.. automodule:: otto.link.check
   :members:

.. automodule:: otto.link.check_live
   :members:

.. automodule:: otto.link.sandbox
   :members:

.. automodule:: otto.link.probes
   :members:

.. automodule:: otto.link.judge
   :members:

.. automodule:: otto.models.link
   :members:
