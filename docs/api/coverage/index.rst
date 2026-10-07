coverage
========

The coverage package collects, merges, and renders code-coverage
data from embedded and remote targets.

The verbs and their reports are imported from the package::

    from otto.coverage import (
        get_coverage,
        clean_coverage,
        CleanReport,
        GetReport,
        FailedReset,
        CoverageInputError,
        CoverageCleanError,
        NoCoverageHostsError,
    )

.. automodule:: otto.coverage
