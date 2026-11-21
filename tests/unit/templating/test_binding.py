"""The Jsonnet binding shim: one fixed try-order, one "no binding" error, one import site."""

import ast
import json
import platform
import subprocess
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest

from otto.templating import binding
from otto.templating.errors import TemplatingError, TemplatingUnavailableError
from tests._fixtures.paths import CHECKOUT_ROOT

_BINDING_MODULES = frozenset({"_jsonnet", "_gojsonnet"})
_SHIM = "src/otto/templating/binding.py"


@pytest.fixture
def fresh_binding_cache() -> Iterator[None]:
    """Clear load_binding's memo around a test that changes which bindings import."""
    binding.load_binding.cache_clear()
    yield
    binding.load_binding.cache_clear()


def _fake_binding(name: str) -> ModuleType:
    module = ModuleType(name)
    module.evaluate_snippet = lambda *_args, **_kwargs: json.dumps({"from": name})
    return module


def test_load_binding_names_the_platform_and_python_when_none_is_installed(
    monkeypatch: pytest.MonkeyPatch, fresh_binding_cache: None
) -> None:
    monkeypatch.setitem(sys.modules, "_jsonnet", None)
    monkeypatch.setitem(sys.modules, "_gojsonnet", None)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "ppc64le")
    major, minor = sys.version_info.major, sys.version_info.minor

    with pytest.raises(TemplatingUnavailableError) as raised:
        binding.load_binding()

    assert isinstance(raised.value, TemplatingError)
    assert str(raised.value) == (
        f"Jsonnet templating is not available on Linux ppc64le (Python {major}.{minor}): "
        "otto installs no Jsonnet binding there. Install one yourself (pip install jsonnet "
        "builds it from source) or render on a supported platform."
    )


def test_the_cpp_binding_wins_when_both_import(
    monkeypatch: pytest.MonkeyPatch, fresh_binding_cache: None
) -> None:
    monkeypatch.setitem(sys.modules, "_jsonnet", _fake_binding("_jsonnet"))
    monkeypatch.setitem(sys.modules, "_gojsonnet", _fake_binding("_gojsonnet"))
    assert binding.load_binding().name == "jsonnet"


def test_the_go_binding_loads_when_the_cpp_one_is_absent(
    monkeypatch: pytest.MonkeyPatch, fresh_binding_cache: None
) -> None:
    monkeypatch.setitem(sys.modules, "_jsonnet", None)
    monkeypatch.setitem(sys.modules, "_gojsonnet", _fake_binding("_gojsonnet"))
    assert binding.load_binding().name == "gojsonnet"


def test_load_binding_is_memoized(
    monkeypatch: pytest.MonkeyPatch, fresh_binding_cache: None
) -> None:
    monkeypatch.setitem(sys.modules, "_jsonnet", _fake_binding("_jsonnet"))
    first = binding.load_binding()
    monkeypatch.setitem(sys.modules, "_jsonnet", None)
    assert binding.load_binding() is first


def test_the_installed_binding_evaluates_with_tlas_and_the_import_callback() -> None:
    """The contract both real bindings honour: TLA codes, the import callback, the limits.

    It evaluates with the binding this platform installed, so on a platform
    with none (Linux aarch64 on 3.15, or any architecture no marker selects)
    it fails with TemplatingUnavailableError. That is deliberate: the suite never skips
    for a missing binding, and the template tests are not expected to pass
    where templating is unavailable.
    """
    served: list[tuple[str, str]] = []

    def import_callback(directory: str, rel: str) -> tuple[str, bytes]:
        served.append((directory, rel))
        return f"{directory}{rel}", b'{ greeting: "hi" }'

    out = binding.load_binding().evaluate_snippet(
        "/lab/entry.jsonnet",
        'function(count) (import "lib.libsonnet") + { count: count }',
        tla_codes={"count": "3"},
        import_callback=import_callback,
        max_stack=500,
        max_trace=20,
    )

    assert json.loads(out) == {"greeting": "hi", "count": 3}
    assert served == [("/lab/", "lib.libsonnet")]


def _binding_imports(tree: ast.Module) -> "list[tuple[int, bool]]":
    """``(line, inside_a_function)`` for every import of a binding module in *tree*.

    It sees ``import X``, ``from X import ...`` and ``import_module("X")`` with
    a literal name. It does not see ``__import__("X")``, a computed name, or
    loading through ``importlib.util`` (``find_spec`` / ``module_from_spec``);
    a binding imported that way would pass this scan.
    """
    hits: list[tuple[int, bool]] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module.split(".")[0]]
        elif (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
                or (isinstance(node.func, ast.Name) and node.func.id == "import_module")
            )
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names = [node.args[0].value.split(".")[0]]
        if _BINDING_MODULES.intersection(names):
            hits.append((getattr(node, "lineno", 0), in_function))
        inner = in_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(tree, in_function=False)
    return hits


def test_only_the_shim_imports_a_binding() -> None:
    """``_jsonnet`` and ``_gojsonnet`` are imported in binding.py alone, inside a function."""
    sites: dict[str, list[tuple[int, bool]]] = {}
    for path in sorted((CHECKOUT_ROOT / "src" / "otto").rglob("*.py")):
        hits = _binding_imports(ast.parse(path.read_text(), filename=str(path)))
        if hits:
            sites[path.relative_to(CHECKOUT_ROOT).as_posix()] = hits

    assert set(sites) == {_SHIM}, f"a Jsonnet binding is imported outside {_SHIM}: {sites}"
    assert len(sites[_SHIM]) == len(_BINDING_MODULES)
    assert all(in_function for _, in_function in sites[_SHIM]), (
        f"{_SHIM} imports a binding at module scope: {sites[_SHIM]}"
    )


def test_importing_the_package_loads_no_binding() -> None:
    """The facade and the error classes are import-light: no binding, no shim."""
    probe = (
        "import sys\n"
        "import otto.templating\n"
        "from otto.templating import TemplatingError, RenderError\n"
        "watched = {'_jsonnet', '_gojsonnet', 'otto.templating.binding'}\n"
        "loaded = sorted(watched & set(sys.modules))\n"
        "print(loaded)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], check=True, capture_output=True, text=True
    ).stdout
    assert out.strip() == "[]"
