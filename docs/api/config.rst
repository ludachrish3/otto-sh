config
======

The config package handles environment variables, repository discovery,
settings parsing, and lab loading.

.. Ignore ``otto.config.__all__`` HERE ONLY. That list exists for the Python
   runtime — it is what keeps ``from otto.config import *`` and ``dir()``
   working now that the .fleet/.lab names resolve through PEP 562 — but
   autodoc reads it as "document every name in it", including the re-exports
   whose real home is a submodule this same page documents below. Each one
   then registers a second python-domain target (``otto.config.Repo`` beside
   ``otto.config.repo.Repo``), and every ``:class:`Repo``` in the tree becomes
   an ambiguous cross-reference: 32 ref.python warnings, fatal under ``-W``,
   reported far from their cause. With this flag autodoc selects members the
   way it did before ``__all__`` existed, and the runtime keeps its list.

.. automodule:: otto.config
   :ignore-module-all:

.. automodule:: otto.config.fleet

.. automodule:: otto.config.env

.. automodule:: otto.config.lab

.. automodule:: otto.config.scope

.. automodule:: otto.config.dependencies

.. automodule:: otto.config.repo

.. automodule:: otto.config.user_settings

.. automodule:: otto.config.home

.. automodule:: otto.config.version
