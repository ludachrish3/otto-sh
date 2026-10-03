session
=======

For how a script uses these functions, and what :func:`otto.open_context
<otto.context.open_context>` runs in which order, see the
:doc:`library guide <../cookbook/python-library>`.

.. automodule:: otto.session
   :members:
   :imported-members:

.. otto.env has no API page; Unsatisfied is documented here because it is the
   element type of DependencyRefusedError.unsatisfied.

Each entry of ``DependencyRefusedError.unsatisfied`` is one of these:

.. autoclass:: otto.env.preflight.Unsatisfied
