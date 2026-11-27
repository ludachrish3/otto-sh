"""Shared utilities for converting Options dataclasses into ``inspect.Parameter`` lists for Typer.

Used across otto's CLI-facing code (verb-wide and instruction options), and also
holds the per-verb options registry: ``register_options``/
``@options(verbs=[...])`` record which options classes apply to ``otto run``
and ``otto test``, and ``merge_option_params``/``flatten_option_instances``
merge them.
"""

import contextlib
import contextvars
import dataclasses
import functools
import inspect
import re
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ForwardRef,
    TypeVar,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from typing_extensions import dataclass_transform

from .errors import OttoError
from .registry import Ref, Registry, get_registering_project, registration_boundary
from .utils import annotation_is_subclass, unwrap_optional

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator, Iterable, Mapping, Sequence
    from contextlib import AbstractContextManager

    import pydantic
    from _typeshed import DataclassInstance

    from .registry import Proposed

T = TypeVar("T")

__all__ = [
    "OPTIONS",
    "OptionVerb",
    "OptionsCollisionError",
    "OptionsNotAvailableError",
    "OptionsRegistrationError",
    "OptionsValidationError",
    "get_options",
    "options",
    "options_key",
    "register_options",
    "use_options",
    "verbs_for",
]


def build_options(
    opts_cls: type, kwargs: dict[str, Any], *, suppliers: "Mapping[str, type] | None" = None
) -> Any:
    """Construct an Options instance; convert pydantic ``ValidationError`` to a clean library error.

    Raises ``OptionsValidationError``; the CLI translates it to a usage error
    at its boundary (``otto.cli.invoke.usage_error_from``).

    Plain stdlib dataclasses construct exactly as before — no pydantic is
    involved unless the class is a ``@pydantic.dataclasses.dataclass`` (e.g. via
    ``@otto.options``) and a field constraint (``Field(gt=0)``, a validator, ...)
    rejects the value.

    A required field *kwargs* leaves out raises the same error, naming the
    field, for a plain dataclass too (whose own complaint is a bare
    ``TypeError``). The CLI marks such a field required, so only a library
    caller reaches this.

    *suppliers* maps a field name to the class whose instance supplied its
    value. A rejected field whose supplier is another class than *opts_cls*
    is reported as ``<opts_cls>.<field> (from <supplier>): <problem>``, so a
    value matched by name from an unrelated class is traced to where it came
    from. Every other problem, and every problem with no *suppliers*, keeps
    the plain ``<field>: <problem>`` text the CLI's usage errors show.
    """
    import pydantic

    if dataclasses.is_dataclass(opts_cls):
        missing = [
            f.name
            for f in dataclasses.fields(opts_cls)
            if f.init
            and f.name not in kwargs
            and f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ]
        if missing:
            raise OptionsValidationError("; ".join(f"{name}: Field required" for name in missing))
    try:
        return opts_cls(**kwargs)
    except pydantic.ValidationError as exc:
        problems = "; ".join(
            _problem(opts_cls, e["loc"], e["msg"], suppliers or {}) for e in exc.errors()
        )
        raise OptionsValidationError(problems) from exc


def _problem(
    opts_cls: type, loc: "Sequence[Any]", msg: str, suppliers: "Mapping[str, type]"
) -> str:
    """One pydantic problem as ``build_options`` reports it, naming a foreign supplier."""
    where = ".".join(str(p) for p in loc)
    supplier = suppliers.get(str(loc[0])) if loc else None
    if supplier is None or supplier is opts_cls:
        return f"{where}: {msg}"
    return f"{opts_cls.__qualname__}.{where} (from {supplier.__qualname__}): {msg}"


def _field_default(f: "dataclasses.Field[Any]") -> Any:
    """Return what dataclass field *f* defaults to, or ``inspect.Parameter.empty``.

    An ``@options`` (pydantic dataclass) field written as
    ``= Field(default=X, <constraint>)`` stores a ``FieldInfo`` as the
    dataclass-field default; it is unwrapped to the real value.
    """
    from pydantic.fields import FieldInfo

    default = f.default if f.default is not dataclasses.MISSING else inspect.Parameter.empty
    if isinstance(default, FieldInfo):
        if default.default_factory is not None:
            # Called here, when the signature is built. Typer passes an unset
            # flag's default to the command, so this value is what an unset
            # flag builds the instance with: one per signature, which is one
            # per CLI invocation.
            return default.default_factory()
        if not default.is_required():
            return default.default
        return inspect.Parameter.empty
    return default


@functools.cache
def _secret_types() -> "tuple[type, ...]":
    """Return the pydantic secret types: a value of one ``str()``s as a mask, not the value.

    A function, not a module constant, so ``pydantic`` stays a call-time
    import (see :func:`sensitive_field_names`). :func:`contains_secret_type`
    and the annotation-text pattern below both read it.
    """
    import pydantic

    return (pydantic.SecretStr, pydantic.SecretBytes, pydantic.Secret)


@functools.cache
def _secret_type_name() -> "re.Pattern[str]":
    """Return a pattern matching a secret type's name as a whole word in annotation text."""
    names = "|".join(t.__name__ for t in _secret_types())
    return re.compile(rf"\b(?:{names})\b")


def contains_secret_type(tp: Any) -> bool:
    """Return whether *tp* IS, or recursively CONTAINS, a pydantic secret type.

    The secret types are ``SecretStr``, ``SecretBytes`` and ``Secret`` (a
    subscripted ``Secret[str]`` counts by its origin).

    Walks ``typing.get_args`` through every wrapper a field's bare type hint
    can carry -- ``Union``/``X | None``/``Optional[X]`` (a nested-``Union``
    special case), a container generic (``list[X]``, ``tuple[X, ...]``), or a
    residual ``Annotated[X, ...]`` a caller built by hand rather than through
    ``get_type_hints`` -- so ``SecretStr | None``, ``list[SecretStr]`` and
    the rest are caught the same as a bare ``SecretStr`` field, not just the
    unwrapped case. A non-type, non-generic argument (a ``typer.Option(...)``
    instance riding along in an ``Annotated`` tuple, say) has no args of its
    own, so the recursion bottoms out at ``False`` for it instead of raising.

    A generic (``list[SecretStr]``) is judged by its origin and then its
    args; whether the origin, or a bare *tp*, is a secret class is
    :func:`otto.utils.annotation_is_subclass`'s one class check.

    A reference left unresolved (a string, or a ``ForwardRef`` from
    ``_field_types``' fallback) is judged by its text: one naming a secret
    type as a whole word counts (``pydantic.SecretStr``,
    ``Optional[SecretStr]`` and ``Secret[str]`` do, ``NotSecretStr`` does
    not), so an annotation that cannot be evaluated never hides a secret
    field. The limit is the text itself: an alias imported only for type
    checking (``Password = SecretStr`` under ``TYPE_CHECKING``, annotated
    ``"Password | None"``) does not name the type and is not seen; give such
    a field ``repr=False``. A runtime alias is evaluated and seen.
    """
    secret_types = _secret_types()
    if isinstance(tp, (str, ForwardRef)):
        text = tp if isinstance(tp, str) else tp.__forward_arg__
        return _secret_type_name().search(text) is not None
    origin = get_origin(tp)
    if origin is not None:
        if annotation_is_subclass(origin, secret_types):
            return True
        return any(contains_secret_type(arg) for arg in get_args(tp))
    return annotation_is_subclass(tp, secret_types)


def _field_types(cls: type) -> "dict[str, Any]":
    """Return the bare type of each annotated field of dataclass *cls*, base classes included.

    :func:`typing.get_type_hints`, unless an annotation cannot be evaluated:
    one naming something imported only for type checking
    (``output_dir: "Path | None"`` with ``Path`` under ``TYPE_CHECKING``, or
    ``Callable[["SSHClientConnection"], ...]``) raises ``NameError`` on every
    Python, ``"os.Nope"`` raises ``AttributeError``, and an invalid
    subscription ``TypeError``. ``get_type_hints`` is all or nothing, so the
    fallback goes field by field: it reads each class's own annotations over
    the MRO -- Python 3.14's :mod:`annotationlib` ``FORWARDREF`` format,
    below 3.14 :func:`inspect.get_annotations`' raw values -- and evaluates
    each string one at a time, the way ``get_type_hints`` would
    (:func:`_evaluated`). Only an annotation that itself fails stays a string
    or a ``ForwardRef``, which :func:`contains_secret_type` judges by its
    text; an unresolvable sibling never demotes a resolvable field to text.
    An evaluated ``Annotated[X, ...]`` is stripped to ``X``.
    """
    try:
        return get_type_hints(cls, include_extras=False)
    except (NameError, AttributeError, TypeError):
        pass
    hints: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        found = _unevaluated_annotations(klass)
        hints.update({name: _strip_annotated(_evaluated(tp, klass)) for name, tp in found.items()})
    return hints


def _evaluated(tp: Any, klass: type) -> Any:
    """Return string annotation *tp* of *klass* evaluated, or the string when it cannot be.

    The same evaluation :func:`typing.get_type_hints` gives one annotation:
    the text in the defining module's globals, with the class namespace as
    locals. Anything else (an evaluated type, a ``ForwardRef`` 3.14 already
    failed to resolve) is returned as it is.

    The defining module is found with :func:`inspect.getmodule`, never
    imported: a class whose module is gone (or was never a real module)
    evaluates against no globals.
    """
    if not isinstance(tp, str):
        return tp
    module = inspect.getmodule(klass)
    globalns = dict(vars(module)) if module is not None else {}
    try:
        return eval(tp, globalns, dict(vars(klass)))  # noqa: S307 -- get_type_hints evaluates this same text
    except Exception:  # noqa: BLE001 -- any failure leaves the text for contains_secret_type
        return tp


def _unevaluated_annotations(klass: type) -> "dict[str, Any]":
    """Return *klass*'s own annotations, any name that cannot be resolved left unresolved."""
    import sys

    if sys.version_info >= (3, 14):
        import annotationlib

        return annotationlib.get_annotations(klass, format=annotationlib.Format.FORWARDREF)
    return inspect.get_annotations(klass)


def _strip_annotated(tp: Any) -> Any:
    """Return *tp* without an outer ``Annotated[...]``, as ``include_extras=False`` would."""
    return get_args(tp)[0] if get_origin(tp) is Annotated else tp


def sensitive_field_names(cls: "type[DataclassInstance | pydantic.BaseModel]") -> "set[str]":
    """Return the field names on *cls* whose VALUE must never be shown or stored.

    *cls* is a dataclass (stdlib or pydantic) or a pydantic model. A field is
    sensitive when it sets ``repr=False`` (``dataclasses.field``,
    or a pydantic ``Field``), or when its declared type IS, or CONTAINS
    (:func:`contains_secret_type` -- a union, an optional, a container), a
    ``pydantic.SecretStr``/``SecretBytes``. The second case is technically
    redundant with ``_options_line``'s own ``repr()`` (already masked there),
    but the ``would run:`` echo (``_param_words``) has no options
    instance to call ``repr()`` on -- only the raw parsed value -- so it needs
    this set to know which flag's value to hide. :func:`options_params` reads
    it too: such a field's default is never shown by ``--help``, and so never
    serialised into the completion cache.

    ``pydantic`` is imported function-local, for the same reason
    :func:`options` defers it: this runs at command-build time, not at import
    time.

    The secret-fields guard (``tests/unit/models/test_secret_fields_guard.py``)
    asks this same question of every otto dataclass and model.
    """
    import pydantic

    if issubclass(cls, pydantic.BaseModel):
        return {
            name
            for name, info in cls.model_fields.items()
            if info.repr is False or contains_secret_type(info.annotation)
        }
    bare_types = _field_types(cls)
    names: set[str] = set()
    for f in dataclasses.fields(cls):
        if not f.repr:
            names.add(f.name)
            continue
        bare = bare_types.get(f.name)
        if bare is not None and contains_secret_type(bare):
            names.add(f.name)
    return names


def sensitive_names(classes: "Iterable[type]") -> frozenset[str]:
    """Return the union of :func:`sensitive_field_names` over every class of *classes*.

    The one place a command's masked field names are collected, from every
    options class that contributes flags to it.
    """
    names: set[str] = set()
    for cls in classes:
        names |= sensitive_field_names(cast("type[DataclassInstance]", cls))
    return frozenset(names)


def _default_hidden(annotation: Any) -> Any:
    """Return *annotation* with its ``typer.Option`` set to ``show_default=False``.

    A bare type gains an ``Annotated[..., typer.Option(show_default=False)]``;
    an existing ``typer.Option`` is copied, never mutated, since the same
    ``OptionInfo`` object is shared by every class that inherits the field.

    ``typer`` is imported function-local, as everywhere in this module, so
    ``import otto.params`` never loads it.
    """
    import copy

    import typer
    from typer.models import OptionInfo

    if get_origin(annotation) is not Annotated:
        return Annotated[annotation, typer.Option(show_default=False)]
    base, *extras = get_args(annotation)
    hidden = []
    for extra in extras:
        if isinstance(extra, OptionInfo):
            extra = copy.copy(extra)  # noqa: PLW2901 — the copy replaces the shared original
            extra.show_default = False
        hidden.append(extra)
    if not any(isinstance(extra, OptionInfo) for extra in hidden):
        hidden.append(typer.Option(show_default=False))
    return Annotated[(base, *hidden)]


def options_params(opts_cls: "type[DataclassInstance]") -> list[inspect.Parameter]:
    """Convert an Options dataclass into inspect.Parameters for Typer.

    Each field must be annotated as ``Annotated[T, typer.Option(...)]`` so that
    Typer can extract the help text and other metadata.  Works with inherited
    fields because ``get_type_hints`` and ``dataclasses.fields`` both traverse
    the full MRO.

    Typer converts each value to its field's type and nothing more. It drops
    every other ``Annotated`` entry, so a ``Field(gt=...)`` constraint or a
    pydantic validator is enforced in one place only: the options class itself,
    through :func:`build_options` (#1119).
    """
    params: list[inspect.Parameter] = []
    hints = get_type_hints(opts_cls, include_extras=True)
    flds = {f.name: f for f in dataclasses.fields(opts_cls)}
    sensitive = sensitive_field_names(opts_cls)
    for name, typ in hints.items():
        default = _field_default(flds[name])
        if name in sensitive:
            # Never shown by --help, and so never written to the completion
            # cache either (its serialiser stores no default for a flag whose
            # help hides it): a ``repr=False`` default may be a credential.
            typ = _default_hidden(typ)  # noqa: PLW2901 — the hidden form replaces the hint
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=typ,
            )
        )
    return params


def declaring_class(cls: type, field_name: str) -> type:
    """Return the class in *cls*'s MRO whose OWN annotations introduce *field_name*.

    ``inspect.get_annotations`` reads one class's annotations without
    inheriting, on every supported interpreter (3.14's lazy ``__annotate__``
    included), so the first hit walking the MRO is the introducing class.
    It decides collisions on ONE command only: two classes offering one flag
    name on the same command share it when they inherit it from one base,
    and collide when each declares it (``merge_option_params``). It does not
    decide which values reach a class: the field name does
    (``shared_field_values``, ``OptionsSource``).
    """
    for klass in cls.__mro__:
        if field_name in inspect.get_annotations(klass):
            return klass
    raise KeyError(field_name)


def shared_field_values(
    target_cls: "type[DataclassInstance]", source: Any | None
) -> dict[str, Any]:
    """Values of *source* for the fields it shares with *target_cls* by name.

    A field is shared when both classes carry an ``init=True`` field of that
    name, whichever class declared it on either side. ``None`` or a
    non-dataclass *source* shares nothing, so the caller builds pure defaults.

    A bare options CLASS passed as *source* shares nothing either, and is the
    one case worth spelling out: ``dataclasses.is_dataclass`` answers True for
    a class as well as for an instance, so without the ``isinstance(source,
    type)`` arm the ``getattr`` below would read each field's DEFAULT off the
    class and hand it back as though a caller had chosen it.
    """
    if source is None or not dataclasses.is_dataclass(source) or isinstance(source, type):
        return {}
    source_names = {f.name for f in dataclasses.fields(source) if f.init}
    return {
        f.name: getattr(source, f.name)
        for f in dataclasses.fields(target_cls)
        if f.init and f.name in source_names
    }


def _pick(
    cls: type,
    *,
    kwargs: "Mapping[str, Any]",
    given: "frozenset[str] | None",
    offered: bool,
) -> dict[str, Any]:
    """Return the values of *kwargs* that *cls* takes: the one place this is decided.

    *cls*'s ``init=True`` field names found in *kwargs*: all of them when the
    command *offered* *cls*'s flags or *given* is ``None`` (every kwarg was
    given), else only the names in *given*, so a flag nobody typed never
    reaches a class the command did not offer.
    """
    return {
        f.name: kwargs[f.name]
        for f in dataclasses.fields(cast("DataclassInstance", cls))
        if f.init and f.name in kwargs and (offered or given is None or f.name in given)
    }


@dataclasses.dataclass(frozen=True)
class OptionsSource:
    """Where a project instruction body's options come from: flat kwargs, or an instance.

    ``from_kwargs`` is the CLI: the parsed flags of a merged command, from
    which each body takes the names its class carries (``_pick``). The
    ``ensure`` converge under ``otto test`` uses it too, over the ``test``
    verb's parsed flags. ``from_instance`` is a library caller holding one
    options object: each body takes the fields it shares with that instance
    by NAME and defaults the rest. Both construct through ``build_options``
    so pydantic validation and its exit-2 rendering are identical on both
    paths.

    *kwargs* holds every parsed flag. *given* names the flags that were
    typed (``None``: all of them); *offered* holds the classes whose flags
    the command offered, which take every parsed flag they carry, where any
    other class takes only the given ones. *suppliers* names, per field, the
    class whose instance supplied a kwarg (for ``build_options``' messages).
    *instances* are instances already built, handed back as they are.
    """

    kwargs: dict[str, Any] | None = None
    instance: Any | None = None
    given: frozenset[str] | None = None
    offered: frozenset[type] = frozenset()
    suppliers: "Mapping[str, type]" = dataclasses.field(default_factory=dict)
    instances: "Mapping[type, Any]" = dataclasses.field(default_factory=dict)

    @classmethod
    def from_kwargs(
        cls,
        kwargs: dict[str, Any],
        *,
        given: "Iterable[str] | None" = None,
        offered: "Iterable[type]" = (),
        suppliers: "Mapping[str, type] | None" = None,
        instances: "Mapping[type, Any] | None" = None,
    ) -> "OptionsSource":
        """Build a source over the flat parsed kwargs of a CLI dispatch."""
        return cls(
            kwargs=dict(kwargs),
            given=None if given is None else frozenset(given),
            offered=frozenset(offered),
            suppliers=dict(suppliers or {}),
            instances=dict(instances or {}),
        )

    @classmethod
    def from_instance(cls, instance: Any | None) -> "OptionsSource":
        """Build a source over one options instance, a library caller's."""
        return cls(instance=instance)

    def build(self, opts_cls: "type[DataclassInstance]") -> Any:
        """Return *opts_cls*'s options from this source; pydantic validates a built one.

        In this order: *instance* itself when it is of exactly *opts_cls*;
        ``instances[opts_cls]`` when present and *instance* shares no field
        name with *opts_cls* (an entry point's own instance comes first);
        else built from the kwargs ``_pick`` gives *opts_cls*, with
        *instance*'s same-named fields laid over them.
        """
        if self.instance is not None and type(self.instance) is opts_cls:
            return self.instance
        own = shared_field_values(opts_cls, self.instance)
        if opts_cls in self.instances and not own:
            return self.instances[opts_cls]
        picked = _pick(
            opts_cls,
            kwargs=self.kwargs or {},
            given=self.given,
            offered=opts_cls in self.offered,
        )
        suppliers = dict(self.suppliers)
        for name in own:
            suppliers[name] = type(self.instance)
        picked.update(own)
        return build_options(opts_cls, picked, suppliers=suppliers or None)


# ── Per-verb options registry ────────────────────────────────────────────

OptionVerb = Annotated[str, "one or two command names joined by one space"]
"""An entry of ``verbs=``: one or two command names joined by one space (``"run install"``).

A ``str`` to every type checker; spelled as an alias so the name stays the
type alias it has always been.
"""


class OptionsRegistrationError(OttoError, ValueError):
    """An options registration that otto refuses.

    It named a bad verb list (not a list, empty, an entry that is not one or
    two command names, or an entry repeated or covered by another), or a key
    that is not its class's own (a raw record under the wrong key, or a
    string naming a re-export); or a string registration did not import.
    """


class OptionsCollisionError(OttoError):
    """Two options classes declare one flag name, or one flag spelling, from unrelated classes."""


class OptionsValidationError(OttoError, ValueError):
    """An options class could not be built: the pydantic problems, joined with ``"; "``."""


class OptionsNotAvailableError(OttoError, LookupError):
    """A read of option values (``ctx.options(cls)``, :func:`get_options`) has no value for *cls*.

    Two reasons, distinguished in the message: *cls* is not an options class
    (a dataclass type); or the command has not read its flags yet (its
    record is pending), so values must be read in a body or a hook.

    Lives here, not in ``otto.context``, so ``otto.context`` never needs
    ``otto.errors`` at module scope for this one class — it already depends on
    ``otto.params`` for the registry :meth:`~otto.context.OttoContext.options`
    resolves against, and this taxonomy root travels with it.
    """


@dataclasses.dataclass(frozen=True)
class OptionsEntry:
    """One registration: the class (or a lazy reference to it) and the entries it serves.

    The entries are ``verbs=`` as registered: ``"run"``, ``"run install"``, a
    plugin's ``"flash erase"``.

    The project that registered it is the engine's: ``OPTIONS.project(key)``.
    """

    target: "type | Ref"
    verbs: "tuple[OptionVerb, ...]"


@dataclasses.dataclass(frozen=True)
class OptionsOrigin:
    """A resolved options class with the project that registered it (``None`` is otto)."""

    cls: type
    project: str | None


def options_key(cls_or_path: "type | str | Ref") -> str:
    """Return the registry key for a class, a ``"module:Attr"`` string or a ``Ref``."""
    if isinstance(cls_or_path, Ref):
        return cls_or_path.target
    if isinstance(cls_or_path, str):
        return Ref(target=cls_or_path).target
    return f"{cls_or_path.__module__}:{cls_or_path.__qualname__}"


_ENTRY_MAX_WORDS = 2
"""The most command names an entry holds: a top-level command and one beneath it."""

_ENTRY_FORM = 'one or two command names joined by one space, such as "run" or "run install"'
"""The accepted form of an entry, as every refusal of a mistyped one states it."""


def _check_verbs(verbs: "list[OptionVerb]") -> None:
    """Refuse a verb list that is not a list, before any record is built."""
    if not isinstance(verbs, list):
        raise OptionsRegistrationError(
            f'verbs must be a list of entries, such as ["run", "run install"]; got {verbs!r}'
        )
    _check_verb_values(verbs)


def _check_verb_values(verbs: "Sequence[object]") -> None:
    """Refuse an empty list, a malformed entry, and an entry repeated or covered by another.

    An entry is judged by its form alone: whether a command of that name
    exists is not known when a class registers, so ``["tset"]`` is kept.
    """
    if not verbs:
        raise OptionsRegistrationError("register_options(...) names no verbs")
    for entry in verbs:
        if not isinstance(entry, str):
            raise OptionsRegistrationError(
                f"verbs entry {entry!r} is not a string; write {_ENTRY_FORM}"
            )
        words = entry.split()
        if not 1 <= len(words) <= _ENTRY_MAX_WORDS or " ".join(words) != entry:
            raise OptionsRegistrationError(f"verbs entry {entry!r} is not {_ENTRY_FORM}")
    entries = cast("Sequence[str]", verbs)
    for index, entry in enumerate(entries):
        if entries.index(entry) != index:
            raise OptionsRegistrationError(f"verbs names {entry!r} twice")
        path = tuple(entry.split())
        for other in entries:
            if other != entry and entry_covers(other, path):
                raise OptionsRegistrationError(
                    f"verbs entry {entry!r} is covered by {other!r}; drop {entry!r}"
                )


def entry_covers(entry: str, path: "Sequence[str]") -> bool:
    """Whether *entry*'s words are a prefix of the command *path*.

    A path is the top-level command's name, then the command names beneath
    it down to the leaf (``("run", "install")``); a host id is an argument
    and is not in it. ``"run"`` covers every ``otto run`` command,
    ``"run install"`` that one.
    """
    words = entry.split()
    return tuple(path[: len(words)]) == tuple(words)


def _check_options_entry(
    name: str, entry: OptionsEntry, proposed: "Proposed[OptionsEntry]"
) -> None:
    """``OPTIONS``' validate: the key is the class's own, and the entries are well formed."""
    del proposed
    key = options_key(entry.target)
    if name != key:
        raise OptionsRegistrationError(
            f"options class registered under {name!r} but its key is {key!r}"
        )
    _check_verb_values(entry.verbs)


def _check_options_resolved(name: str, entry: OptionsEntry) -> None:
    """``OPTIONS``' check on a resolved string registration: it names the class's own path.

    A string registration resolves to the ``Ref``'s TARGET class, which may be
    a re-export -- a name imported into the module the string names, rather
    than the class's own defining module. That class's OWN key
    (``module:qualname``) then differs from the key it was registered under,
    so ``verbs_for(cls)`` would answer "not registered" for the very class
    just resolved, and nothing would stop the same class from being
    registered again under its defining path.
    """
    resolved_key = options_key(entry.target)
    if resolved_key != name:
        raise OptionsRegistrationError(
            f"{name!r} is a re-export: it resolves to a class whose own defining "
            f"path is {resolved_key!r}, not {name!r}; register it by its defining "
            f"path (module:qualname) instead of the re-exported one"
        )


OPTIONS: "Registry[OptionsEntry]" = Registry(
    "options class",
    entry=OptionsEntry,
    register_hint="@otto.options(verbs=[...]) in an init module",
    validate=_check_options_entry,
    check_resolved=_check_options_resolved,
)
"""The registered options classes, keyed by class path, with the verbs each serves."""


@registration_boundary
def register_options(
    cls_or_path: "type | str", *, verbs: "list[OptionVerb]", overwrite: bool = False
) -> None:
    """Register an options class for the commands whose flags it joins.

    Call it from an init module. Each entry of *verbs* is a command as typed
    after ``otto``, one or two command names joined by one space: ``"test"``,
    ``"run"`` (every ``otto run`` command), ``"run install"`` (that one), a
    plugin's ``"flash"``. An entry covers every command whose path starts
    with it. A list that is not a list, is empty, holds an entry of another
    form, or repeats an entry or one another covers (``["run", "run
    install"]``) raises ``OptionsRegistrationError``; whether the command
    exists is not judged here.

    *cls_or_path* is the class, or a ``"package.module:Attr"`` string that is
    imported only when a command it covers needs it. Each class registers
    once, naming every entry it serves; a second registration raises
    :class:`~otto.registry.DuplicateRegistration` unless *overwrite* is true,
    which replaces the list.

    While the command line has an entry checker installed (it installs one
    once its startup check has run), it judges the entries first, and an entry
    that names no command, or one that does not accept registered options,
    raises ``OptionsRegistrationError`` here with nothing recorded.
    """
    _check_verbs(verbs)
    target: "type | Ref" = Ref(target=cls_or_path) if isinstance(cls_or_path, str) else cls_or_path
    _judge_entries(options_key(target), verbs)
    OPTIONS.register(
        name=options_key(target),
        entry=OptionsEntry(target=target, verbs=tuple(verbs)),
        overwrite=overwrite,
    )


_BUILT_MARKER = "__otto_options_class__"
"""Set in the own ``__dict__`` of every class :func:`options` returns; a subclass has none."""


@overload
def options(
    cls: "type[T]",
    /,
    *,
    verbs: "list[OptionVerb] | None" = ...,
    overwrite: bool = ...,
    **dataclass_kwargs: Any,
) -> "type[T]": ...


@overload
def options(
    cls: None = None,
    /,
    *,
    verbs: "list[OptionVerb] | None" = ...,
    overwrite: bool = ...,
    **dataclass_kwargs: Any,
) -> "Callable[[type[T]], type[T]]": ...


@dataclass_transform(field_specifiers=(dataclasses.field,))
@registration_boundary
def options(
    cls: "type | None" = None,
    /,
    *,
    verbs: "list[OptionVerb] | None" = None,
    overwrite: bool = False,
    **dataclass_kwargs: Any,
) -> Any:
    """Declare an options class (a pydantic dataclass); with *verbs*, also register it for them.

    ``@options`` alone registers nothing: use it for an instruction's own
    options class or a shared base. ``@options(verbs=["run", "test"])`` is
    ``register_options(Cls, verbs=[...])`` at the definition site (the same
    entries, refused for the same reasons, the entry checker's included), which
    imports the module at startup; the string form of ``register_options``
    stays the lazy choice. The registration is credited to the module that
    applies the decorator; *overwrite* is ``register_options``'.

    Passes ``defer_build=True`` into pydantic's dataclass config by default
    (merged under an inherited or class-body ``__pydantic_config__`` and any
    caller-supplied ``config``, the caller winning on conflict), so a class's
    schema and validator build on first use rather than at import -- the same
    laziness ``OttoModel`` gives ``BaseModel`` subclasses.

    ``pydantic`` itself is imported here, at call time, not at module scope:
    ``otto.params`` sits under CLI entry points (``otto.cli.run``, ``otto
    run``'s ``--help``, ...) whose import contracts forbid loading pydantic
    just to print help text (tests/unit/test_import_contracts.py). Only
    actually declaring an ``@options`` class pays for it -- exactly the
    module that needed pydantic anyway to be a pydantic dataclass.

    The record is always keyword-only (``kw_only=True``); an explicit
    ``kw_only=False`` raises ``OptionsRegistrationError``.

    A class this decorator already built is refused with
    ``OptionsRegistrationError``: ``options(Cls)`` is not a read of its
    values (that is :func:`get_options`), and registering it for verbs is
    ``register_options``'. A subclass of a built class is a new class and
    is decorated as usual.
    """
    if dataclass_kwargs.get("kw_only", True) is not True:
        raise OptionsRegistrationError(
            f"@options records are keyword-only; drop kw_only={dataclass_kwargs['kw_only']!r}"
        )
    import pydantic.dataclasses

    @registration_boundary
    def build(target: type) -> type:
        if _BUILT_MARKER in vars(target):
            name = target.__name__
            raise OptionsRegistrationError(
                f"{name} is already an options class; read its values with "
                f"otto.get_options({name}), register it with "
                f"otto.register_options({name}, verbs=[...], overwrite=True)"
            )
        # Merge order: an inherited or class-body ``__pydantic_config__``
        # first, then the caller's ``config`` (wins on a shared key), then
        # our own default. Pydantic's own ``dataclass()`` reads
        # ``config if config is not None else getattr(cls, '__pydantic_config__', None)``
        # -- passing ANY non-None ``config`` therefore makes it ignore
        # ``__pydantic_config__`` entirely, silently dropping a base's or the
        # class body's own config. So the merged result is written back onto
        # ``target.__pydantic_config__`` and ``config`` is never passed,
        # which also means pydantic's "config set via both" warning (gated by
        # ``config is not None``) never fires. ``dataclass_kwargs`` is read,
        # never mutated -- a decorator built once (``deco = options(config=X)``)
        # and applied to several classes must keep its config for every one.
        config = dict(getattr(target, "__pydantic_config__", None) or {})
        config.update(dataclass_kwargs.get("config") or {})
        config.setdefault("defer_build", True)
        target.__pydantic_config__ = cast("pydantic.ConfigDict", config)  # ty: ignore[missing-attribute]
        call_kwargs = {k: v for k, v in dataclass_kwargs.items() if k != "config"}
        # Options records are keyword-only like every public record (spec §6.1);
        # the rule is not a default a caller can switch off.
        call_kwargs["kw_only"] = True
        built = cast("type", pydantic.dataclasses.dataclass(target, **call_kwargs))
        if verbs is not None:
            _check_verbs(verbs)
            _judge_entries(options_key(built), verbs)
            OPTIONS.register(
                name=options_key(built),
                entry=OptionsEntry(target=built, verbs=tuple(verbs)),
                overwrite=overwrite,
            )
        setattr(built, _BUILT_MARKER, True)
        return built

    return build if cls is None else build(cls)


@dataclasses.dataclass(frozen=True)
class Registration:
    """One registration as registered: nothing about it is resolved or imported."""

    key: str
    """The registry key, ``"module:QualName"``."""
    entries: "tuple[str, ...]"
    """The ``verbs=`` entries, as registered."""
    project: str | None
    """The registering project; ``None`` for otto's own or a registration made outside one."""


@dataclasses.dataclass
class _EntryChecker:
    """The one record the entry checker lives on, so no function rebinds a module global."""

    check: "Callable[[Registration], None] | None" = None


_ENTRY_CHECKER = _EntryChecker()


def set_entry_checker(checker: "Callable[[Registration], None] | None") -> None:
    """Install *checker* as the judge of every registration made from now on; ``None`` removes it.

    The registry installs none itself, so a library process judges no entry
    against the commands. The command line installs one once its startup
    check has run and removes it when the invocation ends: a registration
    made in between (a command body's, a lazily imported module's) is judged
    when it is made. While one is set, :func:`register_options` and
    ``@options(verbs=)`` call it with the registration they are about to make,
    after their own form check and before the registry is written; what it
    raises propagates and nothing is recorded.
    """
    _ENTRY_CHECKER.check = checker


def _judge_entries(key: str, verbs: "list[OptionVerb]") -> None:
    """Hand the registration about to be made to the installed entry checker, if any."""
    check = _ENTRY_CHECKER.check
    if check is not None:
        check(Registration(key=key, entries=tuple(verbs), project=get_registering_project()))


def registrations() -> list[Registration]:
    """Every registration in ``OPTIONS``, in registration order, resolving nothing."""
    return [
        Registration(key=key, entries=tuple(stored.verbs), project=OPTIONS.project(name=key))
        for key, stored in OPTIONS.raw_items()
    ]


def covering(
    path: "Sequence[str]",
    *,
    kept: "Collection[str] | None" = None,
    among: "Iterable[Registration] | None" = None,
) -> list[Registration]:
    """Return the registrations of *among* that cover the command *path*, in order.

    *among* defaults to :func:`registrations`. A registration is kept when
    one of its entries covers *path* (:func:`entry_covers`) and its project
    is ``None`` (a registration no project owns always applies) or in
    *kept*; ``kept=None`` keeps every project. Nothing is resolved or
    imported, so a caller can tell which classes a command takes before it
    pays for any of them.

    A path under ``host`` is covered by nothing yet: host commands do not
    take registered flags.
    """
    if path and path[0] == "host":
        return []
    pool = registrations() if among is None else among
    return [
        registration
        for registration in pool
        if (kept is None or registration.project is None or registration.project in kept)
        and any(entry_covers(entry, path) for entry in registration.entries)
    ]


def resolve_registration(registration: Registration) -> OptionsOrigin:
    """Resolve *registration* to its class and the project that registered it.

    A string registration is imported here, the one point every string
    registration is resolved, so a re-export (``OPTIONS``' resolved check)
    is refused here rather than left for a caller to discover as a silent
    double-registration.

    A string registration whose module or attribute fails to import raises
    ``OptionsRegistrationError`` naming the target, its entries and the
    registering project, with the import error chained.
    """
    key = registration.key
    stored = OPTIONS.peek(name=key)
    if isinstance(stored.target, Ref):
        try:
            stored.target.resolve()
        except Exception as exc:  # any failure to import it, chained below
            # An OttoError, so a reader that walks every command can
            # contain it and `otto run` says it in one line.
            entries = ", ".join(repr(entry) for entry in registration.entries)
            raise OptionsRegistrationError(
                f"options class {key!r}, registered for {entries} by "
                f"{_who(registration.project)}, cannot be imported: {exc}"
            ) from exc
    cls = cast("type", OPTIONS.get(key).target)
    return OptionsOrigin(cls=cls, project=registration.project)


def verb_option_classes(verb: str) -> list[OptionsOrigin]:
    """Every class registered for the whole of *verb*, resolved, in registration order.

    The classes whose entries cover the one-word path ``(verb,)``: a class
    registered for ``"run"`` is here, one registered for ``"run install"``
    alone is not. Each is resolved by ``resolve_registration``, which
    raises ``OptionsRegistrationError`` for one that cannot be imported.
    """
    return [resolve_registration(registration) for registration in covering((verb,))]


def verbs_for(cls: type) -> "list[OptionVerb] | None":
    """Return the entries *cls* is registered for, as registered, or ``None`` when it is not."""
    key = options_key(cls)
    return list(OPTIONS.peek(name=key).verbs) if key in OPTIONS else None


def _who(project: str | None) -> str:
    return "otto" if project is None else f"project {project!r}"


def authored_flags(option_info: Any) -> list[str]:
    """Return the user-authored flag strings from a ``typer.Option`` instance.

    Typer stores the first positional flag as the info's ``default`` attribute
    and the rest in ``param_decls``; concatenate them in declaration order so
    the rebuilder reproduces the original call.
    """
    flags: list[str] = []
    primary = getattr(option_info, "default", None)
    if isinstance(primary, str) and (primary.startswith("-") or "/" in primary):
        flags.append(primary)
    flags.extend(getattr(option_info, "param_decls", ()) or ())
    return flags


@dataclasses.dataclass(frozen=True)
class DeclaredParameter:
    """How Typer reads one signature parameter before it builds the command."""

    annotation: Any
    """Its type, ``Annotated`` metadata stripped."""
    info: Any
    """The ``typer.Option``/``typer.Argument`` info that governs it, or ``None``."""
    argument: bool
    """Whether Typer makes it a positional argument rather than an option."""
    declarations: "list[str]"
    """The spellings its author wrote, in order (empty when Typer names it)."""
    default: Any
    """Its value's default: the info's own for the default-value style."""


def declared_parameter(param: inspect.Parameter) -> DeclaredParameter:
    """Read *param* as Typer reads it: the info that governs it, and whether it is an argument.

    The one reading of a parameter before Typer has built it. Both of
    Typer's styles are read: ``Annotated`` metadata, the form
    :func:`options_params` produces (where the first declaration may sit in
    the info's ``default``, :func:`authored_flags`), and a default that is
    itself a ``typer.Option(...)``/``typer.Argument(...)`` (the default-value
    style), whose declarations follow the default value
    (``typer.Option(0, "--lvl", "-l")``) and whose ``default`` is that
    value. A parameter with no info is an option when it has a default and
    an argument when it has none.
    """
    from typer.models import OptionInfo, ParameterInfo

    annotation = param.annotation
    if get_origin(annotation) is Annotated:
        annotation, *extras = get_args(annotation)
        infos = [extra for extra in extras if isinstance(extra, ParameterInfo)]
        info = infos[0] if infos else None
        return DeclaredParameter(
            annotation=annotation,
            info=info,
            argument=(
                not isinstance(info, OptionInfo)
                if info is not None
                else param.default is inspect.Parameter.empty
            ),
            declarations=authored_flags(info) if isinstance(info, OptionInfo) else [],
            default=param.default,
        )
    if isinstance(param.default, ParameterInfo):
        info = param.default
        return DeclaredParameter(
            annotation=annotation,
            info=info,
            argument=not isinstance(info, OptionInfo),
            # Here the info's own default is the value, never a declaration.
            declarations=list(info.param_decls or ()) if isinstance(info, OptionInfo) else [],
            default=info.default,
        )
    return DeclaredParameter(
        annotation=annotation,
        info=None,
        argument=param.default is inspect.Parameter.empty,
        declarations=[],
        default=param.default,
    )


def flag_spellings(param: inspect.Parameter) -> list[str]:
    """Return every word that selects *param*'s flag on the command Typer builds from it.

    The one reading of a field's spellings before Typer has built the
    command (``merge_option_params`` runs first), over
    ``declared_parameter``. Each declaration the author wrote is split
    on ``/``, so ``"--cov/--no-cov"`` is two words. With none written, Typer
    generates ``--<name>`` from the parameter name, underscores turned to
    dashes, and for a field it makes a flag, one annotated ``bool`` once
    ``None`` is stripped from a union, adds ``--no-<name>``. That last rule
    restates Typer's own (it has no public function for it); a parity test
    holds the two together. A parameter Typer makes an argument has no
    spelling.
    """
    import typer.main

    declared = declared_parameter(param)
    if declared.argument:
        return []
    declarations = [decl for decl in declared.declarations if not decl.isidentifier()]
    if declarations:
        return [word.strip() for decl in declarations for word in decl.split("/") if word.strip()]
    name = typer.main.get_default_option_flag_name(param.name)
    if unwrap_optional(declared.annotation) is bool:
        return [f"--{name}", f"--no-{name}"]
    return [f"--{name}"]


def _class_owner(project: str | None, declared: type) -> str:
    """Who declares a field, in a collision message: ``project 'a' (pkg.mod.Opts)``."""
    return f"{_who(project)} ({declared.__module__}.{declared.__qualname__})"


def _owner(origin: OptionsOrigin, declared: type, field: str) -> str:
    """One field and who declares it, in a collision message."""
    return f"field {field!r} of {_class_owner(origin.project, declared)}"


def merge_option_params(
    origins: list[OptionsOrigin],
    *,
    what: str,
    reserved: dict[str, str] | None = None,
    reserved_flags: "Mapping[str, str] | None" = None,
) -> list[inspect.Parameter]:
    """One keyword-only parameter per field, merged across *origins* by declaring class.

    A field two classes inherit from one base is one parameter. The same name
    introduced by two unrelated classes, or a name in *reserved*, raises
    ``OptionsCollisionError`` naming both owners, so a user learns before any
    body runs rather than by one project silently reading the other's value.

    Spellings are checked the same way: two fields whose :func:`flag_spellings`
    share a word (``--debug``, ``--no-debug``, ``-d``) collide unless they are
    the one field of one declaring class, and so does a field spelled like a
    key of *reserved_flags*, which maps a spelling to who owns it (a command's
    own parameter).
    """
    reserved = reserved or {}
    owned_flags: "dict[str, str]" = dict(reserved_flags or {})
    seen: dict[str, OptionsOrigin] = {}
    declared_by: dict[str, type] = {}
    spelled_by: dict[str, str] = {}
    params: list[inspect.Parameter] = []
    for origin in origins:
        for param in options_params(cast("type[DataclassInstance]", origin.cls)):
            declared = declaring_class(origin.cls, param.name)
            owner = _owner(origin, declared, param.name)
            if param.name in reserved:
                raise OptionsCollisionError(
                    f"{what}: {owner} collides with {reserved[param.name]}; rename the field"
                )
            if param.name in seen:
                if declared_by[param.name] is not declared:
                    prior = _class_owner(seen[param.name].project, declared_by[param.name])
                    raise OptionsCollisionError(
                        f"{what}: field {param.name!r} is declared by both {prior} "
                        f"and {_class_owner(origin.project, declared)}; "
                        "share one base class, in a required dependency or a library "
                        "package, or rename the field"
                    )
                continue
            for spelling in flag_spellings(param):
                if spelling in owned_flags:
                    raise OptionsCollisionError(
                        f"{what}: flag {spelling} of {owner} collides with "
                        f"{owned_flags[spelling]}; respell the flag"
                    )
                if spelling in spelled_by:
                    raise OptionsCollisionError(
                        f"{what}: flag {spelling} is spelled by both {spelled_by[spelling]} "
                        f"and {owner}; respell one of the flags"
                    )
                spelled_by[spelling] = owner
            seen[param.name] = origin
            declared_by[param.name] = declared
            params.append(param.replace(kind=inspect.Parameter.KEYWORD_ONLY))
    return params


def flatten_option_instances(instances: list[object]) -> dict[str, Any]:
    """Return the flat keyword values a set of options instances stands for.

    An instance of any options class is accepted, registered or not. Only
    ``init=True`` fields count, so a computed field is never a value. Two
    instances that carry one field name must agree on its value.
    """
    flat: dict[str, Any] = {}
    for instance in instances:
        for f in dataclasses.fields(cast("DataclassInstance", instance)):
            if not f.init:
                continue
            value = getattr(instance, f.name)
            if f.name in flat and flat[f.name] != value:
                raise ValueError(
                    f"options field {f.name!r} is given two values: {flat[f.name]!r} and {value!r}"
                )
            flat[f.name] = value
    return flat


# ── Option values: what a command recorded, task-scoped layers, the lookup ──


@dataclasses.dataclass
class RecordedOptions:
    """What one command was given: the record every read of its option values starts from.

    *parsed* holds every option-field value the command parsed and *given*
    the names of those someone gave. A class in *offered* (the command
    offered its flags) takes every parsed value it carries; any other class
    only the given ones (``_pick``). *suppliers* names, per field, the class
    whose instance supplied it, for messages only. *instances* holds the
    instances prepared for the command and those built on first read.
    While *pending*, the command has not read its flags and a read is
    refused.
    """

    group: str | None = None
    """The top-level command it was recorded under (``"run"``, ``"cov"``, a plugin's name).

    Set for a command that accepts registered options; ``None`` for one that
    records only its own class, and for a record no command made.
    """
    parsed: dict[str, Any] = dataclasses.field(default_factory=dict)
    given: frozenset[str] = frozenset()
    offered: "tuple[type, ...]" = ()
    suppliers: "dict[str, type]" = dataclasses.field(default_factory=dict)
    instances: "dict[type, Any]" = dataclasses.field(default_factory=dict)
    pending: bool = False

    @classmethod
    def all_given(
        cls,
        kwargs: "Mapping[str, Any]",
        *,
        group: str | None = None,
        offered: "tuple[type, ...]" = (),
    ) -> "RecordedOptions":
        """Record *kwargs* as values someone gave, every one of them: a caller's own values."""
        return cls(group=group, parsed=dict(kwargs), given=frozenset(kwargs), offered=offered)


@dataclasses.dataclass(eq=False)
class _Layer:
    """One block's values: its instances, the field values it gives, and what it built."""

    instances: "dict[type, Any]"
    values: dict[str, Any]
    suppliers: "dict[str, type]"
    # (class, id(record)) -> (record, instance): the record is kept beside the
    # instance so its id is never reused while the entry lives.
    built: "dict[tuple[type, int], tuple[Any, Any]]" = dataclasses.field(default_factory=dict)

    def touches(self, cls: type) -> bool:
        """Whether this layer holds an instance of *cls* or sets one of its field names."""
        return cls in self.instances or any(name in self.values for name in init_field_names(cls))


_LAYERS: "contextvars.ContextVar[tuple[_Layer, ...]]" = contextvars.ContextVar(
    "otto_option_layers", default=()
)
_COMMAND_OPTIONS: "contextvars.ContextVar[RecordedOptions | None]" = contextvars.ContextVar(
    "otto_command_options", default=None
)


def init_field_names(cls: type) -> list[str]:
    """Return the ``init=True`` field names of dataclass *cls*."""
    return [f.name for f in dataclasses.fields(cast("DataclassInstance", cls)) if f.init]


def _init_values(instance: Any) -> dict[str, Any]:
    """Return the ``init=True`` field values of dataclass *instance*."""
    return {name: getattr(instance, name) for name in init_field_names(type(instance))}


def _check_instances(instances: "tuple[object, ...]") -> None:
    """Refuse anything that is not an options instance: a class, or a non-dataclass."""
    for instance in instances:
        if isinstance(instance, type) or not dataclasses.is_dataclass(instance):
            raise OptionsRegistrationError(
                f"expected an options instance, got {instance!r}; "
                "hand in an instance of an options class, such as Acme()"
            )


@contextlib.contextmanager
def _pushed(var: "contextvars.ContextVar[Any]", value: Any) -> "Generator[None]":
    """Set *var* to *value* for the block; restore it on the way out, from any context."""
    outer = var.get()
    token = var.set(value)
    try:
        yield
    finally:
        try:
            var.reset(token)
        except ValueError:  # the block was entered in another Context
            var.set(outer)


def _lay(instances: "tuple[object, ...]", values: dict[str, Any]) -> "AbstractContextManager[None]":
    """Lay one layer of *instances*, giving *values*, over the task's layers for the block.

    The task's layers are read when the block is entered, not when this is
    called, so a block made in one place and entered in another lays itself
    over the layers in force where it is entered.
    """
    layer = _Layer(
        instances={type(inst): inst for inst in instances},
        values=values,
        suppliers={
            name: type(inst)
            for inst in reversed(instances)
            for name in init_field_names(type(inst))
        },
    )
    return _laid(layer)


@contextlib.contextmanager
def _laid(layer: _Layer) -> "Generator[None]":
    """Push *layer* over the task's layers as they are on entry, for the block."""
    with _pushed(_LAYERS, (*_LAYERS.get(), layer)):
        yield


def use_options(*instances: object) -> "AbstractContextManager[None]":
    """Set option values for the code inside the block, in this task and the tasks it starts.

    Inside the block, a read of an instance's own class returns that
    instance, and every other options class takes the instances' field
    values by name over what it would read outside. Blocks nest; an inner
    block overrides only the names it sets. Values are read-only, and are
    not copied.

    Args:
        *instances: Options instances (dataclass instances). Two that carry
            one field name must agree on its value.

    Returns:
        A context manager; leaving it restores the values in force before.

    Raises:
        OptionsRegistrationError: An argument is a class or not a dataclass instance.
        ValueError: Two instances give one field two values.

    Example:
        >>> import otto
        >>> from otto.params import get_options, use_options
        >>> @otto.options
        ... class Acme:
        ...     debug_level: int = 0
        >>> with use_options(Acme(debug_level=3)):
        ...     get_options(Acme).debug_level
        3
    """
    _check_instances(instances)
    return _lay(instances, flatten_option_instances(list(instances)))


def own_options(
    *instances: object, gives: "Collection[str]" = ()
) -> "AbstractContextManager[None]":
    """Lay an entry point's own instance(s): read back as they are; only *gives* reach others.

    Like :func:`use_options`, except that only the field names in *gives*
    reach other options classes.
    """
    _check_instances(instances)
    values: dict[str, Any] = {}
    for inst in instances:
        values.update({k: v for k, v in _init_values(inst).items() if k in gives})
    return _lay(instances, values)


def command_options(recorded: "RecordedOptions | None") -> "AbstractContextManager[None]":
    """Hold *recorded*, a command with no context's record, for the block; ``None`` is a no-op."""
    if recorded is None:
        return contextlib.nullcontext()
    return _pushed(_COMMAND_OPTIONS, recorded)


def recorded_in_force() -> "RecordedOptions | None":
    """Return the recorded flags in force (the one accessor).

    The installed run's record when a run is installed; else the contextless
    command's; else ``None``.
    """
    from .invocation import installed_run

    run = installed_run()
    if run is not None:
        return run._recorded_options()  # noqa: SLF001 — private protocol member, kept out of the API
    return _COMMAND_OPTIONS.get()


def _first_read(cls: type, kwargs: dict[str, Any], suppliers: "Mapping[str, type]") -> Any:
    """Build *cls* for a read; a rejection names the class in front of ``build_options``' text."""
    try:
        return build_options(cls, kwargs, suppliers=suppliers or None)
    except OptionsValidationError as exc:
        text = str(exc)
        if text.startswith(f"{cls.__qualname__}."):  # a foreign supplier's text names it already
            raise
        raise OptionsValidationError(f"{cls.__qualname__}: {text}") from exc


def _recorded_values(cls: type, recorded: "RecordedOptions | None") -> dict[str, Any]:
    """Return what *cls* takes from *recorded* (``_pick``); nothing with no record."""
    if recorded is None:
        return {}
    return _pick(cls, kwargs=recorded.parsed, given=recorded.given, offered=cls in recorded.offered)


def build_offered(recorded: "RecordedOptions") -> "dict[type, Any]":
    """Build every class *recorded* offers from *recorded* alone: the one such build.

    Each class takes what ``_pick`` gives it from the record; the task's
    layers play no part. A bad value raises ``build_options``' own
    ``OptionsValidationError``, naming the field, before anything is kept.
    """
    return {
        cls: build_options(
            cls, _recorded_values(cls, recorded), suppliers=recorded.suppliers or None
        )
        for cls in recorded.offered
    }


def _refuse_pending(recorded: "RecordedOptions | None", what: str) -> None:
    """Raise ``OptionsNotAvailableError`` for *what* when *recorded* is pending (one message)."""
    if recorded is not None and recorded.pending:
        raise OptionsNotAvailableError(
            f"{what} cannot be read: the command has not read its flags yet; "
            "read option values in a body or a hook"
        )


def _lookup(cls: Any, recorded: "RecordedOptions | None", layers: "Sequence[_Layer]") -> Any:
    """``lookup_options`` over an explicit sequence of *layers*, outermost first."""
    if not isinstance(cls, type) or not dataclasses.is_dataclass(cls):
        raise OptionsNotAvailableError(f"{cls!r} is not an options class (a dataclass type)")
    _refuse_pending(recorded, cls.__qualname__)
    at = next((i for i in reversed(range(len(layers))) if layers[i].touches(cls)), None)
    if at is None:
        if recorded is None:
            return _first_read(cls, {}, {})
        if cls not in recorded.instances:
            recorded.instances[cls] = _first_read(
                cls, _recorded_values(cls, recorded), recorded.suppliers
            )
        return recorded.instances[cls]
    top = layers[at]
    if cls in top.instances:
        return top.instances[cls]
    key = (cls, id(recorded))
    if key in top.built:
        return top.built[key][1]
    base_at = next((i for i in reversed(range(at)) if cls in layers[i].instances), None)
    suppliers: dict[str, type] = dict(recorded.suppliers) if recorded is not None else {}
    if base_at is None:
        kwargs = _recorded_values(cls, recorded)
        start = 0
    else:
        kwargs = _init_values(layers[base_at].instances[cls])
        suppliers.update(dict.fromkeys(kwargs, cls))
        start = base_at + 1
    names = set(init_field_names(cls))
    for layer in layers[start : at + 1]:
        for name, value in layer.values.items():
            if name in names:
                kwargs[name] = value
                suppliers[name] = layer.suppliers.get(name, cls)
    built = _first_read(cls, kwargs, suppliers)
    top.built[key] = (recorded, built)
    return built


def lookup_options(cls: "type[T]", recorded: "RecordedOptions | None") -> "T":
    """Return *cls*'s option values from *recorded* and the task's layers: the one lookup.

    The innermost layer that touches *cls* (holds an instance of it, or sets
    one of its field names) decides: its own instance of *cls* as it is,
    else a build, once per layer and record, from the nearest outer
    instance of *cls* (or *recorded*'s values) with every layer's values
    from there inward laid over it. With no such layer: *recorded*'s
    instance, built once; with no record, *cls*'s defaults. A failed build
    is not kept.

    Raises:
        OptionsNotAvailableError: *cls* is not a dataclass type, or *recorded* is pending.
        OptionsValidationError: The build failed; the message names *cls* and the field.
    """
    return _lookup(cls, recorded, _LAYERS.get())


def get_options(cls: "type[T]") -> "T":
    """Return the option values in force for options class *cls*, from anywhere.

    Reads the recorded flags of the command in force (if any) with every
    ``use_options`` block around the caller laid over them. With neither,
    the class's defaults.

    Args:
        cls: An options class (a dataclass type), registered or not.

    Returns:
        An instance of *cls*. Treat it as read-only.

    Raises:
        OptionsNotAvailableError: *cls* is not an options class, or the
            command has not read its flags yet.
        OptionsValidationError: A required field has no value, or the
            class's validation rejects one; the message names the class.

    Example:
        >>> import otto
        >>> from otto.params import get_options
        >>> @otto.options
        ... class Acme:
        ...     debug_level: int = 0
        >>> get_options(Acme).debug_level
        0
    """
    return lookup_options(cls, recorded_in_force())


def options_snapshot(
    recorded: "RecordedOptions | None",
    *,
    own: object | None = None,
    handed: "Mapping[str, Any] | None" = None,
    layers: bool = True,
) -> OptionsSource:
    """Return the option values in force as an ``OptionsSource`` for project-action bodies.

    *kwargs* is *recorded*'s parsed values, the layers' values over them
    (outermost first), and *handed* over those. *given* adds the names the
    layers and *handed* set to *recorded*'s (``None`` with no record).
    *instances* holds, for every class with an instance in a layer or in
    *recorded*, what :func:`lookup_options` returns, so the two agree; a
    class whose rebuild fails, or one of whose values *handed* changes, is
    left out and built from the kwargs when asked for. *own* is the
    source's own instance. With *layers* false the layers contribute nothing.
    A pending *recorded* raises ``OptionsNotAvailableError``, as a read does.
    """
    _refuse_pending(recorded, "option values")
    stack = _LAYERS.get() if layers else ()
    handed = dict(handed or {})
    kwargs: dict[str, Any] = dict(recorded.parsed) if recorded is not None else {}
    suppliers: dict[str, type] = dict(recorded.suppliers) if recorded is not None else {}
    for layer in stack:
        kwargs.update(layer.values)
        suppliers.update({name: layer.suppliers[name] for name in layer.values})
    kwargs.update(handed)
    given: frozenset[str] | None = None
    if recorded is not None:
        set_by_layers = {name for layer in stack for name in layer.values}
        given = recorded.given | set_by_layers | set(handed)
    classes = dict.fromkeys(
        [c for layer in stack for c in layer.instances]
        + (list(recorded.instances) if recorded is not None else [])
    )
    instances: dict[type, Any] = {}
    for cls in classes:
        try:
            inst = _lookup(cls, recorded, stack)
        except OptionsValidationError:
            continue
        values = _init_values(inst)
        if any(name in values and values[name] != value for name, value in handed.items()):
            continue
        instances[cls] = inst
    return OptionsSource(
        kwargs=kwargs,
        instance=own,
        given=given,
        offered=frozenset(recorded.offered) if recorded is not None else frozenset(),
        suppliers=suppliers,
        instances=instances,
    )
