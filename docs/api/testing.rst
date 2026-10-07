testing
=======

Reusable conformance helpers for otto's pluggable backend interfaces. Call one
per interface from a pytest test; each raises a single ``AssertionError``
listing every contract violation. ``assert_host_registrable`` is the one
helper that is not per interface: it asks whether ``register_host_class``
would accept a host class.

.. automodule:: otto.testing
