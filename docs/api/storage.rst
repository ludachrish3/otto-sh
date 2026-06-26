storage
=======

The storage package provides a DB-agnostic host-source (``LabRepository``)
backend, selected by name and constructed via :func:`otto.storage.build_lab_repository`.
The built-in ``json`` backend reads ``hosts.json`` files; custom backends
register a name via :func:`otto.storage.register_lab_repository` from an
``init`` module.

.. autofunction:: otto.storage.build_lab_repository

.. autofunction:: otto.storage.register_lab_repository

.. automodule:: otto.storage.protocol

.. automodule:: otto.storage.json_repository

.. automodule:: otto.storage.registry

.. automodule:: otto.storage.errors

.. automodule:: otto.storage.factory
