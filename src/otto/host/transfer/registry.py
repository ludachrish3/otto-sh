"""Registry of file-transfer backends, keyed by protocol name.

Provides :func:`register_transfer_backend` (for custom backends added from
init modules) and :func:`build_transfer_backend`, which builds the backend a
host's ``transfer`` names from its :class:`~otto.host.transfer.TransferContext`.
The registry is unified across host families — unix backends (``scp``,
``sftp``, ``ftp``, ``nc``, ``shell``) and embedded backends (``console``,
``tftp``) share one namespace so a cross-family protocol is a single entry.

Each entry carries its backend's declarations as :class:`TransferMetadata`,
read with ``TRANSFER_BACKENDS.peek(name).metadata`` without importing the
backend. The built-in backends are registered here, by
:class:`~otto.registry.Ref`, with their declarations stated beside each
reference: naming or describing a protocol imports no backend module, and
building one imports only that backend's, whose class is then checked against
what was stated.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...errors import OttoError
from ...registry import BackendRegistry, Ref, class_backend, registration_boundary
from .base import BaseFileTransfer, ProgressGranularity, TransferContext

if TYPE_CHECKING:
    from ...registry import BackendEntry, Proposed


@dataclass(frozen=True)
class TransferMetadata:
    """What a transfer backend declares: its families, its progress promise, whether it logs in.

    Static: read with ``TRANSFER_BACKENDS.peek(name).metadata`` without
    importing the backend's class. A wrapper registration copies the three
    from the class's attributes; a built-in states them beside its reference,
    and its class is checked against them when it is first built.
    """

    host_families: frozenset[str]
    """The non-empty set of host families this backend serves (``unix``, ``embedded``)."""

    progress_granularity: ProgressGranularity
    """What the backend promises the progress bar, per direction."""

    authenticates: bool
    """Whether the backend performs its own login (ftp does; scp/sftp/nc/shell ride the term)."""


class TransferBackendError(OttoError):
    """A transfer backend could not be built."""


class TransferConstructionError(TransferBackendError, ValueError):
    """Building a transfer backend failed; the message names the stage, the backend and its origin.

    The stages are lookup (the name is not registered), resolution (a
    registered reference failed to import, or its class disagrees with the
    declarations stated beside it), construction (its ``create`` raised) and
    result (``create`` returned something that is not a
    :class:`~otto.host.transfer.BaseFileTransfer`). The cause is chained.
    """


def _describe_parse_error(exc: Exception) -> str:
    """Describe a configuration that failed to parse, never quoting the rejected value.

    A transfer backend takes no configuration, so the registry never parses
    one; the registry requires the describer all the same.
    """
    from pydantic import ValidationError  # lazy: this module stays free of pydantic

    from ...models.base import compact_validation_error

    if isinstance(exc, ValidationError):
        return compact_validation_error(exc)
    return f"{type(exc).__name__} (its message is not shown: it may quote the rejected value)"


def _check_transfer_result(name: str, obj: object) -> None:
    """Refuse a built object that is not a :class:`~otto.host.transfer.BaseFileTransfer`."""
    if not isinstance(obj, BaseFileTransfer):
        raise TypeError(
            f"transfer backend {name!r} built a {type(obj).__name__}, not a BaseFileTransfer"
        )


def _check_declarations(
    name: str,
    *,
    host_families: object,
    progress_granularity: object,
    authenticates: object,
    where: str,
) -> None:
    """Refuse declarations that do not state families, a progress granularity and login.

    The one statement of the three rules, read off a class's attributes
    (*where* ``"cls."``) or off an entry's metadata (*where* ``""``).
    """
    if not host_families:
        raise ValueError(
            f"register_transfer_backend({name!r}): {where}host_families is empty; "
            f"a transfer backend must declare at least one host family "
            f"(e.g. frozenset({{'unix'}}))."
        )
    # isinstance, not hasattr: a subclass declaring a bare int (or inheriting
    # only the ClassVar ANNOTATION, which creates no attribute) would satisfy a
    # name check while promising nothing the matrix or the conformance surface
    # can read.
    if not isinstance(progress_granularity, ProgressGranularity):
        raise ValueError(  # noqa: TRY004 — this registry refuses with ValueError uniformly (see host_families above)
            f"register_transfer_backend({name!r}): {where}progress_granularity is missing "
            f"or not a ProgressGranularity; a transfer backend must declare what it "
            f"promises the progress bar (e.g. ProgressGranularity(put=8192, get=8192))."
        )
    if not isinstance(authenticates, bool):
        raise ValueError(  # noqa: TRY004 — this registry refuses with ValueError uniformly (see host_families above)
            f"register_transfer_backend({name!r}): {where}authenticates must be a bool; "
            f"declare True only for a backend that performs its own login "
            f"(ftp does; scp/sftp/nc/shell ride the term session)."
        )


def _validate_transfer_backend(name: str, cls: type[BaseFileTransfer]) -> None:
    """Refuse a backend class that does not declare its families, progress granularity and login.

    Runs on a plugin's class in :func:`register_transfer_backend`, and on a
    built-in's class when its reference is first resolved.
    """
    _check_declarations(
        name,
        host_families=cls.host_families,
        progress_granularity=getattr(cls, "progress_granularity", None),
        authenticates=getattr(cls, "authenticates", None),
        where="cls.",
    )


def _check_transfer_entry(
    name: str,
    entry: "BackendEntry[TransferContext, BaseFileTransfer, TransferMetadata]",
    proposed: "Proposed[BackendEntry[TransferContext, BaseFileTransfer, TransferMetadata]]",
) -> None:
    """``TRANSFER_BACKENDS``' validate: a class backend whose stated declarations hold.

    It reads only the metadata, so a backend registered by reference is
    checked without importing it.
    """
    del proposed
    metadata = entry.metadata
    if not isinstance(metadata, TransferMetadata):
        raise TypeError(
            f"register_transfer_backend({name!r}): the entry's metadata is a "
            f"{type(metadata).__name__}, not a TransferMetadata"
        )
    if entry.cls is None:
        raise ValueError(
            f"register_transfer_backend({name!r}): a transfer backend is a class "
            "(class_backend()), not a configured factory"
        )
    _check_declarations(
        name,
        host_families=metadata.host_families,
        progress_granularity=metadata.progress_granularity,
        authenticates=metadata.authenticates,
        where="",
    )


def _declared(cls: type[BaseFileTransfer]) -> TransferMetadata:
    """Return the declarations *cls* makes in its class attributes."""
    return TransferMetadata(
        host_families=frozenset(cls.host_families),
        progress_granularity=cls.progress_granularity,
        authenticates=cls.authenticates,
    )


def _check_transfer_agrees(
    name: str, entry: "BackendEntry[TransferContext, BaseFileTransfer, TransferMetadata]"
) -> None:
    """``TRANSFER_BACKENDS``' resolved check: the class declares what its entry states.

    A built-in states its declarations beside its reference so they are read
    without importing it; when the class is imported, it must agree with them.
    """
    cls = entry.cls
    if not (isinstance(cls, type) and issubclass(cls, BaseFileTransfer)):
        raise TypeError(
            f"transfer backend {name!r} resolves to {cls!r}, not a BaseFileTransfer subclass"
        )
    _validate_transfer_backend(name, cls)
    declared = _declared(cls)
    differing = [
        field
        for field in ("host_families", "progress_granularity", "authenticates")
        if getattr(declared, field) != getattr(entry.metadata, field)
    ]
    if differing:
        detail = "; ".join(
            f"{field}: stated {getattr(entry.metadata, field)!r}, "
            f"the class declares {getattr(declared, field)!r}"
            for field in differing
        )
        raise ValueError(
            f"transfer backend {name!r}: {cls.__qualname__} disagrees with the metadata "
            f"registered for it ({detail})"
        )


TRANSFER_BACKENDS: "BackendRegistry[TransferContext, BaseFileTransfer, TransferMetadata]" = (
    BackendRegistry(
        "transfer backend",
        register_hint="otto.host.transfer.register_transfer_backend()",
        error=TransferConstructionError,
        describe_parse_error=_describe_parse_error,
        result=_check_transfer_result,
        validate=_check_transfer_entry,
        check_resolved=_check_transfer_agrees,
    )
)
"""Every transfer backend, by the name a host's ``transfer`` selects."""


@registration_boundary
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

    The three are copied into the entry's :class:`TransferMetadata`.
    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in); by default a duplicate name raises
    :class:`~otto.registry.DuplicateRegistration`.
    """
    _validate_transfer_backend(name, cls)
    TRANSFER_BACKENDS.register(
        name, class_backend(cls=cls, metadata=_declared(cls)), overwrite=overwrite
    )


def build_transfer_backend(name: str, ctx: TransferContext) -> BaseFileTransfer:
    """Build the transfer backend registered under *name* for *ctx*.

    Raises:
        TransferConstructionError: If *name* is not registered (the message
            lists registered names and suggests near-misses), its class fails
            to import or disagrees with its stated declarations, its
            ``create`` raises, or ``create`` returns something that is not a
            :class:`~otto.host.transfer.BaseFileTransfer`. The message names
            the stage and the module that registered the backend; the cause
            is chained.
    """
    return TRANSFER_BACKENDS.build(TRANSFER_BACKENDS.prepare(name, {}, ctx))


def _register_builtin_backends() -> None:
    """Register otto's built-in backends by reference, each with its declarations.

    The declarations restate each class's attributes so they are read without
    importing it; the resolved check refuses a class that drifts from them.
    """
    unix = frozenset({"unix"})
    embedded = frozenset({"embedded"})
    for name, target, metadata in [
        (
            "console",
            "otto.host.transfer.console:ConsoleFileTransfer",
            TransferMetadata(
                embedded,
                ProgressGranularity(
                    put=32,
                    get=None,
                    note=(
                        "get is one `fs read` command on the Zephyr shell -- the bytes arrive "
                        "in a single reply and the one event arrives when it completes"
                    ),
                ),
                authenticates=False,
            ),
        ),
        (
            "ftp",
            "otto.host.transfer.ftp:FtpFileTransfer",
            TransferMetadata(unix, ProgressGranularity(put=8192, get=8192), authenticates=True),
        ),
        (
            "nc",
            "otto.host.transfer.nc:NcFileTransfer",
            TransferMetadata(unix, ProgressGranularity(put=8192, get=8192), authenticates=False),
        ),
        (
            "scp",
            "otto.host.transfer.scp:ScpFileTransfer",
            TransferMetadata(
                unix,
                ProgressGranularity(
                    put=16384,
                    get=16384,
                    note=(
                        "the stride is whatever `block_size` reaches `asyncssh` (default "
                        "16384): the `scp_options.block_size` field, or an `extra` of the "
                        "same name, which is applied last and wins"
                    ),
                ),
                authenticates=False,
            ),
        ),
        (
            "sftp",
            "otto.host.transfer.sftp:SftpFileTransfer",
            TransferMetadata(unix, ProgressGranularity(put=16384, get=16384), authenticates=False),
        ),
        (
            "shell",
            "otto.host.transfer.shell:ShellFileTransfer",
            TransferMetadata(unix, ProgressGranularity(put=4096, get=4096), authenticates=False),
        ),
        (
            "tftp",
            "otto.host.transfer.tftp:TftpFileTransfer",
            TransferMetadata(
                embedded,
                ProgressGranularity(
                    put=None,
                    get=None,
                    note="not implemented; both directions raise NotImplementedError",
                ),
                authenticates=False,
            ),
        ),
    ]:
        TRANSFER_BACKENDS.register(name, class_backend(cls=Ref(target), metadata=metadata))


_register_builtin_backends()
