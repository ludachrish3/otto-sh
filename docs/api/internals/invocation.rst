invocation internals
====================

These names are not part of the public API and carry no stability promise.
The leaf's public names (:class:`~otto.context.RunPolicy`,
:class:`~otto.context.HostResolver`, :class:`~otto.context.ContextBinding`)
are declared on :doc:`/api/context`.

.. currentmodule:: otto.invocation

.. autodata:: otto.invocation.VARIANTS

.. autofunction:: otto.invocation.check_variant

.. autofunction:: otto.invocation.current_policy

.. autofunction:: otto.invocation.installed_policy

.. autofunction:: otto.invocation.installed_resolver

.. autofunction:: otto.invocation.install_var

.. autofunction:: otto.invocation.install_policy

.. autofunction:: otto.invocation.install_resolver

.. autofunction:: otto.invocation.reset_binding

.. autoclass:: otto.invocation.HostRegistration

.. autoclass:: otto.invocation.Boundary
   :members: release

.. autofunction:: otto.invocation.register

.. autofunction:: otto.invocation.unregister

.. autofunction:: otto.invocation.acquire_boundary

.. autofunction:: otto.invocation.acquire_boundary_waiting

.. autofunction:: otto.invocation.refuse_inside_a_sweep

.. autofunction:: otto.invocation.sweep_unheld

.. autofunction:: otto.invocation.shut_down

.. autofunction:: otto.invocation.abandon_closed_loops
