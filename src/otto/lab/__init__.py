"""The lab and fleet API: load a lab, reach its hosts, walk the fleet of interest.

``from otto.lab import get_host`` is the spelling to use. Each name is
implemented in an ``otto.config`` module and documented there, once; spec 5
may move an implementation without changing this path:

- :class:`~otto.config.lab.Lab`, a loaded lab, and
  :func:`~otto.config.lab.load_lab`, which builds one from lab data;
- :func:`~otto.config.fleet.get_lab` and :func:`~otto.config.fleet.get_host`:
  the active context's lab, and one of its hosts by id (never scoped);
- :func:`~otto.config.fleet.all_hosts`,
  :func:`~otto.config.fleet.do_for_all_hosts` and
  :func:`~otto.config.fleet.run_on_all_hosts`: walks over the run's fleet of
  interest;
- :func:`~otto.config.fleet.fleet_of_interest`: the ids such a walk takes,
  computed from a lab and its repos without connecting or bootstrapping;
- :class:`~otto.config.scope.EmptySelectionError`: a walk's pattern matched no
  host the walk may reach.

``get_hosts_in_play`` is not exported, on purpose. It is the reservation
readers' tolerant spelling (an empty declared fleet is zero hosts in play,
never an abort), and a walk written against the most discoverable name would
silently touch nothing. Its readers import it from ``otto.config.fleet`` by
hand, which is the point.

Every name is exported lazily (PEP 562), the shape every otto package shares:
``import otto.lab`` loads neither ``otto.config`` nor the host classes, and a
name loads its defining module on first access. The resolver does not write a
resolved name back into the module dict; see ``otto.config``'s ``__dir__`` for
why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.fleet import all_hosts as all_hosts
    from ..config.fleet import do_for_all_hosts as do_for_all_hosts
    from ..config.fleet import fleet_of_interest as fleet_of_interest
    from ..config.fleet import get_host as get_host
    from ..config.fleet import get_lab as get_lab
    from ..config.fleet import run_on_all_hosts as run_on_all_hosts
    from ..config.lab import Lab as Lab
    from ..config.lab import load_lab as load_lab
    from ..config.scope import EmptySelectionError as EmptySelectionError

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "Lab": "otto.config.lab",
    "load_lab": "otto.config.lab",
    "get_lab": "otto.config.fleet",
    "get_host": "otto.config.fleet",
    "all_hosts": "otto.config.fleet",
    "do_for_all_hosts": "otto.config.fleet",
    "run_on_all_hosts": "otto.config.fleet",
    "fleet_of_interest": "otto.config.fleet",
    "EmptySelectionError": "otto.config.scope",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.lab's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "EmptySelectionError",
    "Lab",
    "all_hosts",
    "do_for_all_hosts",
    "fleet_of_interest",
    "get_host",
    "get_lab",
    "load_lab",
    "run_on_all_hosts",
]
