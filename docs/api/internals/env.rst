env internals
=============

``otto.env`` is the ``otto env`` command and the startup preflight. Its
``Unsatisfied`` is documented here because it is the element type of
``otto.session.DependencyRefusedError.unsatisfied``.

.. autoclass:: otto.env.preflight.Unsatisfied
