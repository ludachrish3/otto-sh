snmp internals
==============

The names ``otto.monitor.snmp`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/monitor/snmp`.

.. currentmodule:: otto.monitor.snmp

.. autodata:: otto.monitor.snmp.OTTO_PEN

.. autodata:: otto.monitor.snmp.OID_SYS_UPTIME

.. autodata:: otto.monitor.snmp.SnmpVersion

.. autodata:: otto.monitor.snmp.SNMP_METRICS

.. autodata:: otto.monitor.snmp.CORE_OIDS

.. autofunction:: otto.monitor.snmp.net_oids

.. autofunction:: otto.monitor.snmp.fs_oids

.. autofunction:: otto.monitor.snmp.expand_oid_bundles

.. autofunction:: otto.monitor.snmp.get_snmp_metric

.. autofunction:: otto.monitor.snmp.resolve_snmp_metric

.. autofunction:: otto.monitor.snmp.process_snmp_values

.. autoclass:: otto.monitor.snmp.SnmpClient

.. autoclass:: otto.monitor.snmp.SnmpSource
