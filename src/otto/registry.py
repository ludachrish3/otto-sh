"""Generic named registry for pluggable components.

Every otto extension seam (term/transfer backends, host classes, lab
repositories, CLI commands, ...) stores its entries in a :class:`Registry`:
one storage idiom, uniform fail-loud errors with did-you-mean suggestions,
and per-entry origin attribution. Domain modules keep their public
``register_*``/``build_*`` wrapper functions; this class is the shared engine
behind them.

The module also holds the *registering-repo marker* (:func:`registering_repo`,
:func:`get_registering_repo`): the context variable ``bootstrap()`` sets around
each repo's init imports, so those same registration seams can record *which
repo* an entry came from rather than only which module.

A registry may also carry a lazy *loader* that fills it on first read
(:func:`suspend_loaders` reads without running it), and a registry refuses
entries registered from outside otto while repo test files load
(:func:`loading_test_files`) unless it opted in: test files load only for the
commands that read suites, so anything else they registered would exist for
some commands and not others.

>>> r: Registry[str] = Registry("demo backend", register_hint="register_demo()")
>>> r.register("json", "the-json-backend", origin="example")
>>> r.get("json")
'the-json-backend'
>>> r.names()
['json']

References
----------
A registry entry may be a real object, or a :class:`Ref` wrapping a
``"package.module:attribute"`` string that names one without importing it.
:meth:`Registry.names`, ``in``, ``len()`` and :meth:`Registry.origin` never
import a :class:`Ref`'s target: listing and attribution are free regardless
of how many entries are built-ins registered by reference.
:meth:`Registry.get` (and :meth:`Registry.items`, which resolves every entry
the same way) is the only read that pays: the first ``get`` of a given name
imports its target, runs the registry's *validate* hook (if any) on the resolved
object, and caches the result in place of the ``Ref`` so later reads of that
name are free too. Plugins may register either a real object or a ``Ref``;
:meth:`Registry.register` treats them the same way except that a real
object is validated immediately, at registration, rather than deferred to
first lookup.
"""

import contextlib
import contextvars
import dataclasses
import difflib
import inspect
import weakref
from collections.abc import Callable, Iterator
from typing import Any, ClassVar, Generic, TypeVar, cast

from otto.errors import OttoError

T = TypeVar("T")
"""Type variable for the entry type stored in a :class:`Registry`."""


@dataclasses.dataclass(frozen=True)
class Ref:
    """A ``"package.module:attribute"`` reference to a registry entry, imported on first lookup."""

    target: str

    def __post_init__(self) -> None:
        module, sep, attr = self.target.partition(":")
        if not sep or not module or not attr:
            raise ValueError(f"Ref target must be 'package.module:attribute', got {self.target!r}")

    def resolve(self) -> object:
        """Import the module and return the attribute.

        Import and attribute errors propagate unchanged.
        """
        import importlib

        module, _, attr = self.target.partition(":")
        return getattr(importlib.import_module(module), attr)


def caller_module(depth: int = 1) -> str:
    """Return the ``__name__`` of the module *depth* call frames above the caller."""
    frame = inspect.currentframe()
    for _ in range(depth + 1):
        frame = frame.f_back if frame is not None else None
    if frame is None:
        # Defensive, deliberately untested: CPython always provides frames
        # here; only exotic runtimes without frame introspection (or a depth
        # beyond the stack) land on this. Origin degrades, nothing breaks.
        return "<unknown>"
    return frame.f_globals.get("__name__", "<unknown>")


class RegistrationRefused(OttoError, ValueError):  # noqa: N818 — interface-fixed name the loader and its docs refer to
    """A test file tried to register something other than a suite.

    Test files load on demand, only for the commands that read suites, so
    anything else they registered would silently exist for some commands and not
    others. Extensions belong in an init module, which loads for every command.
    """


_LOADERS_SUSPENDED: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "otto_registry_loaders_suspended", default=False
)
_LOADING_TEST_FILES: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "otto_loading_test_files", default=False
)


@contextlib.contextmanager
def suspend_loaders() -> "Iterator[None]":
    """Read registries without running their loaders (test isolation, introspection)."""
    token = _LOADERS_SUSPENDED.set(True)
    try:
        yield
    finally:
        _LOADERS_SUSPENDED.reset(token)


@contextlib.contextmanager
def loading_test_files() -> "Iterator[None]":
    """Mark the block as importing repo test files: only suites may register inside it."""
    token = _LOADING_TEST_FILES.set(True)
    try:
        yield
    finally:
        _LOADING_TEST_FILES.reset(token)


def is_loading_test_files() -> bool:
    """Whether the current context is importing repo test files."""
    return _LOADING_TEST_FILES.get()


def _is_otto_origin(origin: str) -> bool:
    return origin == "otto" or origin.startswith("otto.")


def refuse_during_test_load(kind: str, name: str, origin: str) -> None:
    """Raise :class:`RegistrationRefused` for a non-otto registration made while test files load.

    Keyed on ORIGIN, not on the phase alone: a test file is often the first thing
    to import an otto module that registers its own entries at import time
    (``otto.project.actions`` registers the built-in project instructions), and
    that registration is otto's, not the test file's.
    """
    if _LOADING_TEST_FILES.get() and not _is_otto_origin(origin):
        raise RegistrationRefused(
            f"{kind} {name!r} is registered from {origin!r} while repo test files load; "
            f"register it from an init module listed in .otto/settings.toml, not a test "
            f"file (test files load only for the commands that read suites)"
        )


class Registry(Generic[T]):
    """Named registry of pluggable components; fail-loud lookups with suggestions."""

    _instances: "ClassVar[weakref.WeakSet[Registry[Any]]]" = weakref.WeakSet()

    def __init__(
        self,
        kind: str,
        *,
        register_hint: str,
        collision_hint: str | None = None,
        loader: str | None = None,
        accepts_test_files: bool = False,
        validate: "Callable[[str, T], None] | None" = None,
    ) -> None:
        """Create a registry for *kind* entries (e.g. ``"term backend"``).

        *register_hint* names the public registration function shown in lookup
        errors (e.g. ``"otto.register_term_backend()"``).

        *collision_hint* replaces the default "Pass overwrite=True to replace it
        deliberately." sentence in duplicate-registration errors. Pass it for a
        registry with no ``overwrite`` escape hatch (e.g. CLI commands), where
        the default sentence would point at a parameter that does not exist.

        *loader* is a ``'module:function'`` called before every read, resolved
        lazily so this module imports nothing; the function decides whether
        there is anything to load, and a read from inside it does not recurse.

        *accepts_test_files* says whether entries may be registered while repo
        test files load (only the suites registry).

        *validate* is this registry's own check of an entry. It runs exactly
        once per entry: at :meth:`register` for a real object, or at the
        entry's first :meth:`get` when it was registered as a :class:`Ref`.
        """
        self.defined_in = caller_module()
        """The module that constructed this registry.

        A guard over :meth:`instances` reads it to tell otto's own seams from
        registries built elsewhere."""
        self._kind = kind
        self._register_hint = register_hint
        self._collision_hint = collision_hint or "Pass overwrite=True to replace it deliberately."
        self._entries: dict[str, "T | Ref"] = {}
        self._origins: dict[str, str] = {}
        self._loader = loader
        self._accepts_test_files = accepts_test_files
        self._validate = validate
        self._loading = False
        Registry._instances.add(self)

    @classmethod
    def instances(cls) -> "list[Registry[Any]]":
        """Every live registry, for guards that must cover registries added later."""
        return list(cls._instances)

    def _load(self) -> None:
        if self._loader is None or self._loading or _LOADERS_SUSPENDED.get():
            return
        module_name, _, attr = self._loader.partition(":")
        import importlib

        self._loading = True
        try:
            getattr(importlib.import_module(module_name), attr)()
        finally:
            self._loading = False

    def register(
        self, name: str, obj: "T | Ref", *, overwrite: bool = False, origin: str | None = None
    ) -> None:
        """Register *obj* under *name*; duplicates are loud unless *overwrite*.

        *obj* is either a real entry or a :class:`Ref` naming one; a real
        object is validated now (see the *validate* constructor argument),
        while a ``Ref`` is validated at its first :meth:`get`.

        *origin* attributes the entry (defaults to the caller's module); it is
        used in collision and listing messages.

        Raises:
            RegistrationRefused: If repo test files are loading, *origin* is
                outside the ``otto`` package, and this registry does not
                accept test-file registrations.
            ValueError: If *name* is already registered and *overwrite* is
                false; the message names both registering modules and ends
                with this registry's collision hint. Also raised by
                *validate*, for a real *obj* that fails the registry's check
                (nothing is stored, and no collision state changes).
        """
        entry_origin = origin if origin is not None else caller_module()
        if not self._accepts_test_files:
            refuse_during_test_load(self._kind, name, entry_origin)
        if name in self._entries and not overwrite:
            raise ValueError(
                f"{self._kind} {name!r} is already registered by "
                f"{self._origins[name]!r}; second registration from "
                f"{entry_origin!r}. {self._collision_hint}"
            )
        if not isinstance(obj, Ref) and self._validate is not None:
            self._validate(name, obj)
        self._entries[name] = obj
        self._origins[name] = entry_origin

    def _ensure_known(self, name: str) -> None:
        """Raise the rich unknown-name error unless *name* is registered.

        Shared by every read that must fail loudly on an unknown name without
        resolving a :class:`Ref` (:meth:`origin`, :meth:`unregister`) as well
        as by :meth:`get`.
        """
        if name in self._entries:
            return
        known = ", ".join(self._entries) or "<none>"
        close = difflib.get_close_matches(name, list(self._entries), n=1)
        suggestion = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"Unknown {self._kind} {name!r}.{suggestion} Registered: {known}. "
            f"Custom entries can be added via {self._register_hint}."
        )

    def _resolved(self, name: str) -> T:
        """Return *name*'s entry, resolving and caching a :class:`Ref`.

        Assumes the caller already ran ``_load()``, so :meth:`items` loads
        once for every name it resolves.
        """
        self._ensure_known(name)
        entry = self._entries[name]
        if isinstance(entry, Ref):
            # Ref.resolve() returns `object`; T has no runtime representation
            # to narrow onto, so this cast bridges the two.
            resolved = cast("T", entry.resolve())
            if self._validate is not None:
                self._validate(name, resolved)
            self._entries[name] = resolved
            return resolved
        return entry

    def get(self, name: str) -> T:
        """Return the entry registered under *name*, resolving a :class:`Ref` on first read.

        A :class:`Ref` entry is imported, validated (if this registry has a
        *validate* hook) and cached in place of the ``Ref`` before returning,
        so later reads of the same name are free. An import or attribute
        error from a failing ``Ref`` propagates unchanged, and the entry is
        left as the ``Ref`` for a later retry.

        Raises:
            ValueError: If *name* is unknown; the message lists registered
                names, adds a did-you-mean suggestion, and points at the
                registration function.
        """
        self._load()
        return self._resolved(name)

    def unregister(self, name: str) -> None:
        """Remove the entry registered under *name*, without resolving a :class:`Ref`.

        Raises:
            ValueError: If *name* is unknown (same rich error as :meth:`get`).
        """
        self._load()
        self._ensure_known(name)
        del self._entries[name]
        del self._origins[name]

    def names(self) -> list[str]:
        """Return registered names in registration order."""
        self._load()
        return list(self._entries)

    def origin(self, name: str) -> str:
        """Return the module that registered *name*, without resolving a :class:`Ref`.

        Raises:
            ValueError: If *name* is unknown (same rich error as :meth:`get`).
        """
        self._load()
        self._ensure_known(name)
        return self._origins[name]

    def items(self) -> list[tuple[str, T]]:
        """Return ``(name, entry)`` pairs in registration order, resolving every :class:`Ref`.

        Calls the loader once, up front, then resolves each entry the same
        way :meth:`get` does (through the shared ``_resolved`` helper) rather
        than calling ``get`` itself, which would run the loader again per
        name.
        """
        self._load()
        return [(name, self._resolved(name)) for name in list(self._entries)]

    def _raw_items(self) -> "list[tuple[str, T | Ref, str]]":
        """Return ``(name, raw_entry, origin)`` for every entry, without resolving or loading.

        Test support: lets a fixture snapshot and restore a registry's exact
        stored state — a real object or a :class:`Ref` — across a test without
        importing every ``Ref``'s target as a side effect of the snapshot
        itself. Product code has no reason to see an unresolved ``Ref``; use
        :meth:`get` or :meth:`items`.
        """
        return [(name, self._entries[name], self._origins[name]) for name in self._entries]

    def _restore_raw(self, name: str, entry: "T | Ref", origin: str) -> None:
        """Write *entry* and *origin* back for *name* directly.

        No validate, no refusal, no collision check.

        Test support, the counterpart of :meth:`_raw_items`: a fixture uses the
        pair to snapshot and restore a registry's exact stored state around a
        test. Going through :meth:`register` instead would re-run *validate*
        on every restored real object; once a validator's outcome can depend on
        state a test changed, one raising here would leave every registry
        after it in the snapshot list unrestored for the rest of the worker
        process, not just the one entry.
        """
        self._entries[name] = entry
        self._origins[name] = origin

    def __contains__(self, name: str) -> bool:
        """Return whether *name* is registered."""
        self._load()
        return name in self._entries

    def __len__(self) -> int:
        """Return the number of registered entries."""
        self._load()
        return len(self._entries)


# ── Registering-repo marker ─────────────────────────────────────────────
# Bootstrap wraps each repo's init-module imports in registering_repo(name)
# so registration functions (product/dev-tool providers, project actions)
# can attribute what they register to the repo whose import is running.
# A ContextVar, not a module global: exception-safe restore for free, and
# nested use (a repo importing another's init helper) unwinds correctly.

_REGISTERING_REPO: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "otto_registering_repo", default=None
)


def get_registering_repo() -> str | None:
    """Name of the repo whose init modules are currently being imported, if any."""
    return _REGISTERING_REPO.get()


@contextlib.contextmanager
def registering_repo(name: str) -> "Iterator[None]":
    """Attribute registrations inside this block to repo *name* (bootstrap-only)."""
    token = _REGISTERING_REPO.set(name)
    try:
        yield
    finally:
        _REGISTERING_REPO.reset(token)
