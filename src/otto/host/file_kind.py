"""
The built-in ``file`` kind — the zero-code declared product/dev tool.

One artifact, staged via :meth:`~otto.host.host.Host.put`, with optional
``install``/``uninstall``/``check`` command strings run on the host. One
class serves BOTH seams: :class:`~otto.host.product.Product` and
:class:`~otto.host.dev_tool.DevTool` deliberately share the same abstract
surface, so a single concrete type satisfies the two contracts — which seam
an instance lives in is decided by the registry that built it, never by the
type.

The defaults are the honest floor of today's Host surface: without an
``install`` command, install is a no-op success (staging placed the
artifact); without ``check``, ``is_installed`` answers False — otto assumes
not installed and re-stages, which is safe for the simple cases this kind
serves. ``host.exists()`` does not exist yet (see
:class:`~otto.host.product.FileProduct`); when the remote file-ops phase
lands, the ``check`` default upgrades to an artifact-existence test.
Anything richer than this is a repo-registered kind — the mechanism working
as intended, not a limitation.
"""

import string
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ..declared import DeclaredEntry
from ..result import Result
from ..utils import Status, anchor_path
from .dev_tool import DEV_TOOL_KINDS, DevTool
from .product import PRODUCT_KINDS, FileProduct, cov_dir_of_name

if TYPE_CHECKING:
    from .host import Host


@dataclass
class DeclaredFile(FileProduct, DevTool):
    """A ``kind = "file"`` entry's runtime form (both seams).

    ``stage`` is :class:`~otto.host.product.FileProduct`'s (``host.put``);
    the three remaining hooks run the declared command strings, or take the
    module docstring's honest defaults when a string is absent.
    """

    install_cmd: str | None = None
    uninstall_cmd: str | None = None
    check_cmd: str | None = None

    @override
    async def install(self, host: "Host") -> Result:
        if self.install_cmd is None:
            return Result(Status.Success)
        return await host.run(self.install_cmd)

    @override
    async def uninstall(self, host: "Host") -> Result:
        if self.uninstall_cmd is None:
            return Result(Status.Success)
        return await host.run(self.uninstall_cmd)

    @override
    async def is_installed(self, host: "Host") -> bool:
        if self.check_cmd is None:
            return False
        return (await host.run(self.check_cmd)).status is Status.Success


def str_param(
    entry: DeclaredEntry, params: dict[str, Any], key: str, *, required: bool = False
) -> str | None:
    """Pop *key* off *params* as a string, or ``None`` when absent.

    Shared by every built-in kind factory (see :mod:`otto.host.llext_kind`),
    so the rejection grammar — ``[[seam]] 'name': ...`` — is written once.
    """
    value = params.pop(key, None)
    if value is None:
        if required:
            raise ValueError(
                f"[[{entry.seam}]] {entry.name!r}: kind {entry.kind!r} requires an {key!r} param"
            )
        return None
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 — existing API contract; test suite expects ValueError
            f"[[{entry.seam}]] {entry.name!r}: {key!r} must be a string, got {value!r}"
        )
    return value


_COVERAGE_PARAMS = ("cov_dir", "debug_log_globs", "instrumented")
_PLACEHOLDERS = ("cov_dir", "name")
_VALID = "artifact, dest_dir, install, uninstall, check, cov_dir, debug_log_globs, instrumented"


_FORMATTER = string.Formatter()


def _placeholder_error(entry: DeclaredEntry, key: str, offending: str) -> ValueError:
    """Build the one named-placeholder-error message, for either failure site."""
    return ValueError(
        f"[[{entry.seam}]] {entry.name!r}: {key!r} has an unknown placeholder "
        f"({offending!r}); "
        f"valid: {', '.join('{' + p + '}' for p in _PLACEHOLDERS)} — write a literal "
        "brace as {{ or }}"
    )


def _substitute(
    entry: DeclaredEntry, key: str, command: str | None, values: dict[str, str]
) -> str | None:
    """Expand ``{cov_dir}``/``{name}`` in one command string, strictly.

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


def _file_kind(entry: DeclaredEntry, host: "Host") -> DeclaredFile:  # noqa: ARG001 — required by the KindRegistry factory signature Callable[[DeclaredEntry, Host], T]; this simple kind ignores host
    """Build a :class:`DeclaredFile` from a validated entry's params."""
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
    dest_dir = str_param(entry, params, "dest_dir")
    install = str_param(entry, params, "install")
    uninstall = str_param(entry, params, "uninstall")
    check = str_param(entry, params, "check")
    cov_dir = str_param(entry, params, "cov_dir")
    if cov_dir == "":
        raise ValueError(f"[[{entry.seam}]] {entry.name!r}: 'cov_dir' must not be empty")
    debug_log_globs = str_list_param(entry, params, "debug_log_globs")
    instrumented = bool_param(entry, params, "instrumented")
    if params:
        raise ValueError(
            f"[[{entry.seam}]] {entry.name!r}: kind 'file' got unknown param(s): "
            f"{sorted(params)}; valid: {_VALID}"
        )
    if entry.seam == "products":
        values = {"cov_dir": cov_dir or cov_dir_of_name(entry.name), "name": entry.name}
        install = _substitute(entry, "install", install, values)
        uninstall = _substitute(entry, "uninstall", uninstall, values)
        check = _substitute(entry, "check", check, values)
    return DeclaredFile(
        # Local path: forward slashes in TOML, anchored to the declaring repo
        # (never the CWD); dest_dir stays in the HOST's path domain — host.put
        # resolves it against the host's default_dest_dir (spec §4).
        artifact=anchor_path(Path(artifact), entry.base_dir),
        name=entry.name,
        dest_dir=Path(dest_dir) if dest_dir else Path(),
        cov_dir=cov_dir,
        debug_log_globs=debug_log_globs,
        install_cmd=install,
        uninstall_cmd=uninstall,
        check_cmd=check,
        instrumented_override=instrumented,
    )


PRODUCT_KINDS.register("file", _file_kind, origin=__name__)
DEV_TOOL_KINDS.register("file", _file_kind, origin=__name__)
