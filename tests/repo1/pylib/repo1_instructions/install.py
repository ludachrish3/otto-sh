from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from repo1_common.options import RepoOptions

from otto.cli.run import instruction
from otto.configmodule.configmodule import do_for_all_hosts, run_on_all_hosts
from otto.context import get_context
from otto.host import LocalHost
from otto.host.unix_host import UnixHost
from otto.logger import get_otto_logger

logger = get_otto_logger()


@dataclass
class _Options(RepoOptions):
    debug: Annotated[
        bool,
        typer.Option(
            "--field/--debug",
            help="Use field or debug products.",
        ),
    ] = False


@instruction(options=_Options)
async def test_instruction(opts: _Options):

    local_host = LocalHost()
    output_dir = get_context().output_dir
    local_file1 = output_dir / "output1.bin"
    local_file2 = output_dir / "output2.bin"
    status = await local_host.run(f"dd if=/dev/urandom of={local_file1} bs=1K count=50")
    if not status.status.is_ok:
        return status

    status = await local_host.run(f"dd if=/dev/urandom of={local_file2} bs=1K count=100")
    if not status.status.is_ok:
        return status

    for i in range(10):
        logger.info(f"{i=}")

    logger.info(
        f"This is a test instruction in repo1: device_type={opts.device_type!r}, "
        f"lab_env={opts.lab_env!r}, debug={opts.debug}"
    )

    # Run startup commands on every host concurrently.
    await run_on_all_hosts(["echo start", "uname -a"])

    # Push the two generated files to every host concurrently.
    transfer_results = await do_for_all_hosts(
        UnixHost.put,
        src_files=[local_file1, local_file2],
        dest_dir=Path(),
    )

    for host_id, result in transfer_results.items():
        match result:
            case BaseException():
                logger.exception(
                    f"Exception transferring to {host_id}: ",
                    exc_info=result,
                )

            case transfer_status, _ if transfer_status.is_ok:
                continue

            case transfer_status, error_str:
                logger.error(
                    f"Failed to transfer a file to {host_id}: {transfer_status=} {error_str}"
                )

    logger.info("Done")

    return status
