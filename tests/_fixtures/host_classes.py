"""Shared walk over the ``BaseHost`` subclasses otto itself defines."""

import importlib
import pkgutil
import sys

from otto.host.host import BaseHost


def host_classes_in_the_tree() -> "list[type]":
    """Every live ``BaseHost`` subclass otto itself defines.

    Two filters, both necessary. Classes outside ``otto.`` are test doubles and
    downstream subclasses, which this repository does not speak for. And a
    ``@dataclass(slots=True)`` class is REPLACED by a new class object at
    decoration time while the pre-slots original stays in
    ``__subclasses__()`` forever — so a class is live only when its own module
    still names it.
    """
    import otto.docker
    import otto.host

    for package in (otto.host, otto.docker):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            importlib.import_module(module.name)

    def descendants(cls: type) -> "list[type]":
        found = []
        for sub in cls.__subclasses__():
            found.append(sub)
            found += descendants(sub)
        return found

    live = []
    for cls in descendants(BaseHost):
        if not cls.__module__.startswith("otto."):
            continue
        if getattr(sys.modules[cls.__module__], cls.__name__, None) is not cls:
            continue
        if cls not in live:
            live.append(cls)
    return live
