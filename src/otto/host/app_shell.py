"""AppShell parsing engine — ``Parsed`` models and the ``parse=`` dispatch.

This module holds the *parsing half* of otto's AppShell feature: regex-backed
pydantic models (:class:`Parsed`) and the functions that turn REPL output into
typed objects. The interactive :class:`AppShell` REPL itself lives alongside
these in this file but is added by a later task.

A :class:`Parsed` subclass pairs a pydantic model with the compiled regex that
produces it. Named groups feed same-named fields; pydantic converts the
captured strings to the field types. A field typed as another ``Parsed``
subclass — or ``list[Sub]`` / ``Sub | None`` — is parsed *recursively* from the
region its group captured, so composite REPL output (mysql's bordered table
*and* its trailing stats line, say) maps to a nested object graph.

The public entry point is :func:`apply_parse`, which dispatches on the shape of
the ``parse=`` spec:

* a ``Parsed`` subclass  -> single :func:`parse_one` (``pattern.search``);
* ``list[Sub]``          -> :func:`parse_all` (``pattern.finditer``), where the
  empty list is a valid "zero rows" answer;
* any other callable     -> called as an escape hatch, its return value used
  verbatim and any exception surfaced as :class:`ParseMismatch`.
"""

import re
import types
from typing import Any, ClassVar, Union, get_args, get_origin

from typing_extensions import override

from otto.models.base import OttoModel


class ParseMismatch(ValueError):  # noqa: N818 — spec-mandated public name; an `Error` suffix would break the documented AppShell API
    """Output did not match the model's pattern (or the callable raised)."""


class Parsed(OttoModel):
    """A pydantic model plus the regex that produces it.

    Named groups feed same-named fields; a field typed as another ``Parsed``
    subclass (or ``list[Sub]`` / ``Sub | None``) is recursively parsed from the
    region its group captured. Subclasses must define ``pattern`` as a compiled
    :class:`re.Pattern`; a class-definition-time check enforces that the
    pattern's named groups are a subset of the field names (typo guard) and a
    superset of the required fields (so pattern/model drift is impossible).
    Because every subclass self-checks at its own definition, nested models are
    validated automatically at each level.
    """

    pattern: ClassVar[re.Pattern[str]]

    @classmethod
    @override
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        pattern = getattr(cls, "pattern", None)
        if not isinstance(pattern, re.Pattern):
            raise TypeError(f"{cls.__name__} must define a compiled ClassVar 'pattern'")
        groups = set(pattern.groupindex)
        fields = set(cls.model_fields)
        required = {name for name, field in cls.model_fields.items() if field.is_required()}
        if groups - fields:
            raise TypeError(
                f"{cls.__name__}: pattern named groups {sorted(groups - fields)} "
                f"have no matching field"
            )
        if required - groups:
            raise TypeError(
                f"{cls.__name__}: required fields {sorted(required - groups)} "
                f"have no pattern named group"
            )


def _unwrap_optional(annotation: Any) -> Any:
    """Return the inner type of an ``X | None`` annotation, else the annotation.

    Handles both the ``types.UnionType`` (``X | None``) and ``typing.Union``
    (``Optional[X]``) spellings. A union of several non-``None`` members is
    returned unchanged — only the ``Sub | None`` shape is unwrapped.
    """
    if get_origin(annotation) in (types.UnionType, Union):
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _parse_region(annotation: Any, region: str | None) -> Any:
    """Interpret one group's captured ``region`` per its field ``annotation``.

    A ``None`` region (an optional group that did not participate) is passed
    through as ``None``. A ``Parsed``-typed field recurses via
    :func:`parse_one`; a ``list[Sub]`` field via :func:`parse_all`; any scalar
    field keeps the raw string for pydantic to convert.
    """
    if region is None:
        return None
    inner = _unwrap_optional(annotation)
    if get_origin(inner) is list:
        (element,) = get_args(inner)
        if isinstance(element, type) and issubclass(element, Parsed):
            return parse_all(element, region)
    elif isinstance(inner, type) and issubclass(inner, Parsed):
        return parse_one(inner, region)
    return region


def _from_match(model: type[Parsed], match: re.Match[str]) -> Parsed:
    """Build ``model`` from a match, recursing into nested ``Parsed`` fields.

    Only named groups (guaranteed a subset of the field names by the
    class-definition check) contribute to the data dict; fields without a
    matching group keep their defaults.
    """
    data = {
        name: _parse_region(model.model_fields[name].annotation, region)
        for name, region in match.groupdict().items()
    }
    return model(**data)


def parse_one(model: type[Parsed], text: str) -> Parsed:
    """Search ``text`` with ``model.pattern`` and build a single instance.

    Raises :class:`ParseMismatch` (naming the pattern) if nothing matches.
    """
    match = model.pattern.search(text)
    if match is None:
        raise ParseMismatch(f"{model.__name__}: no match for pattern {model.pattern.pattern!r}")
    return _from_match(model, match)


def parse_all(model: type[Parsed], text: str) -> list[Parsed]:
    """Return one ``model`` per ``finditer`` match; the empty list is valid."""
    return [_from_match(model, match) for match in model.pattern.finditer(text)]


def apply_parse(spec: Any, text: str) -> Any:
    """Apply a ``parse=`` spec to ``text`` and return the parsed value.

    ``spec`` is a :class:`Parsed` subclass (single :func:`parse_one`), a
    ``list[Sub]`` of one (:func:`parse_all`), or any other callable used as an
    escape hatch — its exceptions are wrapped in :class:`ParseMismatch`.
    """
    if isinstance(spec, type) and issubclass(spec, Parsed):
        return parse_one(spec, text)
    if get_origin(spec) is list:
        (element,) = get_args(spec)
        if isinstance(element, type) and issubclass(element, Parsed):
            return parse_all(element, text)
        raise TypeError(f"list parse spec element must be a Parsed subclass, got {element!r}")
    if callable(spec):
        try:
            return spec(text)
        except Exception as exc:
            raise ParseMismatch(str(exc)) from exc
    raise TypeError(f"unsupported parse spec: {spec!r}")
