"""Host fingerprint: one batched probe, parsed defensively (spec 2026-09-24 §3.3)."""

import pytest

from otto.check.errors import CheckHostUnreachableError
from otto.check.fingerprint import (
    LINK_TOOLS,
    LINK_VERSIONS,
    fingerprint_command,
    parse_fingerprint,
    probe_fingerprint,
)

from ._fakes import ScriptedHost

MODERN = """\
kernel=6.8.0-86-generic
isa=aarch64
user=vagrant
tool:tc=1
tool:ip=1
tool:ping=1
tool:socat=1
tool:python3=0
tool:bash=1
netns=1
netem=1
ver:iproute2=tc utility, iproute2-6.1.0, libbpf 1.1.0
"""

OLDOS = """\
kernel=3.10.0-1160.el7.aarch64
isa=aarch64
user=root
tool:tc=1
tool:ip=1
tool:ping=1
tool:socat=0
tool:python3=1
tool:bash=1
netns=1
netem=?
ver:iproute2=tc utility, iproute2-ss170501
"""

BUSYBOX = """\
kernel=5.15.0
isa=armv7l
user=root
tool:tc=1
busybox:tc=1
tool:ip=1
busybox:ip=1
tool:ping=1
busybox:ping=1
tool:socat=0
tool:python3=0
tool:bash=0
netns=0
netem=0
ver:iproute2=BusyBox v1.36.1 (2023-11-01) multi-call binary.
"""


class TestParse:
    def test_modern_gnu_host(self) -> None:
        fp = parse_fingerprint("test1", "10.10.200.11", MODERN)
        assert fp.kernel == "6.8.0-86-generic"
        assert fp.isa == "aarch64"
        assert fp.userland == "gnu"
        assert fp.privileged is False, "only otto's own elevation proves a non-root login can"
        assert fp.netns is True
        assert fp.netem_module is True
        assert fp.tools == {
            "tc": True, "ip": True, "ping": True, "socat": True, "python3": False, "bash": True,
        }  # fmt: skip
        assert fp.versions == {"iproute2": "6.1.0"}
        assert fp.raw == MODERN

    def test_old_date_versioned_iproute2_as_root(self) -> None:
        fp = parse_fingerprint("test3", "10.10.200.13", OLDOS)
        assert fp.versions == {"iproute2": "ss170501"}
        assert fp.privileged is True  # root, no sudo needed
        assert fp.netem_module is None  # "?" = could not tell

    def test_busybox_tc_has_no_iproute2_version(self) -> None:
        fp = parse_fingerprint("bb", "10.0.0.9", BUSYBOX)
        assert fp.userland == "busybox"
        assert fp.versions == {"iproute2": None}
        assert fp.privileged is True
        assert fp.netns is False
        assert fp.netem_module is False, "modinfo said the module does not exist"

    def test_banner_noise_and_missing_lines_are_tolerated(self) -> None:
        fp = parse_fingerprint("h", "1.2.3.4", "Welcome to host!\nisa=x86_64\ngarbage line\n")
        assert fp.isa == "x86_64"
        assert fp.kernel is None
        assert fp.userland == "unknown"
        assert fp.privileged is False
        assert fp.tools == {}


class TestCommand:
    def test_one_batched_command_names_every_tool_and_version(self) -> None:
        cmd = fingerprint_command(LINK_TOOLS, LINK_VERSIONS)
        for tool in LINK_TOOLS:
            assert tool in cmd
        assert "ver:iproute2=" in cmd
        assert "sudo" not in cmd, "privilege is otto's own elevation, not a sudo -n guess"
        assert "echo netem=0" in cmd
        assert "ip netns list" in cmd
        assert 'echo "kernel=$(uname -r)"' in cmd
        assert 'echo "isa=$(uname -m)"' in cmd


class TestProbe:
    @pytest.mark.asyncio
    async def test_probe_runs_one_command_and_parses(self) -> None:
        host = ScriptedHost("test1", ip="10.10.200.11").answer("uname", MODERN)
        fp = await probe_fingerprint(host, tools=LINK_TOOLS, versions=LINK_VERSIONS)
        assert len(host.commands) == 1
        assert fp.host_id == "test1"
        assert fp.address == "10.10.200.11"

    @pytest.mark.asyncio
    async def test_a_down_host_is_a_host_named_error(self) -> None:
        host = ScriptedHost("test9", fail_all=True)
        with pytest.raises(CheckHostUnreachableError, match="test9"):
            await probe_fingerprint(host, tools=LINK_TOOLS, versions=LINK_VERSIONS)


class TestRootRun:
    @pytest.mark.asyncio
    async def test_sudo_unless_root_and_never_raises_on_failure(self) -> None:
        from otto.check.fingerprint import check_root_run

        host = ScriptedHost("test1").answer("tc qdisc", "Error: nope", ok=False)
        result = await check_root_run(host, "tc qdisc replace dev x root netem rate 1mbit")
        assert not result.is_ok
        assert host.sudo_commands == ["tc qdisc replace dev x root netem rate 1mbit"]
        assert host.raw_commands == ["sh -c 'tc qdisc replace dev x root netem rate 1mbit'"]
        root = ScriptedHost("test3", current_user="root")
        await check_root_run(root, "true; false")
        assert root.sudo_commands == []
        assert root.raw_commands == ["sh -c 'true; false'"], "root runs the same shell text"

    @pytest.mark.asyncio
    async def test_down_host_is_host_named(self) -> None:
        from otto.check.fingerprint import check_root_run

        with pytest.raises(CheckHostUnreachableError, match="test9"):
            await check_root_run(ScriptedHost("test9", fail_all=True), "true")


class TestElevation:
    """Privilege is what otto's own elevation achieves, never a ``sudo -n`` guess."""

    @pytest.mark.asyncio
    async def test_a_password_sudo_that_otto_answers_is_root(self) -> None:
        from otto.check.fingerprint import probe_elevation

        host = ScriptedHost("test1").answer("id -u", "[sudo] password for vagrant: \n0\n")
        elevation = await probe_elevation(host)
        assert elevation.ok
        assert host.sudo_commands == ["id -u"], "asked through otto's elevation"

    @pytest.mark.asyncio
    async def test_an_elevation_that_fails_is_not_root_and_keeps_what_it_saw(self) -> None:
        from otto.check.fingerprint import probe_elevation

        said = "sudo: 3 incorrect password attempts"
        host = ScriptedHost("test1").answer("id -u", said, ok=False)
        elevation = await probe_elevation(host)
        assert not elevation.ok
        assert elevation.command == "id -u"
        assert elevation.output == said

    @pytest.mark.asyncio
    async def test_an_elevation_that_lands_on_another_user_is_not_root(self) -> None:
        from otto.check.fingerprint import probe_elevation

        assert not (await probe_elevation(ScriptedHost("t").answer("id -u", "1000\n"))).ok

    @pytest.mark.asyncio
    async def test_a_host_with_no_elevation_mechanism_is_not_root(self) -> None:
        from otto.check.fingerprint import probe_elevation
        from otto.host.errors import UnsupportedOnUserlandError

        class NoMechanism(ScriptedHost):
            async def run(self, cmd: str, sudo: bool = False, **_: object):
                raise UnsupportedOnUserlandError("neither sudo nor su on test1")

        elevation = await probe_elevation(NoMechanism("test1"))
        assert not elevation.ok
        assert elevation.output == "neither sudo nor su on test1"


class TestElevationThatNeverAnswers:
    """A sudo or su waiting for a password the lab doesn't declare must not abort the check."""

    @pytest.mark.asyncio
    async def test_a_prompt_nobody_answers_is_not_root_within_its_own_bound(self) -> None:
        from otto.check.fingerprint import ELEVATION_TIMEOUT_S, probe_elevation
        from otto.result import CommandResult, Results, Status

        seen: list[float | None] = []

        class Prompting(ScriptedHost):
            async def run(self, cmd: str, sudo: bool = False, **kw: object) -> Results:
                timeout = kw.get("timeout")
                seen.append(timeout if isinstance(timeout, float | int) else None)
                return Results.collect(
                    [
                        CommandResult(
                            status=Status.Error,
                            value=f"Command timed out after {timeout}s\n[sudo] password for v:",
                            command=cmd,
                            retcode=-1,
                            timed_out=True,
                        )
                    ]
                )

        elevation = await probe_elevation(Prompting("test1"))
        assert seen == [ELEVATION_TIMEOUT_S]
        assert ELEVATION_TIMEOUT_S < 30, "well under the check's per-command host timeout"
        assert not elevation.ok
        assert elevation.hint is not None
        assert f"no answer within {ELEVATION_TIMEOUT_S:g} s" in elevation.hint
        assert "password the lab does not declare" in elevation.hint
        assert elevation.output is not None
        assert "[sudo] password for v:" in elevation.output

    @pytest.mark.asyncio
    async def test_a_host_that_drops_the_connection_is_still_unreachable(self) -> None:
        from otto.check.fingerprint import probe_elevation

        with pytest.raises(CheckHostUnreachableError, match="test9"):
            await probe_elevation(ScriptedHost("test9", fail_all=True))


class TestNetemProbe:
    """``netem=0`` only where a module tree exists to be missing from; otherwise ``?``."""

    @staticmethod
    def _run(tmp_path: object, *, tree: bool, builtin: bool, modinfo: str | None) -> str:
        import os
        import subprocess
        from pathlib import Path

        from otto.check.fingerprint import _netem_probe

        root = Path(str(tmp_path))
        release = "6.8.0-test"
        stubs = root / "bin"
        stubs.mkdir()
        (stubs / "uname").write_text(f"#!/bin/sh\necho {release}\n")
        if modinfo is not None:
            (stubs / "modinfo").write_text(f"#!/bin/sh\n{modinfo}\n")
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        if tree:
            (root / "lib" / "modules" / release).mkdir(parents=True)
            listed = "kernel/net/sched/sch_netem.ko\n" if builtin else "kernel/fs/ext4.ko\n"
            (root / "lib" / "modules" / release / "modules.builtin").write_text(listed)
        cmd = _netem_probe(sys_root=str(root / "sys"), lib_root=str(root / "lib"))
        out = subprocess.run(
            ["sh", "-c", cmd],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{stubs}:/usr/bin:/bin"},
        )
        return out.stdout.strip()

    def test_a_module_tree_without_netem_is_missing(self, tmp_path: object) -> None:
        assert self._run(tmp_path, tree=True, builtin=False, modinfo="exit 1") == "netem=0"

    def test_no_module_tree_is_unknown_not_missing(self, tmp_path: object) -> None:
        """A monolithic or custom kernel has no tree for modinfo to search."""
        assert self._run(tmp_path, tree=False, builtin=False, modinfo="exit 1") == "netem=?"

    def test_no_modinfo_is_unknown(self, tmp_path: object) -> None:
        assert self._run(tmp_path, tree=True, builtin=False, modinfo=None) == "netem=?"

    def test_a_built_in_module_is_present(self, tmp_path: object) -> None:
        assert self._run(tmp_path, tree=True, builtin=True, modinfo="exit 1") == "netem=1"

    def test_the_real_probe_reads_the_real_paths(self) -> None:
        from otto.check.fingerprint import _netem_probe

        cmd = fingerprint_command(LINK_TOOLS, LINK_VERSIONS)
        assert _netem_probe() in cmd
        assert "/lib/modules/$(uname -r)" in _netem_probe()
        assert "/sys/module/sch_netem" in _netem_probe()
