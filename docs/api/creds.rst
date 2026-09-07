creds
=====

The creds package supplies credentials by inventory key from a store selected
by ``[creds]`` in ``~/.otto/settings.toml`` (or a project's override). The
inventory wraps the selected store in
:class:`otto.inventory.creds.CredsOverlay`, which merges its entries under the
record's by login; the lab file's entries layer over both.

For configuration and the merge rules see
:doc:`../guide/configuration/inventory`; for writing a store of your own,
:doc:`../library/creds-backends`.

.. automodule:: otto.creds
   :no-members:

.. autofunction:: otto.creds.register_creds_backend

.. autofunction:: otto.creds.get_creds_backend_class

The store contract
------------------

.. automodule:: otto.creds.protocol

Configuration
-------------

.. automodule:: otto.creds.config

The json store
--------------

.. automodule:: otto.creds.json_store

Backend registry
----------------

.. automodule:: otto.creds.registry

Exceptions
----------

.. automodule:: otto.creds.errors
