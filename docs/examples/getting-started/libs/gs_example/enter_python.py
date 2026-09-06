"""The two-dialect session setup: land in bash, end in the python3 REPL."""

# doc: begin enter-python
from otto import SetupContext
from otto.host.session import HostSession


async def enter_python(session: HostSession, ctx: SetupContext) -> None:
    """``run()`` is bash-framed here; ``send``/``expect`` navigate; frame entry confirms."""
    present = (await session.run("command -v python3")).only
    # `> 0`, not `!= 0`: a command that never ran reports -1, which is not the
    # same fact as an interpreter that is missing.
    if present.retcode > 0:
        raise RuntimeError(f"{ctx.host_name}: python3 is not on PATH")
    await session.send("python3 -u -i\n")
    await session.expect(r">>> \Z", timeout=ctx.params.get("start_timeout", 10.0))


# doc: end enter-python
