testing
=======

Reusable conformance helpers for otto's pluggable backend interfaces. Call one
per interface from a pytest test; each raises a single ``AssertionError``
listing every contract violation.

.. autofunction:: otto.testing.assert_lab_repository_conforms

.. autofunction:: otto.testing.assert_reservation_backend_conforms
