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
verdicts themselves come from :doc:`otto.check <internals/check>`. See the
:doc:`link guide <../cli/link/index>` for CLI usage, the in-path model, and
the Python API.

.. automodule:: otto.link
   :members:
