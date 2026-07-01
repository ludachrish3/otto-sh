"""No-op instruction for e2e discovery tests."""

from otto.cli.run import instruction
from otto.utils import CommandStatus, Status


@instruction()
async def noop() -> CommandStatus:
    """No-op instruction for e2e discovery tests."""
    return CommandStatus(command="noop", output="", status=Status.Success, retcode=0)
