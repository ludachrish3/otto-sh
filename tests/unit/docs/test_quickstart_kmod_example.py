"""The kernel-module track's example keeps the shape its page teaches (spec #594 §3.1).

The page says the module's logic is freestanding, so one source builds into the
module and into GoogleTest; that the sentinels bracket every instrumented object;
that the declared artifacts are where the two build commands put them; that a
build leaves the git repository clean; that the download holds the project and
nothing built (init writes the library and sentinels); and that the e2e test unloads in
its teardown under ensure("installed"). Each test below pins one of those sentences.
"""

import importlib.util
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import tomli

from otto.result import CommandResult
from otto.utils import Status
from scripts import refresh_docs_captures as rdc
from scripts.build_example_archives import build
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

EXAMPLE = CHECKOUT_ROOT / "docs" / "examples" / "quickstart-kmod"
MODULE = EXAMPLE / "module"


def _composite_objects() -> list[str]:
    kbuild = (MODULE / "Kbuild").read_text()
    (objects,) = re.findall(r"^qs_wordcount-y\s*:=\s*(.+)$", kbuild, flags=re.MULTILINE)
    return objects.split()


def test_the_module_logic_includes_no_kernel_or_libc_header():
    for name in ["wordcount.c", "wordcount.h"]:
        includes = re.findall(r"^\s*#\s*include\s*(\S+)", (MODULE / name).read_text(), re.MULTILINE)
        assert includes in ([], ['"wordcount.h"']), (name, includes)


def test_one_logic_source_builds_into_the_module_and_the_unit_tests():
    assert "wordcount.o" in _composite_objects()
    unit = (EXAMPLE / "tests" / "unit" / "CMakeLists.txt").read_text()
    assert "${PROJECT_SOURCE_DIR}/module/wordcount.c" in unit


def test_the_sentinels_bracket_every_instrumented_object():
    objects = _composite_objects()
    assert objects[0] == "kmodcov_begin.o"
    assert objects[-1] == "kmodcov_end.o"
    kbuild = (MODULE / "Kbuild").read_text()
    flagged = set(
        re.findall(r"^CFLAGS_(\S+\.o)\s*\+=\s*\$\(KMODCOV_CFLAGS\)", kbuild, re.MULTILINE)
    )
    assert flagged == set(objects[1:-1])


def test_the_declared_artifacts_are_where_the_build_commands_put_them():
    settings = tomli.loads((EXAMPLE / ".otto" / "settings.toml").read_text())
    (tool,) = settings["dev_tools"]
    (product,) = settings["products"]
    # third_party/otto_kmodcov/build.sh <build-dir> writes <build-dir>/lib/otto_kmodcov.ko.
    assert tool == tool | {
        "kind": "kmodcov",
        "artifact": "build/kmodcov/lib/otto_kmodcov.ko",
        "source": "third_party/otto_kmodcov",
    }
    assert re.search(
        r"^obj-m\s*:=\s*qs_wordcount\.o$", (MODULE / "Kbuild").read_text(), re.MULTILINE
    )
    assert product == product | {
        "kind": "kmod",
        "artifact": "module/qs_wordcount.ko",
        "coverage": "module",
    }
    makefile = (MODULE / "Makefile").read_text()
    assert "KMODCOV ?= $(abspath $(CURDIR)/../build/kmodcov/lib)" in makefile
    assert re.search(r"^KDIR \?=", makefile, re.MULTILINE), "the page's KDIR override needs ?="


def test_googletest_comes_from_the_build_machine_when_installed():
    cmake = (EXAMPLE / "CMakeLists.txt").read_text()
    assert "FIND_PACKAGE_ARGS NAMES GTest" in cmake
    assert "URL_HASH SHA256=" in cmake
    assert cmake.index("FetchContent_MakeAvailable") < cmake.index("add_compile_options(--coverage")


@pytest.mark.parametrize(
    "built",
    [
        "build/CMakeCache.txt",
        "build/kmodcov/lib/otto_kmodcov.ko",
        "module/qs_wordcount.ko",
        "module/qs_wordcount.o",
        "module/qs_wordcount.mod",
        "module/qs_wordcount.mod.c",
        "module/qs_wordcount.mod.o",
        "module/.qs_wordcount.ko.cmd",
        "module/qs_main.o",
        "module/qs_main.gcno",
        "module/modules.order",
        "module/Module.symvers",
        "pylib/quickstart_kmod/__pycache__/wordcount.cpython-312.pyc",
    ],
)
def test_a_build_leaves_the_git_repository_clean(built, tmp_path):
    """Judged by the example's own .gitignore alone, in a fresh git repository.

    otto's checkout ignores build/, *.gcno and __pycache__/ in its root
    .gitignore, which a reader's unpacked project does not have.
    """
    project = tmp_path / "quickstart-kmod"
    project.mkdir()
    shutil.copy(EXAMPLE / ".gitignore", project / ".gitignore")
    env = git_env(tmp_path / "home")
    subprocess.run(["git", "init", "-q", str(project)], env=env, check=True)
    done = subprocess.run(
        ["git", "-C", str(project), "check-ignore", "--no-index", "-q", built],
        env=env,
        check=False,
    )
    assert done.returncode == 0, f"{built} is not ignored by the example's .gitignore"


def _shipped(tmp_path: Path) -> list[str]:
    """The example's tracked files, relative to it: exactly what the archive ships."""
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
    return [Path(p).relative_to("docs/examples/quickstart-kmod").as_posix() for p in listed]


def test_the_archive_holds_the_project_and_nothing_built(tmp_path):
    archives = build(CHECKOUT_ROOT / "docs" / "examples", tmp_path, checkout=CHECKOUT_ROOT)
    built = next((p for p in archives if p.name == "quickstart-kmod.tar.gz"), None)
    assert built is not None, f"no quickstart-kmod.tar.gz among {[p.name for p in archives]}"
    with tarfile.open(built) as tar:
        names = sorted(tar.getnames())
    assert names == sorted("quickstart-kmod/" + p for p in _shipped(tmp_path))
    assert "quickstart-kmod/.gitignore" in names
    assert not [n for n in names if "/third_party/" in n], "otto init --kmodcov vendors the library"
    assert not [n for n in names if n.endswith(("/kmodcov_begin.c", "/kmodcov_end.c"))], (
        "the sentinels come from otto init --kmodcov's starter"
    )
    assert not [n for n in names if n.endswith((".ko", ".o", ".gcno")) or "/build/" in n]


SENTINELS = {
    # What otto.init.areas._scaffold_kmodcov writes into the starter, byte for byte.
    "kmodcov_begin.c": '#include "kmodcov.h"\nKMODCOV_SENTINEL_BEGIN;\n',
    "kmodcov_end.c": '#include "kmodcov.h"\nKMODCOV_SENTINEL_END;\n',
}


def test_otto_init_kmodcov_supplies_the_sentinels_and_leaves_the_settings_alone(tmp_path):
    """The track's first step: init writes the sentinels the Kbuild links, and appends nothing."""
    project = tmp_path / "quickstart-kmod"
    for rel in _shipped(tmp_path):  # only what the reader unpacks: no stale starter or build
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(EXAMPLE / rel, project / rel)
    starter = project / "third_party" / "otto_kmodcov-consumer"
    assert not (project / "third_party").exists()
    assert not [n for n in SENTINELS if (project / "module" / n).exists()]
    before = (project / ".otto" / "settings.toml").read_bytes()
    env = {**git_env(tmp_path / "home"), "OTTO_HOME": str(tmp_path / "otto-home"), "NO_COLOR": "1"}
    env.pop("OTTOPATH", None)
    otto = Path(sys.executable).with_name("otto")
    done = subprocess.run(
        [str(otto), "init", "--kmodcov", "--path", str(project)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (project / ".otto" / "settings.toml").read_bytes() == before
    objects = _composite_objects()
    assert [objects[0], objects[-1]] == ["kmodcov_begin.o", "kmodcov_end.o"]
    for name, text in SENTINELS.items():
        assert (starter / name).read_bytes() == text.encode("utf-8"), name
    assert (project / "third_party" / "otto_kmodcov" / "build.sh").is_file()


HOSTLESS_INPUTS = [
    ".otto/settings.toml",
    "lab_data/lab.json",
    "CMakeLists.txt",
    "tests/unit/CMakeLists.txt",
    "tests/unit/test_wordcount.cpp",
    "module/wordcount.c",
    "module/wordcount.h",
]
"""What the hostless check reads (check() in check_docs_examples.py): the settings and lab for
--list-products and -n run install, and every input of the cmake build and ctest."""


def test_the_hostless_path_needs_nothing_otto_init_writes(tmp_path):
    """The hostless check never runs init: every input it reads is in the downloaded archive."""
    # Plain membership, no CMake parsing (D7): whether the build uses these files
    # is CMake's to say, and the hostless check's own configure/build/ctest says it.
    archives = build(CHECKOUT_ROOT / "docs" / "examples", tmp_path / "out", checkout=CHECKOUT_ROOT)
    built = next(p for p in archives if p.name == "quickstart-kmod.tar.gz")
    with tarfile.open(built) as tar:
        members = set(tar.getnames())
    missing = [f for f in HOSTLESS_INPUTS if f"quickstart-kmod/{f}" not in members]
    assert not missing, missing


def test_the_e2e_test_unloads_in_its_teardown_under_ensure_installed():
    source = (EXAMPLE / "tests" / "test_wordcount.py").read_text()
    assert '@pytest.mark.ensure("installed")\nclass TestWordcount' in source
    assert 'scope="class", autouse=True' in source
    assert 'await get_host("dut").uninstall()' in source


def _helper() -> ModuleType:
    """Load the example's wordcount.py alone: importing its package would register the metric."""
    path = EXAMPLE / "pylib" / "quickstart_kmod" / "wordcount.py"
    spec = importlib.util.spec_from_file_location("qs_kmod_wordcount_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dut(*results: CommandResult) -> "tuple[SimpleNamespace, list[tuple[str, bool]]]":
    calls: list[tuple[str, bool]] = []
    answers = list(results)

    async def exec_(cmd: str, *, sudo: bool = False) -> CommandResult:
        calls.append((cmd, sudo))
        return answers.pop(0)

    return SimpleNamespace(exec=exec_), calls


def _ok(value: str) -> CommandResult:
    return CommandResult(Status.Success, value=value, command="", retcode=0)


@pytest.mark.asyncio
async def test_the_helper_writes_the_line_as_one_argument_under_sudo(monkeypatch):
    helper = _helper()
    dut, calls = _dut(_ok(""), _ok("2\n"))
    monkeypatch.setattr(helper, "get_host", lambda _name: dut)
    assert await helper.count_words("a; reboot") == 2
    (write, sudo), (read, read_sudo) = calls
    assert sudo is True
    assert read_sudo is False
    shell, flag, inner = shlex.split(write)
    assert (shell, flag) == ("sh", "-c")
    assert shlex.split(inner) == [
        "printf",
        "%s",
        "a; reboot",
        ">",
        "/sys/module/qs_wordcount/parameters/line",
    ]
    assert read == "cat /sys/module/qs_wordcount/parameters/words"


@pytest.mark.asyncio
async def test_the_helper_refuses_a_failed_write_and_never_reads(monkeypatch):
    helper = _helper()
    failed = CommandResult(Status.Failed, value="Permission denied\n", command="", retcode=1)
    dut, calls = _dut(failed)
    monkeypatch.setattr(helper, "get_host", lambda _name: dut)
    with pytest.raises(helper.ModuleCallError, match=r"'one'.*Permission denied"):
        await helper.count_words("one")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_helper_refuses_a_count_that_is_not_a_number(monkeypatch):
    helper = _helper()
    dut, calls = _dut(_ok(""), _ok("garbled\n"))
    monkeypatch.setattr(helper, "get_host", lambda _name: dut)
    with pytest.raises(helper.ModuleCallError, match=r"words held 'garbled\\n', not a count"):
        await helper.count_words("one")
    assert len(calls) == 2


def _kmod_captures() -> list[rdc.Capture]:
    return [c for c in rdc.load_manifest(rdc.DEFAULT_MANIFEST) if c.project == "quickstart-kmod"]


def test_every_kmod_store_assertion_names_its_marker():
    caps = _kmod_captures()
    asserted = [a for c in caps for a in c.store]
    assert asserted, "the kernel-module track asserts no hit counts"
    assert all(a.marker and a.marker.startswith("// qs: ") for a in asserted)
    rdc.verify_markers(caps, rdc.EXAMPLES)
    assert {s.tier for c in caps for s in c.store_sources} == {"system", "manual", "unit"}


def test_the_init_routine_counts_only_when_the_module_loads_after_the_clean():
    """D2: the manual tier (cov clean after the load) drops init(); the e2e run has it.

    The e2e run starts with the module unloaded, so its ensure loads it after the clean.
    """
    init = {a.tier: a.hits for c in _kmod_captures() for a in c.store if a.marker == "// qs: init"}
    assert init == {"manual": 0, "system": 1}


def test_the_kmod_set_is_bed_only_and_cleans_stale_module_output_before_building():
    caps = _kmod_captures()
    assert caps
    assert all(not c.labless for c in caps)
    ids = [c.id for c in caps]
    assert ids.index("qs-kmod-module-clean") < ids.index("qs-kmod-build-module")
    assert ids.index("qs-kmod-build-module") < ids.index("qs-kmod-tree-clean")
    assert ids.index("qs-kmod-uninstall") < ids.index("qs-kmod-test-cov")
