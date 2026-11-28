"""The lab a run reads, scoped or whole: :class:`LabView`, :class:`ScopedLab` and :func:`whole_lab`.

A foundation leaf: it imports no otto module at runtime. :class:`LabView` is
what both a :class:`~otto.labs.loader.Lab` and a :class:`ScopedLab` offer.
:class:`ScopedLab` reads a run's canonical lab through the run's scope judge:
its ``hosts`` holds only the hosts the run may target, and every write reaches
the canonical lab.

Library code that must see every host the lab holds, whatever the run's
projects target, reads its lab through :func:`whole_lab`: a context taking a
lab (its ``lab`` setter and ``open_context``), the fleet-of-interest query,
the link self-lockout guard and its endpoint check, a new tunnel's conflict
scan, docker's container registrations, their sweep and rollback, its
use-case facts, and its refusal naming the docker-capable hosts outside the
scope. Every other library function, link and tunnel recovery included, uses
the lab it is given.
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, TypeVar, cast, overload

from typing_extensions import final, override

if TYPE_CHECKING:
    from .host.host import Host
    from .labs.loader import Lab
    from .link.model import Link

_T = TypeVar("_T")

MERGE_REFUSAL = "a scoped lab view cannot be merged; merge ctx.unscoped.lab"
"""The text of the :class:`TypeError` a merge with a scoped view raises, from either side."""

READ_ONLY_HOSTS = (
    "ctx.lab.hosts is read-only in a scoped command; "
    "write through ctx.lab.add_host or ctx.unscoped.lab"
)
"""The text of the :class:`TypeError` a write through a scoped view's ``hosts`` raises."""


class LabView(Protocol):
    """What a run's lab offers its readers: the type of ``ctx.lab`` and ``otto.get_lab()``.

    A :class:`~otto.labs.loader.Lab` satisfies it, and so does a
    :class:`ScopedLab`, which a scoped command reads. Code written against
    :class:`LabView` runs on either.
    """

    @property
    def name(self) -> str:
        """The lab's name; a merged lab's is ``a+b``."""
        ...

    @property
    def component_names(self) -> list[str]:
        """The lab names this lab was assembled from, in merge order."""
        ...

    @property
    def resources(self) -> set[str]:
        """The resources required to reserve this lab."""
        ...

    @property
    def metadata(self) -> dict[str, dict[str, Any]]:
        """Each component lab's opaque user data, keyed by lab name."""
        ...

    @property
    def hosts(self) -> "Mapping[str, Host]":
        """The hosts the reader may reach, keyed by id, in the lab's order."""
        ...

    @property
    def links(self) -> "Sequence[Link]":
        """The declared links the reader may reach."""
        ...

    def static_links(self) -> "list[Link]":
        """Return the static link layer: implicit hop edges plus declared links."""
        ...

    def add_host(self, host: "Host") -> None:
        """Register *host* in the lab."""
        ...

    def __otto_summary__(self) -> str:
        """Return the one-line identity a failing ``expect()`` report shows."""
        ...


class _Judge(Protocol):
    """The run's scope judge, as a view reads it (``otto.host.targeting.HostScope``)."""

    def admits(self, host_id: str) -> bool:
        """Report whether the run may target *host_id*."""
        ...

    def targetable_ids(self) -> list[str]:
        """Return the ids the run may target, in the lab's order."""
        ...

    def refusal(self, host_id: str) -> KeyError:
        """Return the error a lookup of a refused *host_id* raises."""
        ...

    def settings_files(self) -> list[str]:
        """Return the ``settings.toml`` files whose patterns would widen the scope."""
        ...


class _ScopeSource(Protocol):
    """What a view reads its lab and judge from: a run context (``OttoContext``)."""

    @property
    def _lab(self) -> "Lab":
        """The canonical lab, read live."""
        ...

    def _host_scope(self) -> _Judge:
        """Build the run's scope judge over the canonical lab as it stands."""
        ...


def _canonical(source: _ScopeSource) -> "Lab":
    """Return *source*'s canonical lab as it stands now."""
    return source._lab  # noqa: SLF001 — the one read of a scope source's lab


def _judge(source: _ScopeSource) -> _Judge:
    """Build *source*'s scope judge over its lab as it stands now."""
    return source._host_scope()  # noqa: SLF001 — the one build of a scope source's judge


@final
class _ScopedHosts(Mapping[str, "Host"]):
    """A read-only mapping of the hosts a run may target, judged live on every read."""

    __slots__ = ("_source",)

    def __init__(self, source: _ScopeSource) -> None:
        self._source = source

    @override
    def __repr__(self) -> str:
        """Show the ids the run may target, in the lab's order, or ``...`` when unjudgeable.

        A repr never raises: when the scope cannot be resolved (a project
        failed to load), it shows ``...`` in place of the ids.
        """
        try:
            ids = repr(_judge(self._source).targetable_ids())
        except Exception:  # noqa: BLE001 — a repr must not raise; the read itself will
            ids = "..."
        return f"{type(self).__name__}({ids})"

    @override
    def __getitem__(self, host_id: str) -> "Host":
        judge = _judge(self._source)
        if not judge.admits(host_id):
            raise judge.refusal(host_id)
        return _canonical(self._source).hosts[host_id]

    @override
    def __contains__(self, host_id: object) -> bool:
        return isinstance(host_id, str) and _judge(self._source).admits(host_id)

    @overload
    def get(self, host_id: object, /) -> "Host | None": ...
    @overload
    def get(self, host_id: object, default: "Host | _T", /) -> "Host | _T": ...
    @override
    def get(self, host_id: object, default: object = None, /) -> object:
        if not (isinstance(host_id, str) and _judge(self._source).admits(host_id)):
            return default
        return _canonical(self._source).hosts[host_id]

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(_judge(self._source).targetable_ids())

    @override
    def __len__(self) -> int:
        return len(_judge(self._source).targetable_ids())

    def __setitem__(self, host_id: str, host: "Host") -> NoReturn:
        raise TypeError(READ_ONLY_HOSTS)

    def __delitem__(self, host_id: str) -> NoReturn:
        raise TypeError(READ_ONLY_HOSTS)

    def pop(self, *_args: object) -> NoReturn:
        """Refuse: the mapping is read-only."""
        raise TypeError(READ_ONLY_HOSTS)

    def popitem(self) -> NoReturn:
        """Refuse: the mapping is read-only."""
        raise TypeError(READ_ONLY_HOSTS)

    def clear(self) -> NoReturn:
        """Refuse: the mapping is read-only."""
        raise TypeError(READ_ONLY_HOSTS)

    def update(self, *_args: object, **_kwargs: object) -> NoReturn:
        """Refuse: the mapping is read-only."""
        raise TypeError(READ_ONLY_HOSTS)

    def setdefault(self, *_args: object) -> NoReturn:
        """Refuse: the mapping is read-only."""
        raise TypeError(READ_ONLY_HOSTS)


@final
class ScopedLab:
    """A run's lab as a scoped command reads it: only the hosts the run may target.

    It reads the run's one canonical :class:`~otto.labs.loader.Lab` live, so
    a container registered mid-run is judged when it is read, and a view kept
    across ``ctx.lab = other`` follows the context.

    - ``hosts`` is a read-only mapping of the hosts the run may target, in the
      lab's order, holding the canonical host objects. ``id in hosts`` and
      ``hosts.get(id)`` judge that one id. ``hosts[id]`` raises
      :class:`~otto.config.scope.HostOutOfScopeError` (a :class:`KeyError`)
      for a lab host outside the scope, and
      ``otto.host.targeting.UnknownHostError`` (also a :class:`KeyError` and
      an :class:`~otto.errors.OttoError`) for an id the lab does not hold.
      Assigning or deleting through it raises :class:`TypeError`.
    - ``links`` and :meth:`static_links` are the canonical lab's, less each
      link with an endpoint that is in the lab and outside the scope. A link
      to a host the lab does not hold stays. ``links`` is a tuple, so a write
      to it raises; write through ``ctx.unscoped.lab.links``.
    - ``name``, ``component_names``, ``resources``, ``metadata`` and the
      ``expect()`` summary are the canonical lab's, unfiltered.
    - :meth:`add_host` registers in the canonical lab, so a host's lab
      backlink is always the canonical lab.
    - ``lab + view``, ``view + lab``, ``sum([view])`` and
      ``dataclasses.replace(view)`` raise :class:`TypeError`. Merge
      ``ctx.unscoped.lab`` instead.

    Args:
        source: The run context the view reads its lab and scope from.
    """

    __slots__ = ("_hosts", "_source")

    def __init__(self, source: _ScopeSource) -> None:
        self._source = source
        self._hosts = _ScopedHosts(source)

    @override
    def __repr__(self) -> str:
        """Show the canonical lab's name and the ids the run may target, in the lab's order.

        A repr never raises: when the scope cannot be resolved (a project
        failed to load), it shows the name alone.
        """
        name = _canonical(self._source).name
        try:
            ids = _judge(self._source).targetable_ids()
        except Exception:  # noqa: BLE001 — a repr must not raise; the read itself will
            return f"{type(self).__name__}(name={name!r})"
        return f"{type(self).__name__}(name={name!r}, hosts={ids!r})"

    @property
    def name(self) -> str:
        """The canonical lab's name; a merged lab's is ``a+b``."""
        return _canonical(self._source).name

    @property
    def component_names(self) -> list[str]:
        """The canonical lab's component lab names, in merge order."""
        return _canonical(self._source).component_names

    @property
    def resources(self) -> set[str]:
        """The resources required to reserve the canonical lab."""
        return _canonical(self._source).resources

    @property
    def metadata(self) -> dict[str, dict[str, Any]]:
        """The canonical lab's per-component user data, keyed by lab name."""
        return _canonical(self._source).metadata

    @property
    def hosts(self) -> "Mapping[str, Host]":
        """The hosts the run may target, keyed by id, in the lab's order; read-only."""
        return self._hosts

    @property
    def links(self) -> "Sequence[Link]":
        """The canonical lab's declared links, less those that reach out of scope; immutable.

        A fresh tuple on every read, so a write fails loudly rather than
        landing on a copy. Add or remove a link through ``ctx.unscoped.lab.links``.
        """
        lab = _canonical(self._source)
        return tuple(_in_scope_links(lab.links, lab=lab, judge=_judge(self._source)))

    def static_links(self) -> "list[Link]":
        """Return the canonical static link layer, less the links that reach out of scope."""
        lab = _canonical(self._source)
        return _in_scope_links(lab.static_links(), lab=lab, judge=_judge(self._source))

    def add_host(self, host: "Host") -> None:
        """Register *host* in the canonical lab; see :meth:`otto.labs.loader.Lab.add_host`."""
        _canonical(self._source).add_host(host)

    def __otto_summary__(self) -> str:
        """Return the canonical lab's ``expect()`` summary, such as ``<Lab bench>``."""
        return _canonical(self._source).__otto_summary__()

    def __add__(self, other: object) -> NoReturn:
        raise TypeError(MERGE_REFUSAL)

    def __radd__(self, other: object) -> NoReturn:
        raise TypeError(MERGE_REFUSAL)


def _in_scope_links(links: "Sequence[Link]", *, lab: "Lab", judge: _Judge) -> "list[Link]":
    """Return *links* less each one with an endpoint that is in *lab* and outside the scope."""
    targetable = set(judge.targetable_ids())
    return [
        link
        for link in links
        if all(end.host in targetable or end.host not in lab.hosts for end in (link.a, link.b))
    ]


def refuse_view_merge(other: object) -> None:
    """Raise the merge refusal when *other* is a :class:`ScopedLab`.

    ``Lab.__add__`` calls it before it changes anything, so ``lab + view``
    leaves the lab and its hosts' backlinks as they were.

    Raises:
        TypeError: *other* is a scoped view.
    """
    if isinstance(other, ScopedLab):
        raise TypeError(MERGE_REFUSAL)


def whole_lab(lab: LabView) -> "Lab":
    """Return the whole :class:`~otto.labs.loader.Lab` that *lab* reads.

    A :class:`ScopedLab` gives the canonical lab it reads; a
    :class:`~otto.labs.loader.Lab` is its own whole lab and comes back as it
    is. Readers import this function when they call it, not at module scope.
    """
    if isinstance(lab, ScopedLab):
        return _canonical(lab._source)  # noqa: SLF001 — this module's own view
    return cast("Lab", lab)


def settings_files_of(lab: LabView) -> list[str]:
    """Return the ``settings.toml`` files whose patterns would widen *lab*'s scope.

    A :class:`ScopedLab` names its run's declaring files; any other lab has no
    scope to widen, so the list is empty.
    """
    if isinstance(lab, ScopedLab):
        return _judge(lab._source).settings_files()  # noqa: SLF001 — this module's own view
    return []
