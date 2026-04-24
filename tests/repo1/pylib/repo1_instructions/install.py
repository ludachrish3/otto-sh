from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from repo1_common.options import RepoOptions

from otto.cli.run import instruction
from otto.configmodule.configmodule import do_for_all_hosts, run_on_all_hosts
from otto.host import LocalHost
from otto.host.remoteHost import RemoteHost
from otto.logger import getOttoLogger

logger = getOttoLogger()


@dataclass
class _Options(RepoOptions):
    debug: Annotated[bool,
        typer.Option('--field/--debug',
            help='Use field or debug products.',
        )
    ] = False


@instruction(options=_Options)
async def test_instruction(opts: _Options):

    localHost = LocalHost()
    local_file1 = logger.output_dir / "output1.bin"
    local_file2 = logger.output_dir / "output2.bin"
    status = await localHost.run(f'dd if=/dev/urandom of={local_file1} bs=1M count=100')
    if not status.status.is_ok:
        return status

    status = await localHost.run(f'dd if=/dev/urandom of={local_file2} bs=1M count=150')
    if not status.status.is_ok:
        return status

    for i in range(10):
        logger.info(f'{i=}')

    logger.info(f"This is a test instruction in repo1: device_type={opts.device_type!r}, "
                f"lab_env={opts.lab_env!r}, debug={opts.debug}")

    # Run startup commands on every host concurrently.
    await run_on_all_hosts(['echo start', 'uname -a'])

    # Push the two generated files to every host concurrently.
    transfer_results = await do_for_all_hosts(
        RemoteHost.put,
        src_files=[local_file1, local_file2],
        dest_dir=Path(),
    )

    for host_id, result in transfer_results.items():
        match result:
            case BaseException():
                logger.exception(
                    f"Exception transferring to {host_id}: ", exc_info=result,
                )

            case transfer_status, _ if transfer_status.is_ok:
                continue

            case transfer_status, error_str:
                logger.error(
                    f"Failed to transfer a file to {host_id}: "
                    f"{transfer_status=} {error_str}"
                )

    logger.info("Done")

    return status
