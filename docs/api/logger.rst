logger
======

The logger package provides otto's logging infrastructure: custom levels,
formatters, and the root-logger installer. There is no otto-specific
logger accessor — otto's own modules and any consumer's code alike use the
stdlib idiom directly, ``logging.getLogger(__name__)``, with nothing to
register. An embedding process that wants otto's own console and log-file
sinks opts in with :func:`otto.logger.install <otto.logger.management.install>`
and undoes it with :func:`otto.logger.reset <otto.logger.management.reset>`
— see the :doc:`library guide <../library/index>` and the
:doc:`architecture page <../architecture/utilities/logging>` for the three
postures this pair fits into.

.. automodule:: otto.logger.levels

.. automodule:: otto.logger.formatters

.. automodule:: otto.logger.mode

.. automodule:: otto.logger.management
