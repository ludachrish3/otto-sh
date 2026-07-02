"""Registry of file-transfer backends, keyed by protocol name.

Provides :func:`register_transfer_backend` (for custom backends added from
init modules) and :func:`build_transfer_backend` (used by
:class:`~otto.host.transfer.BaseFileTransfer` construction). The registry is
unified across host families — unix backends (``scp``, ``sftp``, ``ftp``,
``nc``) and embedded backends (``console``, ``tftp``) share one namespace so
a cross-family protocol is a single entry.
"""

from ...registry import Registry, caller_module
from .base import BaseFileTransfer

# Unified registry of transfer-protocol name -> backend class, spanning BOTH
# host families. ``EmbeddedFileTransfer`` registers ``console``/``tftp`` into
# this same registry (see transfer/embedded.py), so one namespace holds every
# transfer protocol and a future cross-family protocol (tftp) is a single
# entry. ``build_*`` returns the class so the host can call ``.create(ctx)``.
TRANSFER_BACKENDS: Registry[type[BaseFileTransfer]] = Registry(
    "transfer backend", register_hint="otto.host.transfer.register_transfer_backend()"
)


def register_transfer_backend(
    name: str, cls: type[BaseFileTransfer], *, overwrite: bool = False
) -> None:
    """Make a custom transfer backend available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The backend
    must declare a non-empty :attr:`BaseFileTransfer.host_families`; otherwise
    it could never validate against any host and is rejected here.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in); by default a duplicate name raises.
    """
    if not cls.host_families:
        raise ValueError(
            f"register_transfer_backend({name!r}): cls.host_families is empty; "
            f"a transfer backend must declare at least one host family "
            f"(e.g. frozenset({{'unix'}}))."
        )
    TRANSFER_BACKENDS.register(name, cls, overwrite=overwrite, origin=caller_module())


def build_transfer_backend(name: str) -> type[BaseFileTransfer]:
    """Return the transfer-backend class registered under *name*.

    Raises:
        ValueError: If *name* is not registered; the message lists registered
            names and suggests near-misses.
    """
    return TRANSFER_BACKENDS.get(name)
