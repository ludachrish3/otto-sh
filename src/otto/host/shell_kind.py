"""
The built-in ``shell`` kind — the zero-code declared product/dev tool.

One artifact, staged via :meth:`~otto.host.host.Host.put`, with optional
``install``/``uninstall``/``check`` command strings run on the host. The
runtime form is :class:`~otto.host.declared_product.DeclaredProduct`; this
module maps an entry's params onto it — and, through :func:`build_declared`,
onto any ``class =`` subclass — so the two routes cannot drift.

Anything richer than this is a repo-registered kind — the mechanism working
as intended, not a limitation.
"""

import dataclasses
import importlib
import inspect
import string
import types
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..declared import DeclaredEntry
from ..utils import anchor_path
from .declared_product import DeclaredProduct
from .product import cov_dir_of_name, validate_stage_dir

if TYPE_CHECKING:
    from .host import Host


def _who(entry: DeclaredEntry) -> str:
    """``kind 'shell'`` or ``class 'pkg.mod:Sub'`` — what the entry wrote."""
    return f"kind {entry.kind!r}" if entry.kind is not None else f"class {entry.cls!r}"


def str_param(
    entry: DeclaredEntry, params: dict[str, Any], key: str, *, required: bool = False
) -> str | None:
    """Pop *key* off *params* as a string, or ``None`` when absent.

    Shared by every built-in kind factory (see :mod:`otto.host.embedded_kind`),
    so the rejection grammar — ``[[seam]] 'name': ...`` — is written once.
    """
    value = params.pop(key, None)
    if value is None:
        if required:
            raise ValueError(
                f"[[{entry.seam}]] {entry.name!r}: {_who(entry)} requires an {key!r} param"
            )
        return None
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
            f"[[{entry.seam}]] {entry.name!r}: {key!r} must be a string, got {value!r}"
        )
    return value


_COVERAGE_PARAMS = ("cov_dir", "debug_log_globs", "instrumented")
_PLACEHOLDERS = ("cov_dir", "name")
_VALID = "artifact, stage_dir, install, uninstall, check, cov_dir, debug_log_globs, instrumented"

RETIRED_PARAMS = {"dest_dir": "stage_dir"}
"""Param keys a built-in kind refuses BY NAME, mapped to what replaced them.

A retired key that fell through to the generic unknown-param message would
stop the load but never say what to write instead -- and the rename moved
where the artifact lands, so a lab that keeps the old spelling must be told,
not merely refused."""


def reject_retired_params(entry: DeclaredEntry, params: dict[str, Any]) -> None:
    """Refuse a :data:`RETIRED_PARAMS` key by name, pointing at what replaced it.

    Called by every built-in kind — including the ones that have no
    ``stage_dir`` of their own — so the rename gets one message everywhere
    rather than a generic unknown-param list on some kinds.
    """
    for old, new in RETIRED_PARAMS.items():
        if old in params:
            raise ValueError(
                f"[[{entry.seam}]] {entry.name!r}: {old!r} was renamed to {new!r} -- "
                f"rename the key; the artifact lands at <{new}>/<artifact basename> on every kind"
            )


def stage_dir_param(entry: DeclaredEntry, params: dict[str, Any]) -> Path:
    """Pop ``stage_dir`` off *params*, refusing the retired ``dest_dir`` spelling.

    Shared by every built-in kind that stages an artifact, so the rename is
    refused identically in both seams and no kind can quietly keep honoring
    the old key.
    """
    reject_retired_params(entry, params)
    value = str_param(entry, params, "stage_dir")
    return validate_stage_dir(Path(value) if value else Path(), f"[[{entry.seam}]] {entry.name!r}")


_FORMATTER = string.Formatter()


def _placeholder_error(entry: DeclaredEntry, key: str, offending: str) -> ValueError:
    """Build the one named-placeholder-error message, for either failure site."""
    return ValueError(
        f"[[{entry.seam}]] {entry.name!r}: {key!r} has an unknown placeholder "
        f"({offending!r}); "
        f"valid: {', '.join('{' + p + '}' for p in _PLACEHOLDERS)} — write a literal "
        "brace as {{ or }}"
    )


def substitute_placeholders(
    entry: DeclaredEntry, key: str, command: str | None, values: dict[str, str]
) -> str | None:
    """Expand ``{cov_dir}``/``{name}`` in one command string, strictly.

    Shared by every built-in kind that takes command text (see
    :mod:`otto.host.kmod_kind`).

    ``str.format_map`` alone is not strict enough: it only validates the
    ROOT field name against the mapping, so a conversion (``{cov_dir!r}``),
    a format spec (``{cov_dir:>12}``), an attribute/index access
    (``{name.upper}``, ``{cov_dir[1]}``), or a positional/empty field
    (``{}``, ``{0}``) all pass ``format_map`` silently — quietly rewriting
    the command that runs on the host. So this walks the parsed fields
    (:class:`string.Formatter`) FIRST and rejects anything but a bare
    ``{cov_dir}``/``{name}``; only a template that passes the walk is handed
    to ``format_map``, which then cannot fail.

    ``Formatter.parse`` is itself a lazy generator that raises a bare,
    unnamed ``ValueError`` (e.g. "Single '{' encountered in format string")
    on an unbalanced brace — materializing it into a list up front, inside
    its own ``try``, catches that case too and gives it the same named
    error grammar as every other rejection here.
    """
    if command is None:
        return None
    try:
        fields = list(_FORMATTER.parse(command))
    except ValueError as e:
        raise _placeholder_error(entry, key, str(e)) from e
    for _literal, field_name, format_spec, conversion in fields:
        if field_name is None:
            continue  # a chunk of literal text with no field in it
        if field_name in _PLACEHOLDERS and conversion is None and not format_spec:
            continue
        offending = "{" + field_name
        if conversion is not None:
            offending += f"!{conversion}"
        if format_spec:
            offending += f":{format_spec}"
        offending += "}"
        raise _placeholder_error(entry, key, offending)
    return command.format_map(values)


def bool_param(entry: DeclaredEntry, params: dict[str, Any], key: str) -> bool | None:
    """Pop *key* off *params* as a bool, or ``None`` when absent (see :func:`str_param`)."""
    value = params.pop(key, None)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(  # noqa: TRY004 — matches str_param's ValueError contract
            f"[[{entry.seam}]] {entry.name!r}: {key!r} must be a bool, got {value!r}"
        )
    return value


def str_list_param(entry: DeclaredEntry, params: dict[str, Any], key: str) -> list[str]:
    """Pop *key* off *params* as a list of strings, ``[]`` when absent (see :func:`str_param`)."""
    value = params.pop(key, None)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(
            f"[[{entry.seam}]] {entry.name!r}: {key!r} must be a list of strings, got {value!r}"
        )
    return list(value)


_BASE_FIELDS = frozenset(f.name for f in dataclasses.fields(DeclaredProduct))
"""The fields every DeclaredProduct has: mapped by the shell keys, never by name."""


def _own_hints(cls: type[DeclaredProduct]) -> dict[str, Any]:
    """Resolve the annotations *cls* and its subclass bases declare, never the base's.

    :func:`typing.get_type_hints` on a subclass also evaluates every base's
    annotations, and the product base names types it only imports under
    ``TYPE_CHECKING`` — so each class between *cls* and
    :class:`~otto.host.declared_product.DeclaredProduct` is resolved on its
    own, in its own module's namespace (a user class may use string annotations).
    Only names that are dataclass fields are resolved: a ``ClassVar``/``Final``
    declaration is never a field, and :func:`typing.get_type_hints` refuses it
    once it is a string.
    """
    hints: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        if klass is DeclaredProduct or not issubclass(klass, DeclaredProduct):
            continue
        # The class came in through an import, so its module is loaded by
        # construction: import_module is the cached lookup, never a question.
        module = importlib.import_module(klass.__module__)
        field_names = {f.name for f in dataclasses.fields(klass)}
        own = {n: a for n, a in inspect.get_annotations(klass).items() if n in field_names}
        hints.update(
            typing.get_type_hints(
                types.SimpleNamespace(__annotations__=own),
                globalns=vars(module),
                localns=dict(vars(klass)),
            )
        )
    return hints


def _subclass_fields(
    entry: DeclaredEntry, cls: type[DeclaredProduct], params: dict[str, Any]
) -> dict[str, Any]:
    """Pop the keys that are *cls*'s own dataclass fields off *params*, checked by annotation.

    ``str``/``int``/``bool``/``Path``/``list[str]`` annotations are checked
    (``Path`` is built from a string, in whichever domain the subclass means);
    any other annotation passes the TOML value through verbatim — the
    subclass knows what it asked for. A field without a default is required.
    """
    who = f"[[{entry.seam}]] {entry.name!r}: class {entry.cls!r}"
    try:
        hints = _own_hints(cls)
    except Exception as e:  # a bad annotation on a user class is a settings error, named
        raise ValueError(f"{who}: its annotations cannot be resolved ({e})") from e
    out: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in _BASE_FIELDS or not f.init:
            continue
        if f.name not in params:
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise ValueError(f"{who} requires a {f.name!r} key")
            continue
        value = params.pop(f.name)
        ann = hints.get(f.name, f.type)
        if ann is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{who}: {f.name!r} must be a bool, got {value!r}")
        elif ann is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{who}: {f.name!r} must be an integer, got {value!r}")
        elif ann is str:
            if not isinstance(value, str):
                raise ValueError(f"{who}: {f.name!r} must be a string, got {value!r}")
        elif ann is Path:
            if not isinstance(value, str):
                raise ValueError(f"{who}: {f.name!r} must be a path string, got {value!r}")
            value = Path(value)
        elif ann == list[str] and (
            not isinstance(value, list) or not all(isinstance(v, str) for v in value)
        ):
            raise ValueError(f"{who}: {f.name!r} must be a list of strings, got {value!r}")
        out[f.name] = value
    return out


def _own_field_names(cls: type[DeclaredProduct]) -> list[str]:
    return [f.name for f in dataclasses.fields(cls) if f.name not in _BASE_FIELDS and f.init]


def build_declared(
    entry: DeclaredEntry,
    host: "Host",  # noqa: ARG001 — the KindFactory signature; this kind ignores host
    cls: type[DeclaredProduct],
) -> DeclaredProduct:
    """Build *cls* from a validated entry's params.

    *cls* is :class:`~otto.host.declared_product.DeclaredProduct` or a subclass.
    The ``shell`` kind and a ``class =`` entry share this one mapping, so a
    key means the same thing on both routes.
    """
    params = dict(entry.params)
    if entry.seam == "dev_tools":
        for key in _COVERAGE_PARAMS:
            if key in params:
                raise ValueError(
                    f"[[dev_tools]] {entry.name!r}: {key!r} is a product-only param "
                    "(dev tools have no coverage)"
                )
    artifact = str_param(entry, params, "artifact", required=True)
    assert artifact is not None  # noqa: S101 — internal invariant: required=True makes str_param raise above when missing
    stage_dir = stage_dir_param(entry, params)
    install = str_param(entry, params, "install")
    uninstall = str_param(entry, params, "uninstall")
    check = str_param(entry, params, "check")
    cov_dir = str_param(entry, params, "cov_dir")
    if cov_dir == "":
        raise ValueError(f"[[{entry.seam}]] {entry.name!r}: 'cov_dir' must not be empty")
    debug_log_globs = str_list_param(entry, params, "debug_log_globs")
    instrumented = bool_param(entry, params, "instrumented")
    extra = _subclass_fields(entry, cls, params)
    if params:
        valid = ", ".join([_VALID, *_own_field_names(cls)])
        raise ValueError(
            f"[[{entry.seam}]] {entry.name!r}: {_who(entry)} got unknown param(s): "
            f"{sorted(params)}; valid: {valid}"
        )
    if entry.seam == "products":
        values = {"cov_dir": cov_dir or cov_dir_of_name(entry.name), "name": entry.name}
        install = substitute_placeholders(entry, "install", install, values)
        uninstall = substitute_placeholders(entry, "uninstall", uninstall, values)
        check = substitute_placeholders(entry, "check", check, values)
    return cls(
        # Local path: forward slashes in TOML, anchored to the declaring repo
        # (never the CWD); stage_dir stays in the HOST's path domain — host.put
        # resolves it against the host's default_dest_dir (spec §4).
        artifact=anchor_path(Path(artifact), entry.base_dir),
        name=entry.name,
        stage_dir=stage_dir,
        cov_dir=cov_dir,
        debug_log_globs=debug_log_globs,
        install_cmd=install,
        uninstall_cmd=uninstall,
        check_cmd=check,
        instrumented_override=instrumented,
        **extra,
    )


def _shell_kind(entry: DeclaredEntry, host: "Host") -> DeclaredProduct:
    """Build the ``shell`` kind: :func:`build_declared` with the base class."""
    return build_declared(entry, host, DeclaredProduct)


def resolve_class(entry: DeclaredEntry) -> type[DeclaredProduct]:
    """Import a ``class = "pkg.mod:Sub"`` entry's class and check it is a DeclaredProduct subclass.

    The registry runs this before any match, so a typo'd path fails every
    ingest. It resolves through :class:`~otto.registry.Ref`, so the import
    runs outside the test-file loading phase like every kind's.
    """
    from ..registry import Ref

    if entry.cls is None:  # pragma: no cover — the registry routes only class entries here
        raise ValueError(f"[[{entry.seam}]] {entry.name!r}: no class to build")
    try:
        obj = Ref(entry.cls).resolve()
    except (ImportError, AttributeError, ValueError) as e:
        raise ValueError(f"[[{entry.seam}]] {entry.name!r}: class {entry.cls!r} — {e}") from e
    if not (isinstance(obj, type) and issubclass(obj, DeclaredProduct)):
        raise ValueError(  # noqa: TRY004 — a settings error, ValueError like every entry refusal
            f"[[{entry.seam}]] {entry.name!r}: class {entry.cls!r} is not a DeclaredProduct "
            f"subclass (got {obj!r})"
        )
    return obj


def class_entry(entry: DeclaredEntry, host: "Host") -> DeclaredProduct:
    """Build a ``class = "pkg.mod:Sub"`` entry: resolve its class, then :func:`build_declared`."""
    return build_declared(entry, host, resolve_class(entry))
