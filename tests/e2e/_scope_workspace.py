"""A two-project workspace on disk for the e2e scope tests: ``OTTOPATH`` roots and their lab.

``project_a`` carries lab ``alpha`` and is always active; ``project_b``
declares ``[project]`` patterns. The default is phase 1's mixed shape:
``project_a`` declares nothing and ``project_b`` applies to lab ``beta``
only, so under ``-l alpha`` it does not take part.
"""

import json
import os
from pathlib import Path


def scoped_roots(tmp_path: Path, *, declare_a: bool = False, hosts: bool = False) -> str:
    """Write the two projects under *tmp_path* and return them as an ``OTTOPATH`` value.

    *declare_a* gives ``project_a`` its own declaration, ``lab_patterns =
    ["alpha"]`` and ``host_patterns = ["a-dut"]``. *hosts* gives lab
    ``alpha`` three hosts instead of one: ``a-dut``, ``a-jump`` (which no
    project targets) and ``b-dut``, which ``project_b`` then targets
    (``lab_patterns = ["alpha", "beta"]``, ``host_patterns = ["b-dut"]``).
    With both, under ``-l alpha`` the run targets ``a-dut``, ``b-dut`` and
    the built-in ``local``; ``-E project_b`` leaves ``b-dut`` targeted only
    by a switched-off project.
    """
    from tests._fixtures.labdata import lab_json_v2
    from tests._fixtures.project_scaffold import make_project

    elements = ["a-dut", "a-jump", "b-dut"] if hosts else ["a"]
    document = lab_json_v2(
        [
            {"ip": f"192.0.2.{octet}", "element": element, "labs": ["alpha"]}
            for octet, element in enumerate(elements, start=1)
        ]
    )
    a_scope = (
        '[project]\nlab_patterns = ["alpha"]\nhost_patterns = ["a-dut"]\n' if declare_a else ""
    )
    b_scope = (
        '[project]\nlab_patterns = ["alpha", "beta"]\nhost_patterns = ["b-dut"]\n'
        if hosts
        else '[project]\nlab_patterns = ["beta"]\nhost_patterns = [".*"]\n'
    )
    a = make_project(
        tmp_path / "project_a",
        name="project_a",
        tests=["tests"],
        extra=f'{a_scope}[[lab.sources]]\nbackend = "json"\npaths = ["labdata"]\n',
        files={
            "labdata/lab.json": json.dumps(document),
            "tests/test_a.py": "def test_in_a(): pass\n",
        },
    )
    b = make_project(
        tmp_path / "project_b",
        name="project_b",
        tests=["tests"],
        extra=b_scope,
        files={"tests/test_b.py": "def test_in_b(): pass\ndef test_only_b(): pass\n"},
    )
    return f"{a}{os.pathsep}{b}"
