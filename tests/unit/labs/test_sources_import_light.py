"""Settings parsing reads ``otto.labs.sources``, which imports no lab parser or host factory."""

import subprocess
import sys
from pathlib import Path

from tests._fixtures.project_scaffold import make_project

_HEAVY = ("otto.labs.json_repository", "otto.labs.documents", "otto.host.factory")

_PROBE = """
import sys
from pathlib import Path

from otto.config.project import Project
from otto.labs.sources import JSON_FILE_RULE, LabSourceFacts, compile_lab_sources

project = Project(root=Path(sys.argv[1]))
project.parse_settings()
assert [s.backend for s in project.lab_sources] == ["json"], project.lab_sources
assert compile_lab_sources(None, project_name="r", root=project.root) == []
assert LabSourceFacts(file_inputs=()).file_rule == JSON_FILE_RULE
print("\\n".join(name for name in sys.argv[2:] if name in sys.modules))
"""


def test_settings_parsing_imports_no_parser(tmp_path: Path) -> None:
    make_project(tmp_path, name="r", extra='[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n')
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path), *_HEAVY],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == []
