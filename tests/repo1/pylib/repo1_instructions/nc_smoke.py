"""Manual smoke test for the telnet netcat transfer path.

Exercises a multi-file telnet ``nc`` put + get round-trip against the lab's
telnet host and verifies every file byte-for-byte. Handy whenever the nc /
telnet transfer code is touched and you want a quick real-hardware check
beyond the unit + stability suites.

Run it against the lab (``veggies``)::

    OTTO_SUT_DIRS=<repo1 dir> otto -l veggies run nc-smoke

A pass means the telnet nc control plane (port discovery, listener probes,
file-size stats) and both data paths still work end to end.

To confirm the nc-monitor retirement specifically — that control-plane work
no longer opens a dedicated session — re-run with ``--log-level DEBUG`` and
count the telnet connects::

    grep -c 'via telnet on port' <run-dir>/otto.log

It should track the transfer's pooled-session count (~file count); the
retired design added one extra connect for the standalone ``_nc_monitor``
session on top of that.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from repo1_common.options import RepoOptions

from otto.cli.run import instruction
from otto.configmodule.configmodule import get_host
from otto.host import LocalHost
from otto.logger import getOttoLogger
from otto.utils import CommandStatus, Status

logger = getOttoLogger()


@dataclass
class _Options(RepoOptions):
    host_id: Annotated[str, typer.Option(
        help="Lab host id to target. Must be a telnet host with transfer=nc.",
    )] = "tomato_seed"

    file_count: Annotated[int, typer.Option(
        help="Number of files to transfer (exercises concurrent fan-out).",
    )] = 3

    file_mb: Annotated[int, typer.Option(
        help="Size of each generated file, in MiB.",
    )] = 5


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


@instruction(options=_Options)
async def nc_smoke(opts: _Options) -> CommandStatus:
    """Round-trip files over telnet netcat and verify them byte-for-byte."""

    def _result(ok: bool, detail: str) -> CommandStatus:
        return CommandStatus(
            command='nc-smoke',
            output=detail,
            status=Status.Success if ok else Status.Error,
            retcode=0 if ok else 1,
        )

    local = LocalHost()
    work = logger.output_dir / 'nc_smoke'
    src_dir = work / 'src'
    roundtrip_dir = work / 'roundtrip'
    src_dir.mkdir(parents=True, exist_ok=True)
    roundtrip_dir.mkdir(parents=True, exist_ok=True)

    # --- generate source files ------------------------------------------------
    src_files: list[Path] = []
    for i in range(opts.file_count):
        f = src_dir / f'nc_smoke_{i}.bin'
        gen = await local.run(
            f'dd if=/dev/urandom of={f} bs=1M count={opts.file_mb} status=none'
        )
        if not gen.status.is_ok:
            return _result(False, f"failed to generate {f}: {gen.output}")
        src_files.append(f)
    logger.info(f"Generated {len(src_files)} x {opts.file_mb} MiB source file(s)")

    # `get_host` returns the lab host; tomato is term=telnet, transfer=nc in
    # the tech1 lab. A fresh instruction run gets a cold host.
    host = get_host(opts.host_id)
    logger.info(
        f"Target {opts.host_id}: term={host.term!r}, transfer={host.transfer!r}"
    )

    remote_dir = Path('/tmp')
    remote_files = [remote_dir / f.name for f in src_files]

    try:
        # --- PUT: local -> remote --------------------------------------------
        logger.info(f"nc PUT: {len(src_files)} file(s) -> {opts.host_id}:{remote_dir}")
        put_status, put_msg = await host.put(src_files, remote_dir)
        if not put_status.is_ok:
            return _result(False, f"put failed: {put_status} {put_msg}")
        logger.info("nc PUT succeeded")

        # --- GET: remote -> local --------------------------------------------
        logger.info(f"nc GET: {len(remote_files)} file(s) -> {roundtrip_dir}")
        get_status, get_msg = await host.get(remote_files, roundtrip_dir)
        if not get_status.is_ok:
            return _result(False, f"get failed: {get_status} {get_msg}")
        logger.info("nc GET succeeded")

        # --- verify byte-for-byte --------------------------------------------
        for src in src_files:
            back = roundtrip_dir / src.name
            if not back.exists():
                return _result(False, f"round-tripped file missing: {back}")
            if _sha256(src) != _sha256(back):
                return _result(False, f"checksum mismatch for {src.name}")
        logger.info(f"Round-trip verified: {len(src_files)} file(s) byte-for-byte intact")

    finally:
        # Best-effort cleanup of the remote temp files.
        if remote_files:
            quoted = ' '.join(str(p) for p in remote_files)
            await host.run(f'rm -f {quoted}')
        await host.close()

    return _result(True, f"nc telnet round-trip OK ({len(src_files)} files)")
