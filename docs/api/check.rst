Check
=====

The ``otto.check`` package is the shared core of otto's setup checks,
``otto link check`` and ``otto tunnel check``: the verdict vocabulary every
check reports in, the one-command host fingerprint, the proven-range list
and the labels it gives each host, the stdout renderer, and the ``--report``
JSON writer. It imports no check of its own, so each check builds on it
without depending on the others. See :doc:`../cli/check-verdicts` for what the verdicts and labels
mean to a user, and the :doc:`link check <../cli/link/check>` and
:doc:`tunnel check <../cli/tunnel/check>` guides for the two checks built on
it. ``otto.check.clock`` holds the probe clock both checks time with: bash's
``$EPOCHREALTIME``, and the bounds every timed socat client runs under.
``otto.check.sweep`` holds the age rule both checks' leftover sweeps follow,
the bound it uses, and how that bound is derived.

The public names are documented once, on the package, since that is where
they are imported from. Each submodule below lists only what the package
does not re-export.

.. automodule:: otto.check
   :members:

.. automodule:: otto.check.verdict
   :members:
   :exclude-members: FeatureResult, ReportVerdicts, UnmeasuredReason, Verdict,
      count_verdicts

.. automodule:: otto.check.proven
   :members:
   :exclude-members: ProvenEntry, ProvenRange, RangeLabel, label_against_range,
      load_proven_range, range_labels

.. automodule:: otto.check.fingerprint
   :members:
   :exclude-members: CHECK_HOST_TIMEOUT, LINK_TOOLS, LINK_VERSIONS, TUNNEL_TOOLS,
      TUNNEL_VERSIONS, HostFingerprint,
      check_exec, fingerprint_command, parse_fingerprint, probe_fingerprint

.. automodule:: otto.check.render
   :members:
   :exclude-members: CheckRow, CheckSection, render_sections, section_counts

.. automodule:: otto.check.report
   :members:
   :exclude-members: REPORT_SCHEMA, report_to_json

.. automodule:: otto.check.errors
   :members:
   :exclude-members: CheckCommandFailedError, CheckHostUnreachableError

.. automodule:: otto.check.clock
   :members:

.. automodule:: otto.check.sweep
   :members:
