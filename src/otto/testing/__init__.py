"""Public testing helpers for otto backend authors.

Conformance suites that assert a backend satisfies one of otto's pluggable
interfaces. Import the helper for the interface you implement and call it from a
pytest test (it raises a single ``AssertionError`` listing every contract
violation):

    from otto.testing import (
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
    )
"""

from .conformance import (
    assert_lab_repository_conforms as assert_lab_repository_conforms,
)
from .conformance import (
    assert_reservation_backend_conforms as assert_reservation_backend_conforms,
)
