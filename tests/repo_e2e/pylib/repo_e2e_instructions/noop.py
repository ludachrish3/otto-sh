"""No-op instruction for e2e discovery tests."""

from otto.cli.run import instruction
from otto.result import CommandResult
from otto.utils import Status


@instruction()
async def noop() -> CommandResult:
    """No-op instruction for e2e discovery tests."""
    return CommandResult(Status.Success, value="", command="noop", retcode=0)
