"""The ``IMPAIRERS`` registry: the link impairers lab data can name.

Mirrors the transfer-backend registry (``otto.host.transfer.registry``): custom
impairers register from init modules under a name; a host's ``impairer`` pin /
``valid_impairers`` menu select one per placement host (spec §5). NetEm is the
only first-party registrant (``otto.link.netem``), named here by reference. The
contract an impairer implements, :class:`~otto.link.params.LinkImpairer`, lives in
:mod:`otto.link.params` with the read-back shape it returns.
"""

from ..registry import ClassEntry, Ref, Registry, registration_boundary, resolved
from .params import LinkImpairer


def _check_impairer_class(name: str, cls: object) -> None:
    """Refuse anything but a :class:`~otto.link.params.LinkImpairer` subclass with a host family.

    One with no host family could never validate against any host. One check,
    two call sites: ``validate`` runs it on a class registered eagerly,
    ``check_resolved`` on the class a ``Ref`` names, once imported.
    """
    if not (isinstance(cls, type) and issubclass(cls, LinkImpairer)):
        raise TypeError(f"register_impairer({name!r}): {cls!r} is not a LinkImpairer subclass")
    if not cls.host_families:
        raise ValueError(
            f"register_impairer({name!r}): cls.host_families is empty; an impairer "
            f"must declare at least one host family (e.g. frozenset({{'unix'}}))."
        )


def _validate_impairer_entry(
    name: str, entry: "ClassEntry[LinkImpairer]", _proposed: object
) -> None:
    if not isinstance(entry.cls, Ref):
        _check_impairer_class(name, entry.cls)


IMPAIRERS: "Registry[ClassEntry[LinkImpairer]]" = Registry(
    "impairer",
    entry=ClassEntry,
    register_hint="otto.link.register_impairer()",
    validate=_validate_impairer_entry,
    check_resolved=lambda name, entry: _check_impairer_class(name, resolved(entry.cls)),
)
"""The impairer classes lab data can name, keyed by impairer name."""
_STOCK_REFS = {"netem": Ref("otto.link.netem:NetEmImpairer")}
IMPAIRERS.register("netem", ClassEntry(_STOCK_REFS["netem"]))


def _is_unresolved_stock(name: str) -> bool:
    """Return whether *name* is still otto's own registration, not yet imported.

    Read from the stored record, so it imports nothing. A class registered over
    a stock name with ``overwrite=True`` is not stock. A resolved record is not
    unresolved either; its class is already imported, so reading it is free.
    """
    ref = _STOCK_REFS.get(name)
    return ref is not None and name in IMPAIRERS and IMPAIRERS.peek(name).cls == ref


@registration_boundary
def register_impairer(
    name: str, cls: "type[LinkImpairer] | Ref", *, overwrite: bool = False
) -> None:
    """Make a custom impairer available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The impairer
    must declare a non-empty :attr:`~otto.link.params.LinkImpairer.host_families`; otherwise it
    could never validate against any host and is rejected here. *cls* may be
    a :class:`~otto.registry.Ref` (``"module:Class"``), imported and checked
    at its first use.

    Raises:
        TypeError: If *cls* is not a :class:`~otto.link.params.LinkImpairer` subclass.
        ValueError: If ``cls.host_families`` is empty.
        otto.registry.DuplicateRegistration: If *name* is taken and *overwrite*
            is false.
    """
    IMPAIRERS.register(name, ClassEntry(cls), overwrite=overwrite)


def build_impairer(name: str) -> type[LinkImpairer]:
    """Return the impairer class registered under *name* (rich unknown-name error)."""
    return resolved(IMPAIRERS.get(name).cls)
