parsers internals
=================

The names ``otto.monitor.parsers`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/monitor/parsers`.

.. currentmodule:: otto.monitor.parsers

.. autoclass:: otto.monitor.parsers.TimedSample

.. autoclass:: otto.monitor.parsers.TickResult

.. autofunction:: otto.monitor.parsers.human_readable

.. autoclass:: otto.monitor.parsers.MemParser

.. autoclass:: otto.monitor.parsers.DiskParser

.. autoclass:: otto.monitor.parsers.LoadParser

.. autoclass:: otto.monitor.parsers.NetDevParser

.. autoclass:: otto.monitor.parsers.SocketsParser

.. autoclass:: otto.monitor.parsers.DiskIoParser

.. autoclass:: otto.monitor.parsers.PerCoreCpuParser

.. autoclass:: otto.monitor.parsers.ProcCountParser

.. autoclass:: otto.monitor.parsers.HostParsersEntry

.. autoclass:: otto.monitor.parsers.PatternParsersEntry

.. autoclass:: otto.monitor.parsers.ProjectParserEntry

.. autofunction:: otto.monitor.parsers.pattern_key

.. autodata:: otto.monitor.parsers.HOST_PARSERS

.. autodata:: otto.monitor.parsers.HOST_PATTERN_PARSERS

.. autodata:: otto.monitor.parsers.PROJECT_PARSERS

.. autofunction:: otto.monitor.parsers.default_catalog

.. autofunction:: otto.monitor.parsers.get_host_parsers
