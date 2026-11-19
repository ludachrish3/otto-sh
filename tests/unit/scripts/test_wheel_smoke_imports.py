"""Which of otto's import statements the wheel smoke runs (#1181).

``scripts/wheel_smoke.py`` runs every third-party import statement in otto's
source so a dependency floor missing a name otto imports on a deferred path
fails the smoke. A statement it wrongly skips blinds the floor guard; one it
wrongly runs fails it on an import otto never makes. Both directions are
pinned here, one shape each.
"""

import sys
import textwrap

import pytest

from scripts.wheel_smoke import third_party_statements

pytestmark = pytest.mark.interpreter_agnostic


def _found(source: str) -> "list[str]":
    return [found.code for found in third_party_statements(textwrap.dedent(source))]


def _in_try(source: str) -> "list[tuple[str, bool]]":
    return [(f.code, f.in_try) for f in third_party_statements(textwrap.dedent(source))]


def test_a_function_local_import_is_run():
    assert _found(
        """
        def snmp_get():
            from pysnmp.hlapi.v1arch.asyncio import get_cmd
        """
    ) == ["from pysnmp.hlapi.v1arch.asyncio import get_cmd"]


def test_the_standard_library_otto_and_relative_imports_are_not_dependencies():
    assert (
        _found(
            """
            import os, json
            from __future__ import annotations
            from otto.result import Result
            from . import sibling
            """
        )
        == []
    )


def test_a_mixed_import_line_runs_its_third_party_module_alone():
    assert _found("import os, requests\n") == ["import requests"]


def test_a_type_checking_import_is_skipped():
    assert (
        _found(
            """
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from pydantic import BaseModel
            """
        )
        == []
    )


def test_an_import_in_a_try_that_handles_import_error_is_marked():
    """Marked, not dropped: the smoke still runs it when an installed package provides it."""
    assert _in_try(
        """
        try:
            import zstandard
        except ImportError:
            raise UnreadableError("no zstd decompressor") from None
        """
    ) == [("import zstandard", True)]


def test_an_import_in_a_try_for_another_error_is_not_marked():
    assert _in_try(
        """
        try:
            from uv import find_uv_bin
        except OSError:
            pass
        """
    ) == [("from uv import find_uv_bin", False)]


def test_a_try_around_a_definition_does_not_mark_the_imports_inside_it():
    """The function's import runs when it is called, outside that ``try``."""
    assert _in_try(
        """
        try:
            def connect():
                import asyncssh
        except ImportError:
            connect = None
        """
    ) == [("import asyncssh", False)]


def test_a_try_around_a_class_marks_the_imports_in_its_body():
    """A class body runs where it stands, inside the ``try``."""
    assert _in_try(
        """
        try:
            class C:
                import absent_extra
        except ImportError:
            C = None
        """
    ) == [("import absent_extra", True)]


def test_a_present_module_without_top_level_txt_is_still_required():
    """The uv wheel has no ``top_level.txt``, so Python 3.10's ``packages_distributions``
    misses it; presence is judged by ``find_spec``, which finds it."""
    import importlib.util

    assert importlib.util.find_spec("uv") is not None


def test_a_marked_import_is_skipped_only_when_nothing_installed_provides_it(tmp_path, monkeypatch):
    """A declared dependency in a ``try`` must import; an absent extra is otto's to cope with."""
    from scripts import wheel_smoke

    pkg = tmp_path / "otto"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "try:\n    import absent_extra_1181\nexcept ImportError:\n    pass\n"
        "try:\n    from pytest import not_a_name_1181\nexcept ImportError:\n    raise\n"
    )
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    monkeypatch.delitem(sys.modules, "otto", raising=False)
    failures = wheel_smoke._third_party_imports()
    assert len(failures) == 1, failures
    assert "from pytest import not_a_name_1181" in failures[0]


@pytest.mark.parametrize(("op", "taken"), [(">=", sys.version_info >= (3, 99)), ("<", True)])
def test_a_version_gate_runs_only_the_branch_this_interpreter_takes(op, taken):
    found = _found(
        f"""
        import sys
        if sys.version_info {op} (3, 99):
            import in_body
        else:
            import in_else
        """
    )
    assert found == (["import in_body"] if taken else ["import in_else"])


def test_a_gate_that_depends_on_more_than_the_version_is_not_judged():
    """Once the version part holds, the rest names otto's own state: the import runs."""
    assert _found(
        """
        import sys
        if sys.version_info >= (3, 0) and FEATURE:
            import maybe
        """
    ) == ["import maybe"]


def test_a_gate_the_version_alone_decides_is_judged():
    """``and`` with a failing version part is false whatever the rest says."""
    assert (
        _found(
            """
            import sys
            if sys.version_info >= (3, 99) and FEATURE:
                import never
            """
        )
        == []
    )
