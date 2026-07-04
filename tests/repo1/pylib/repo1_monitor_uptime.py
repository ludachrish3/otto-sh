"""Init module proving per-host monitor-parser scoping end to end.

Registers :class:`otto.examples.monitor.UptimeParser` for the host id named
by ``OTTO_E2E_UPTIME_HOST``. The default id matches no lab host, making this
module a deliberate no-op for every other test that bootstraps repo1 — the
registration sits in HOST_PARSERS but is never looked up.
"""

import os

from otto.examples.monitor import UptimeParser
from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers

register_host_parsers(
    os.environ.get("OTTO_E2E_UPTIME_HOST", "e2e-uptime-unregistered"),
    {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()},
)
