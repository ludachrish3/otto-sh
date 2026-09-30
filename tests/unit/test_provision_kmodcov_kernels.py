"""scripts/provision_kmodcov_kernels.sh: the pin table the kmodcov build columns stand on.

Only the parts a machine with neither docker nor the network can check: the
usage text (``--help`` and an unknown flag), ``--list`` (its shape, that it
agrees with the Makefile default, and that it needs no tool), the refusal of
an unknown id, the refusal of a missing tool before anything is fetched, the
remedy named for a rotted pin, and the binfmt check 2.6.32 needs before any
network fetch. The provisioning itself is dev-VM work and is proven by
`make kmodcov` building against what it left behind.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SCRIPT = PROJECT_ROOT / "scripts" / "provision_kmodcov_kernels.sh"
CROSS = "x86_64-cross"


def _run(args, *, path: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *args],
        env={"PATH": path, "OTTO_KMODCOV_KERNELS_DIR": "/nonexistent/kmodcov-kernels"},
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def makefile_kernel_ids() -> list[str]:
    text = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"^KMODCOV_KERNELS \?= (.+)$", text, re.MULTILINE)
    assert m, "the Makefile declares no `KMODCOV_KERNELS ?= …` line"
    return [k.strip() for k in m.group(1).split(",") if k.strip()]


def listed(path: str = "") -> list[list[str]]:
    result = _run(["--list"], path=path)
    assert result.returncode == 0, result.stderr
    return [line.split() for line in result.stdout.splitlines() if line.strip()]


def test_the_script_is_executable():
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_list_needs_neither_the_network_nor_docker(tmp_path: Path):
    """An empty PATH: every external command would fail, so a green here is a pure-bash list."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    rows = listed(path=str(empty))
    assert rows, "--list printed nothing"


def test_list_prints_id_image_compiler_and_tree_per_line():
    for row in listed():
        assert len(row) == 4, row
        kernel_id, image, cc, tree = row
        assert image == f"otto-kmodcov-kernel:{kernel_id}", row
        assert cc.startswith("gcc"), row
        assert not tree.startswith("/"), f"{tree} is not relative to <id>/"


def test_list_ids_equal_the_makefile_default_minus_the_cross_build():
    assert [row[0] for row in listed()] == [k for k in makefile_kernel_ids() if k != CROSS]


def test_the_2_6_32_column_builds_with_gcc_4_7_and_the_headers_columns_with_the_release_gcc():
    by_id = {row[0]: row for row in listed()}
    assert by_id["2.6.32"][2] == "gcc-4.7"
    assert by_id["2.6.32"][3] == "src/linux-2.6.32.71"
    for kernel_id, row in by_id.items():
        if kernel_id != "2.6.32":
            assert row[2] == "gcc", row
            assert re.fullmatch(r"root/usr/src/linux-headers-\d+\.\d+\.\d+-\d+-generic", row[3]), (
                row
            )


def test_an_unknown_id_is_refused_naming_the_known_ones(tmp_path: Path):
    # Hermetic PATH, not the ambient one: the refusal text is built with `tr`, but /bin:/usr/bin
    # also carries a real curl and docker, which no test in this file should see.
    bin_dir = _bin_dir_with_real_tools(tmp_path, ("tr",))
    result = _run(["5.10"], path=str(bin_dir))
    assert result.returncode == 1
    assert "5.10" in result.stderr
    for kernel_id in [row[0] for row in listed()]:
        assert kernel_id in result.stderr


def test_a_missing_tool_is_refused_before_anything_is_fetched(tmp_path: Path):
    """PATH with every tool but docker: the refusal names docker and its package, and no
    download directory appears."""
    bin_dir = _bin_dir_with_real_tools(
        tmp_path, ("sha256sum", "dpkg", "tar", "xz", "mkdir", "cat", "sed", "cp", "rm")
    )
    _stub_tool(bin_dir, "curl")
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "3.13"],
        env={"PATH": str(bin_dir), "OTTO_KMODCOV_KERNELS_DIR": str(tmp_path / "kernels")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "docker" in result.stderr, result.stderr
    assert "docker.io" in result.stderr, result.stderr
    assert not (tmp_path / "kernels").exists()


def test_a_bad_flag_prints_usage(tmp_path: Path):
    # Hermetic PATH, not the ambient one: usage_text builds the id list with `tr`, so a missing
    # `tr` must fail this test rather than pass on the bare "usage:" text.
    bin_dir = _bin_dir_with_real_tools(tmp_path, ("tr",))
    result = _run(["--frobnicate"], path=str(bin_dir))
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert any(kernel_id in result.stderr for kernel_id in makefile_kernel_ids())


def test_help_prints_usage_to_stdout_and_exits_0(tmp_path: Path):
    bin_dir = _bin_dir_with_real_tools(tmp_path, ("tr",))
    result = _run(["--help"], path=str(bin_dir))
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert any(kernel_id in result.stdout for kernel_id in makefile_kernel_ids())


def _bin_dir_with_real_tools(tmp_path: Path, tools: tuple[str, ...]) -> Path:
    """Symlink *tools* off the plain coreutils path (``os.defpath``), never the test's own
    ambient PATH: this file's own hermeticity proof runs it with PATH pointed nowhere.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in tools:
        real = shutil.which(tool, path=os.defpath)
        assert real, f"{tool} is not on {os.defpath}"
        (bin_dir / tool).symlink_to(real)
    return bin_dir


def _stub_tool(bin_dir: Path, name: str) -> None:
    """A fake *name* on *bin_dir*'s PATH: `require` only checks presence, never runs it, and the
    tests below never reach a point that runs it either."""
    stub = bin_dir / name
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)


def test_a_rotted_pin_names_the_re_pin_remedy(tmp_path: Path):
    """A pin whose file Ubuntu's pool has since dropped gets curl's own 404, but the script
    must say what to do about it, not just relay curl's message."""
    bin_dir = _bin_dir_with_real_tools(
        tmp_path,
        ("sha256sum", "dpkg", "tar", "xz", "mkdir", "cat", "sed", "cp", "rm", "basename"),
    )
    _stub_tool(bin_dir, "docker")
    fake_curl = bin_dir / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 22\n")
    fake_curl.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "3.13"],
        env={"PATH": str(bin_dir), "OTTO_KMODCOV_KERNELS_DIR": str(tmp_path / "kernels")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "could not fetch" in result.stderr, result.stderr
    assert "re-pin in" in result.stderr, result.stderr
    assert "never skip" in result.stderr, result.stderr


def test_2_6_32_without_the_binfmt_registered_is_refused_before_any_docker_work(tmp_path: Path):
    """2.6.32 builds amd64 under emulation; docker is stubbed present here, so this proves only
    that the check runs before fetching, not before requiring docker, and that it names the
    packages that register the binfmt."""
    bin_dir = _bin_dir_with_real_tools(
        tmp_path,
        ("sha256sum", "dpkg", "tar", "xz", "mkdir", "cat", "sed", "cp", "rm"),
    )
    _stub_tool(bin_dir, "docker")
    _stub_tool(bin_dir, "curl")
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "2.6.32"],
        env={
            "PATH": str(bin_dir),
            "OTTO_KMODCOV_KERNELS_DIR": str(tmp_path / "kernels"),
            "OTTO_KMODCOV_BINFMT_CHECK": str(tmp_path / "no-such-binfmt"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "qemu-user-static" in result.stderr, result.stderr
    assert "binfmt-support" in result.stderr, result.stderr
    assert not (tmp_path / "kernels").exists()


def test_2_6_32_with_the_binfmt_registered_proceeds_past_the_check(tmp_path: Path):
    """A present binfmt path does not block 2.6.32: the run reaches the network fetch (and fails
    there, on the fake curl) instead of the binfmt refusal."""
    bin_dir = _bin_dir_with_real_tools(
        tmp_path,
        ("sha256sum", "dpkg", "tar", "xz", "mkdir", "cat", "sed", "cp", "rm", "basename"),
    )
    _stub_tool(bin_dir, "docker")
    fake_curl = bin_dir / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 22\n")
    fake_curl.chmod(0o755)
    present_binfmt = tmp_path / "present-binfmt"
    present_binfmt.write_text("")
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "2.6.32"],
        env={
            "PATH": str(bin_dir),
            "OTTO_KMODCOV_KERNELS_DIR": str(tmp_path / "kernels"),
            "OTTO_KMODCOV_BINFMT_CHECK": str(present_binfmt),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "could not fetch" in result.stderr, result.stderr
    assert "qemu-user-static" not in result.stderr, result.stderr


def test_list_agrees_with_the_kernel_column_table_id_by_id():
    """The script's pin table and the lane's KernelColumn table cannot drift apart."""
    from tests.e2e.cov._repo5_build import KERNEL_IDS, kernel_column

    assert [row[0] for row in listed()] == KERNEL_IDS[1:]
    kernels = Path("/k")
    for kernel_id, image, cc, tree in listed():
        column = kernel_column(kernel_id, kernels_dir=kernels, cross_kdir=Path("/x"))
        assert column.image == image, kernel_id
        assert (column.cc or "gcc") == cc, kernel_id
        assert column.tree == kernels / kernel_id / tree, kernel_id
