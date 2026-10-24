"""The shell track's example project keeps the shape its page teaches (spec #594 §3.1).

The page says the example is hierarchical the way real product trees are: a
subsystem library in its own object directory, two products whose sources
share a basename, a gtest tree under the project root. It says the e2e test
derives GCOV_PREFIX_STRIP from where the project sits, that GoogleTest comes
from the build machine when it is installed, and that the download holds the
project and nothing built. Each test below pins one of those sentences.
"""

import importlib.util
import re
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from otto.result import CommandResult
from otto.utils import Status
from scripts import refresh_docs_captures as rdc
from scripts.build_example_archives import build
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

EXAMPLE = CHECKOUT_ROOT / "docs" / "examples" / "quickstart-shell"


def test_the_library_and_both_products_build_from_their_own_directories():
    assert (EXAMPLE / "src" / "textlib" / "text.cpp").is_file()
    assert (EXAMPLE / "src" / "wordcount" / "main.cpp").is_file()
    assert (EXAMPLE / "src" / "shout" / "main.cpp").is_file()
    assert (EXAMPLE / "tests" / "unit" / "test_text.cpp").is_file()


def test_the_e2e_helper_derives_the_strip_count_from_the_project_root():
    source = (EXAMPLE / "pylib" / "quickstart_shell" / "tools.py").read_text()
    assert "len(PROJECT_ROOT.parts) - 1" in source
    for path in [
        EXAMPLE / "pylib" / "quickstart_shell" / "tools.py",
        *(EXAMPLE / "tests").glob("*.py"),
    ]:
        assert not re.search(r"GCOV_PREFIX_STRIP=\d", path.read_text()), path


def test_googletest_comes_from_the_build_machine_when_installed():
    cmake = (EXAMPLE / "CMakeLists.txt").read_text()
    assert "FIND_PACKAGE_ARGS NAMES GTest" in cmake
    # Coverage flags come after GoogleTest is made available, so its sources stay uninstrumented.
    assert cmake.index("FetchContent_MakeAvailable") < cmake.index("add_compile_options(--coverage")


def test_the_archive_holds_the_project_and_nothing_built(tmp_path):
    archives = build(CHECKOUT_ROOT / "docs" / "examples", tmp_path, checkout=CHECKOUT_ROOT)
    built = next((p for p in archives if p.name == "quickstart-shell.tar.gz"), None)
    assert built is not None, f"no quickstart-shell.tar.gz among {[p.name for p in archives]}"
    with tarfile.open(built) as tar:
        names = sorted(tar.getnames())
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(CHECKOUT_ROOT),
            "ls-files",
            "--",
            str(EXAMPLE.relative_to(CHECKOUT_ROOT)),
        ],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(tmp_path / "home"),
    ).stdout.split()
    assert names == sorted(
        "quickstart-shell/" + Path(p).relative_to("docs/examples/quickstart-shell").as_posix()
        for p in tracked
    )
    assert "quickstart-shell/.gitignore" in names
    assert not [n for n in names if "/build/" in n or "/captures/" in n or "__pycache__" in n]


def test_every_shell_store_assertion_names_its_marker():
    """The runner's load-time guard only checks entries that carry a marker; the track's all do."""
    caps = [c for c in rdc.load_manifest(rdc.DEFAULT_MANIFEST) if c.project == "quickstart-shell"]
    asserted = [a for c in caps for a in c.store]
    assert asserted, "the shell track asserts no hit counts"
    assert all(a.marker and a.marker.startswith("// qs: ") for a in asserted)
    rdc.verify_markers(caps, rdc.EXAMPLES)
    assert {s.tier for c in caps for s in c.store_sources} == {"system", "manual", "unit"}


def test_the_track_set_is_bed_only_and_starts_from_a_fresh_build():
    caps = [c for c in rdc.load_manifest(rdc.DEFAULT_MANIFEST) if c.project == "quickstart-shell"]
    assert caps
    assert all(not c.labless for c in caps)
    assert caps[0].argv[:2] == ["git", "-C"]
    assert not caps[0].record


def _tools_module() -> ModuleType:
    """Load the example's tools.py alone: importing its package would register the metric."""
    path = EXAMPLE / "pylib" / "quickstart_shell" / "tools.py"
    spec = importlib.util.spec_from_file_location("qs_shell_tools_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dut_answering(result: CommandResult) -> SimpleNamespace:
    async def exec_(_cmd: str) -> CommandResult:
        return result

    product = SimpleNamespace(name="shout", cov_dir="/tmp/shout")
    return SimpleNamespace(products=[product], exec=exec_)


@pytest.mark.asyncio
async def test_the_e2e_helper_refuses_a_tool_that_failed(monkeypatch):
    tools = _tools_module()
    failed = CommandResult(
        Status.Failed, value="boom\n", command="/tmp/quickstart/shout x", retcode=2
    )
    monkeypatch.setattr(tools, "get_host", lambda _name: _dut_answering(failed))
    with pytest.raises(tools.ToolFailedError, match=r"/tmp/quickstart/shout x.*exit code 2"):
        await tools.run_tool("shout", "x")


@pytest.mark.asyncio
async def test_the_e2e_helper_says_a_tool_timed_out(monkeypatch):
    tools = _tools_module()
    timed_out = CommandResult(
        Status.Error,
        value="Command timed out after 5s",
        command="/tmp/quickstart/shout x",
        retcode=-1,
        timed_out=True,
    )
    monkeypatch.setattr(tools, "get_host", lambda _name: _dut_answering(timed_out))
    with pytest.raises(tools.ToolFailedError) as raised:
        await tools.run_tool("shout", "x")
    message = str(raised.value)
    assert "/tmp/quickstart/shout x" in message
    assert "timed out" in message
    assert "exit code" not in message


@pytest.mark.asyncio
async def test_the_e2e_helper_says_a_killed_tool_did_not_finish(monkeypatch):
    """A signal kill or a lost session also leaves retcode -1, with no timeout."""
    tools = _tools_module()
    killed = CommandResult(Status.Failed, value="", command="/tmp/quickstart/shout x", retcode=-1)
    monkeypatch.setattr(tools, "get_host", lambda _name: _dut_answering(killed))
    with pytest.raises(tools.ToolFailedError) as raised:
        await tools.run_tool("shout", "x")
    message = str(raised.value)
    assert "did not finish" in message
    assert "timed out" not in message
    assert "exit code" not in message


@pytest.mark.asyncio
async def test_the_e2e_helper_names_a_product_dut_does_not_have(monkeypatch):
    tools = _tools_module()
    ok = CommandResult(Status.Success, value="", command="", retcode=0)
    monkeypatch.setattr(tools, "get_host", lambda _name: _dut_answering(ok))
    with pytest.raises(LookupError, match=r"'nope'.*dut"):
        await tools.run_tool("nope", "x")


@pytest.mark.asyncio
async def test_the_e2e_helper_returns_what_a_tool_printed(monkeypatch):
    tools = _tools_module()
    ok = CommandResult(Status.Success, value="X\n", command="/tmp/quickstart/shout x", retcode=0)
    monkeypatch.setattr(tools, "get_host", lambda _name: _dut_answering(ok))
    assert await tools.run_tool("shout", "x") == "X"
