"""ASCII art banner printed at startup by the top-level ``otto`` callback."""

from rich.text import (
    Text,
)

banner = Text(style="bold green", no_wrap=True)
banner.append(r"          __  __       " "\n")
banner.append(r"   ____  / /_/ /_____  " "\n")
banner.append(r"  / __ \/ __/ __/ __ \ " "\n")
banner.append(r" / /_/ / /_/ /_/ /_/ / " "\n")
banner.append(r" \____/\__/\__/\____/  " "\n")
