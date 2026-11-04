"""The counter reset check's marker frame, as host doubles must answer it.

The check script prints ``printf '\\n%s\\n' otto-gcda-check`` before ``find``
(``otto.host.product.gcda_check_cmd``), and its parser reads only what
follows the marker. A double that answers the check must print the frame
too, exactly as a shell would. Restated here rather than imported from the
product, so a change to the script turns these doubles RED.
"""

CHECK_MARK = "otto-gcda-check"

CHECK_PREFIX = f"printf '\\n%s\\n' {CHECK_MARK}; "
"""The check command's opening, as the pins spell it."""

CHECK_FRAME = f"\n{CHECK_MARK}\n"
"""What that opening prints."""


def framed(cmd: str, output: str) -> str:
    """*output* as a shell returns it for *cmd*: the check's frame first."""
    return CHECK_FRAME + output if CHECK_MARK in cmd else output
