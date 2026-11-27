"""No function that decides marking, attaching or completion coverage names a command.

Whether a command accepts registered options is its registration's answer
(``accepts_options``, else whether it uses the lab), never a list of command
names. A name typed into one of the functions that decide it would make one
command behave differently from another with the same declaration. The one
stated exception, ``otto.params.covering``'s ``host`` line, lives outside
``src/otto/cli`` and is not a decider here.

The scan reads the source, so it imports nothing it judges.
"""

import ast
import importlib.util
from pathlib import Path

from otto.cli.builtin_commands import builtin_command_names


def _source(module: str) -> Path:
    spec = importlib.util.find_spec(module)
    assert spec is not None, module
    assert spec.origin is not None, module
    return Path(spec.origin)


SRC = _source("otto").parent

DECIDERS = {
    "otto.cli.invoke": [
        "spec_accepts_options",
        "leaf_accepts_options",
        "pending_options",
        "settle_options",
        "attached_params",
        "offered_options",
        "completion_candidates",
        "_cached_candidates",
        "live_candidates",
        "covering_uses",
        "offer_gate",
        "kept_projects",
        "prepare_command_options",
    ],
    "otto.cli.registry": ["collect_option_candidates"],
    "otto._shim_complete": ["_params", "_options", "_positionals"],
}
"""Per module, the functions that decide whether a command marks, attaches or completes flags."""


def _functions(module: str) -> "dict[str, ast.FunctionDef]":
    tree = ast.parse(_source(module).read_text())
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_every_decider_exists():
    """A renamed or removed function cannot silently leave the guard."""
    missing = [
        f"{module}.{name}"
        for module, names in DECIDERS.items()
        for name in names
        if name not in _functions(module)
    ]
    assert missing == []


def test_no_function_that_decides_marking_attaching_or_completion_names_a_command():
    names = set(builtin_command_names())
    found = [
        f"{module}.{function.name} names {node.value!r} (line {node.lineno})"
        for module, wanted in DECIDERS.items()
        for function in (_functions(module)[name] for name in wanted)
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names
    ]
    assert found == []


def test_option_groups_and_option_verbs_are_gone():
    """The fixed lists of command names that marked a command are gone, and nothing reads them."""
    gone = ("OPTION_GROUPS", "OPTION_VERBS", "option_group")
    found = [
        f"{path.relative_to(SRC)}: {name}"
        for path in sorted(SRC.rglob("*.py"))
        for name in gone
        if name in _identifiers(path)
    ]
    assert found == []


def _identifiers(path: Path) -> "set[str]":
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.name.rpartition(".")[2])
    return names
