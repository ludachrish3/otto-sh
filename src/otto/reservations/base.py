"""The official base class for reservation backends.

:class:`~otto.reservations.protocol.ReservationBackend` is the *contract*:
a ``runtime_checkable`` Protocol otto's gate, factory and conformance helper
are written against, satisfied structurally by any class with the two
methods. :class:`ReservationBackendBase` is the recommended way to satisfy
it. Inheriting buys an implementer three things the Protocol cannot give:

* A forgotten method fails at instantiation, with Python's own
  ``TypeError`` naming it — not at the first gated command.
* The constructor otto's factory calls is spelled out once:
  :func:`~otto.reservations.build_backend` always passes ``repo_dir=`` and
  ``username=``, and passes ``url=`` when the setting is present, and every
  ``[reservations.<name>]`` key arrives as a further keyword argument. A
  subclass declares those keys as its own parameters and forwards the
  otto-owned ones to ``super().__init__``.
* The method docstrings sit on the class the implementer is reading.

Optional capabilities stay structural. A backend signals one by implementing
the method: add ``list_usernames`` for
:class:`~otto.reservations.protocol.SupportsUsernameCompletion`, add
``holders`` for
:class:`~otto.reservations.protocol.SupportsResourceHolders`. Otto detects
each with ``isinstance`` against the capability Protocol, so there is no flag
to set and nothing on this base to override. Naming the Protocol as an extra
base (``class Mine(ReservationBackendBase, SupportsResourceHolders)``) is
optional: it changes nothing at runtime and lets a type checker hold the
signature to the contract.
"""

from abc import ABC, abstractmethod
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from .check import ReservationBackendError

if TYPE_CHECKING:
    from datetime import datetime

    from .protocol import Reservation


class ReservationBackendBase(ABC):
    """Inherit from this to write a reservation backend.

    Implement the two abstract methods; keep the constructor shape. See the
    module docstring for what inheriting buys and how optional capabilities
    are signalled.

    Parameters
    ----------
    url : str | None
        The ``url`` key of the ``[reservations]`` table, when set. Use it or
        ignore it — a backend may hardcode its endpoint instead.
    repo_dir : Path | None
        The SUT repo root. Otto always passes it; anchor any relative
        path-like setting of your own against it.
    username : str | None
        The identity otto resolved for this invocation (``--holder`` or the
        login name). Otto always passes it; it is what :attr:`reservations`
        queries for.
    """

    def __init__(
        self,
        *,
        url: "str | None" = None,
        repo_dir: "Path | None" = None,
        username: "str | None" = None,
    ) -> None:
        self.url = url
        self.repo_dir = Path(repo_dir) if repo_dir is not None else None
        self.username = username

    @cached_property
    def reservations(self) -> "list[Reservation]":
        """The invoking user's reservations, active right now.

        Fetched on first access and never again, so one otto run makes one
        query. It is deliberately **not** populated in ``__init__``: the base
        constructor runs before a subclass has assigned its own state, and an
        eager fetch would also mean that merely constructing a backend
        contacts the scheduler — which would fail runs that need no
        reservation at all whenever the scheduler is down.

        A backend that genuinely wants to fail fast may pre-seed the cache
        with ``self.reservations = self.fetch_reservations(self.username)``
        after ``super().__init__()``; this is available but not recommended.

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            If no username was resolved for this backend.
        """
        if self.username is None:
            raise ReservationBackendError(
                "no username was resolved for this backend; otto passes username= "
                "at construction, so a backend built by hand must supply it"
            )
        return self.fetch_reservations(self.username)

    @abstractmethod
    def fetch_reservations(
        self,
        username: str,
        start: "datetime | None" = None,
        end: "datetime | None" = None,
    ) -> "list[Reservation]":
        """Return *username*'s reservations overlapping ``[start, end]``.

        Both bounds default to **this instant**, so the unbounded call returns
        what the user holds right now — the tightest query, not the widest.

        The window predicate is **overlap**, not containment: a booking that
        began before *start* and ends after *end* is active during the window
        and MUST be returned. Reading it as "contained in" or "beginning
        within" makes otto's gate fail open, admitting a second user onto held
        hardware.

        Return one row per ``(user, resource)`` window — a booking covering
        three racks yields three :class:`~otto.reservations.protocol.Reservation`
        objects. Omit windows that ended at or before now. Order is not
        significant; otto sorts where it displays. Resource strings must match
        the lab file's identifiers byte for byte; normalize here, not in otto.

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer (network error,
            file I/O error, DB error, credential rejection, malformed data).
            Never swallow and return an empty list: the CLI turns this
            exception into a fail-closed startup error, while an empty list is
            a refusal that blames the user.
        """

    @abstractmethod
    def backend_name(self) -> str:
        """Return a short, stable identifier for this backend (e.g. ``"json"``).

        Shown by ``otto reservation whoami`` and in skip warnings; changing it
        between versions breaks log-history searches.
        """
