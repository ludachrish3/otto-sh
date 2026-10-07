testing
=======

Reusable conformance helpers for otto's pluggable backend interfaces. Call one
per interface from a pytest test; each raises a single ``AssertionError``
listing every contract violation. ``assert_host_registrable`` is the one
helper that is not per interface: it asks whether ``register_host_class``
would accept a host class.

.. autofunction:: otto.testing.assert_creds_store_conforms

.. autofunction:: otto.testing.assert_host_conforms

.. autofunction:: otto.testing.assert_host_registrable

.. autofunction:: otto.testing.assert_inventory_conforms

.. autofunction:: otto.testing.assert_lab_repository_conforms

.. autofunction:: otto.testing.assert_reservation_backend_conforms

.. autofunction:: otto.testing.assert_transfer_backend_conforms
