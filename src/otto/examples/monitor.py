"""A minimal custom metric parser — the template for writing your own.

To chart a metric otto doesn't ship, subclass
:class:`~otto.monitor.parsers.MetricParser`: set the presentation attributes,
set ``command`` to the exact shell command to run each tick, and implement
``parse()`` to turn that command's output into labelled numeric points.
Then register it from an init module listed in ``.otto/settings.toml`` —
per host (exact id or ``re.compile`` pattern) or project-wide::

    from otto.examples.monitor import UptimeParser
    from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers

    register_host_parsers(
        "router1",
        {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()},
    )

otto's own test suite registers this parser exactly that way (see
``tests/repo1/pylib/repo1_monitor_uptime.py``), so the example is executed,
not just documented.
"""

from typing_extensions import override

from ..monitor.parsers import MetricDataPoint, MetricParser, ParseContext


class UptimeParser(MetricParser):
    """Chart host uptime in seconds from ``cat /proc/uptime``.

    ``/proc/uptime`` holds two floats — seconds since boot and aggregate idle
    seconds; the first is the metric. Returns an empty dict when the output
    doesn't parse (the series simply doesn't appear that tick).
    """

    y_title = "Uptime"
    unit = "s"
    command = "cat /proc/uptime"
    chart = "Uptime"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        try:
            return {"Uptime": MetricDataPoint(round(float(output.split(maxsplit=1)[0]), 2))}
        except (IndexError, ValueError):
            return {}
