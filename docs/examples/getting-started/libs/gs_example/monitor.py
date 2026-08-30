"""Two metric parsers the customizations page adds to the monitor.

``EntropyParser`` charts a value otto does not ship. ``BusyBoxSocketsParser``
replaces the built-in sockets parser on the BusyBox guests, whose userland has
no ``ss``: same chart, same series names, a command that exists there.
"""

import re

from typing_extensions import override

from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext


# doc: begin entropy-parser
class EntropyParser(MetricParser):
    """Chart the kernel's available entropy from ``/proc``."""

    y_title = "Entropy"
    unit = "bits"
    command = "cat /proc/sys/kernel/random/entropy_avail"
    chart = "Entropy"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        try:
            return {"Entropy": MetricDataPoint(float(output.split(maxsplit=1)[0]))}
        except (IndexError, ValueError):
            return {}
        # doc: end entropy-parser


# doc: begin busybox-sockets
class BusyBoxSocketsParser(MetricParser):
    """TCP socket-state counts from ``netstat -tn`` — BusyBox has no ``ss``.

    Same series names as otto's built-in ``SocketsParser`` so the chart is
    the same chart; only the command and the parse differ.
    """

    y_title = "Sockets"
    unit = ""
    command = "netstat -tn"
    tab = "network"
    tab_label = "Network"
    chart = "Sockets"

    _state = re.compile(r"^tcp\s.*\s(?P<state>ESTABLISHED|TIME_WAIT)\s*$")

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        counts = {"ESTABLISHED": 0, "TIME_WAIT": 0}
        for line in output.splitlines():
            m = self._state.match(line.strip())
            if m:
                counts[m["state"]] += 1
        return {
            "Established": MetricDataPoint(float(counts["ESTABLISHED"])),
            "Time-wait": MetricDataPoint(float(counts["TIME_WAIT"])),
        }
        # doc: end busybox-sockets
