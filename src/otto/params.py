"""Shared utilities for converting Options dataclasses into ``inspect.Parameter`` lists for Typer.

Used across otto's CLI-facing code (verb-wide and instruction options), and also
holds the per-verb options registry: ``register_options``/
``@options(verbs=[...])`` record which options classes apply to ``otto run``
and ``otto test``, and ``merge_option_params``/``flatten_option_instances``
merge them.
"""

import dataclasses
import inspect
from typing import TYPE_CHECKING, Annotated, Any, cast, get_args, get_origin, get_type_hints

import typer
from typing_extensions import dataclass_transform

from .errors import OttoError
from .registry import Ref, Registry, caller_module, get_registering_repo

if TYPE_CHECKING:
    import pydantic
    from _typeshed import DataclassInstance


def build_options(opts_cls: type, kwargs: dict[str, Any]) -> Any:
    """Construct an Options instance; convert pydantic ``ValidationError`` to a clean exit-2 error.

    Uses ``typer.BadParameter``, not ``click.BadParameter``: Typer >= 0.26
    vendors its own click fork and only its handler catches the vendored
    exception — a real ``click.BadParameter`` would escape uncaught (exit 1, no
    message), the same trap that bit the missing-``--lab`` gate.

    Plain stdlib dataclasses construct exactly as before — no pydantic is
    involved unless the class is a ``@pydantic.dataclasses.dataclass`` (e.g. via
    ``@otto.options``) and a field constraint (``Field(gt=0)``, a validator, ...)
    rejects the value.
    """
    import pydantic

    try:
        return opts_cls(**kwargs)
    except pydantic.ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise typer.BadParameter(problems) from exc


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
            # Called once here (signature-build time), not per invocation —
            # safe because the pydantic dataclass calls the factory again on
            # each construction, so the per-instance field is always fresh;
            # this value is only Typer's CLI-default sentinel.
            return default.default_factory()
        if not default.is_required():
            return default.default
        return inspect.Parameter.empty
    return default


def _is_secret(value: object) -> bool:
    """Whether *value* is a pydantic secret, whose ``str()`` is a mask, not the value."""
    import pydantic

    return isinstance(value, (pydantic.SecretStr, pydantic.SecretBytes, pydantic.Secret))


def drop_unset_secrets(opts_cls: type, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return *kwargs* without the secret-defaulted flags the command line left unset.

    :func:`options_params` gives such a flag the CLI default ``None``; a
    ``None`` for it therefore means "not given", and dropping it lets the
    dataclass apply its own secret default.
    """
    if not dataclasses.is_dataclass(opts_cls):
        return kwargs
    unset = {
        f.name
        for f in dataclasses.fields(opts_cls)
        if kwargs.get(f.name, ...) is None and _is_secret(_field_default(f))
    }
    return {k: v for k, v in kwargs.items() if k not in unset}


def contains_secret_type(tp: Any) -> bool:
    """Return whether *tp* IS, or recursively CONTAINS, ``pydantic.SecretStr``/``SecretBytes``.

    Walks ``typing.get_args`` through every wrapper a field's bare type hint
    can carry -- ``Union``/``X | None``/``Optional[X]`` (a nested-``Union``
    special case), a container generic (``list[X]``, ``tuple[X, ...]``), or a
    residual ``Annotated[X, ...]`` a caller built by hand rather than through
    ``get_type_hints`` -- so ``SecretStr | None``, ``list[SecretStr]`` and
    the rest are caught the same as a bare ``SecretStr`` field, not just the
    unwrapped case. A non-type, non-generic argument (a ``typer.Option(...)``
    instance riding along in an ``Annotated`` tuple, say) has no args of its
    own, so the recursion bottoms out at ``False`` for it instead of raising.

    ``get_origin`` is checked FIRST, not ``isinstance(tp, type)``: a
    ``types.GenericAlias`` -- what ``list[SecretStr]`` actually is -- answers
    ``isinstance(..., type)`` ``True`` in CPython despite being a container,
    so checking that first would ``issubclass`` it against ``SecretStr``
    directly (a silent ``False``, never reached) instead of recursing into
    its own args.
    """
    import pydantic

    if get_origin(tp) is not None:
        return any(contains_secret_type(arg) for arg in get_args(tp))
    if isinstance(tp, type):
        return issubclass(tp, (pydantic.SecretStr, pydantic.SecretBytes))
    return False


def sensitive_field_names(cls: "type[DataclassInstance]") -> "set[str]":
    """Return the field names on *cls* whose VALUE must never be shown or stored.

    A field is sensitive when it sets ``repr=False`` (``dataclasses.field``,
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
    """
    bare_types = get_type_hints(cls, include_extras=False)
    names: set[str] = set()
    for f in dataclasses.fields(cls):
        if not f.repr:
            names.add(f.name)
            continue
        bare = bare_types.get(f.name)
        if bare is not None and contains_secret_type(bare):
            names.add(f.name)
    return names


def _default_hidden(annotation: Any) -> Any:
    """Return *annotation* with its ``typer.Option`` set to ``show_default=False``.

    A bare type gains an ``Annotated[..., typer.Option(show_default=False)]``;
    an existing ``typer.Option`` is copied, never mutated, since the same
    ``OptionInfo`` object is shared by every class that inherits the field.
    """
    import copy

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
        if _is_secret(default):
            # Click renders a default through the option's type, and a secret
            # renders as '**********' -- which the dataclass would then take
            # as the value. None survives click untouched and means "not
            # given" (see :func:`drop_unset_secrets`), so the dataclass's own
            # default applies and neither the help nor a dry run shows it.
            default = None
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
    Identity of THAT class -- not the field's name -- is what decides whether
    two options classes share a flag: two repos each spelling ``lab_env``
    have two fields; two classes inheriting it from one base have one.
    """
    for klass in cls.__mro__:
        if field_name in inspect.get_annotations(klass):
            return klass
    raise KeyError(field_name)


def shared_field_values(
    target_cls: "type[DataclassInstance]", source: Any | None
) -> dict[str, Any]:
    """Values of *source* for the fields it shares with *target_cls* by declaring class.

    A field is shared when both classes carry it AND ``declaring_class`` is
    the same object on both sides. ``None`` or a non-dataclass *source* shares
    nothing, so the caller builds pure defaults.

    A bare options CLASS passed as *source* shares nothing either, and is the
    one case worth spelling out: ``dataclasses.is_dataclass`` answers True for
    a class as well as for an instance, so without the ``isinstance(source,
    type)`` arm the ``getattr`` below would read each field's DEFAULT off the
    class and hand it back as though a caller had chosen it.
    """
    if source is None or not dataclasses.is_dataclass(source) or isinstance(source, type):
        return {}
    source_cls = type(source)
    source_names = {f.name for f in dataclasses.fields(source)}
    shared: dict[str, Any] = {}
    for f in dataclasses.fields(target_cls):
        if f.name not in source_names:
            continue
        if declaring_class(target_cls, f.name) is declaring_class(source_cls, f.name):
            shared[f.name] = getattr(source, f.name)
    return shared


@dataclasses.dataclass(frozen=True)
class OptionsSource:
    """Where a project instruction body's options come from: flat kwargs, or an instance.

    ``from_kwargs`` is the CLI: the parsed flags of a merged command, from
    which each body takes the subset its class declares. The ``ensure``
    converge under ``otto test`` uses it too, over the ``test`` verb's parsed
    flags. ``from_instance`` is a library caller holding one options object:
    each body takes the fields it shares with that instance by declaring
    class and defaults the rest. Both construct through ``build_options`` so
    pydantic validation and its exit-2 rendering are identical on both paths.
    """

    kwargs: dict[str, Any] | None = None
    instance: Any | None = None

    @classmethod
    def from_kwargs(cls, kwargs: dict[str, Any]) -> "OptionsSource":
        """Build a source over the flat parsed kwargs of a CLI dispatch."""
        return cls(kwargs=dict(kwargs))

    @classmethod
    def from_instance(cls, instance: Any | None) -> "OptionsSource":
        """Build a source over one options instance, a library caller's."""
        return cls(instance=instance)

    def build(self, opts_cls: "type[DataclassInstance]") -> Any:
        """Construct *opts_cls* from this source; pydantic validates the result."""
        if self.kwargs is not None:
            picked = drop_unset_secrets(
                opts_cls,
                {
                    f.name: self.kwargs[f.name]
                    for f in dataclasses.fields(opts_cls)
                    if f.name in self.kwargs
                },
            )
        else:
            picked = shared_field_values(opts_cls, self.instance)
        return build_options(opts_cls, picked)


# ── Per-verb options registry ────────────────────────────────────────────

OPTION_VERBS: list[str] = ["run", "test"]
"""Verbs whose flags include every options class registered for them."""


class OptionsRegistrationError(OttoError, ValueError):
    """A ``register_options`` call named a bad verb list, or a class twice."""


class OptionsCollisionError(OttoError):
    """Two options classes declare one flag name from unrelated classes."""


class OptionsNotAvailableError(OttoError, LookupError):
    """``ctx.options(cls)`` (:meth:`otto.context.OttoContext.options`) has no value for *cls*.

    Three reasons, distinguished in the message: *cls* was never registered
    with :func:`register_options` / ``@options(verbs=[...])``; the context has
    no verb bound at all (nothing has called
    :meth:`~otto.context.OttoContext.bind_verb_options` yet); or *cls* is
    registered, but for a different verb than the one bound in this context.

    Lives here, not in ``otto.context``, so ``otto.context`` never needs
    ``otto.errors`` at module scope for this one class — it already depends on
    ``otto.params`` for the registry :meth:`~otto.context.OttoContext.options`
    resolves against, and this taxonomy root travels with it.
    """


@dataclasses.dataclass(frozen=True)
class OptionsEntry:
    """One registration: the class (or a lazy reference to it) and the verbs it serves."""

    target: "type | Ref"
    verbs: list[str]
    repo: str | None


@dataclasses.dataclass(frozen=True)
class OptionsOrigin:
    """A resolved options class with the repo that registered it (``None`` is otto)."""

    cls: type
    repo: str | None


OPTIONS: "Registry[OptionsEntry]" = Registry(
    "options class",
    register_hint="@otto.options(verbs=[...]) in an init module",
)


def options_key(cls_or_path: "type | str | Ref") -> str:
    """Return the registry key for a class, a ``"module:Attr"`` string or a ``Ref``."""
    if isinstance(cls_or_path, Ref):
        return cls_or_path.target
    if isinstance(cls_or_path, str):
        return Ref(cls_or_path).target
    return f"{cls_or_path.__module__}:{cls_or_path.__qualname__}"


def _check_verbs(verbs: list[str]) -> None:
    if not isinstance(verbs, list):
        raise OptionsRegistrationError(
            f'verbs must be a list of verb names, such as ["test"]; got {verbs!r}'
        )
    if not verbs:
        raise OptionsRegistrationError("register_options(...) names no verbs")
    for verb in verbs:
        if verb not in OPTION_VERBS:
            raise OptionsRegistrationError(
                f"unknown verb {verb!r}; verbs that accept options: {', '.join(OPTION_VERBS)}"
            )
        if verbs.count(verb) > 1:
            raise OptionsRegistrationError(f"register_options(...) names {verb!r} twice")


def _register(target: "type | Ref", verbs: list[str], *, origin: str) -> None:
    key = options_key(target)
    if key in OPTIONS:
        raise OptionsRegistrationError(
            f"options class {key} is already registered for "
            f"{', '.join(OPTIONS.get(key).verbs)}; name every verb in one register_options call"
        )
    OPTIONS.register(
        key,
        OptionsEntry(target=target, verbs=list(verbs), repo=get_registering_repo()),
        origin=origin,
    )


def register_options(cls_or_path: "type | str", *, verbs: list[str]) -> None:
    """Register an options class for the verbs whose flags it joins.

    Call it from an init module. *cls_or_path* is the class, or a
    ``"package.module:Attr"`` string that is imported only when one of *verbs*
    is dispatched. Each class registers once, naming every verb it serves.
    """
    _check_verbs(verbs)
    target: "type | Ref" = Ref(cls_or_path) if isinstance(cls_or_path, str) else cls_or_path
    _register(target, verbs, origin=caller_module())


@dataclass_transform(field_specifiers=(dataclasses.field,))
def options(
    cls: "type | None" = None, /, *, verbs: "list[str] | None" = None, **dataclass_kwargs: Any
) -> Any:
    """Declare an options class (a pydantic dataclass); with *verbs*, also register it for them.

    ``@options`` alone registers nothing: use it for an instruction's own
    options class or a shared base. ``@options(verbs=["run", "test"])`` is
    ``register_options(Cls, verbs=[...])`` at the definition site, which
    imports the module at startup; the string form of ``register_options``
    stays the lazy choice.

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
    """
    import pydantic.dataclasses

    def build(target: type) -> type:
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
        target.__pydantic_config__ = cast("pydantic.ConfigDict", config)  # ty: ignore[unresolved-attribute]
        call_kwargs = {k: v for k, v in dataclass_kwargs.items() if k != "config"}
        built = cast("type", pydantic.dataclasses.dataclass(target, **call_kwargs))
        if verbs is not None:
            _check_verbs(verbs)
            _register(built, verbs, origin=target.__module__)
        return built

    return build if cls is None else build(cls)


def verb_option_classes(verb: str) -> list[OptionsOrigin]:
    """Every class registered for *verb*, resolved, in registration order.

    A string registration resolves to the ``Ref``'s TARGET class, which may
    be a re-export -- a name imported into the module the string names,
    rather than the class's own defining module. That class's OWN key
    (``module:qualname``) then differs from the key it was registered under,
    so ``verbs_for(cls)`` would answer "not registered" for the very class
    this function just resolved, and nothing stops the same class from being
    registered again under its defining path. Caught here, at the one point
    every string registration is resolved, rather than left for a caller to
    discover as a silent double-registration.

    A string registration whose module or attribute fails to import raises
    ``OptionsRegistrationError`` naming the target and the registering repo,
    with the import error chained.
    """
    found: list[OptionsOrigin] = []
    for key, entry in OPTIONS.items():
        if verb not in entry.verbs:
            continue
        if isinstance(entry.target, Ref):
            try:
                cls = cast("type", entry.target.resolve())
            except Exception as exc:  # any failure to import it, chained below
                # An OttoError, so a reader that walks every command can
                # contain it and `otto run` says it in one line.
                raise OptionsRegistrationError(
                    f"options class {key!r}, registered for {verb} by {_who(entry.repo)}, "
                    f"cannot be imported: {exc}"
                ) from exc
            resolved_key = options_key(cls)
            if resolved_key != key:
                raise OptionsRegistrationError(
                    f"{key!r} is a re-export: it resolves to a class whose own defining "
                    f"path is {resolved_key!r}, not {key!r}; register it by its defining "
                    f"path (module:qualname) instead of the re-exported one"
                )
        else:
            cls = entry.target
        found.append(OptionsOrigin(cls=cls, repo=entry.repo))
    return found


def verbs_for(cls: type) -> list[str] | None:
    """Return the verbs *cls* is registered for, or ``None`` when it is not registered."""
    key = options_key(cls)
    return list(OPTIONS.get(key).verbs) if key in OPTIONS else None


def _who(repo: str | None) -> str:
    return "otto" if repo is None else f"repo {repo!r}"


def merge_option_params(
    origins: list[OptionsOrigin], *, what: str, reserved: dict[str, str] | None = None
) -> list[inspect.Parameter]:
    """One keyword-only parameter per field, merged across *origins* by declaring class.

    A field two classes inherit from one base is one parameter. The same name
    introduced by two unrelated classes, or a name in *reserved*, raises
    ``OptionsCollisionError`` naming both owners, so a user learns before any
    body runs rather than by one repo silently reading the other's value.
    """
    reserved = reserved or {}
    seen: dict[str, OptionsOrigin] = {}
    declared_by: dict[str, type] = {}
    params: list[inspect.Parameter] = []
    for origin in origins:
        for param in options_params(cast("type[DataclassInstance]", origin.cls)):
            declared = declaring_class(origin.cls, param.name)
            if param.name in reserved:
                raise OptionsCollisionError(
                    f"{what}: field {param.name!r} of {_who(origin.repo)} "
                    f"({declared.__module__}.{declared.__qualname__}) collides with "
                    f"{reserved[param.name]}; rename the field"
                )
            if param.name in seen:
                if declared_by[param.name] is not declared:
                    prior = seen[param.name]
                    prior_cls = declared_by[param.name]
                    raise OptionsCollisionError(
                        f"{what}: field {param.name!r} is declared by both "
                        f"{_who(prior.repo)} ({prior_cls.__module__}.{prior_cls.__qualname__}) "
                        f"and {_who(origin.repo)} ({declared.__module__}.{declared.__qualname__}); "
                        "share one base class, in a required dependency or a library "
                        "package, or rename the field"
                    )
                continue
            seen[param.name] = origin
            declared_by[param.name] = declared
            params.append(param.replace(kind=inspect.Parameter.KEYWORD_ONLY))
    return params


def flatten_option_instances(instances: list[object], *, verb: str) -> dict[str, Any]:
    """Return the flat keyword values a set of options instances stands for under *verb*.

    Every instance must be of a class registered for *verb*. Two instances
    that carry one shared-base field must agree on its value.
    """
    flat: dict[str, Any] = {}
    for instance in instances:
        cls = type(instance)
        verbs = verbs_for(cls)
        if verbs is None or verb not in verbs:
            where = "not registered" if verbs is None else f"registered for {', '.join(verbs)}"
            raise OptionsRegistrationError(
                f"{cls.__qualname__} is {where}, not {verb}; declare it with "
                f"@otto.options(verbs=[{verb!r}, ...]) in an init module"
            )
        for f in dataclasses.fields(cast("DataclassInstance", instance)):
            value = getattr(instance, f.name)
            if f.name in flat and flat[f.name] != value:
                raise ValueError(
                    f"options field {f.name!r} is given two values: {flat[f.name]!r} and {value!r}"
                )
            flat[f.name] = value
    return flat
