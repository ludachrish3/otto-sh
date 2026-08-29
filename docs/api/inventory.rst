inventory
=========

The inventory package supplies the tool-agnostic half of a host — address,
interfaces, credentials, versions, location — from a backend selected by
``[inventory]`` in ``~/.otto/settings.toml`` (or a project's override), and joins
it to the otto-specific host entry in ``lab.json`` with
:func:`otto.inventory.resolve_host_entry`.

For configuration, the JSON and NetBox backends, and the adoption path, see
:doc:`../guide/configuration/inventory`; for writing a backend of your own,
:doc:`../library/inventory-backends`.

.. automodule:: otto.inventory
   :no-members:

.. autofunction:: otto.inventory.build_inventory

.. autofunction:: otto.inventory.build_inventory_from_declarations

.. autofunction:: otto.inventory.compile_inventory

.. autofunction:: otto.inventory.construct_inventory

.. autofunction:: otto.inventory.register_inventory_backend

.. autofunction:: otto.inventory.get_inventory_backend_class

The join
--------

.. autofunction:: otto.inventory.resolve_host_entry

.. automodule:: otto.inventory.resolve

Configuration
-------------

.. automodule:: otto.inventory.config

The backend contract
--------------------

.. automodule:: otto.inventory.protocol

Credentials
-----------

.. automodule:: otto.inventory.creds

Backend registry
----------------

.. automodule:: otto.inventory.registry

The json backend
----------------

.. automodule:: otto.inventory.json_backend

The netbox backend
------------------

.. automodule:: otto.inventory.netbox

The stage-1 document
--------------------

.. automodule:: otto.inventory.snapshot

The snapshot cache
------------------

.. automodule:: otto.inventory.cache

The doctor
----------

.. automodule:: otto.inventory.doctor

Exceptions
----------

.. automodule:: otto.inventory.errors
