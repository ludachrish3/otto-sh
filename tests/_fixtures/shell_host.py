"""A host double that runs every command through a real ``sh`` on this machine.

Shared by the counter reset's tests (``tests/unit/host/test_product.py``),
the cleanup sweep's (``test_cov_sweep.py``) and the coverage-loss lifecycle
tests, so all of them drive the same plain and elevated paths over a real
shell and a real ``find``. :func:`install_shell_lab` makes such hosts the
coverage lab.
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

from otto.host.product import ShellProduct
from otto.result import CommandResult, Result, Results
from otto.utils import Status

ELEVATED_MARK = "OTTO_TEST_ELEVATED"
"""Set to ``1`` in the environment of every command the double ran elevated."""


class ShellHost:
    """Host double that runs each command through a real ``sh`` on this machine.

    ``exec`` runs the line as-is. ``run(..., sudo=True)`` models how
    ``otto.host.privilege.PosixPrivilege._elevate`` elevates: sudo takes an
    ARGV TAIL, so the line is appended raw after a prefix — here ``env
    OTTO_TEST_ELEVATED=1``, which (like sudo) execs only the first word of
    what follows. Anything after a top-level ``||`` therefore runs unelevated,
    exactly as it would on a real host. ``bin_dir`` goes first on PATH so a
    test can stand in its own ``find``. ``noise`` is prefixed to every
    command's output, the way a session's merged stream carries what sudo,
    PAM or a login shell prints before the command runs. ``calls`` records
    ``("exec" | "run", command)`` in order.
    """

    def __init__(self, bin_dir: Path | None = None, noise: str = "", host_id: str = "h1"):
        self.id = host_id
        self.calls: list[tuple[str, str]] = []
        self._noise = noise
        self._env = dict(os.environ)
        if bin_dir is not None:
            self._env["PATH"] = f"{bin_dir}{os.pathsep}{self._env.get('PATH', '')}"
        self._env.pop(ELEVATED_MARK, None)

    async def _sh(self, line):
        proc = await asyncio.create_subprocess_shell(
            line,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._env,
        )
        out, _ = await proc.communicate()
        status = Status.Success if proc.returncode == 0 else Status.Error
        value = self._noise + out.decode()
        return CommandResult(status, value=value, command=line, retcode=proc.returncode)

    async def exec(self, cmd, **kwargs):
        self.calls.append(("exec", cmd))
        return await self._sh(cmd)

    async def run(self, cmd, *, sudo=False, **kwargs):
        assert sudo, "the double only models elevated runs"
        self.calls.append(("run", cmd))
        return Results.collect([await self._sh(f"env {ELEVATED_MARK}=1 {cmd}")])


def fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    """Write a *name* stand-in into the test's bin dir and return that dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\n{body}\n")
    tool.chmod(0o755)
    return bin_dir


class CovShellProduct(ShellProduct):
    """An instrumented shell product with the abstract halves stubbed: coverage only."""

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return True


def coverage_shell_host(host_id: str, products: "dict[str, Path]", **kwargs) -> ShellHost:
    """A :class:`ShellHost` carrying one instrumented product per ``{name: cov_dir}``."""
    host = ShellHost(host_id=host_id, **kwargs)
    host.products = [
        CovShellProduct(
            artifact=Path(f"/build/{name}"),
            name=name,
            cov_dir=str(cov_dir),
            instrumented_override=True,
        )
        for name, cov_dir in products.items()
    ]
    return host


def install_shell_lab(monkeypatch, *hosts) -> None:
    """Make *hosts* the coverage lab: the fleet walks, ``[coverage]``, no capture project.

    ``otto.fleet.all_hosts`` and ``do_for_all_hosts`` walk *hosts* (pattern
    applied, sequentially, exceptions returned as values as the real walk
    does); ``[coverage].hosts`` is ``.*``; there is no coverage project, so a
    collection fetches and stops before capture; no embedded board dumps.
    Everything between them (the clean, the fetcher, the archive) is real.
    """

    def fake_all_hosts(pattern=None, **kw):
        return iter([h for h in hosts if pattern is None or pattern.fullmatch(h.id)])

    async def fake_do_for_all(method, *args, pattern=None, **kw):
        out = {}
        for h in hosts:
            if pattern is None or pattern.fullmatch(h.id):
                try:
                    out[h.id] = await method(h, *args)
                except Exception as e:  # noqa: BLE001 — mirrors return_exceptions=True
                    out[h.id] = e
        return out

    monkeypatch.setattr("otto.fleet.all_hosts", fake_all_hosts)
    monkeypatch.setattr("otto.fleet.do_for_all_hosts", fake_do_for_all)
    monkeypatch.setattr(
        "otto.config.coverage_settings.get_cov_config", lambda projects: {"hosts": ".*"}
    )
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_project", lambda projects: None)
    monkeypatch.setattr(
        "otto.coverage.fetcher.embedded.collect_embedded_coverage", AsyncMock(return_value={})
    )
