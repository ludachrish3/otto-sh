"""What a line reader yields for a command's output, for tests that feed one."""

import re


def lf_lines(text: str) -> list[str]:
    """What a line reader yields for *text*: split after each ``\\n`` only."""
    return [piece for piece in re.split(r"(?<=\n)", text) if piece]
