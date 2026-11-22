"""Settings TOML for test projects: inline values, tables, and ``[[lab.sources]]`` tables.

One spelling of each, so a path is quoted the same way wherever a test writes
it into a project's ``.otto/settings.toml``.
"""

import json
from pathlib import Path
from typing import Any


def toml_value(value: Any) -> str:
    """*value* as an inline TOML value: tables inline, strings as basic strings."""
    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def table(header: str, **keys: Any) -> str:
    """A TOML table (``[x]`` or ``[[x]]`` *header*) holding *keys* as inline values."""
    return header + "\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in keys.items()) + "\n"


def jsonnet_source(**keys: Any) -> str:
    """Return a ``backend = "jsonnet"`` ``[[lab.sources]]`` table holding *keys*."""
    return table("[[lab.sources]]", backend="jsonnet", **keys)


def json_source(*paths: Path) -> str:
    """Return a ``backend = "json"`` ``[[lab.sources]]`` table reading *paths*."""
    return table("[[lab.sources]]", backend="json", paths=[str(p) for p in paths])
