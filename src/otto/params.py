"""Shared utilities for converting Options dataclasses into ``inspect.Parameter`` lists for Typer.

Used by both ``otto.suite.register`` (suite options) and ``otto.cli.run``
(instruction options).
"""

import dataclasses
import inspect
from typing import TYPE_CHECKING, Any, get_type_hints

import pydantic
import typer
from pydantic.fields import FieldInfo

if TYPE_CHECKING:
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
    try:
        return opts_cls(**kwargs)
    except pydantic.ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise typer.BadParameter(problems) from exc


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
    for name, typ in hints.items():
        f = flds[name]
        default = f.default if f.default is not dataclasses.MISSING else inspect.Parameter.empty
        # An ``@options`` (pydantic dataclass) field written as
        # ``= Field(default=X, <constraint>)`` stores a ``FieldInfo`` as the
        # dataclass-field default. Unwrap it to the real value so Typer gets a
        # usable CLI default instead of a ``FieldInfo`` object.
        if isinstance(default, FieldInfo):
            if default.default_factory is not None:
                # Called once here (signature-build time), not per invocation —
                # safe because the pydantic dataclass calls the factory again on
                # each construction, so the per-instance field is always fresh;
                # this value is only Typer's CLI-default sentinel.
                default = default.default_factory()
            elif not default.is_required():
                default = default.default
            else:
                default = inspect.Parameter.empty
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
    which each body takes the subset its class declares. ``from_instance`` is
    the ``ensure`` fixture (and a library caller holding one options object):
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
        """Build a source over one options instance (a suite's, or a library caller's)."""
        return cls(instance=instance)

    def build(self, opts_cls: "type[DataclassInstance]") -> Any:
        """Construct *opts_cls* from this source; pydantic validates the result."""
        if self.kwargs is not None:
            picked = {
                f.name: self.kwargs[f.name]
                for f in dataclasses.fields(opts_cls)
                if f.name in self.kwargs
            }
        else:
            picked = shared_field_values(opts_cls, self.instance)
        return build_options(opts_cls, picked)
