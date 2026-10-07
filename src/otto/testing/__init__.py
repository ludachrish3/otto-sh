"""Public testing helpers for otto backend authors.

Conformance suites that assert a backend satisfies one of otto's pluggable
interfaces. Import the helper for the interface you implement and call it from a
pytest test (it raises a single ``AssertionError`` listing every contract
violation):

    from otto.testing import (
        assert_creds_store_conforms,
        assert_host_conforms,
        assert_host_registrable,
        assert_inventory_conforms,
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
        assert_transfer_backend_conforms,
    )

Every name is exported lazily (PEP 562), the shape every otto package shares:
the host and transfer-backend suites (``.conformance_host``) load only when
one of them is named. The resolver does not write a resolved name back into
the module dict; see ``otto.config``'s ``__dir__`` for why.

``assert_host_registrable`` is the one helper that is not a conformance suite:
it raises what ``register_host_class`` would refuse, and registers nothing.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conformance import assert_creds_store_conforms as assert_creds_store_conforms
    from .conformance import assert_inventory_conforms as assert_inventory_conforms
    from .conformance import assert_lab_repository_conforms as assert_lab_repository_conforms
    from .conformance import (
        assert_reservation_backend_conforms as assert_reservation_backend_conforms,
    )
    from .conformance_host import assert_host_conforms as assert_host_conforms
    from .conformance_host import assert_host_registrable as assert_host_registrable
    from .conformance_host import (
        assert_transfer_backend_conforms as assert_transfer_backend_conforms,
    )

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "assert_creds_store_conforms": "otto.testing.conformance",
    "assert_inventory_conforms": "otto.testing.conformance",
    "assert_lab_repository_conforms": "otto.testing.conformance",
    "assert_reservation_backend_conforms": "otto.testing.conformance",
    "assert_host_conforms": "otto.testing.conformance_host",
    "assert_host_registrable": "otto.testing.conformance_host",
    "assert_transfer_backend_conforms": "otto.testing.conformance_host",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.testing's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "assert_creds_store_conforms",
    "assert_host_conforms",
    "assert_host_registrable",
    "assert_inventory_conforms",
    "assert_lab_repository_conforms",
    "assert_reservation_backend_conforms",
    "assert_transfer_backend_conforms",
]
