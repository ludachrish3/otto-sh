"""The docker track's example keeps the shape its page teaches (spec #594 §3.1, docker track).

The page says: the tools are the shell track's hierarchical tree plus a third
tool, built on the build machine as static binaries so they run on a small
base image; two tools are shell products installed into one compose
container, the third is baked into an image run as a product of its own;
each image has its own build context and its long-running process is its
CMD; one helper sets GCOV_PREFIX_STRIP for all three; the placeholder lab
resolves the default parent with no host; the download holds the project
and nothing built. Each test below pins one of those sentences.
"""

import importlib.util
import ipaddress
import json
import re
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import tomli
import yaml

from otto.host.docker_image_kind import DockerImageProduct
from otto.result import CommandResult
from otto.utils import Status
from scripts.build_example_archives import build
from tests._fixtures._host_pool import DOCKER_HOST
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

EXAMPLES = CHECKOUT_ROOT / "docs" / "examples"
EXAMPLE = EXAMPLES / "quickstart-docker"
BED = EXAMPLES / "bed" / "quickstart-docker"
BASE_IMAGE = "alpine:3.20"  # the base the bed's docker host holds; the docker e2e builds on it too
SETTINGS = tomli.loads((EXAMPLE / ".otto" / "settings.toml").read_text())
CONTAINER = "docker-host.quickstart.tools"
CONTAINER_PRODUCTS = ("wordcount", "shout")
REQUIRED = [
    ".gitignore",
    ".otto/settings.toml",
    "CMakeLists.txt",
    "lab_data/lab.json",
    "docker/runtime/Dockerfile",
    "docker/runtime/compose.yml",
    "docker/banner/Dockerfile",
    "src/textlib/text.cpp",
    "src/textlib/text.h",
    "src/textlib/CMakeLists.txt",
    "src/wordcount/main.cpp",
    "src/wordcount/CMakeLists.txt",
    "src/shout/main.cpp",
    "src/shout/CMakeLists.txt",
    "src/banner/main.cpp",
    "src/banner/CMakeLists.txt",
    "tests/unit/CMakeLists.txt",
    "tests/unit/test_text.cpp",
    "tests/test_tools.py",
    "pylib/quickstart_docker/__init__.py",
    "pylib/quickstart_docker/tools.py",
]


def _product(name: str) -> dict:
    return next(p for p in SETTINGS["products"] if p["name"] == name)


def _image(name: str) -> dict:
    return next(i for i in SETTINGS["docker"]["images"] if i["name"] == name)


def test_the_tools_are_the_shell_track_s_tree_plus_banner():
    shell = EXAMPLES / "quickstart-shell"
    for rel in [
        "src/textlib/text.cpp",
        "src/textlib/text.h",
        "src/wordcount/main.cpp",
        "src/shout/main.cpp",
        "tests/unit/test_text.cpp",
        "tests/unit/CMakeLists.txt",
    ]:
        assert (EXAMPLE / rel).read_bytes() == (shell / rel).read_bytes(), rel
    banner = (EXAMPLE / "src" / "banner" / "CMakeLists.txt").read_text()
    assert "target_link_libraries(banner PRIVATE textlib)" in banner


def test_every_product_links_statically():
    cmake = (EXAMPLE / "CMakeLists.txt").read_text()
    for target in ("wordcount", "shout", "banner"):
        assert f"target_link_options({target} PRIVATE -static)" in cmake, target


def test_googletest_comes_from_the_build_machine_when_installed():
    cmake = (EXAMPLE / "CMakeLists.txt").read_text()
    assert "FIND_PACKAGE_ARGS NAMES GTest" in cmake
    # Coverage flags come after GoogleTest is made available, so its sources stay uninstrumented.
    assert cmake.index("FetchContent_MakeAvailable") < cmake.index("add_compile_options(--coverage")


def test_every_image_builds_on_the_one_base():
    for name in ("quickstart-runtime", "quickstart-banner"):
        dockerfile = (EXAMPLE / _image(name)["dockerfile"]).read_text()
        assert re.findall(r"(?m)^FROM (\S+)", dockerfile) == [BASE_IMAGE], name


def test_each_image_s_long_running_process_is_its_cmd():
    for name in ("quickstart-runtime", "quickstart-banner"):
        dockerfile = (EXAMPLE / _image(name)["dockerfile"]).read_text()
        assert 'CMD ["sleep", "infinity"]' in dockerfile, name
    [compose] = SETTINGS["docker"]["composes"]
    services = yaml.safe_load((EXAMPLE / compose["path"]).read_text())["services"]
    # No command or entrypoint: either could background a process with `&`.
    assert services == {"tools": {"image": "quickstart-runtime:latest", "pull_policy": "never"}}


def test_every_image_has_a_context_of_its_own():
    """Each image has a context of its own, never the project root nor a bare docker/."""
    contexts = [image["context"] for image in SETTINGS["docker"]["images"]]
    assert len(set(contexts)) == len(contexts), contexts
    for context in contexts:
        assert Path(context).parts[:1] in (("docker",), ("build",)), context
        assert len(Path(context).parts) >= 2, context


def test_the_singleton_image_copies_what_the_build_puts_in_its_context():
    image = _image("quickstart-banner")
    copies = re.findall(r"(?m)^COPY (\S+) (\S+)$", (EXAMPLE / image["dockerfile"]).read_text())
    [(src, dest)] = copies
    assert (Path(image["context"]) / src).as_posix() == "build/src/banner/banner"
    assert dest == "/opt/quickstart/banner"
    assert _product("banner")["image"] == "quickstart-banner:latest"


def test_both_shell_products_match_the_compose_service_s_host_id():
    # CONTAINER is the host otto registers for service tools of use-case quickstart.
    [use_case] = SETTINGS["docker"]["use_cases"]
    [compose] = SETTINGS["docker"]["composes"]
    assert use_case["name"] == "quickstart"
    assert compose["services"] == ["tools"]
    for name in CONTAINER_PRODUCTS:
        assert _product(name)["kind"] == "shell"
        assert re.fullmatch(_product(name)["match"]["id"], CONTAINER), name
    assert re.fullmatch(_product("banner")["match"]["id"], "docker-host")
    assert not re.fullmatch(_product("banner")["match"]["id"], CONTAINER)


def test_the_coverage_pattern_names_the_parent_and_the_container_only():
    pattern = SETTINGS["coverage"]["hosts"]
    assert re.fullmatch(pattern, "docker-host")
    assert re.fullmatch(pattern, CONTAINER)
    assert not re.fullmatch(pattern, "docker-host.quickstart.other")


@pytest.mark.parametrize(
    "lab_file",
    [EXAMPLE / "lab_data" / "lab.json", BED / "lab_data" / "lab.json"],
    ids=["placeholder", "bed"],
)
def test_the_lab_has_exactly_one_docker_capable_host(lab_file):
    hosts = [h for e in json.loads(lab_file.read_text())["elements"] for h in e["hosts"]]
    assert [h.get("docker_capable") for h in hosts] == [True]


def test_the_placeholder_lab_holds_no_bed_facts():
    text = (EXAMPLE / "lab_data" / "lab.json").read_text()
    assert "vagrant" not in text
    for ip in re.findall(r'"ip": "([\d.]+)"', text):
        assert ipaddress.ip_address(ip) in ipaddress.ip_network("192.0.2.0/24"), ip


def test_the_bed_overlay_is_the_host_the_docker_lease_guards():
    bed_lab = json.loads((BED / "lab_data" / "lab.json").read_text())
    tech1 = json.loads((CHECKOUT_ROOT / "tests/_fixtures/lab_data/tech1/lab.json").read_text())
    [leased] = [e for e in tech1["elements"] if e["name"] == DOCKER_HOST]
    [bed_host] = [h for e in bed_lab["elements"] for h in e["hosts"]]
    assert bed_host["ip"] == leased["hosts"][0]["ip"]


def test_the_bed_overlay_holds_exactly_the_lab():
    held = sorted(p.relative_to(BED).as_posix() for p in BED.rglob("*") if p.is_file())
    assert held == ["lab_data/lab.json"]


def test_the_e2e_helper_derives_the_strip_count_from_the_project_root():
    source = (EXAMPLE / "pylib" / "quickstart_docker" / "tools.py").read_text()
    assert "len(PROJECT_ROOT.parts) - 1" in source
    for path in [
        EXAMPLE / "pylib" / "quickstart_docker" / "tools.py",
        *(EXAMPLE / "tests").glob("*.py"),
    ]:
        assert not re.search(r"GCOV_PREFIX_STRIP=\d", path.read_text()), path


def test_run_output_is_never_committed():
    assert "otto-output/" in (EXAMPLE / ".gitignore").read_text().splitlines()


def test_the_archive_holds_every_required_file_and_nothing_built(tmp_path):
    archives = build(EXAMPLES, tmp_path, checkout=CHECKOUT_ROOT)
    built = next((p for p in archives if p.name == "quickstart-docker.tar.gz"), None)
    assert built is not None, [p.name for p in archives]
    with tarfile.open(built) as tar:
        names = set(tar.getnames())
    missing = [r for r in REQUIRED if f"quickstart-docker/{r}" not in names]
    assert not missing, missing
    assert not [n for n in names if "/build/" in n or "/bed/" in n or "__pycache__" in n]


def _tools() -> ModuleType:
    """Load the example's tools.py alone, under a name no other test imports."""
    path = EXAMPLE / "pylib" / "quickstart_docker" / "tools.py"
    spec = importlib.util.spec_from_file_location("qs_docker_tools_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hosts(result: CommandResult, seen: "list[tuple[str, str]]"):
    """A ``get_host`` double: the compose container and its parent, each logging what it runs."""

    def host(host_id: str, products: "list[object]") -> SimpleNamespace:
        async def exec_(cmd: str) -> CommandResult:
            seen.append((host_id, cmd))
            return result

        return SimpleNamespace(products=products, exec=exec_)

    banner = DockerImageProduct(
        artifact=Path("quickstart-banner:latest"),
        name="banner",
        image="quickstart-banner:latest",
        container_name="quickstart-banner",
        cov_dir="/tmp/quickstart-docker/banner",
    )
    banner.kind = "docker_image"  # as ingest stamps a declared entry
    by_id = {
        CONTAINER: host(
            CONTAINER,
            [
                SimpleNamespace(name=n, kind="shell", cov_dir=f"/tmp/{n}")
                for n in CONTAINER_PRODUCTS
            ],
        ),
        "docker-host": host(
            "docker-host",
            [banner],
        ),
    }
    return by_id.__getitem__


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CONTAINER_PRODUCTS)
async def test_the_helper_runs_a_container_product_in_its_container(monkeypatch, name):
    tools = _tools()
    seen: "list[tuple[str, str]]" = []
    ok = CommandResult(Status.Success, value="out\n", command="", retcode=0)
    monkeypatch.setattr(tools, "get_host", _hosts(ok, seen))
    assert await tools.run_tool(name, "a b") == "out"
    assert seen == [
        (
            CONTAINER,
            f"GCOV_PREFIX=/tmp/{name} GCOV_PREFIX_STRIP={tools.STRIP} /opt/quickstart/{name} a b",
        )
    ]


@pytest.mark.asyncio
async def test_the_helper_runs_the_image_product_through_docker_exec_on_the_parent(monkeypatch):
    tools = _tools()
    seen: "list[tuple[str, str]]" = []
    ok = CommandResult(Status.Success, value="[2] a b\n", command="", retcode=0)
    monkeypatch.setattr(tools, "get_host", _hosts(ok, seen))
    assert await tools.run_tool("banner", "a b") == "[2] a b"
    assert seen == [
        (
            "docker-host",
            (
                f"docker exec -e GCOV_PREFIX_STRIP={tools.STRIP} quickstart-banner "
                "/opt/quickstart/banner a b"
            ),
        )
    ]


@pytest.mark.asyncio
async def test_the_helper_refuses_a_tool_that_failed(monkeypatch):
    tools = _tools()
    failed = CommandResult(
        Status.Failed, value="boom\n", command="/opt/quickstart/shout x", retcode=2
    )
    monkeypatch.setattr(tools, "get_host", _hosts(failed, []))
    with pytest.raises(
        tools.ToolFailedError, match=rf"shout x.*failed on {CONTAINER}.*exit code 2"
    ):
        await tools.run_tool("shout", "x")


@pytest.mark.asyncio
async def test_the_helper_names_a_product_neither_host_has(monkeypatch):
    tools = _tools()
    seen: "list[tuple[str, str]]" = []
    ok = CommandResult(Status.Success, value="", command="", retcode=0)
    monkeypatch.setattr(tools, "get_host", _hosts(ok, seen))
    with pytest.raises(LookupError, match=rf"'nope'.*{CONTAINER}.*docker-host"):
        await tools.run_tool("nope", "x")
    assert seen == []
