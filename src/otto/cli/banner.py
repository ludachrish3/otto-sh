"""ASCII art banner printed on every ``otto`` help screen."""

from rich.text import (
    Text,
)

banner = Text(style="bold green", no_wrap=True)
banner.append(r"          __  __       " "\n")
banner.append(r"   ____  / /_/ /_____  " "\n")
banner.append(r"  / __ \/ __/ __/ __ \ " "\n")
banner.append(r" / /_/ / /_/ /_/ /_/ / " "\n")
banner.append(r" \____/\__/\__/\____/  " "\n")


def print_banner() -> None:
    """Print the otto banner, centered, via rich."""
    from rich import print as rprint
    from rich.align import Align

    rprint(Align.center(banner))
