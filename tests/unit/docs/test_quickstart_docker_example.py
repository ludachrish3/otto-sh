"""The docker track's example keeps the shape its page teaches (spec #594 §3.1, docker track).

The page says: the tools are the shell track's hierarchical tree plus a third
tool, built on the build machine as static binaries so they run on a small
base image; two tools are shell products installed into one compose
container, the third is baked into an image run as a product of its own;
each image has its own build context and its long-running process is its
CMD; one helper sets GCOV_PREFIX_STRIP for all three; the placeholder lab
resolves the default parent with no host; the download holds the project
and nothing built. Each test below pins one of those sentences. The bed
capture set's pins follow, under their own heading.
"""

import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import tomli
import yaml

from otto.config.project import Project
from otto.docker.compose import project_staging_keys, use_case_project
from otto.docker.staging import compose_staging_root
from otto.host.docker_host import staging_root
from otto.host.docker_image_kind import DockerImageProduct
from otto.result import CommandResult
from otto.utils import Status
from scripts import refresh_docs_captures as rdc
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


def _product(name: str) -> dict[str, Any]:
    return next(p for p in SETTINGS["products"] if p["name"] == name)


def _image(name: str) -> dict[str, Any]:
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


# ---- The bed capture set -----------------------------------------------------
# The set runs on the docker host every docker e2e test uses, so it may change
# only what it creates, and must leave the host as it found it. The names it
# creates are resolved here through otto, from the suffix the manifest pins, so
# a change to how otto names a stack or a staging tree turns these red.

SET = "quickstart-docker"
LAB = "example_lab"
L = ["{otto}", "--lab", LAB]
EXEC = [*L, "host", "docker-host", "exec"]
BASELINE = "/tmp/quickstart-docker-baseline"
TAIL = [
    "qs-docker-down",
    "qs-docker-cleanup",
    "qs-docker-sweep",
    "qs-docker-sweep-root",
    "qs-docker-cache-prune",
    "qs-docker-verify-clean",
]
STATUS_CHECKED = ("qs-docker-preflight", "qs-docker-cache-prune", "qs-docker-verify-clean")


def _captures() -> "list[rdc.Capture]":
    return [c for c in rdc.load_manifest(rdc.DEFAULT_MANIFEST) if c.project == SET]


def _cap(cap_id: str) -> "rdc.Capture":
    return next(c for c in _captures() if c.id == cap_id)


def _text(cap_id: str) -> str:
    return _cap(cap_id).argv[-1]


def _suffix() -> str:
    return _cap("qs-docker-up").env["OTTO_COMPOSE_SUFFIX"]


ROOT_SNIPPET = "docker_staging_root"
"""The manifest snippet that names the docker host's private staging root, in ``$s``."""
ROOT = Path("$s")
"""The staging root as the set's texts spell it: the snippet's variable."""
LOGIN = "docsuser"
"""The login the stub ``id`` answers: the shell tests' docker-host account."""


@pytest.fixture
def ours(monkeypatch) -> "dict[str, list[str]]":
    """Every name the set creates on the docker host, as otto resolves it under the set's suffix."""
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", _suffix())
    project = Project(root=EXAMPLE)
    [use_case] = SETTINGS["docker"]["use_cases"]
    banner = _product("banner")
    return {
        "staging": [
            str(compose_staging_root(ROOT, k.key)) for k in project_staging_keys(project, LAB)
        ],
        "label": [f"com.docker.compose.project={use_case_project(LAB, use_case['name'])}"],
        "images": [image.name for image in project.docker_settings.images],
        "container": [banner["container_name"]],
        "cov_root": [str(Path(banner["cov_dir"]).parent)],
    }


def test_every_otto_capture_pins_one_compose_suffix():
    otto = [c for c in _captures() if c.argv[0] == "{otto}"]
    assert otto
    suffix = _suffix()
    assert suffix
    assert suffix != os.environ.get("USER")
    assert [c.id for c in otto if c.env != {"OTTO_COMPOSE_SUFFIX": suffix}] == []


def test_the_set_is_ordered_overlaid_and_holds_the_docker_host_lease():
    manifest = rdc.read_manifest(rdc.DEFAULT_MANIFEST)
    assert SET in manifest.ordered
    assert manifest.overlays[SET] == BED.relative_to(EXAMPLES).as_posix()
    assert manifest.leases.get("docker_host") == [SET]


def test_the_set_starts_with_a_refusing_preflight(ours):
    caps = _captures()
    assert caps[0].id == "qs-docker-preflight"
    assert not caps[0].always
    pre = _text("qs-docker-preflight")
    for name in [n for names in ours.values() for n in names]:
        assert name in pre, name
    assert f"test ! -e {BASELINE} && " in pre
    assert pre.index(f"mkdir {BASELINE}") > pre.rindex("test -z")
    assert "rm " not in pre
    assert "prune" not in pre


def test_the_teardown_is_the_set_s_always_tail():
    caps = _captures()
    assert [c.id for c in caps[-len(TAIL) :]] == TAIL
    assert all(c.always for c in caps[-len(TAIL) :])
    assert not any(c.always for c in caps[: -len(TAIL)])
    ids = [c.id for c in caps]
    for first, second in [
        ("qs-docker-cmake-build", "qs-docker-build-dry"),
        ("qs-docker-build", "qs-docker-up"),
        ("qs-docker-up", "qs-docker-install"),
        ("qs-docker-install", "qs-docker-cov-clean"),
        ("qs-docker-git-commit-manual", "qs-docker-test-cov"),
        ("qs-docker-test-cov", "qs-docker-report"),
    ]:
        assert ids.index(first) < ids.index(second), (first, second)


def test_cleanup_runs_after_down_and_stays_on_this_project():
    """After ``down``, cleanup removes the staging (a stack with containers keeps it).

    Its lab-wide steps stay off: a tunnel or an impairment on the docker host
    may be another session's. The example declares no toolchain, so cleanup's
    host-global toolchain step removes nothing either.
    """
    assert _cap("qs-docker-down").argv == [*L, "docker", "compose", "down", "quickstart"]
    assert _cap("qs-docker-down").record
    cleanup = _cap("qs-docker-cleanup")
    assert cleanup.argv == [*L, "run", "cleanup", "--no-reset-impairments", "--no-remove-tunnels"]
    for lab_file in (EXAMPLE / "lab_data" / "lab.json", BED / "lab_data" / "lab.json"):
        assert "toolchain" not in lab_file.read_text(), lab_file
    assert "toolchain" not in (EXAMPLE / ".otto" / "settings.toml").read_text()


def test_the_sweeps_remove_only_what_otto_leaves(ours):
    sweep = _text("qs-docker-sweep")
    for name in ours["label"] + ours["images"] + ours["container"]:
        assert name in sweep, name
    assert sweep.startswith(f"test -f {BASELINE}/alpine && {{ ")
    assert sweep.endswith("; }")
    for broad in ("prune", "alpine:", "docker rm -f $(docker ps", "/tmp/otto-docker"):
        assert broad not in sweep, broad
    root = _cap("qs-docker-sweep-root")
    assert root.argv[:-1] == [*EXEC, "--sudo"]
    assert root.argv[-1] == f"sh -c 'test -f {BASELINE}/alpine && rm -rf {ours['cov_root'][0]}'"


def test_verify_checks_every_absence_the_preflight_checked():
    """One inventory: VERIFY repeats each of the preflight's checks but the baseline's own."""
    pre = _text("qs-docker-preflight").split(f" && mkdir {BASELINE}")[0].split(" && ")
    verify = _text("qs-docker-verify-clean").split(" && ")
    missing = [c for c in pre if c not in verify and c != f"test ! -e {BASELINE}"]
    assert missing == []


def test_the_staging_root_snippet_is_the_resolver_s_root_for_the_login(tmp_path):
    """The set's texts never spell the root: the snippet is ``staging_root`` of ``id -un``.

    Red if otto's root moves and the snippet stays: the preflight's and the
    verify's checks would test a path otto never stages under, and pass.
    """
    snippet = rdc.read_manifest(rdc.DEFAULT_MANIFEST).snippets[ROOT_SNIPPET]
    assert snippet == f'w=$(id -un) && test -n "$w" && s={staging_root("$w")}'
    done = subprocess.run(
        ["sh", "-c", f'{snippet} && printf "%s" "$s"'],
        env={**os.environ, "PATH": _stubbed_path(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == str(staging_root(LOGIN))


@pytest.mark.parametrize("answer", ["fail", ""])
def test_the_staging_root_snippet_fails_when_the_login_cannot_be_read(tmp_path, answer):
    """No login, no root: the chain stops, so no absence check runs on a root of no one."""
    snippet = rdc.read_manifest(rdc.DEFAULT_MANIFEST).snippets[ROOT_SNIPPET]
    done = subprocess.run(
        ["sh", "-c", f"{snippet} && echo reached"],
        env={**os.environ, "PATH": _stubbed_path(tmp_path), "ID_LOGIN": answer},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "reached" not in done.stdout


CACHE = "private_cache_records"
"""The manifest snippet that decides which build cache records are private."""


def test_one_cache_classification_serves_preflight_prune_and_verify():
    """The three texts name the one snippet; none spells its own inventory."""
    raw = {c["id"]: c["argv"][-1] for c in tomli.loads(rdc.DEFAULT_MANIFEST.read_text())["capture"]}
    for cap_id in STATUS_CHECKED:
        assert raw[cap_id].count(f"{{snippet:{CACHE}}}") == 1, cap_id
        assert "buildx du" not in raw[cap_id], cap_id
    assert [i for i, text in raw.items() if "buildx du" in text] == []


def test_the_cache_classification_lists_private_records_only(tmp_path):
    snippet = rdc.read_manifest(rdc.DEFAULT_MANIFEST).snippets[CACHE]
    done = subprocess.run(
        ["sh", "-c", f"{snippet} && printf '%s\\n' \"$k\""],
        env={
            **os.environ,
            "PATH": _stubbed_path(tmp_path),
            "DOCKER_LOG": str(tmp_path / "docker.log"),
            "DOCKER_CACHE": "c base:shared a lib:shared b",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["a", "b", "c"]


@pytest.mark.parametrize("cap_id", STATUS_CHECKED)
def test_every_docker_query_is_status_checked(cap_id):
    text = _text(cap_id)
    assert text.count("$(docker") == len(re.findall(r"\b\w=\$\(docker", text))
    assert len(re.findall(r"\b\w=\$\(docker[^)]*\)", text)) == len(
        re.findall(r"\b\w=\$\(docker[^)]*\) && ", text)
    )


def test_only_the_captures_that_interleave_two_hosts_are_unordered():
    unordered = [c.id for c in _captures() if c.unordered]
    assert unordered == ["qs-docker-install", "qs-docker-cov-get-manual", "qs-docker-test-cov"]


def test_every_store_assertion_names_its_marker():
    caps = _captures()
    asserted = [a for c in caps for a in c.store]
    assert asserted
    assert all(a.marker and a.marker.startswith("// qs: ") for a in asserted)
    rdc.verify_markers(caps, EXAMPLES)
    assert {s.tier for c in caps for s in c.store_sources} == {"system", "manual", "unit"}
    assert {a.product for c in caps for a in c.store if a.tier == "system"} == {
        "wordcount",
        "shout",
        "banner",
    }


# The shell texts themselves, run here against a stub docker. Every absolute
# /tmp path moves under tmp_path, so nothing on this machine is read or written.
_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_LOG"
if [ -n "$DOCKER_FAIL" ]; then case "$*" in "$DOCKER_FAIL"*) exit 1 ;; esac; fi
if [ -n "$DOCKER_LEFT" ]; then case "$*" in "$DOCKER_LEFT"*) echo deadbeef; exit 0 ;; esac; fi
case "$*" in
  "image inspect"*) echo sha256:alpine ;;
  "buildx du"*)
    # buildx's verbose layout: one block per record, then the totals, whose
    # Shared line is a size. An entry "<id>:shared" is a record marked Shared.
    for entry in $DOCKER_CACHE; do
      shared=false; case "$entry" in *:shared) shared=true ;; esac
      printf 'ID:\t\t%s\nShared:\t\t%s\nSize:\t\t1B\n\n' "${entry%:shared}" "$shared"
    done
    printf 'Shared:\t\t1B\nPrivate:\t0B\nTotal:\t\t1B\n' ;;
esac
exit 0
"""
_ID_STUB = f"""#!/bin/sh
[ "$*" = "-un" ] || exit 2
login=${{ID_LOGIN-{LOGIN}}}
[ "$login" = fail ] && exit 1
printf '%s\\n' "$login"
"""
"""``id -un`` as the docker host answers it: ``$ID_LOGIN`` (``fail`` exits 1), else LOGIN."""


def _stubbed_path(tmp_path: Path) -> str:
    """Put the stub ``docker`` and ``id`` first on a PATH, under *tmp_path*; return that PATH."""
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        for name, text in [("docker", _STUB), ("id", _ID_STUB)]:
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def _on_host(tmp_path: Path, path: str) -> Path:
    """*path* as the shell tests' docker host has it: the stub login's root, /tmp under tmp_path."""
    resolved = path.replace(str(ROOT), str(staging_root(LOGIN)))
    return Path(resolved.replace("/tmp/", f"{tmp_path}/"))


QUERIES = ["ps", "network ls", "image ls", "image inspect", "buildx du"]
"""Each kind of docker query the texts make; a stub fails the calls that start with one."""
NAMED = [
    "ps -aq --filter label",
    "ps -aq --filter name",
    "network ls",
    "image ls -q quickstart-runtime",
    "image ls -q quickstart-banner",
]
"""Each query that finds one of the set's names; a stub answers it with an id."""


def _sh(tmp_path: Path, cap_id: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the capture's shell text against the stub docker and id, with /tmp under *tmp_path*."""
    path = _stubbed_path(tmp_path)
    text = _text(cap_id)
    if text.startswith("sh -c '"):
        text = text.removeprefix("sh -c '").removesuffix("'")
    run_env = {
        **os.environ,
        "PATH": path,
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "DOCKER_FAIL": "",
        "DOCKER_LEFT": "",
        "DOCKER_CACHE": "",
        **env,
    }
    return subprocess.run(
        ["sh", "-c", text.replace("/tmp/", f"{tmp_path}/")],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(tmp_path: Path) -> "list[str]":
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def _baseline(tmp_path: Path) -> Path:
    return tmp_path / Path(BASELINE).name


def test_the_preflight_writes_the_baseline_when_everything_is_absent(tmp_path):
    done = _sh(tmp_path, "qs-docker-preflight", DOCKER_CACHE="b a")
    assert done.returncode == 0, done.stderr
    assert (_baseline(tmp_path) / "alpine").read_text() == "sha256:alpine\n"
    assert (_baseline(tmp_path) / "cache").read_text() == "a\nb\n"


@pytest.mark.parametrize("query", QUERIES)
def test_the_preflight_refuses_a_failed_query(tmp_path, query):
    done = _sh(tmp_path, "qs-docker-preflight", DOCKER_FAIL=query)
    assert done.returncode != 0
    assert any(call.startswith(query) for call in _calls(tmp_path))
    assert not _baseline(tmp_path).exists()


@pytest.mark.parametrize("query", NAMED)
def test_the_preflight_refuses_a_name_already_present(tmp_path, query):
    done = _sh(tmp_path, "qs-docker-preflight", DOCKER_LEFT=query)
    assert done.returncode != 0
    assert not _baseline(tmp_path).exists()


def test_the_preflight_refuses_a_path_already_present(tmp_path, ours):
    for path in [BASELINE, *ours["staging"], *ours["cov_root"]]:
        target = _on_host(tmp_path, path)
        target.mkdir(parents=True)
        done = _sh(tmp_path, "qs-docker-preflight")
        assert done.returncode != 0, path
        assert _calls(tmp_path) == [], path
        target.rmdir()


def _verified(
    tmp_path: Path, stub: str = _STUB, left: "Path | None" = None, **env: str
) -> subprocess.CompletedProcess[str]:
    """Take a baseline with the plain stub, then run VERIFY with *stub* and *env*.

    *left*, a directory, is made between the two, as a run would leave it.
    """
    shutil.rmtree(_baseline(tmp_path), ignore_errors=True)
    assert _sh(tmp_path, "qs-docker-preflight", DOCKER_CACHE="a").returncode == 0
    (tmp_path / "docker.log").unlink()
    if left is not None:
        left.mkdir(parents=True)
    (tmp_path / "bin" / "docker").write_text(stub)
    try:
        return _sh(tmp_path, "qs-docker-verify-clean", **{"DOCKER_CACHE": "a", **env})
    finally:
        (tmp_path / "bin" / "docker").write_text(_STUB)


def test_verify_passes_a_clean_host_and_only_then_drops_the_baseline(tmp_path):
    done = _verified(tmp_path)
    assert done.returncode == 0, done.stderr
    assert not _baseline(tmp_path).exists()


@pytest.mark.parametrize("query", QUERIES)
def test_verify_fails_when_a_docker_query_fails(tmp_path, query):
    """A failed query is never certified clean, and the baseline stays for the next run."""
    done = _verified(tmp_path, DOCKER_FAIL=query)
    assert done.returncode != 0
    assert any(call.startswith(query) for call in _calls(tmp_path))
    assert _baseline(tmp_path).exists()


@pytest.mark.parametrize("query", NAMED)
def test_verify_fails_on_a_name_left_behind(tmp_path, query):
    done = _verified(tmp_path, DOCKER_LEFT=query)
    assert done.returncode != 0
    assert _baseline(tmp_path).exists()


def test_verify_fails_on_a_path_left_behind(tmp_path, ours):
    for path in [*ours["staging"], *ours["cov_root"]]:
        target = _on_host(tmp_path, path)
        assert _verified(tmp_path, left=target).returncode != 0, path
        target.rmdir()


def test_verify_fails_on_a_new_cache_record(tmp_path):
    assert _verified(tmp_path, DOCKER_CACHE="a z").returncode != 0


def test_verify_fails_on_a_changed_base_image(tmp_path):
    assert _verified(tmp_path, stub=_STUB.replace("sha256:alpine", "sha256:other")).returncode != 0


@pytest.mark.parametrize("cap_id", ["qs-docker-sweep", "qs-docker-sweep-root"])
def test_a_sweep_without_a_baseline_touches_nothing(tmp_path, ours, cap_id):
    cov_root = _on_host(tmp_path, ours["cov_root"][0])
    cov_root.mkdir(parents=True)
    done = _sh(tmp_path, cap_id)
    assert done.returncode != 0
    assert _calls(tmp_path) == []
    assert cov_root.exists()


def test_the_sweeps_remove_this_set_s_names_after_a_baseline(tmp_path, ours):
    assert _sh(tmp_path, "qs-docker-preflight").returncode == 0
    (tmp_path / "docker.log").unlink()
    cov_root = _on_host(tmp_path, ours["cov_root"][0])
    cov_root.mkdir(parents=True)
    assert _sh(tmp_path, "qs-docker-sweep", DOCKER_LEFT="ps").returncode == 0
    removed = [c for c in _calls(tmp_path) if c.startswith(("rm", "network rm", "image rm"))]
    assert removed == [
        "rm -f deadbeef",
        "rm -f deadbeef",
        f"image rm {' '.join(f'{name}:latest' for name in ours['images'])}",
    ]
    assert _sh(tmp_path, "qs-docker-sweep-root").returncode == 0
    assert not cov_root.exists()


def test_prune_removes_only_the_cache_records_the_run_added(tmp_path):
    assert _sh(tmp_path, "qs-docker-preflight", DOCKER_CACHE="a c").returncode == 0
    (tmp_path / "docker.log").unlink()
    done = _sh(tmp_path, "qs-docker-cache-prune", DOCKER_CACHE="a b c d")
    assert done.returncode == 0, done.stderr
    pruned = [c for c in _calls(tmp_path) if c.startswith("builder prune")]
    assert pruned == ["builder prune -f --filter id=b", "builder prune -f --filter id=d"]


def test_a_shared_cache_record_is_neither_pruned_nor_counted(tmp_path):
    """A record marked Shared holds an image's data, the base image's after a buildx install.

    No prune frees it, so PRUNE leaves it and VERIFY does not count it, even
    as the last record, where the totals' own Shared line follows it.
    """
    assert _verified(tmp_path, DOCKER_CACHE="a base:shared").returncode == 0
    shutil.rmtree(_baseline(tmp_path), ignore_errors=True)
    assert _sh(tmp_path, "qs-docker-preflight", DOCKER_CACHE="a").returncode == 0
    (tmp_path / "docker.log").unlink()
    done = _sh(tmp_path, "qs-docker-cache-prune", DOCKER_CACHE="a b base:shared")
    assert done.returncode == 0, done.stderr
    pruned = [c for c in _calls(tmp_path) if c.startswith("builder prune")]
    assert pruned == ["builder prune -f --filter id=b"]


def test_prune_fails_without_a_baseline_or_on_a_failed_prune(tmp_path):
    assert _sh(tmp_path, "qs-docker-cache-prune", DOCKER_CACHE="b").returncode != 0
    assert _calls(tmp_path) == []
    assert _sh(tmp_path, "qs-docker-preflight").returncode == 0
    done = _sh(tmp_path, "qs-docker-cache-prune", DOCKER_CACHE="b d", DOCKER_FAIL="builder prune")
    assert done.returncode != 0
    assert [c for c in _calls(tmp_path) if c.startswith("builder prune")] == [
        "builder prune -f --filter id=b",
        "builder prune -f --filter id=d",
    ]


# The whole manifest through the real runner, with a stub in place of each
# command: the preflight's shell text runs against the stub docker, every other
# capture just succeeds or fails as told. The set's teardown is armed only by
# this run's own successful preflight, so a refused preflight, or a set that
# never started, removes nothing.


def _run_manifest(tmp_path: Path, monkeypatch, *, fail: str = "", **env: str) -> "list[str]":
    ran: list[str] = []

    def stub(cap: "rdc.Capture", ctx: "rdc.RunContext") -> str:
        ran.append(cap.id)
        if cap.id == "qs-docker-preflight":
            done = _sh(tmp_path, cap.id, **env)
            if done.returncode != 0:
                raise rdc.CaptureError(f"capture {cap.id!r} exited {done.returncode}")
        elif cap.id == fail:
            raise rdc.CaptureError(f"capture {cap.id!r} exited 1")
        return f"$ {cap.id}\n"

    monkeypatch.setattr(rdc, "run_capture", stub)
    ctx = rdc.RunContext(examples_root=EXAMPLES, captures_dir=tmp_path / "out", tmp=tmp_path)
    with pytest.raises(rdc.CaptureError):
        rdc.refresh(rdc.load_manifest(rdc.DEFAULT_MANIFEST), ctx)
    return ran


def _ours_ran(ran: "list[str]") -> "list[str]":
    ids = {c.id for c in _captures()}
    return [i for i in ran if i in ids]


def test_a_refused_preflight_runs_none_of_the_teardown(tmp_path, monkeypatch, capsys):
    ran = _run_manifest(tmp_path, monkeypatch, DOCKER_LEFT="ps -aq --filter label")
    assert _ours_ran(ran) == ["qs-docker-preflight"]
    err = capsys.readouterr().err
    for cap_id in TAIL:
        assert f"skipped {cap_id}" in err, cap_id


def test_a_stale_baseline_refuses_the_preflight_and_runs_none_of_the_teardown(
    tmp_path, monkeypatch
):
    stale = _baseline(tmp_path)
    stale.mkdir()
    (stale / "alpine").write_text("sha256:alpine\n")
    (stale / "cache").write_text("a\n")
    ran = _run_manifest(tmp_path, monkeypatch)
    assert _ours_ran(ran) == ["qs-docker-preflight"]
    assert _calls(tmp_path) == []
    assert (stale / "cache").read_text() == "a\n"


def test_a_failure_before_the_set_starts_runs_none_of_its_teardown(tmp_path, monkeypatch):
    every = rdc.load_manifest(rdc.DEFAULT_MANIFEST)
    first = next(i for i, c in enumerate(every) if c.project == SET)
    earlier = every[first - 1]
    assert earlier.project != SET
    ran = _run_manifest(tmp_path, monkeypatch, fail=earlier.id)
    assert _ours_ran(ran) == []
    assert _calls(tmp_path) == []


def test_a_failure_after_the_preflight_runs_the_whole_teardown(tmp_path, monkeypatch):
    ran = _run_manifest(tmp_path, monkeypatch, fail="qs-docker-up")
    ours = _ours_ran(ran)
    assert ours[: ours.index("qs-docker-up") + 1][-1] == "qs-docker-up"
    assert ours[ours.index("qs-docker-up") + 1 :] == TAIL
