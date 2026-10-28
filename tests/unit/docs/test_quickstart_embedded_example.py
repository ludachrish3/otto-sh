"""The embedded track's example keeps the shape its page teaches.

The page says the logic is freestanding, so one source builds into the extension
and into GoogleTest; that the declared artifact is where ext/build.sh puts it; that
the root CMake tree is the host-side unit tests only; that a build writes under
build/ alone; and that the download holds the project, with its committed starter, and
nothing generated (init writes the patch). Each test below pins one of those sentences.
"""

import importlib.util
import ipaddress
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import tomli

import otto.coverage.embedded_gcov.library as shipped
from otto.result import CommandResult
from otto.utils import Status
from scripts import refresh_docs_captures as rdc
from scripts.build_example_archives import build
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.interpreter_agnostic

EXAMPLE = CHECKOUT_ROOT / "docs" / "examples" / "quickstart-embedded"
BED = CHECKOUT_ROOT / "docs" / "examples" / "bed" / "quickstart-embedded"
NAME = "quickstart-embedded"


def _settings() -> dict:
    return tomli.loads((EXAMPLE / ".otto" / "settings.toml").read_text())


def test_the_logic_includes_no_libc_or_zephyr_header():
    for name in ["mathops.c", "mathops.h"]:
        includes = re.findall(
            r"^\s*#\s*include\s*(\S+)", (EXAMPLE / "logic" / name).read_text(), re.MULTILINE
        )
        assert includes in ([], ['"mathops.h"']), (name, includes)


def test_one_logic_source_builds_into_the_extension_and_the_unit_tests():
    ext = (EXAMPLE / "ext" / "src" / "ext.c").read_text()
    assert '#include "cov_entry.c"' in ext
    assert '#include "../../logic/mathops.c"' in ext
    unit = (EXAMPLE / "tests" / "unit" / "CMakeLists.txt").read_text()
    assert "${PROJECT_SOURCE_DIR}/logic/mathops.c" in unit


def test_the_declared_artifact_is_where_ext_build_sh_puts_it():
    settings = _settings()
    (product,) = settings["products"]
    assert product == product | {
        "kind": "embedded",
        "call_after_load": ["cov_init"],
        "match": {"id": "dut"},
    }
    artifact = product["artifact"]
    script = (EXAMPLE / "ext" / "build.sh").read_text()
    # west build -d <ROOT>/build/ext writes zephyr/<target>.llext; the script strips it beside it.
    assert 'west build -d "$ROOT/build/ext" "$ROOT/ext"' in script
    assert artifact == f"build/ext/zephyr/{product['name']}.stripped.llext"
    assert f"{product['name']}.stripped.llext" in script
    cmake = (EXAMPLE / "ext" / "CMakeLists.txt").read_text()
    assert f"add_llext_target({product['name']}" in cmake
    assert settings["coverage"]["embedded"]["build_dir"] == "build/ext"


def test_the_unit_tier_harvests_the_gtest_objects_never_the_extension_build():
    unit = _settings()["coverage"]["tiers"]["unit"]
    assert unit["harvest_dirs"] == ["build/tests"]


def test_the_root_cmake_tree_never_references_the_extension():
    for path in [EXAMPLE / "CMakeLists.txt", EXAMPLE / "tests" / "unit" / "CMakeLists.txt"]:
        code = "\n".join(
            line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
        )
        assert not re.search(r"\bext\b|zephyr|Zephyr", code), path


def test_googletest_comes_from_the_build_machine_when_installed():
    cmake = (EXAMPLE / "CMakeLists.txt").read_text()
    assert "FIND_PACKAGE_ARGS NAMES GTest" in cmake
    assert "URL_HASH SHA256=" in cmake
    assert cmake.index("FetchContent_MakeAvailable") < cmake.index("add_compile_options(--coverage")


def test_the_build_script_writes_only_under_build():
    script = (EXAMPLE / "ext" / "build.sh").read_text()
    assert 'ROOT="$(cd "$(dirname "$0")/.." && pwd)"' in script
    assert 'BUILD="$ROOT/build/ext"' in script
    # Every path the script writes: rm operands, west's -d, and finish's output.
    written = re.findall(r'rm -f ((?:"[^"]+"\s*)+)', script)
    targets = [t for group in written for t in re.findall(r'"([^"]+)"', group)]
    west = re.findall(r'west build -d "([^"]+)"', script)
    finish = re.search(r'finish \\\n\s+"[^"]+" "([^"]+)"', script)
    assert finish is not None
    targets += [*west, finish.group(1)]
    assert targets
    for target in targets:
        assert target.startswith(("$BUILD/", "$ROOT/build/")), target
    for var in ["ZEPHYR_WORKSPACE", "ZEPHYR_SDK_INSTALL_DIR", "EMBEDDED_GCOV_DIR"]:
        assert f'"${{{var}:?' in script, var
    assert 'command -v "${OTTO:-otto}"' in script, "OTTO defaults to otto on PATH"
    assert "no otto found" in script


def test_relative_inputs_are_made_absolute_before_the_script_changes_directory():
    script = (EXAMPLE / "ext" / "build.sh").read_text()
    cd = script.index('\ncd "$ZEPHYR_WORKSPACE"\n')
    for var in ["OTTO", "ZEPHYR_WORKSPACE", "ZEPHYR_SDK_INSTALL_DIR", "EMBEDDED_GCOV_DIR"]:
        assert script.index(f'{var}="$(cd ') < cd, var


def test_the_build_script_runs_the_shipped_finish_verb():
    script = (EXAMPLE / "ext" / "build.sh").read_text()
    assert '"$OTTO" cov embedded-gcov finish' in script
    assert "--remove-section" not in script, "the strip list lives in otto"
    assert shipped.STRIPPED_SECTIONS


@pytest.mark.parametrize(
    "ignored",
    [
        "build/CMakeCache.txt",
        "build/ext/zephyr/mathops.stripped.llext",
        "third_party/embedded-gcov-patch/embedded-gcov-zephyr-gcc12plus.patch",
        "pylib/quickstart_embedded/__pycache__/x.pyc",
    ],
)
def test_a_build_leaves_the_git_repository_clean(ignored, tmp_path):
    project = tmp_path / NAME
    project.mkdir()
    shutil.copy(EXAMPLE / ".gitignore", project / ".gitignore")
    env = git_env(tmp_path / "home")
    subprocess.run(["git", "init", "-q", str(project)], env=env, check=True)
    done = subprocess.run(
        ["git", "-C", str(project), "check-ignore", "--no-index", "-q", ignored],
        env=env,
        check=False,
    )
    assert done.returncode == 0, f"{ignored} is not ignored by the example's .gitignore"


def _shipped(tmp_path: Path) -> list[str]:
    listed = subprocess.run(
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
        env=git_env(tmp_path / "git-home"),
    ).stdout.split()
    return [Path(p).relative_to(f"docs/examples/{NAME}").as_posix() for p in listed]


def _archive_names(tmp_path: Path) -> set[str]:
    archives = build(CHECKOUT_ROOT / "docs" / "examples", tmp_path / "out", checkout=CHECKOUT_ROOT)
    built = next((p for p in archives if p.name == f"{NAME}.tar.gz"), None)
    assert built is not None, f"no {NAME}.tar.gz among {[p.name for p in archives]}"
    with tarfile.open(built) as tar:
        return set(tar.getnames())


ARCHIVE_MEMBERS = [
    ".gitignore",
    ".otto/settings.toml",
    "lab_data/lab.json",
    "CMakeLists.txt",
    "logic/mathops.c",
    "logic/mathops.h",
    "ext/CMakeLists.txt",
    "ext/prj.conf",
    "ext/build.sh",
    "ext/src/main.c",
    "ext/src/ext.c",
    "ext/src/cov_entry.c",
    "pylib/quickstart_embedded/__init__.py",
    "tests/test_mathops.py",
    "tests/unit/CMakeLists.txt",
    "tests/unit/test_mathops.cpp",
]


def test_the_archive_holds_the_project_and_nothing_generated(tmp_path):
    names = _archive_names(tmp_path)
    assert names == {f"{NAME}/{m}" for m in ARCHIVE_MEMBERS}
    assert names == {f"{NAME}/{p}" for p in _shipped(tmp_path)}
    forbidden = [
        n
        for n in names
        if "/third_party/" in n
        or "/build/" in n
        or n.endswith(("/bed-build.sh", ".gcno", ".llext", ".patch"))
    ]
    assert not forbidden, forbidden


def test_the_committed_starter_is_byte_identical_to_the_one_otto_ships():
    shipped_starter = (shipped.LIBRARY_DIR / "starter" / "cov_entry.c").read_bytes()
    assert (EXAMPLE / "ext" / "src" / "cov_entry.c").read_bytes() == shipped_starter


def test_the_placeholder_lab_holds_no_bed_facts():
    lab = json.loads((EXAMPLE / "lab_data" / "lab.json").read_text())
    text = json.dumps(lab)
    assert "test4" not in text
    assert "hop" not in text
    for ip in re.findall(r'"(?:ip|server)": "([\d.]+)"', text):
        assert ipaddress.ip_address(ip) in ipaddress.ip_network("192.0.2.0/24"), ip


@pytest.mark.parametrize(
    "lab_file",
    [EXAMPLE / "lab_data" / "lab.json", BED / "lab_data" / "lab.json"],
    ids=["placeholder", "bed"],
)
def test_the_console_server_is_a_host_of_the_lab_not_an_address(lab_file):
    """``console_options.server`` is a lab host id: an address there fails on the first connect.

    Each element here holds one host with no board, so its id is the element's name.
    """
    lab = json.loads(lab_file.read_text())
    host_ids = {e["name"] for e in lab["elements"] if len(e["hosts"]) == 1}
    servers = [
        h["console_options"]["server"]
        for e in lab["elements"]
        for h in e["hosts"]
        if "console_options" in h
    ]
    assert servers
    assert set(servers) <= host_ids, (servers, host_ids)


def test_the_bed_overlay_holds_exactly_the_lab_and_the_build_wrapper():
    held = sorted(p.relative_to(BED).as_posix() for p in BED.rglob("*") if p.is_file())
    assert held == ["ext/bed-build.sh", "lab_data/lab.json"]


def test_otto_init_embedded_gcov_writes_the_patch_and_the_starter_the_example_commits(tmp_path):
    """The first step: init writes the patch, and its starter equals the committed one."""
    project = tmp_path / NAME
    for rel in _shipped(tmp_path):
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(EXAMPLE / rel, project / rel)
    assert not (project / "third_party").exists()
    done = run_otto(
        ["init", "--embedded-gcov", "--path", str(project)],
        roots=None,
        cwd=tmp_path,
        extra_env={
            **git_env(tmp_path / "home"),
            "OTTO_HOME": str(tmp_path / "otto-home"),
            "NO_COLOR": "1",
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    consumer = project / "third_party" / "embedded-gcov-patch-consumer"
    patch = project / "third_party" / "embedded-gcov-patch" / "embedded-gcov-zephyr-gcc12plus.patch"
    assert patch.is_file()
    assert (consumer / "cov_entry.c").read_bytes() == (project / "ext/src/cov_entry.c").read_bytes()


HOSTLESS_INPUTS = [
    ".otto/settings.toml",
    "lab_data/lab.json",
    "CMakeLists.txt",
    "tests/unit/CMakeLists.txt",
    "tests/unit/test_mathops.cpp",
    "logic/mathops.c",
    "logic/mathops.h",
]


def test_the_hostless_path_needs_nothing_otto_init_writes(tmp_path):
    names = _archive_names(tmp_path)
    missing = [f for f in HOSTLESS_INPUTS if f"{NAME}/{f}" not in names]
    assert not missing, missing


def test_the_e2e_test_runs_under_ensure_installed():
    source = (EXAMPLE / "tests" / "test_mathops.py").read_text()
    assert '@pytest.mark.ensure("installed")' in source


def _helper() -> ModuleType:
    """Load the example's helper alone, with a stand-in for the lab lookup."""
    path = EXAMPLE / "pylib" / "quickstart_embedded" / "__init__.py"
    spec = importlib.util.spec_from_file_location("qs_embedded_helper_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dut(result: CommandResult, loader: object) -> "tuple[SimpleNamespace, list[str]]":
    calls: list[str] = []

    async def exec_(cmd: str) -> CommandResult:
        calls.append(cmd)
        return result

    return SimpleNamespace(exec=exec_, loader=loader), calls


class _Loader:
    def call_command(self, name: str, fn: str) -> str:
        return f"CALL {name} {fn}"

    def reports_not_loaded(self, output: str, name: str) -> bool:
        return output.startswith("No such")


@pytest.mark.asyncio
async def test_the_helper_asks_the_loader_for_the_command_and_returns_the_output(monkeypatch):
    helper = _helper()
    ok = CommandResult(Status.Success, value="op=1\n", command="", retcode=0)
    dut, calls = _dut(ok, _Loader())
    monkeypatch.setattr(helper, "get_host", lambda _name: dut)
    assert await helper.call_fn("ext", "op_a") == "op=1\n"
    assert calls == ["CALL ext op_a"]


@pytest.mark.asyncio
async def test_the_helper_names_an_extension_that_is_not_resident(monkeypatch):
    helper = _helper()
    gone = CommandResult(Status.Failed, value="No such extension ext", command="", retcode=1)
    dut, _ = _dut(gone, _Loader())
    monkeypatch.setattr(helper, "get_host", lambda _name: dut)
    with pytest.raises(helper.CallError, match="extension ext is not loaded"):
        await helper.call_fn("ext", "op_a")


@pytest.mark.asyncio
async def test_the_helper_refuses_a_failed_call_and_a_host_with_no_loader(monkeypatch):
    helper = _helper()
    bad = CommandResult(Status.Failed, value="boom", command="", retcode=1)
    dut, _ = _dut(bad, _Loader())
    monkeypatch.setattr(helper, "get_host", lambda _name: dut)
    with pytest.raises(helper.CallError, match="op_a failed on dut: 'boom'"):
        await helper.call_fn("ext", "op_a")
    dut, calls = _dut(bad, None)
    monkeypatch.setattr(helper, "get_host", lambda _name: dut)
    with pytest.raises(helper.CallError, match="no binary loader"):
        await helper.call_fn("ext", "op_a")
    assert not calls


def _captures() -> list[rdc.Capture]:
    return [c for c in rdc.load_manifest(rdc.DEFAULT_MANIFEST) if c.project == NAME]


def test_every_store_assertion_names_its_marker():
    caps = _captures()
    asserted = [a for c in caps for a in c.store]
    assert asserted, "the embedded track asserts no hit counts"
    assert all(a.marker and a.marker.startswith("// qs: ") for a in asserted)
    rdc.verify_markers(caps, rdc.EXAMPLES)
    assert {s.tier for c in caps for s in c.store_sources} == {"system", "manual", "unit"}


def test_the_capture_set_is_bed_only_and_orders_its_steps():
    caps = _captures()
    assert caps
    assert all(not c.labless for c in caps)
    ids = [c.id for c in caps]
    before = {
        "qs-embedded-clone-runtime": "qs-embedded-pin-runtime",
        "qs-embedded-pin-runtime": "qs-embedded-apply-patch",
        "qs-embedded-init": "qs-embedded-git-commit",
        "qs-embedded-git-commit": "qs-embedded-build-ext",
        "qs-embedded-build-ext": "qs-embedded-tree-clean",
        "qs-embedded-uninstall-before": "qs-embedded-install",
        "qs-embedded-install": "qs-embedded-status",
        "qs-embedded-status": "qs-embedded-cov-clean",
        "qs-embedded-cov-clean": "qs-embedded-manual-run",
        "qs-embedded-uninstall": "qs-embedded-test-cov",
        "qs-embedded-test-cov": "qs-embedded-report",
    }
    for first, second in before.items():
        assert ids.index(first) < ids.index(second), (first, second)
