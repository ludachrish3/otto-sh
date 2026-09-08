"""Guards on the per-family ``capabilities`` declarations.

THREE JOBS, and they are the three ways a declaration can stop being true.

It can be MISSING: :func:`~otto.host.os_profile.register_host_class` refuses a
class that declares none, so a custom family cannot reach a reader as a blank
row — the guard ``transfer/registry.py`` already applies to
``progress_granularity``, applied to host classes.

It can be UNPUBLISHED: a family can exist in the tree and not on the page,
because two of otto's own families never pass through that registry. The class
walk below finds a host class no row speaks for.

And it can be INCOHERENT: a declaration whose transfer column names both a
registry family and a literal, or neither, renders an empty or doubled cell.
``HostCapabilities`` refuses that at construction.

The page these back is ``docs/guide/hosts/families.md``, whose byte-for-byte
sync with the tree is pinned in ``tests/unit/test_support_matrix.py``.
"""

import importlib
import pkgutil
import sys

import pytest

from otto.host.capability_grid import (
    HostCapabilities,
    SessionIdentity,
    UserSupport,
    shipped_host_families,
)
from otto.host.host import BaseHost
from otto.host.os_profile import register_host_class
from otto.host.remote_host import RemoteHost
from otto.models.host import EmbeddedHostSpec


def _capabilities(**overrides) -> HostCapabilities:
    """A valid declaration, so a test can vary exactly one thing about it."""
    fields = {
        "run_user": UserSupport.refused,
        "exec_user": UserSupport.refused,
        "put_user": UserSupport.refused,
        "get_user": UserSupport.refused,
        "show_progress": False,
        "session_identity": SessionIdentity.none,
        "transfer_family": "unix",
    }
    fields.update(overrides)
    return HostCapabilities(**fields)


class TestRegistrationRefusesAnUndeclaredClass:
    def test_a_class_inheriting_only_the_annotation_is_refused(self):
        """``BaseHost.capabilities`` is a bare ``ClassVar`` annotation.

        It creates no attribute, so a subclass that declares nothing has
        nothing under the name — which is the ordinary shape of the mistake
        and the one the refusal exists for.
        """

        class NoPromise(RemoteHost):
            pass

        assert "capabilities" not in dir(NoPromise)
        with pytest.raises(ValueError, match="capabilities is missing"):
            register_host_class("nopromise", NoPromise, EmbeddedHostSpec)

    def test_a_class_declaring_something_else_under_the_name_is_refused(self):
        """The refusal checks the TYPE, not the name -- a string promises nothing."""

        class BareString(RemoteHost):
            capabilities = "everything"

        with pytest.raises(ValueError, match="capabilities is missing"):
            register_host_class("barestring", BareString, EmbeddedHostSpec)

    def test_a_class_that_declares_one_registers(self):
        """The positive control: without it, a refusal of EVERY class would pass above."""

        class Declared(RemoteHost):
            capabilities = _capabilities()

        register_host_class("declared", Declared, EmbeddedHostSpec)
        from otto.host.os_profile import build_host_class

        assert build_host_class("declared") is Declared


class TestTheDeclarationItself:
    @pytest.mark.parametrize("family", shipped_host_families(), ids=lambda f: f.name)
    def test_every_shipped_family_declares_its_capabilities(self, family):
        assert isinstance(family.capabilities, HostCapabilities)

    def test_a_declaration_naming_no_transfer_at_all_is_refused(self):
        with pytest.raises(ValueError, match="exactly one of transfer_family"):
            _capabilities(transfer_family="")

    def test_a_declaration_naming_a_transfer_twice_is_refused(self):
        with pytest.raises(ValueError, match="exactly one of transfer_family"):
            _capabilities(transfer_family="unix", transfer="scp, by hand")

    def test_every_user_support_value_explains_itself(self):
        """The page RENDERS these docstrings, so a member without one renders a blank cell."""
        for member in (*UserSupport, *SessionIdentity):
            assert (member.__doc__ or "").strip(), member


def _host_classes_in_the_tree() -> "list[type]":
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


def test_every_host_class_in_the_tree_is_published_by_some_row():
    """A family otto ships and the page does not name is the failure mode here.

    ``shipped_host_families`` is hand-maintained where the registry cannot
    answer (``local`` and ``container`` never pass through
    ``register_host_class``), so nothing but this walk would notice a third one
    arriving. Three shapes are legitimate:

    * the class IS a row;
    * it is a subclass of a row's class that redeclares nothing, so the row's
      promises are literally its own (``ZephyrHost``);
    * it is an intermediate BASE of a row's class (``RemoteHost``), which
      declares nothing and which ``register_host_class`` would refuse.

    Anything else is a family with promises nobody publishes.
    """
    rows = {family.cls: family.name for family in shipped_host_families()}
    for cls in _host_classes_in_the_tree():
        if cls in rows:
            continue
        covering = [base for base in rows if issubclass(cls, base)]
        if covering:
            assert "capabilities" not in cls.__dict__, (
                f"{cls.__module__}.{cls.__name__} redeclares capabilities but is "
                f"published under {rows[covering[0]]!r}; give it its own row in "
                f"shipped_host_families()"
            )
            continue
        if any(issubclass(base, cls) for base in rows):
            assert "capabilities" not in cls.__dict__, (
                f"{cls.__module__}.{cls.__name__} is a base of a published family "
                f"and declares capabilities of its own; it needs a row"
            )
            continue
        pytest.fail(
            f"{cls.__module__}.{cls.__name__} is a host class no row speaks for; "
            f"add it to otto.host.capability_grid.shipped_host_families()"
        )
