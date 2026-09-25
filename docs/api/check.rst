Check
=====

The ``otto.check`` package is the shared core of otto's setup checks, such as
``otto link check``: the verdict vocabulary every check reports in, the
one-command host fingerprint, the proven-range list and the labels it gives
each host, the stdout renderer, and the ``--report`` JSON writer. It imports
no check of its own, so each check builds on it without depending on the
others. See :doc:`../cli/check-verdicts` for what the verdicts and labels
mean to a user, and the :doc:`link check guide <../cli/link/check>` for the
first check built on it.

The public names are documented once, on the package, since that is where
they are imported from. Each submodule below lists only what the package
does not re-export.

.. automodule:: otto.check
   :members:

.. automodule:: otto.check.verdict
   :members:
   :exclude-members: FeatureResult, UnmeasuredReason, Verdict, count_verdicts

.. automodule:: otto.check.proven
   :members:
   :exclude-members: ProvenEntry, ProvenRange, RangeLabel, label_against_range,
      load_proven_range

.. automodule:: otto.check.fingerprint
   :members:
   :exclude-members: CHECK_HOST_TIMEOUT, LINK_TOOLS, LINK_VERSIONS, HostFingerprint,
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
