"""Registry of file-transfer backends, keyed by protocol name.

Provides :func:`register_transfer_backend` (for custom backends added from
init modules) and :func:`build_transfer_backend` (used by
:class:`~otto.host.transfer.BaseFileTransfer` construction). The registry is
unified across host families — unix backends (``scp``, ``sftp``, ``ftp``,
``nc``) and embedded backends (``console``, ``tftp``) share one namespace so
a cross-family protocol is a single entry.

The built-in backends are registered here, by :class:`~otto.registry.Ref`:
naming a protocol imports no backend module, and building one imports only
that backend's.
"""

from ...registry import Ref, Registry, caller_module
from .base import BaseFileTransfer, ProgressGranularity


def _validate_transfer_backend(name: str, cls: type[BaseFileTransfer]) -> None:
    """Refuse a backend that does not declare its families, progress granularity and login.

    ``TRANSFER_BACKENDS``'s *validate* hook: it runs on a plugin's class at
    registration and on a built-in's class at its first lookup.
    """
    if not cls.host_families:
        raise ValueError(
            f"register_transfer_backend({name!r}): cls.host_families is empty; "
            f"a transfer backend must declare at least one host family "
            f"(e.g. frozenset({{'unix'}}))."
        )
    # isinstance, not hasattr: a subclass declaring a bare int (or inheriting
    # only the ClassVar ANNOTATION, which creates no attribute) would satisfy a
    # name check while promising nothing the matrix or the conformance surface
    # can read.
    if not isinstance(getattr(cls, "progress_granularity", None), ProgressGranularity):
        raise ValueError(  # noqa: TRY004 — this registry refuses with ValueError uniformly (see host_families above)
            f"register_transfer_backend({name!r}): cls.progress_granularity is missing; "
            f"a transfer backend must declare what it promises the progress bar "
            f"(e.g. ProgressGranularity(put=8192, get=8192))."
        )
    if not isinstance(getattr(cls, "authenticates", None), bool):
        raise ValueError(  # noqa: TRY004 — this registry refuses with ValueError uniformly (see host_families above)
            f"register_transfer_backend({name!r}): cls.authenticates must be a bool; "
            f"declare True only for a backend that performs its own login "
            f"(ftp does; scp/sftp/nc/shell ride the term session)."
        )


# Unified registry of transfer-protocol name -> backend class, spanning BOTH
# host families, so one namespace holds every transfer protocol and a
# cross-family protocol (tftp) is a single entry. ``build_*`` returns the class
# so the host can call ``.create(ctx)``.
TRANSFER_BACKENDS: Registry[type[BaseFileTransfer]] = Registry(
    "transfer backend",
    register_hint="otto.host.transfer.register_transfer_backend()",
    validate=_validate_transfer_backend,
)


def register_transfer_backend(
    name: str, cls: type[BaseFileTransfer], *, overwrite: bool = False
) -> None:
    """Make a custom transfer backend available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The backend
    makes three declarations, and a missing or malformed one is rejected here:

    * a non-empty :attr:`BaseFileTransfer.host_families` -- otherwise it could
      never validate against any host;
    * a :class:`~otto.host.transfer.base.ProgressGranularity` in
      :attr:`BaseFileTransfer.progress_granularity`, so what it promises the
      progress bar is stated rather than inferred;
    * a ``bool`` in :attr:`BaseFileTransfer.authenticates`, saying whether it
      performs its own login. The inherited ``False`` is a declaration; only
      a backend that logs in sets ``True``.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in); by default a duplicate name raises.
    """
    TRANSFER_BACKENDS.register(name, cls, overwrite=overwrite, origin=caller_module())


def build_transfer_backend(name: str) -> type[BaseFileTransfer]:
    """Return the transfer-backend class registered under *name*.

    Raises:
        ValueError: If *name* is not registered; the message lists registered
            names and suggests near-misses.
    """
    return TRANSFER_BACKENDS.get(name)


def _register_builtin_backends() -> None:
    """Register otto's built-in backends by reference.

    Each entry's origin is its backend's own module, where the class lives.
    """
    for name, module, cls in [
        ("console", "otto.host.transfer.console", "ConsoleFileTransfer"),
        ("ftp", "otto.host.transfer.ftp", "FtpFileTransfer"),
        ("nc", "otto.host.transfer.nc", "NcFileTransfer"),
        ("scp", "otto.host.transfer.scp", "ScpFileTransfer"),
        ("sftp", "otto.host.transfer.sftp", "SftpFileTransfer"),
        ("shell", "otto.host.transfer.shell", "ShellFileTransfer"),
        ("tftp", "otto.host.transfer.tftp", "TftpFileTransfer"),
    ]:
        TRANSFER_BACKENDS.register(name, Ref(f"{module}:{cls}"), origin=module)


_register_builtin_backends()
