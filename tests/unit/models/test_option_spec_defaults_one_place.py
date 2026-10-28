"""No option spec restates a default: every one is read off the runtime dataclass.

``otto.host.option_specs`` validates the boundary; ``otto.host.options``
decides the defaults. A spec field whose default is a literal is a second
place for that decision, which is what this test refuses, field by field.
"""

import ast
import inspect

import pytest

from otto.host import option_specs, options


def _spec_classes() -> list[ast.ClassDef]:
    tree = ast.parse(inspect.getsource(option_specs))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.endswith("OptionsSpec")
    ]


def _literal_defaults(cls: ast.ClassDef) -> list[str]:
    """The spec's fields whose default is spelled as a literal."""
    return [
        item.target.id
        for item in cls.body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and isinstance(item.value, ast.Constant)
    ]


@pytest.mark.parametrize("spec", _spec_classes(), ids=lambda c: c.name)
def test_no_spec_field_restates_a_default(spec: ast.ClassDef):
    assert _literal_defaults(spec) == [], f"{spec.name} restates defaults that are the runtime's"


def test_field_default_reads_the_runtime_s_value_and_refuses_what_has_none():
    assert options.field_default(options.SshOptions, "port") == options.SshOptions().port
    with pytest.raises(TypeError, match="local_forwards"):
        options.field_default(options.SshOptions, "local_forwards")  # a default_factory
    with pytest.raises(TypeError, match="no such"):
        options.field_default(options.SshOptions, "no such")
