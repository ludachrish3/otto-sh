"""The CLI import-ban rule's lazy-name lists stay in step with the lazy tables.

``.ast-grep/rules/cli-command-no-module-scope-heavy-import.yml`` bans a short
list of heavy modules from the top of ``src/otto/cli/`` modules. A lazy package
re-exports names from those modules, so ``from ..config import get_lab`` loads
``otto.config.fleet`` as surely as ``from ..config.fleet import get_lab`` does,
and the rule enumerates those re-export names by hand. When a lazy table gains
a name that resolves into a denylisted module, the rule must gain it too, or
the new spelling slips past ``make lint-arch``. This test reads every lazy
package's table and the rule's own regexes, and fails naming the missing name.

It reads the regexes with Python's ``re``: the rule's patterns use only syntax
that means the same in ast-grep's Rust regex (groups, alternation, ``\\w``,
``\\s``, ``{2,}``).
"""

import importlib
import re

import pytest
import yaml

from tests._fixtures.paths import PROJECT_ROOT

RULE = PROJECT_ROOT / ".ast-grep" / "rules" / "cli-command-no-module-scope-heavy-import.yml"

# Every package whose __init__ carries a lazy table. test_lazy_packages.py's
# scan pins that this set is complete; a package missing here only narrows
# this check, it cannot make it pass wrongly.
LAZY_PACKAGES = [
    "otto",
    "otto._webassets",
    "otto.check",
    "otto.cli",
    "otto.config",
    "otto.coverage",
    "otto.coverage.fetcher",
    "otto.coverage.merge",
    "otto.coverage.store",
    "otto.creds",
    "otto.docker",
    "otto.env",
    "otto.host",
    "otto.host.survey",
    "otto.host.transfer",
    "otto.inventory",
    "otto.kmodcov",
    "otto.labs",
    "otto.link",
    "otto.logger",
    "otto.models",
    "otto.monitor",
    "otto.project",
    "otto.reservations",
    "otto.suite",
    "otto.testing",
    "otto.tunnel",
]


def _rule() -> dict:
    return yaml.safe_load(RULE.read_text())


def _denied_module_path() -> re.Pattern[str]:
    return re.compile(_rule()["utils"]["denied-module-path"]["regex"])


def _parent_package_arms() -> list[tuple[re.Pattern[str], re.Pattern[str]]]:
    """The (module_name regex, imported-name regex) pairs of the `from <pkg> import <name>` arms."""
    arms = []
    for arm in _rule()["rule"]["all"][0]["any"]:
        if arm.get("kind") != "import_from_statement" or "all" not in arm:
            continue
        module_re = next(h["has"]["regex"] for h in arm["all"] if h["has"].get("field"))
        name_re = next(h["has"]["regex"] for h in arm["all"] if not h["has"].get("field"))
        arms.append((re.compile(module_re), re.compile(name_re)))
    return arms


def _relative(module: str) -> str:
    """How ``src/otto/cli/`` spells *module*: ``otto.config.fleet`` as ``..config.fleet``."""
    if module != "otto" and not module.startswith("otto."):
        return module  # a third-party module is spelled absolutely
    return ".." + module.removeprefix("otto").removeprefix(".")


def _lazy_table(package: str) -> dict[str, tuple[str, str]]:
    """*package*'s lazy exports as name -> (module, attribute), empty if it has none."""
    try:
        module = importlib.import_module(package)
    except ImportError:
        return {}
    table: dict[str, tuple[str, str]] = {}
    for attr, entries in vars(module).items():
        if not (attr.startswith("_LAZY_") and isinstance(entries, dict)):
            continue
        for name, target in entries.items():
            table[name] = target if isinstance(target, tuple) else (target, name)
    return table


def _defining_module(module: str, attr: str) -> str:
    """Follow lazy re-exports to the module that loads when *attr* is taken from *module*.

    ``otto.get_lab`` is ``("otto.config", "get_lab")``, and otto.config's own
    table sends that on to ``otto.config.fleet``: the ban is about the module
    that finally loads, so a chain of lazy tables is followed to its end.
    """
    seen = set()
    while (module, attr) not in seen:
        seen.add((module, attr))
        onward = _lazy_table(module).get(attr)
        if onward is None:
            return module
        module, attr = onward
    return module


def _lazy_targets(package: str) -> dict[str, str]:
    """Every lazily exported name of *package*, mapped to the module it finally loads."""
    return {
        name: _defining_module(module, attr)
        for name, (module, attr) in _lazy_table(package).items()
    }


def _flagged(package: str, name: str) -> bool:
    """Whether the rule flags ``from <package, relative> import <name>``."""
    spelled = _relative(package)
    if _denied_module_path().match(spelled):
        return True  # the whole package is denylisted (otto.docker)
    return any(
        module_re.match(spelled) and name_re.match(name)
        for module_re, name_re in _parent_package_arms()
    )


def test_the_rule_reads_as_this_test_expects():
    """A reshaped rule must fail here, not turn every check below vacuous."""
    assert _denied_module_path().match("..config.fleet")
    assert _denied_module_path().match("...config.fleet")
    assert not _denied_module_path().match("..config.env")
    assert [m.pattern for m, _ in _parent_package_arms()] == [
        r"^(\.{2,}|otto)$",
        r"^rich$",
        r"^(\.{2,}|otto\.)config$",
        r"^(\.{2,}|otto\.)host$",
        r"^(\.{2,}|otto\.)coverage$",
    ]
    assert _flagged("otto.config", "get_lab")
    assert not _flagged("otto.config", "get_repos")


@pytest.mark.parametrize("package", LAZY_PACKAGES)
def test_every_lazy_name_that_loads_a_denylisted_module_is_banned_too(package):
    denied = _denied_module_path()
    missing = sorted(
        f"{name} (-> {target})"
        for name, target in _lazy_targets(package).items()
        if denied.match(_relative(target)) and not _flagged(package, name)
    )
    assert not missing, (
        f"{package}'s lazy table exports names that load a module "
        f"{RULE.relative_to(PROJECT_ROOT)} bans, but the rule does not ban the names: "
        f"{missing}. Add each to the imported-name regex of the rule's `{package}` arm."
    )
