"""otto.link's ``.manage`` re-exports are lazy (PEP 562), not eager.

Only the future `otto link` CLI calls impair_link/repair_link/etc; every other
otto.link importer (8 of 9 CLI surfaces, via otto.models.host -> IMPAIRERS)
must not pay for otto.host.daemon / otto.link.sentinel. See
tests/unit/import_budget/ for the surface-level snapshot guard; this test
proves the runtime attribute-resolution path directly.
"""

import subprocess
import sys

import pytest

from otto.link import manage


def test_manage_name_resolves_to_manage_module_object():
    from otto.link import impair_link

    assert impair_link is manage.impair_link


def test_manage_names_all_resolve():
    import otto.link as link_mod

    for name in (
        "AppliedPlacement",
        "DirectionState",
        "ImpairReport",
        "LinkState",
        "RepairReport",
        "find_link",
        "impair_link",
        "read_link_states",
        "repair_all",
        "repair_link",
    ):
        assert getattr(link_mod, name) is getattr(manage, name)


def test_unknown_attribute_raises_attribute_error():
    import otto.link as link_mod

    with pytest.raises(AttributeError, match=r"module 'otto\.link' has no attribute 'nope'"):
        _ = link_mod.nope


def test_bare_import_does_not_pull_manage():
    """Fresh subprocess: importing otto.link alone must not import .manage,
    otto.host.daemon, or otto.link.sentinel until a manage-only name is
    actually accessed."""
    code = (
        "import sys; import otto.link; "
        "print('otto.link.manage' in sys.modules, "
        "'otto.host.daemon' in sys.modules, "
        "'otto.link.sentinel' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False False False", out.stdout

    code_after_access = (
        "import sys; import otto.link; otto.link.impair_link; "
        "print('otto.link.manage' in sys.modules, "
        "'otto.host.daemon' in sys.modules, "
        "'otto.link.sentinel' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code_after_access],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True True True", out.stdout
