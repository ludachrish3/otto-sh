"""Structural validation runs before preferences merge their option tables, so no structural
validator may read one: an option-dependent invariant belongs in contextual validation."""

import ast
import inspect

import otto.host.specs as host_models
from otto.host import Element
from otto.host.factory import create_host_from_dict


def _option_tables() -> set[str]:
    """Every ``*_options`` field any spec in :mod:`otto.host.specs` declares."""
    return {
        name
        for obj in vars(host_models).values()
        if isinstance(obj, type) and issubclass(obj, host_models.HostSpec)
        for name in obj.model_fields
        if name.endswith("_options")
    }


def _validator_functions():
    """Every validator, and every module-level function a validator calls by bare name."""
    tree = ast.parse(inspect.getsource(host_models))
    helpers = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
            isinstance(d, ast.Call)
            and getattr(d.func, "id", "") in {"model_validator", "field_validator"}
            for d in node.decorator_list
        ):
            yield node
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    helper = helpers.get(call.func.id)
                    if helper is not None:
                        yield helper


def test_the_vocabulary_holds_every_option_table():
    assert {"console_options", "telnet_options", "ssh_options"} <= _option_tables()


def test_no_structural_validator_reads_an_options_table():
    tables = _option_tables()
    offenders = []
    for fn in _validator_functions():
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Constant):
                name = node.value
            else:
                continue
            if name in tables:
                offenders.append(f"{fn.name} reads {name}")
    assert not offenders


def test_an_option_table_only_a_preference_sets_reaches_the_host():
    host = create_host_from_dict(
        {"ip": "10.0.0.1"}, {".*": {"telnet_options": {"port": 2323}}}, element=Element("e")
    )
    assert host.telnet_options.port == 2323
