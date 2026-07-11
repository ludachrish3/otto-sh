labs
====

The labs package provides a DB-agnostic host-source (``LabRepository``)
backend, selected by name and constructed via :func:`otto.labs.build_lab_repository`.
The built-in ``json`` backend reads ``lab.json`` files; custom backends
register a name via :func:`otto.labs.register_lab_repository` from an
``init`` module.

.. autofunction:: otto.labs.build_lab_repository

.. autofunction:: otto.labs.register_lab_repository

.. autoexception:: otto.labs.LabNotFoundError

.. automodule:: otto.labs.protocol

.. automodule:: otto.labs.json_repository

.. automodule:: otto.labs.registry

.. automodule:: otto.labs.errors
