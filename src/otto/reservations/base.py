"""The official base class for reservation backends.

:class:`~otto.reservations.protocol.ReservationBackend` is the *contract*:
a ``runtime_checkable`` Protocol otto's gate, factory and conformance helper
are written against, satisfied structurally by any class with the three
methods. :class:`ReservationBackendBase` is the recommended way to satisfy
it. Inheriting buys an implementer three things the Protocol cannot give:

* A forgotten method fails at instantiation, with Python's own
  ``TypeError`` naming it — not at the first gated command.
* The constructor otto's factory calls is spelled out once:
  :func:`~otto.reservations.build_backend` always passes ``repo_dir=`` and
  passes ``url=`` when the setting is present, and every
  ``[reservations.<name>]`` key arrives as a further keyword argument. A
  subclass declares those keys as its own parameters and forwards the two
  otto-owned ones to ``super().__init__``.
* The method docstrings sit on the class the implementer is reading.

Optional capabilities stay structural. A backend signals one by implementing
the method: add ``list_usernames`` for
:class:`~otto.reservations.protocol.SupportsUsernameCompletion`, add
``get_reservation_windows`` for
:class:`~otto.reservations.protocol.SupportsReservationWindows`. Otto detects
each with ``isinstance`` against the capability Protocol, so there is no flag
to set and nothing on this base to override. Naming the Protocol as an extra
base (``class Mine(ReservationBackendBase, SupportsReservationWindows)``) is
optional: it changes nothing at runtime and lets a type checker hold the
signature to the contract.
"""

from abc import ABC, abstractmethod
from pathlib import Path


class ReservationBackendBase(ABC):
    """Inherit from this to write a reservation backend.

    Implement the three abstract methods; keep the constructor shape. See the
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
    """

    def __init__(self, *, url: str | None = None, repo_dir: Path | None = None) -> None:
        self.url = url
        self.repo_dir = Path(repo_dir) if repo_dir is not None else None

    @abstractmethod
    def get_reserved_resources(self, username: str) -> set[str]:
        """Return the set of resource identifiers currently reserved by ``username``.

        Return the user's **full** held set — otto does the filtering against
        what the lab needs, and pre-filtering loses information for the error
        message. An empty set means the user holds nothing, which the gate
        refuses when the lab requires anything. Strings must match the
        identifiers in the lab file byte for byte; normalize here, not in
        otto.

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer (network error,
            file I/O error, DB error, credential rejection, malformed data).
            Never swallow and return empty: the CLI turns this exception into
            a fail-closed startup error, and an empty set is a refusal that
            blames the user.
        """

    @abstractmethod
    def who_reserved(self, resource: str) -> list[str]:
        """Return the usernames currently holding ``resource``.

        Used for the refusal message so the caller knows who to talk to.
        Deterministic order, duplicates removed; an **empty list** means no
        one holds it (there is no ``None`` sentinel).

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer.
        """

    @abstractmethod
    def backend_name(self) -> str:
        """Return a short, stable identifier for this backend (e.g. ``"json"``).

        Shown by ``otto reservation whoami`` and in skip warnings; changing it
        between versions breaks log-history searches.
        """
