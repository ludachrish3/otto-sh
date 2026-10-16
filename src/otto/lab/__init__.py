"""The lab and fleet API: load a lab, reach its hosts, walk the fleet of interest.

``from otto.lab import get_host`` is the spelling to use. Each name is
implemented in another module and documented there, once; spec 5 moves an
implementation without changing this path:

- :class:`~otto.labs.loader.Lab`, a loaded lab, and
  :func:`~otto.labs.loader.load_lab`, which builds one from lab data;
- :func:`~otto.fleet.get_lab` and :func:`~otto.fleet.get_host`:
  the active context's lab, and one of its hosts by id (never scoped);
- :func:`~otto.fleet.all_hosts`,
  :func:`~otto.fleet.do_for_all_hosts` and
  :func:`~otto.fleet.run_on_all_hosts`: walks over the run's fleet of
  interest;
- :func:`~otto.fleet.fleet_of_interest`: the ids such a walk takes,
  computed from a lab and its repos without connecting or bootstrapping;
- :class:`~otto.config.scope.EmptySelectionError`: a walk's pattern matched no
  host the walk may reach.

``get_hosts_in_play`` is not exported, on purpose. It is the tolerant
spelling for reservation checks (an empty declared fleet is zero hosts in
play, never an abort), and a walk written against the most discoverable name
would silently touch nothing. ``otto reservation check`` and completion's gate
import it from ``otto.fleet`` by hand, which is the point; the
reservation gate itself reads the same set off the active run in
``otto.invocation``.

Every name is exported lazily (PEP 562), the shape every otto package shares:
``import otto.lab`` loads none of the modules that implement it, nor the host
classes, and a name loads its defining module on first access. The resolver
does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.scope import EmptySelectionError as EmptySelectionError
    from ..fleet import all_hosts as all_hosts
    from ..fleet import do_for_all_hosts as do_for_all_hosts
    from ..fleet import fleet_of_interest as fleet_of_interest
    from ..fleet import get_host as get_host
    from ..fleet import get_lab as get_lab
    from ..fleet import run_on_all_hosts as run_on_all_hosts
    from ..labs.loader import Lab as Lab
    from ..labs.loader import load_lab as load_lab

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "Lab": "otto.labs.loader",
    "load_lab": "otto.labs.loader",
    "get_lab": "otto.fleet",
    "get_host": "otto.fleet",
    "all_hosts": "otto.fleet",
    "do_for_all_hosts": "otto.fleet",
    "run_on_all_hosts": "otto.fleet",
    "fleet_of_interest": "otto.fleet",
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
