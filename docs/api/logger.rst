logger
======

The logger package provides otto's logging infrastructure including
custom levels and formatters. There is no otto-specific logger accessor —
otto modules and library consumers alike use the stdlib idiom directly
(``logging.getLogger(__name__)`` internally, ``logging.getLogger("otto")``
for consumers).

.. automodule:: otto.logger.levels

.. automodule:: otto.logger.formatters

.. automodule:: otto.logger.mode

.. automodule:: otto.logger.management
