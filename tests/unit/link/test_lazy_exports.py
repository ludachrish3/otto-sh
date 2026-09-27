"""otto.link exports every name lazily (PEP 562), not eagerly.

Only ``otto link`` calls impair_link/repair_link/check_link; every other
otto.link importer (every command that validates a host spec, via
otto.models.host -> IMPAIRERS) must not pay for otto.host.daemon,
otto.link.sentinel or otto.check. tests/unit/test_lazy_packages.py holds the
checks every lazy package shares and tests/unit/import_budget/ the
surface-level guard; this file proves otto.link's own resolution paths.
"""

import json
import subprocess
import sys

import pytest

from otto.link import check, impairer, manage, netem


def test_manage_name_resolves_to_manage_module_object():
    from otto.link import impair_link

    assert impair_link is manage.impair_link


def test_check_names_resolve_to_check_module_object():
    """``check_link``/``LinkCheckReport`` resolve against ``.check``, a
    different module from ``.manage``'s names above, so they are proven
    separately."""
    from otto.link import check_link as lazy_check_link

    assert lazy_check_link is check.check_link

    import otto.link as link_mod

    assert link_mod.LinkCheckReport is check.LinkCheckReport


def test_manage_names_all_resolve():
    import otto.link as link_mod

    for name in (
        "AppliedPlacement",
        "DirectionState",
        "DryRunPlan",
        "ImpairReport",
        "LinkCommandFailedError",
        "LinkHostUnreachableError",
        "LinkNotMeasuredError",
        "LinkState",
        "RepairAllReport",
        "RepairReport",
        "find_link",
        "impair_link",
        "read_link_states",
        "repair_all",
        "repair_link",
    ):
        assert getattr(link_mod, name) is getattr(manage, name)


def test_the_registry_and_the_builtin_impairer_resolve():
    import otto.link as link_mod

    assert link_mod.IMPAIRERS is impairer.IMPAIRERS
    assert link_mod.NetEmImpairer is netem.NetEmImpairer
    assert link_mod.build_impairer("netem") is netem.NetEmImpairer


def test_unknown_attribute_raises_attribute_error():
    import otto.link as link_mod

    with pytest.raises(AttributeError, match=r"module 'otto\.link' has no attribute 'nope'"):
        _ = link_mod.nope


def _loaded(code: str, modules: list[str]) -> list[bool]:
    probe = f"{code}; import json, sys; print(json.dumps([m in sys.modules for m in {modules!r}]))"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_bare_import_does_not_pull_manage():
    """Fresh subprocess: importing otto.link alone must not import .manage,
    otto.host.daemon, otto.link.sentinel or otto.check until a name that
    needs them is actually accessed."""
    heavy = ["otto.link.manage", "otto.host.daemon", "otto.link.sentinel", "otto.check"]
    assert _loaded("import otto.link", heavy) == [False, False, False, False]
    assert _loaded("import otto.link; otto.link.impair_link", heavy[:3]) == [True, True, True]
    assert _loaded("import otto.link; otto.link.check_link", ["otto.check"]) == [True]


def test_the_registry_imports_only_the_impairer_layer():
    """``IMPAIRERS`` is what host-spec validation reads; it resolves the
    built-in by reference, so reaching it loads neither the netem builders nor
    the edge model."""
    absent = ["otto.link.netem", "otto.link.model", "otto.link.placement", "otto.link.manage"]
    assert _loaded("from otto.link import IMPAIRERS", absent) == [False] * len(absent)
    assert _loaded(
        "from otto.link import IMPAIRERS; IMPAIRERS.get('netem')", ["otto.link.netem"]
    ) == [True]
