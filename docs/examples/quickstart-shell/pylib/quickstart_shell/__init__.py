"""The shell track's project code: one custom monitor metric.

otto imports this module at startup (``init`` in ``.otto/settings.toml``), so
the parser it registers is collected from every monitored host.
"""

from typing_extensions import override

from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext, register_parsers


class InstallSize(MetricParser):
    """Chart how much disk the staged tools use, in kilobytes."""

    y_title = "Install size"
    unit = "kB"
    command = "du -sk /tmp/quickstart"
    chart = "Install size"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> "dict[str, MetricDataPoint]":
        try:
            return {"Install size": MetricDataPoint(float(output.split(maxsplit=1)[0]))}
        except (IndexError, ValueError):
            return {}


register_parsers([InstallSize()])
