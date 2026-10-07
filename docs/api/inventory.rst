inventory
=========

The inventory package supplies the tool-agnostic half of a host — address,
interfaces, credentials, versions, location — from a backend selected by
``[inventory]`` in ``~/.otto/settings.toml`` (or a project's override), and joins
it to the otto-specific host entry in ``lab.json`` with
:func:`otto.inventory.resolve_host_entry`.

For configuration, the JSON and NetBox backends, and the adoption path, see
:doc:`../configuration/inventory`; for writing a backend of your own,
:doc:`../cookbook/extending/inventory-backends`.

.. automodule:: otto.inventory
