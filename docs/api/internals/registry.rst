registry internals
==================

The names ``otto.registry`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/registry`.

.. currentmodule:: otto.registry

.. autodata:: otto.registry.T

.. autofunction:: otto.registry.caller_module

.. autofunction:: otto.registry.loading_test_files

.. autofunction:: otto.registry.is_loading_test_files

.. autofunction:: otto.registry.refuse_during_test_load

.. autofunction:: otto.registry.get_registering_repo
