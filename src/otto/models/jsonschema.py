"""Generate JSON Schema for the user-edited otto files from the boundary models.

The schemas are a *generated* product of the live models and the host registry,
never a committed artifact — so they cannot drift from the code. The CLI
(:mod:`otto.cli.schema`) writes them to disk; this module is pure (no I/O) so it
is trivially testable and importable.

Emitted documents (default):

- one self-contained file per *distinct* registered host spec
  (``unix-host``, ``embedded-host``, …),
- ``hosts`` — the array schema for the whole ``hosts.json`` file, assembled from
  the registry with ``anyOf`` + an ``os_type`` discriminator hint,
- ``settings`` — for ``settings.toml``,
- ``reservations`` — for the reservations JSON file.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic.json_schema import models_json_schema

from ..host.os_profile import registered_host_specs
from .host import HostSpec
from .settings import ReservationFile, SettingsModel

_SCHEMA_DIALECT = 'https://json-schema.org/draft/2020-12/schema'
_ID_BASE = 'https://otto-sh.readthedocs.io/schemas'


def _stem(spec_cls: type) -> str:
    """File stem for a host spec class: ``UnixHostSpec`` -> ``unix-host``.

    Handles runs of capitals too (``ACMEHostSpec`` -> ``acme-host``), so a
    contrib author's custom spec name still yields a clean stem.
    """
    name = re.sub(r'Spec$', '', spec_cls.__name__)          # UnixHost
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', name)  # ACMEHost -> ACME-Host
    name = re.sub(r'([a-z\d])([A-Z])', r'\1-\2', name)      # UnixHost -> Unix-Host
    return name.lower()                                     # unix-host


def _decorate(doc: dict[str, Any], stem: str, title: str) -> dict[str, Any]:
    """Add the dialect / id / title metadata to a generated schema doc.

    ``doc`` is spread first so the metadata wins — ``model_json_schema()``
    emits its own ``title`` (the class name), which we deliberately override
    with the friendly one.
    """
    return {
        **doc,
        '$schema': _SCHEMA_DIALECT,
        '$id': f'{_ID_BASE}/{stem}.schema.json',
        'title': title,
    }


def _host_array_schema(distinct: list[type[HostSpec]],
                       names: dict[str, type[HostSpec]]) -> dict[str, Any]:
    """Build the ``hosts.json`` array schema.

    Uses ``anyOf`` over the distinct specs with a shared ``$defs`` and an
    ``os_type`` discriminator mapping covering every registered name.
    """
    defs_map, top = models_json_schema(
        [(s, 'validation') for s in distinct],
        ref_template='#/$defs/{model}',
    )
    return {
        'type': 'array',
        'items': {
            'anyOf': [defs_map[(s, 'validation')] for s in distinct],
            'discriminator': {
                'propertyName': 'os_type',
                'mapping': {
                    name: defs_map[(spec, 'validation')]['$ref']
                    for name, spec in names.items()
                },
            },
        },
        '$defs': top['$defs'],
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
        docs[stem] = _decorate(spec.model_json_schema(), stem, f'otto {stem}')

    docs['hosts'] = _decorate(
        _host_array_schema(distinct, names), 'hosts', 'otto hosts.json'
    )
    docs['settings'] = _decorate(
        SettingsModel.model_json_schema(), 'settings', 'otto settings.toml'
    )
    docs['reservations'] = _decorate(
        ReservationFile.model_json_schema(), 'reservations', 'otto reservations'
    )
    return docs
