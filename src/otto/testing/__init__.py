"""Public testing helpers for otto backend authors.

Conformance suites that assert a backend satisfies one of otto's pluggable
interfaces. Import the helper for the interface you implement and call it from a
pytest test (it raises a single ``AssertionError`` listing every contract
violation):

    from otto.testing import (
        assert_creds_store_conforms,
        assert_host_conforms,
        assert_inventory_conforms,
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
        assert_transfer_backend_conforms,
    )
"""

from .conformance import (
    assert_creds_store_conforms as assert_creds_store_conforms,
)
from .conformance import (
    assert_inventory_conforms as assert_inventory_conforms,
)
from .conformance import (
    assert_lab_repository_conforms as assert_lab_repository_conforms,
)
from .conformance import (
    assert_reservation_backend_conforms as assert_reservation_backend_conforms,
)
from .conformance_host import (
    assert_host_conforms as assert_host_conforms,
)
from .conformance_host import (
    assert_transfer_backend_conforms as assert_transfer_backend_conforms,
)
