from .base import BaseFileTransfer

# Unified registry of transfer-protocol name -> backend class, spanning BOTH
# host families. ``EmbeddedFileTransfer`` registers ``console``/``tftp`` into
# this same dict (see transfer/embedded.py), so one namespace holds every
# transfer protocol and a future cross-family protocol (tftp) is a single
# entry. ``build_*`` returns the class so the host can call ``.create(ctx)``.
_TRANSFER_BACKENDS: dict[str, type[BaseFileTransfer]] = {}


def register_transfer_backend(name: str, cls: type[BaseFileTransfer]) -> None:
    """Make a custom transfer backend available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The backend
    must declare a non-empty :attr:`BaseFileTransfer.host_families`; otherwise
    it could never validate against any host and is rejected here.
    """
    if not cls.host_families:
        raise ValueError(
            f"register_transfer_backend({name!r}): cls.host_families is empty; "
            f"a transfer backend must declare at least one host family "
            f"(e.g. frozenset({{'unix'}}))."
        )
    _TRANSFER_BACKENDS[name] = cls


def build_transfer_backend(name: str) -> type[BaseFileTransfer]:
    """Return the transfer-backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _TRANSFER_BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_TRANSFER_BACKENDS))
        raise ValueError(
            f"Unknown transfer backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_transfer_backend()."
        ) from None
