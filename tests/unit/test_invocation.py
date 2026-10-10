"""otto.invocation: the run's policy, peer-host resolver and context bindings (spec 2 §3).

A leaf: it imports no otto module at runtime, so the host layer can read the
run's policy without importing otto.context.
"""

import ast
import contextvars

import pytest

from otto import invocation as inv
from tests._fixtures.paths import PROJECT_ROOT


def test_with_nothing_installed_readers_get_a_fresh_default_each_time():
    first, second = inv.current_policy(), inv.current_policy()
    assert first is not second
    assert first == inv.RunPolicy()
    assert (first.dry_run, first.log_command_output, first.output_dir, first.variant) == (
        False,
        True,
        None,
        "debug",
    )
    assert first.teardown_deadline == 10.0
    assert inv.installed_policy() is None
    assert inv.installed_resolver() is None


def test_an_installed_policy_is_what_readers_and_writers_get():
    policy = inv.RunPolicy(dry_run=True)
    binding = inv.install_policy(policy)
    try:
        assert inv.current_policy() is policy
        assert inv.installed_policy() is policy
    finally:
        inv.reset_binding(binding)
    assert inv.installed_policy() is None


def test_a_policy_refuses_an_unknown_variant():
    with pytest.raises(ValueError, match=r"variant must be 'debug' or 'field', got 'release'"):
        inv.RunPolicy(variant="release")  # type: ignore[arg-type]


def test_a_binding_resets_in_reverse_order_exactly_once():
    # Two tokens for the SAME variable: only a reverse-order reset lands on "outer";
    # a forward-order reset would leave "first" behind.
    var: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="outer")
    binding = inv.install_var(var, "first")
    inv.install_var(var, "second", into=binding)
    inv.install_policy(inv.RunPolicy(variant="field"), into=binding)
    assert (var.get(), inv.current_policy().variant) == ("second", "field")
    inv.reset_binding(binding)
    assert (var.get(), inv.installed_policy()) == ("outer", None)
    with pytest.raises(RuntimeError, match="already reset"):
        inv.reset_binding(binding)


def test_a_reset_binding_takes_no_further_installs():
    binding = inv.install_policy(inv.RunPolicy())
    inv.reset_binding(binding)
    with pytest.raises(RuntimeError, match="already reset"):
        inv.install_policy(inv.RunPolicy(variant="field"), into=binding)
    assert inv.installed_policy() is None  # nothing installed that no reset could undo


def test_a_binding_reset_in_another_execution_context_raises():
    policy = inv.RunPolicy()
    binding = inv.install_policy(policy)
    try:
        with pytest.raises(ValueError, match="different Context"):
            contextvars.copy_context().run(inv.reset_binding, binding)
        assert inv.installed_policy() is policy  # the failed foreign reset changed nothing here
    finally:
        inv.reset_binding(binding)  # still resettable where it was made
    assert inv.installed_policy() is None


def test_the_resolver_is_whatever_was_installed():
    class _Lab:
        def __init__(self) -> None:
            self.name = "rig"
            self.hosts: dict = {}

    lab = _Lab()
    binding = inv.install_resolver(lab)
    try:
        assert inv.installed_resolver() is lab
    finally:
        inv.reset_binding(binding)
    assert inv.installed_resolver() is None


def test_the_leaf_imports_no_otto_module_at_runtime():
    tree = ast.parse((PROJECT_ROOT / "src" / "otto" / "invocation.py").read_text())
    assert _runtime_otto_imports(tree.body) == []


def test_the_leaf_import_scan_sees_an_else_branch_of_type_checking():
    tree = ast.parse(
        "if TYPE_CHECKING:\n    from .host import Host\nelse:\n    from .errors import OttoError\n"
    )
    assert _runtime_otto_imports(tree.body) == ["from .errors import OttoError"]


def _runtime_otto_imports(body: list[ast.stmt]) -> list[str]:
    """The otto imports *body* runs at import time: a TYPE_CHECKING block's else branch included."""
    checked: list[ast.stmt] = []
    for top in body:
        if isinstance(top, ast.If) and _is_type_checking_block(top):
            checked.extend(top.orelse)
        else:
            checked.append(top)
    return [ast.unparse(node) for top in checked for node in ast.walk(top) if _imports_otto(node)]


def _imports_otto(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return bool(node.level) or (node.module or "").startswith("otto")
    if isinstance(node, ast.Import):
        return any(alias.name.startswith("otto") for alias in node.names)
    return False


def _is_type_checking_block(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) in {
        "TYPE_CHECKING",
        "typing.TYPE_CHECKING",
    }
