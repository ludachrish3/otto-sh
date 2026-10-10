"""Implementation modules that carry ``__all__`` keep it literal, unique and bound.

The public API manifest spec (``docs/superpowers/specs/2026-10-04-public-api-manifest-design.md``,
§6 "Guards") checks package facades in ``test_lazy_packages.py``. Every other
module that carries ``__all__`` is checked here, whether it is a declared single-file or tier-2
namespace or any other module that lists one. ``__all__`` must be one literal list of unique
strings, and every listed name must be bound when the module runs in a fresh
interpreter, so a name imported only under ``TYPE_CHECKING`` fails.
Completeness is not required: ``inspect`` or ``Path`` never need listing.
For each module that gets its first ``__all__``, ``from <module> import *`` in a
fresh interpreter binds exactly that list (the narrowing the cutover's footer
names).
"""

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.api_agreement import NamespaceReport, namespace_reports
from tests._fixtures.paths import PROJECT_ROOT

REPO_ROOT = PROJECT_ROOT
SRC = REPO_ROOT / "src"

# The manifest's first ``__all__`` for each declared module that had none
# (appendix F), except ``otto.bootstrap`` and the ``otto.lab`` package.
# Additions are free, so each list is a floor, not the whole ``__all__``.
FIRST_ALL = {
    "otto.cli.registry": ["CLI_COMMANDS", "CommandSpec", "cli_command", "register_cli_command"],
    "otto.context": [
        "OttoContext",
        "ProjectContextView",
        "Variant",
        "get_context",
        "open_context",
        "reset_context",
        "reset_variant",
        "set_context",
        "set_variant",
        "try_get_context",
        "variant",
    ],
    "otto.errors": ["EnsureStateError", "OttoError"],
    "otto.examples.app_shell": ["Listing", "PyRepl", "Row", "Version"],
    "otto.examples.lab_repository": ["ExampleLabRepository"],
    "otto.examples.login_proxy": ["enter_container"],
    "otto.examples.monitor": ["UptimeParser"],
    "otto.examples.options": ["DeviceTestOptions", "RepoOptions"],
    "otto.examples.reservations": [
        "ExampleReservationBackend",
        "ExampleReservationConfig",
        "example_reservations",
    ],
    "otto.examples.reservations_cli": ["check_report", "translate"],
    "otto.examples.session_setup": ["enter_python", "export_app_env"],
    "otto.host.app_shell": [
        "AppShell",
        "AppShellActiveError",
        "Parsed",
        "apply_parse",
        "parse_one",
    ],
    "otto.host.command_frame": [
        "FRAME_CLASSES",
        "BashFrame",
        "CommandFrame",
        "RawFrame",
        "SessionMarkers",
        "ZephyrFrame",
        "register_command_frame",
    ],
    "otto.host.dev_tool": ["DevTool", "register_dev_tool_kind", "register_dev_tool_provider"],
    "otto.host.embedded_filesystem": ["EmbeddedFileSystem", "register_filesystem"],
    "otto.host.login_proxy": [
        "LOGIN_PROXIES",
        "Cred",
        "ProxyContext",
        "ProxyIO",
        "register_login_proxy",
        "resolve_chain",
    ],
    "otto.host.options": [
        "FtpOptions",
        "LocalPortForward",
        "NcOptions",
        "ScpOptions",
        "SftpOptions",
        "SnmpOptions",
        "SshOptions",
        "TelnetOptions",
    ],
    "otto.host.product": [
        "DeclaredEntry",
        "Product",
        "ProductPlan",
        "ShellProduct",
        "planned_stage_dir",
        "put_line",
        "register_product_kind",
        "register_product_provider",
        "sudo_line",
        "unplanned",
    ],
    "otto.host.session_setup": [
        "SESSION_SETUPS",
        "SetupContext",
        "register_session_setup",
        "session_setup_from_spec",
    ],
    "otto.instructions": ["instruction", "run_instruction"],
    "otto.monitor.log_sourced": ["CsvMetricParser", "RegexLogEventParser"],
    "otto.monitor.parsers": [
        "DEFAULT_PARSERS",
        "LogEvent",
        "MetricDataPoint",
        "MetricParser",
        "ParseContext",
        "register_host_parsers",
        "register_parsers",
    ],
    "otto.monitor.snmp": ["SnmpMetric", "register_snmp_metric"],
    "otto.params": [
        "OPTIONS",
        "OptionsCollisionError",
        "OptionsNotAvailableError",
        "OptionsRegistrationError",
        "OptionsValidationError",
        "options",
        "options_key",
        "register_options",
        "verbs_for",
    ],
    "otto.registry": ["Ref", "RegistrationRefused", "Registry", "registering_repo"],
    "otto.result": [
        "CommandNotRunError",
        "CommandResult",
        "NotRunResult",
        "Result",
        "Results",
        "ShellResult",
    ],
    "otto.utils": ["Arg", "Exclude", "Opt", "Status", "cli_exposed"],
}

# Modules that carried ``__all__`` before the manifest's first lists.
EARLIER_CARRIERS = {
    "otto.host.docker_host",
    "otto.host.loop_owner",
    "otto.link.probes",
    "otto.monitor.events",
    "otto.snmp",
    "otto.tls",
}


def _all_statements(tree: ast.Module) -> list[ast.stmt]:
    """Every module-scope statement that assigns or augments ``__all__``."""
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            found.append(node)
    return found


def static_problems(source: str) -> list[str]:
    """What is wrong with *source*'s module-scope ``__all__``, read without running it."""
    tree = ast.parse(source)
    statements = _all_statements(tree)
    problems = []
    if len(statements) != 1:
        problems.append(f"{len(statements)} module-scope __all__ statements")
    mutated = sorted(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "__all__"
    )
    if mutated:
        problems.append(f"__all__ is changed after its literal: {mutated}")
    if not statements or isinstance(statements[0], ast.AugAssign):
        return problems
    value = statements[0].value
    if not isinstance(value, ast.List):
        problems.append(f"__all__ is a {type(value).__name__}, not a list literal")
        return problems
    if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
        problems.append("__all__ holds an entry that is not a string literal")
        return problems
    names = [e.value for e in value.elts if isinstance(e, ast.Constant)]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        problems.append(f"__all__ repeats {repeated}")
    return problems


def runtime_problems(report: NamespaceReport) -> list[str]:
    """What a fresh interpreter found: an import that failed, or a listed name left unbound."""
    if report.error:
        return [f"cannot import: {report.error}"]
    if report.inspect_error:
        return [f"reading its exports fails: {report.inspect_error}"]
    return [f"{name} is in __all__ but not bound at runtime" for name in report.missing]


def implementation_modules_with_all() -> dict[str, Path]:
    """Every non-package module under ``src/otto`` with a module-scope ``__all__`` statement."""
    found = {}
    for path in sorted((SRC / "otto").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if _all_statements(ast.parse(path.read_text(encoding="utf-8"))):
            found[".".join(path.relative_to(SRC).with_suffix("").parts)] = path
    return found


def test_the_scan_finds_every_first_all_module_and_the_earlier_carriers():
    """A scan that found nothing would pass every check below."""
    expected = set(FIRST_ALL) | EARLIER_CARRIERS
    assert expected <= set(implementation_modules_with_all()), sorted(
        expected - set(implementation_modules_with_all())
    )


@pytest.mark.parametrize("module", sorted(FIRST_ALL))
def test_each_first_all_lists_at_least_its_declared_names(module):
    listed = getattr(importlib.import_module(module), "__all__", [])
    assert set(FIRST_ALL[module]) <= set(listed), sorted(set(FIRST_ALL[module]) - set(listed))


_STAR = (
    "import importlib, json, sys\n"
    "bound = {}\n"
    "exec(f'from {sys.argv[1]} import *', bound)\n"
    "bound.pop('__builtins__', None)\n"
    "listed = getattr(importlib.import_module(sys.argv[1]), '__all__', None)\n"
    "listed = None if listed is None else sorted(listed)\n"
    "print(json.dumps({'bound': sorted(bound), 'all': listed}))\n"
)


def _star(module: str) -> dict:
    """What ``from <module> import *`` binds in a fresh interpreter, and its ``__all__``."""
    proc = subprocess.run(
        [sys.executable, "-c", _STAR, module],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", sorted(FIRST_ALL))
def test_star_import_binds_exactly_the_new_all(module):
    """A first ``__all__`` narrows ``from <module> import *`` to exactly its names.

    ``otto.bootstrap`` and ``otto.lab``, the other two appendix-F modules, get the
    same check in ``tests/unit/test_lab_facade.py``.
    """
    got = _star(module)
    assert got["all"] is not None, f"{module} has no __all__"
    assert got["bound"] == got["all"]


def test_star_import_of_otto_context_no_longer_binds_its_internals():
    """Before its first ``__all__``, ``from otto.context import *`` bound these too."""
    leaked = {"ContextVar", "LIBRARY_LAB_NAME"} & set(_star("otto.context")["bound"])
    assert leaked == set()


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ('__all__ = ("a",)\na = 1\n', "__all__ is a Tuple, not a list literal"),
        ('__all__ = list(["a"])\na = 1\n', "__all__ is a Call, not a list literal"),
        ('__all__ = ["a", "a"]\na = 1\n', "__all__ repeats ['a']"),
        ('__all__ = ["a", 1]\na = 1\n', "__all__ holds an entry that is not a string literal"),
        ('__all__ = ["a"]\n__all__ += ["b"]\na = b = 1\n', "2 module-scope __all__ statements"),
        (
            '__all__ = ["a"]\n__all__.extend(["b"])\na = b = 1\n',
            "__all__ is changed after its literal: ['__all__.extend']",
        ),
    ],
    ids=["tuple", "computed", "repeat", "non-string", "augmented", "extended"],
)
def test_static_problems_names_each_bad_shape(source, problem):
    assert problem in static_problems(source)


def test_a_literal_list_of_unique_strings_has_no_static_problem():
    assert static_problems('__all__ = ["a", "b"]\na = b = 1\n') == []


def test_a_name_bound_only_for_the_type_checker_is_a_runtime_problem(tmp_path):
    pkg = tmp_path / "src" / "otto"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    (pkg / "tconly.py").write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .impl import Thing\n"
        '__all__ = ["Thing"]\n',
        encoding="utf-8",
    )
    report = namespace_reports(["otto.tconly"], tmp_path)["otto.tconly"]
    assert runtime_problems(report) == ["Thing is in __all__ but not bound at runtime"]


def test_every_implementation_module_all_is_literal_unique_and_bound():
    modules = implementation_modules_with_all()
    problems = {m: static_problems(p.read_text(encoding="utf-8")) for m, p in modules.items()}
    for module, report in namespace_reports(sorted(modules), REPO_ROOT).items():
        problems[module] += runtime_problems(report)
    assert {m: p for m, p in problems.items() if p} == {}


def test_declared_entry_is_a_runtime_binding_of_the_product_namespace():
    """The manifest declares ``DeclaredEntry`` at ``otto.host.product``.

    A type-checker-only import binds nothing at runtime.
    """
    code = (
        "from otto.host.product import DeclaredEntry\n"
        "import otto.declared\n"
        "assert DeclaredEntry is otto.declared.DeclaredEntry\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
