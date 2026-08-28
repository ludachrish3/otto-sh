"""VS Code snippets generated from the live boundary models (spec §12).

Written by ``otto init`` beside the schemas as ``.vscode/otto.code-snippets``
and refreshed with them, so a host entry starts with every REQUIRED field
present and spelled exactly as the spec spells it. Generated, never
hand-edited; hoisted keys (``element`` …) never appear in a host entry.

The schemas tell an editor what is WRONG once it is typed; the snippets say
what to type. Both come from the same models, so neither can drift from the
other or from what otto accepts.
"""

from typing import Any

from ..host.os_profile import registered_host_specs
from .host import HostSpec
from .jsonschema import _stem
from .lab import HOISTED_HOST_KEYS

# Optional fields worth pre-populating in a host entry, in this order.
# NOT `hop`: its value is another host's id, so a placeholder left unfilled is
# a dangling cross-reference rather than a value the author obviously must
# replace. A field that can only be filled by knowing the rest of the file
# belongs in the schema's autocomplete, not in the starting skeleton.
_COMMON_OPTIONAL = ("os_type", "valid_terms", "valid_transfers", "board", "metadata")


def _host_body(spec_cls: type[HostSpec], os_type: str) -> list[str]:
    """Render one host entry's snippet body: required fields first, then common options.

    Tab stops run over the fields a user must actually fill (``ip``, the
    credential pair, ``board`` …); fields otto can answer itself — the
    ``os_type`` this snippet is for, and the spec's own ``valid_terms`` /
    ``valid_transfers`` defaults — are written out literally, so the entry is
    correct before the first keystroke.
    """
    fields = spec_cls.model_fields
    required = [n for n, f in fields.items() if f.is_required() and n not in HOISTED_HOST_KEYS]
    optional = [n for n in _COMMON_OPTIONAL if n in fields and n not in required]
    names = required + optional
    lines = ["{"]
    stop = 1
    for i, name in enumerate(names):
        comma = "," if i < len(names) - 1 else ""
        field = fields[name]
        if name == "creds":
            value = f'[{{"login": "${{{stop}:admin}}", "password": "${{{stop + 1}:CHANGE_ME}}"}}]'
            stop += 2
        elif name == "os_type":
            value = f'"{os_type}"'
        elif name in ("valid_terms", "valid_transfers"):
            default = field.get_default(call_default_factory=True)
            value = "[" + ", ".join(f'"{v}"' for v in default) + "]"
        elif name == "metadata":
            value = "{}"
        else:
            value = f'"${{{stop}:{name}}}"'
            stop += 1
        lines.append(f'    "{name}": {value}{comma}')
    lines.append("}")
    return lines


def build_snippets(*, builtins_only: bool = False) -> dict[str, dict[str, Any]]:
    """Return a ``.code-snippets`` document: lab entry, element, cred, one host per family.

    With *builtins_only*, skip host specs registered by a repo's ``init``
    modules — the same knob ``otto.models.jsonschema.build_schemas``
    offers, so the two generated products describe the same set of types.
    """
    snippets: dict[str, dict[str, Any]] = {
        "otto lab entry": {
            "scope": "json",
            "prefix": "otto-lab",
            "description": "A labs-table entry: declared resources and metadata",
            "body": [
                '"${1:lab_name}": {',
                '    "resources": ["${2:resource-id}"],',
                '    "metadata": {}',
                "}",
            ],
        },
        "otto element": {
            "scope": "json",
            "prefix": "otto-element",
            "description": "An elements entry: identity, lab membership patterns, hosts",
            "body": [
                "{",
                '    "name": "${1:device}",',
                '    "labs": ["${2:lab_name}"],',
                '    "metadata": {},',
                '    "hosts": [',
                "        $0",
                "    ]",
                "}",
            ],
        },
        "otto cred": {
            "scope": "json",
            "prefix": "otto-cred",
            "description": "A creds entry",
            "body": ['{"login": "${1:admin}", "password": "${2:CHANGE_ME}"}'],
        },
    }
    for os_type, spec_cls in registered_host_specs(builtins_only=builtins_only).items():
        stem = _stem(spec_cls)  # "unix-host"
        key = f"otto {stem.replace('-', ' ')}"  # "otto unix host"; zephyr shares embedded's spec
        if key in snippets:
            continue
        # The description carries no article: "a"/"an" cannot be picked from a
        # spelling (it is "a unix-host" but "an embedded-host"), and a stem is
        # a contrib author's class name, so any rule would be wrong on someone
        # else's type sooner or later.
        snippets[key] = {
            "scope": "json",
            "prefix": f"otto-{stem}",
            "description": f"{stem} entry with every required field",
            "body": _host_body(spec_cls, os_type),
        }
    return snippets
