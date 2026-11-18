"""What each host family promises for its options, progress and session identity.

The options are ``user=``, ``sudo=`` and ``expects=``.

One declaration per shipped host class, read by the documentation renderer and
by the conformance surfaces — the same shape
:class:`~otto.host.transfer.base.ProgressGranularity` gives transfer backends,
for the same reason: a promise a reader can look up must be stated in code
beside the implementation, not retyped into prose that drifts.

:class:`HostCapabilities` is the declaration; every built-in host class carries
one in a ``capabilities`` :data:`~typing.ClassVar`, and
:func:`~otto.host.os_profile.register_host_class` refuses a custom class that
does not. :func:`shipped_host_families` enumerates the families otto ships,
including the two that reach a user without passing through that registry.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal

from typing_extensions import Self, final

from .limitation import Limitation

if TYPE_CHECKING:
    # Type-only: a runtime import here would cycle back through host.py,
    # which imports HostCapabilities for the ClassVar annotation alone.
    from .host import BaseHost


class UserSupport(str, Enum):
    """How one verb of one host family answers a ``user=`` argument.

    Four answers, and the difference between the first two is whose
    credentials move: ``authenticate`` needs the named user's own, ``chown``
    needs only the login identity's privilege.
    """

    def __new__(cls, value: str, meaning: str) -> Self:
        """Attach *meaning* to the member as its ``__doc__``.

        The documentation page renders these strings rather than restating
        them, so the meaning of a value lives exactly once — here.
        """
        member = str.__new__(cls, value)
        member._value_ = value
        member.__doc__ = meaning
        return member

    authenticate = (
        "authenticate",
        (
            "The verb rides a connection opened AS that user, so it runs with "
            "that user's real credentials and permissions rather than an "
            "elevation from the login user."
        ),
    )
    chown = (
        "chown",
        (
            "The verb runs under the login identity's privilege and switches "
            "the result to the named user -- a `chown` over files that have "
            "landed, or `docker exec -u` for a command. The named user's own "
            "credentials are never needed."
        ),
    )
    switch = (
        "switch",
        (
            "The verb runs in a shell session switched to that user, reached "
            "the way an interactive login reaches them -- directly, or by "
            "replaying the proxy hops their cred declares. On an SSH host, a "
            "user with a direct cred is instead authenticated on a connection "
            "of their own."
        ),
    )
    ignored = (
        "ignored",
        "The argument is accepted and validated, and has no other effect.",
    )
    refused = (
        "refused",
        (
            "The verb raises `NotImplementedError`, and refuses under a dry run "
            "too. On `run` and `exec` it is `UnsupportedOptionError`, carrying the "
            "family's declared reason."
        ),
    )


class SessionIdentity(str, Enum):
    """Where a family's PERSISTENT session gets its identity, and who may change it."""

    def __new__(cls, value: str, meaning: str) -> Self:
        """Attach *meaning* to the member as its ``__doc__`` (see :class:`UserSupport`)."""
        member = str.__new__(cls, value)
        member._value_ = value
        member.__doc__ = meaning
        return member

    as_user_scoped = (
        "as_user scoped",
        (
            "`async with host.as_user(...)` switches the live session and "
            "restores the previous identity when the block exits."
        ),
    )
    bound_at_open = (
        "bound at open",
        (
            "The run channel settles its user when it opens and cannot "
            "renegotiate one on a live shell, so a later call naming a "
            "different user refuses until the channel is dropped."
        ),
    )
    none = (
        "none",
        ("The connection's own identity is the only identity there is; nothing switches it."),
    )


@final
@dataclass(frozen=True, kw_only=True)
class OptionSupport:
    """Whether one host family honours one option of ``run`` and ``exec``.

    Build it with :meth:`honoured` or :meth:`refused`. A refused option
    carries the :class:`~otto.host.limitation.Limitation` that says why the
    device cannot do it, and ``run`` and ``exec`` raise
    :exc:`~otto.host.errors.UnsupportedOptionError` from that record.
    """

    limitation: "Limitation | None"
    """The reason the option is refused; ``None`` when it is honoured."""

    @classmethod
    def honoured(cls) -> "OptionSupport":
        """Declare the option honoured."""
        return cls(limitation=None)

    @classmethod
    def refused(cls, *, limitation: Limitation) -> "OptionSupport":
        """Declare the option refused, for the reason *limitation* records."""
        return cls(limitation=limitation)


RunOption = Literal["sudo", "expects", "user"]
"""An option of ``run`` and ``exec`` a family may declare it cannot honour."""


def _require_device(field_name: str, limitation: "Limitation | None") -> None:
    """Refuse a declared refusal that is not the device's: otto would be at fault."""
    if limitation is not None and limitation.kind != "device":
        raise ValueError(
            f"HostCapabilities.{field_name}: a refused option is the device's limitation "
            f"(kind 'device'), got kind {limitation.kind!r}; a gap otto could close is a "
            f"bug, not a refusal"
        )


@final
@dataclass(frozen=True, kw_only=True)
class HostCapabilities:
    """What one host family promises across its four verbs.

    DECLARED, not measured: each field states what the class does, and the
    conformance surfaces probe the class against it. The support matrix
    publishes the measurements; this publishes the promise.
    """

    run_user: UserSupport
    """How ``run(user=...)`` answers -- the PERSISTENT session's verb."""

    exec_user: UserSupport
    """How ``exec(user=...)`` answers -- stateless, one command."""

    put_user: UserSupport
    """How ``put(user=...)`` answers."""

    get_user: UserSupport
    """How ``get(user=...)`` answers."""

    show_progress: bool
    """Whether this family's OWN transfer leg can report progress.

    ``True`` when the bytes move through a backend that emits intermediate
    progress events. Containers are ``False`` even though their transfers do
    show a bar: the leg that reports is the STAGING copy to the parent host,
    and the ``docker cp`` leg that actually crosses into the container reports
    nothing. Per-backend, per-direction strides are published beside the
    backends themselves, not here.
    """

    session_identity: SessionIdentity
    """Where the persistent session's identity comes from, and who may change it."""

    sudo: OptionSupport
    """Whether ``run(sudo=True)`` and ``exec(sudo=True)`` elevate, or are refused."""

    expects: OptionSupport
    """Whether ``run`` and ``exec`` answer ``expects=`` prompts, or are refused."""

    user_refusal: "Limitation | None"
    """Why ``run(user=...)`` or ``exec(user=...)`` is refused on this family.

    Set exactly when :attr:`run_user` or :attr:`exec_user` is
    :attr:`UserSupport.refused`, and ``None`` otherwise. ``put`` and ``get``
    refuse a user in their own bodies, for reasons of their own.
    """

    transfer_family: str = ""
    """The :attr:`~otto.host.transfer.base.BaseFileTransfer.host_families` key
    whose registered backends this family offers.

    The transfer registry is the source for this column wherever it can answer,
    so a backend added to a family shows up with no edit here. Empty when the
    family selects no registered backend, in which case :attr:`transfer` says
    what it uses instead. Exactly one of the two is set.
    """

    transfer: str = ""
    """What moves the bytes when the transfer registry cannot answer.

    Local and container hosts build their backend directly rather than
    selecting a registered one by name, so ``TRANSFER_BACKENDS`` holds nothing
    to list for them and the fact has to be declared. Exactly one of this and
    :attr:`transfer_family` is set.
    """

    note: str = ""
    """One sentence of family-specific caveat, published beside the row."""

    def option_limitation(self, *, option: "RunOption") -> "Limitation | None":
        """Return why this family refuses the ``run``/``exec`` *option*; ``None`` if honoured.

        The one map from an option's name to its declaration: :attr:`sudo`,
        :attr:`expects`, or :attr:`user_refusal` for ``user``.
        """
        if option == "user":
            return self.user_refusal
        return (self.sudo if option == "sudo" else self.expects).limitation

    def __post_init__(self) -> None:
        """Refuse a declaration whose transfer column would render empty or twice.

        Also refuse a refusal with no device limitation behind it, and a
        ``user_refusal`` that disagrees with :attr:`run_user` and :attr:`exec_user`.
        """
        refuses_user = UserSupport.refused in (self.run_user, self.exec_user)
        if refuses_user != (self.user_refusal is not None):
            raise ValueError(
                "HostCapabilities: set user_refusal exactly when run_user or exec_user "
                f"is refused, got run_user={self.run_user.value!r} "
                f"exec_user={self.exec_user.value!r} user_refusal={self.user_refusal!r}"
            )
        _require_device("user_refusal", self.user_refusal)
        _require_device("sudo", self.sudo.limitation)
        _require_device("expects", self.expects.limitation)
        if bool(self.transfer_family) == bool(self.transfer):
            raise ValueError(
                "HostCapabilities: set exactly one of transfer_family (the "
                "registry answers for this family) or transfer (it cannot), "
                f"got transfer_family={self.transfer_family!r} transfer={self.transfer!r}"
            )


@dataclass(frozen=True)
class HostFamily:
    """One shipped host family: its row name, its class, and how a user reaches it."""

    name: str
    """Row name (``unix``, ``embedded``, ``zephyr``, ``container``, ``local``)."""

    cls: "type[BaseHost]"
    """The host class carrying the family's :class:`HostCapabilities`."""

    selector: str
    """How lab data or a command selects this family, in the reader's terms."""

    capabilities: HostCapabilities = field(init=False)
    """The declaration on :attr:`cls` -- resolved through the MRO, so a
    subclass that adds no promises of its own (``zephyr``) reports its base's."""

    def __post_init__(self) -> None:
        """Resolve :attr:`capabilities` off the class (frozen: set via ``object``)."""
        object.__setattr__(self, "capabilities", self.cls.capabilities)


# The module that registers otto's own host classes. A row is a BUILT-IN only
# when ``HOST_CLASSES`` attributes it here; a class an init module registered
# is somebody else's family and this page does not speak for it.
_BUILTIN_ORIGIN = "otto.host.os_profile"


def shipped_host_families() -> list[HostFamily]:
    """Return every host family otto ships, in publication order.

    Two of them never pass through
    :func:`~otto.host.os_profile.register_host_class`, so a walk of
    ``HOST_CLASSES`` alone would under-report:
    :class:`~otto.host.local_host.LocalHost` is constructed directly and
    :class:`~otto.host.docker_host.DockerContainerHost` is built by
    ``otto.docker.compose``; neither is a
    :class:`~otto.host.remote_host.RemoteHost`, which that registry requires.
    ``tests/unit/host/test_capability_grid.py`` walks the class tree and fails
    if a family exists that this function does not name.

    The imports are local: this module is imported by ``os_profile`` itself,
    and by ``host.py`` for the annotation alone.
    """
    from typing import cast

    from ..registry import resolved
    from .docker_host import DockerContainerHost
    from .local_host import LocalHost
    from .os_profile import HOST_CLASSES

    # HOST_CLASSES' records type the class as a RemoteHost (a BaseHost);
    # resolved() narrows away the Ref a built-in is registered as, and the
    # cast widens to the BaseHost this row holds.
    families = [
        HostFamily(
            name=name,
            cls=cast("type[BaseHost]", resolved(HOST_CLASSES.get(name).cls)),
            selector=f"`os_type: {name}`",
        )
        for name in HOST_CLASSES.names()
        if HOST_CLASSES.origin(name=name) == _BUILTIN_ORIGIN
    ]
    families.append(
        HostFamily(
            name="container",
            cls=DockerContainerHost,
            selector="a `[docker]` service, started by `otto docker compose up`",
        )
    )
    families.append(
        HostFamily(
            name="local",
            cls=LocalHost,
            selector="implicit -- the machine otto itself runs on",
        )
    )
    return families
