"""Generate JSON Schema for the user-edited otto files from the boundary models.

The schemas are a *generated* product of the live models and the host registry,
never a committed artifact — so they cannot drift from the code. The CLI
(:mod:`otto.cli.schema`) writes them to disk; this module is pure (no I/O) so it
is trivially testable and importable.

Emitted documents (default):

- one self-contained file per *distinct* registered host spec
  (``unix-host``, ``embedded-host``, …),
- ``lab`` — the object schema for the whole ``lab.json`` file: a ``hosts``
  array (assembled from the registry with ``anyOf`` + an ``os_type``
  discriminator hint) and a ``links`` array, plus the ``^_`` comment-key
  escape,
- ``link`` — the schema for one ``lab.json`` ``links`` entry
  (:class:`~otto.models.link.LinkSpec`),
- ``settings`` — for ``settings.toml``,
- ``reservations`` — for the reservations JSON file,
- ``monitor-meta`` — the monitor dashboard's ``/api/meta`` payload
  (:class:`~otto.models.monitor.MonitorMeta`); not user-edited, it feeds the
  web dashboard's generated TS types (``scripts/gen_web_types.sh``).
"""

import re
from typing import Any

from pydantic.json_schema import models_json_schema

from ..host.connections import TERM_BACKENDS
from ..host.os_profile import registered_host_specs
from ..host.transfer import TRANSFER_BACKENDS
from .host import HostSpec
from .link import LinkSpec
from .monitor import MonitorMeta
from .settings import ReservationFile, SettingsModel

_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_ID_BASE = "https://otto-sh.readthedocs.io/schemas"


def _stem(spec_cls: type) -> str:
    """File stem for a host spec class: ``UnixHostSpec`` -> ``unix-host``.

    Handles runs of capitals too (``ACMEHostSpec`` -> ``acme-host``), so a
    contrib author's custom spec name still yields a clean stem.
    """
    name = re.sub(r"Spec$", "", spec_cls.__name__)  # UnixHost
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)  # ACMEHost -> ACME-Host
    name = re.sub(r"([a-z\d])([A-Z])", r"\1-\2", name)  # UnixHost -> Unix-Host
    return name.lower()  # unix-host


def _decorate(doc: dict[str, Any], stem: str, title: str) -> dict[str, Any]:
    """Add the dialect / id / title metadata to a generated schema doc.

    ``doc`` is spread first so the metadata wins — ``model_json_schema()``
    emits its own ``title`` (the class name), which we deliberately override
    with the friendly one.
    """
    return {
        **doc,
        "$schema": _SCHEMA_DIALECT,
        "$id": f"{_ID_BASE}/{stem}.schema.json",
        "title": title,
    }


def _scalar_or_list_with_enum(prop: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """Rebuild a menu property's schema to accept a scalar **or** an array of registry enum names.

    The model coerces a scalar to a one-element list (``_coerce_menu``), so a lab
    author may write ``valid_transfers = "scp"`` or ``["scp", "sftp"]``; the
    generated schema mirrors both, each branch carrying the same ``enum``.
    """
    meta = {k: v for k, v in prop.items() if k not in ("type", "items", "anyOf")}
    return {
        **meta,
        "anyOf": [
            {"type": "string", "enum": names},
            {"type": "array", "items": {"type": "string", "enum": names}},
        ],
    }


def _inject_selector_enums(schema: dict[str, Any], spec_cls: type[HostSpec]) -> None:
    """Rewrite ``valid_terms`` / ``valid_transfers`` to a scalar-or-list ``anyOf`` schema, in place.

    The schema is generated after init modules load, so the enum includes
    custom per-repo backends as well as the built-ins — strictly better than the
    old static ``Literal``. Both axes are filtered to the spec's host family via
    ``_host_family`` (terms through ``TERM_BACKENDS``, transfers through each
    backend's ``host_families``). No-op for a spec that declares neither field.
    The scalar ``term``/``transfer`` pins are nullable optional strings; their
    schema is left as pydantic generates it.
    """
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    family = getattr(spec_cls, "_host_family", None)
    if "valid_terms" in props:
        names = sorted(
            n
            for n, backend in TERM_BACKENDS.items()
            if family is None or family in backend.host_families
        )
        props["valid_terms"] = _scalar_or_list_with_enum(props["valid_terms"], names)
    if "valid_transfers" in props:
        names = sorted(
            n for n, c in TRANSFER_BACKENDS.items() if family is None or family in c.host_families
        )
        props["valid_transfers"] = _scalar_or_list_with_enum(props["valid_transfers"], names)


def _inject_interface_shorthand(schema: dict[str, Any]) -> None:
    """Rewrite ``interfaces`` to accept a bare IP string per entry, in place.

    ``HostSpec._coerce_interface_shorthand`` is a ``mode="before"`` validator
    (so it's invisible to pydantic's schema generation) that accepts
    ``{"eth0": "10.0.0.5"}`` as shorthand for ``{"eth0": {"ip": "10.0.0.5"}}``.
    Mirrors ``_inject_selector_enums``'s scalar-or-... rewrite pattern: each
    value in the ``interfaces`` map may be a bare string or an
    :class:`~otto.models.host.InterfaceSpec` object. No-op for a spec without
    an ``interfaces`` property.
    """
    props = schema.get("properties")
    if not isinstance(props, dict) or "interfaces" not in props:
        return
    ref = props["interfaces"].get("additionalProperties")
    if not isinstance(ref, dict):
        return
    props["interfaces"]["additionalProperties"] = {"anyOf": [{"type": "string"}, ref]}


def _hosts_array_schema(
    distinct: list[type[HostSpec]], names: dict[str, type[HostSpec]]
) -> dict[str, Any]:
    """Build the ``lab.json`` ``hosts`` array schema.

    Uses ``anyOf`` over the distinct specs with a shared ``$defs`` and an
    ``os_type`` discriminator mapping covering every registered name.
    """
    defs_map, top = models_json_schema(
        [(s, "validation") for s in distinct],
        ref_template="#/$defs/{model}",
    )
    for s in distinct:
        key = defs_map[(s, "validation")]["$ref"].rsplit("/", 1)[-1]
        if key in top["$defs"]:
            _inject_selector_enums(top["$defs"][key], s)
            _inject_interface_shorthand(top["$defs"][key])
    return {
        "type": "array",
        "items": {
            "anyOf": [defs_map[(s, "validation")] for s in distinct],
            "discriminator": {
                "propertyName": "os_type",
                "mapping": {
                    name: defs_map[(spec, "validation")]["$ref"] for name, spec in names.items()
                },
            },
        },
        "$defs": top["$defs"],
    }


def _lab_schema(hosts_array: dict[str, Any]) -> dict[str, Any]:
    """Build the ``lab.json`` object schema: ``hosts``/``links`` sections + ``_`` comments."""
    link_doc = LinkSpec.model_json_schema(ref_template="#/$defs/{model}")
    defs = {**hosts_array.pop("$defs", {}), **link_doc.pop("$defs", {})}
    return {
        "type": "object",
        "properties": {
            "hosts": hosts_array,
            "links": {"type": "array", "items": link_doc},
        },
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
        "$defs": defs,
    }


def build_schemas(*, builtins_only: bool = False) -> dict[str, dict[str, Any]]:
    """Return ``{stem: schema_document}`` for every generated schema.

    Reads whatever host classes are currently registered, so custom specs
    loaded via init modules are included automatically. With *builtins_only*,
    restrict the host schemas to the in-tree built-in types.
    """
    names = registered_host_specs(builtins_only=builtins_only)
    distinct: list[type[HostSpec]] = list(dict.fromkeys(names.values()))

    docs: dict[str, dict[str, Any]] = {}
    for spec in distinct:
        stem = _stem(spec)
        doc = spec.model_json_schema()
        _inject_selector_enums(doc, spec)
        _inject_interface_shorthand(doc)
        docs[stem] = _decorate(doc, stem, f"otto {stem}")

    docs["lab"] = _decorate(
        _lab_schema(_hosts_array_schema(distinct, names)), "lab", "otto lab.json"
    )
    docs["link"] = _decorate(LinkSpec.model_json_schema(), "link", "otto link")
    docs["settings"] = _decorate(
        SettingsModel.model_json_schema(), "settings", "otto settings.toml"
    )
    docs["reservations"] = _decorate(
        ReservationFile.model_json_schema(), "reservations", "otto reservations"
    )
    docs["monitor-meta"] = _decorate(
        MonitorMeta.model_json_schema(),
        "monitor-meta",
        "Monitor dashboard /api/meta payload",
    )
    return docs
