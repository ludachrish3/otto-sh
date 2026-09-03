"""A real :class:`~otto.cli.invoke.RootOptions` for tests that stash ``ctx.meta``.

The typed meta accessors in ``otto.cli.invoke`` isinstance-check
``meta['_otto_root_options']``, so a ``SimpleNamespace`` slice no longer
passes for it. This factory keeps call sites as small as the old slices —
and a ``RootOptions`` field rename now breaks fixtures loudly instead of
letting a double drift from the real dataclass.
"""

from pathlib import Path
from typing import Any

from otto.cli.invoke import RootOptions


def make_root_options(**overrides: Any) -> RootOptions:
    """Build a ``RootOptions`` with every field defaulted; override per test."""
    defaults: dict[str, Any] = {
        "labs": None,
        "xdir": Path(),
        "log_days": 7,
        "log_level": "INFO",
        "rich_log_file": False,
        "show_time": False,
        "dry_run": False,
        "as_user": None,
        "skip_reservation_check": False,
    }
    defaults.update(overrides)
    return RootOptions(**defaults)
