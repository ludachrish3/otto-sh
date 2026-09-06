"""The raw-landing session setup: whatever the console shows, reach the Zephyr prompt."""

# doc: begin land-on-zephyr
from otto import SetupContext
from otto.host.session import HostSession


async def land_on_zephyr(session: HostSession, ctx: SetupContext) -> None:
    """No landing dialect: a newline, then the prompt; frame entry does the rest."""
    await session.send("\n")
    # Zephyr's prompt is per-backend: SHELL_PROMPT_UART is `uart:~$ `, while the
    # telnet backend's default is a bare `~$ `. The shared tail fits both — and
    # stays unanchored: the shell paints the prompt, so an ANSI reset follows the space.
    await session.expect(r"~\$ ", timeout=ctx.params.get("boot_timeout", 30.0))


# doc: end land-on-zephyr
