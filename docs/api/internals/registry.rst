registry internals
==================

The names ``otto.registry`` defines outside its ``__all__``. They are not
public and carry no stability promise; they are documented so the guides
and the reference can link them. The public names are on :doc:`/api/registry`.

.. currentmodule:: otto.registry

.. autodata:: otto.registry.T

.. autodata:: otto.registry.E

.. autodata:: otto.registry.C

.. autodata:: otto.registry.Env

.. autodata:: otto.registry.M

.. autodata:: otto.registry.K

.. autodata:: otto.registry.V

.. autodata:: otto.registry.CapT

.. autodata:: otto.registry.F

.. autoclass:: otto.registry.Capability

.. autoclass:: otto.registry.Proposed

.. autoclass:: otto.registry.BackendEntry

.. autoclass:: otto.registry.Token

.. autoclass:: otto.registry.Subscribed

.. autoclass:: otto.registry.Derived

.. autofunction:: otto.registry.instances

.. autofunction:: otto.registry.resolved

.. autofunction:: otto.registry.registration_boundary

.. autofunction:: otto.registry.loading_test_files

.. autofunction:: otto.registry.is_loading_test_files

.. autofunction:: otto.registry.refuse_during_test_load

.. autofunction:: otto.registry.get_registering_repo
