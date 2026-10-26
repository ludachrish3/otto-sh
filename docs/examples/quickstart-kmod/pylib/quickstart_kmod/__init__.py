"""The kernel-module track's project code: one custom monitor metric.

otto imports this module at startup (``init`` in ``.otto/settings.toml``), so
the parser it registers is collected from every monitored host.
"""

from typing_extensions import override

from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext, register_parsers


class ModuleSize(MetricParser):
    """Chart how much memory the loaded module takes: the size column of /proc/modules."""

    y_title = "Module size"
    unit = "B"
    command = "awk '$1 == \"qs_wordcount\" {print $2}' /proc/modules"
    chart = "Module size"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> "dict[str, MetricDataPoint]":
        try:
            return {"Module size": MetricDataPoint(float(output.split(maxsplit=1)[0]))}
        except (IndexError, ValueError):
            return {}


register_parsers([ModuleSize()])
