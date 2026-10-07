labs
====

The labs package provides DB-agnostic host-source (``LabRepository``) backends,
selected by name per ``[[lab.sources]]`` entry and constructed via
:func:`otto.labs.build_lab_sources`.
The built-in ``json`` backend reads ``lab.json`` files; custom backends
register a name via :func:`otto.labs.register_lab_repository` from an
``init`` module.

.. automodule:: otto.labs
