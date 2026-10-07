Check
=====

The ``otto.check`` package is the shared core of otto's setup checks,
``otto link check`` and ``otto tunnel check``: the verdict vocabulary every
check reports in, the one-command host fingerprint, the proven-range list
and the labels it gives each host, the stdout renderer, and the ``--report``
JSON writer. It imports no check of its own, so each check builds on it
without depending on the others. See :doc:`/cli/check-verdicts` for what the verdicts and labels
mean to a user, and the :doc:`link check </cli/link/check>` and
:doc:`tunnel check </cli/tunnel/check>` guides for the two checks built on
it. ``otto.check.clock`` holds the probe clock both checks time with: bash's
``$EPOCHREALTIME``, and the bounds every timed socat client runs under.
``otto.check.sweep`` holds the age rule both checks' leftover sweeps follow,
the bound it uses, and how that bound is derived.

Each name is documented on the submodule that defines it; a reference to its package path
(``otto.check.Verdict``) resolves there.

.. automodule:: otto.check
   :ignore-module-all:
   :members:

.. automodule:: otto.check.verdict
   :ignore-module-all:
   :members:

.. automodule:: otto.check.proven
   :ignore-module-all:
   :members:

.. automodule:: otto.check.fingerprint
   :ignore-module-all:
   :members:

.. automodule:: otto.check.render
   :ignore-module-all:
   :members:

.. automodule:: otto.check.report
   :ignore-module-all:
   :members:

.. automodule:: otto.check.errors
   :ignore-module-all:
   :members:

.. automodule:: otto.check.clock
   :ignore-module-all:
   :members:

.. automodule:: otto.check.sweep
   :ignore-module-all:
   :members:
