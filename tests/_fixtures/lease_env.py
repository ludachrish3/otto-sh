"""The environment a bed-lease test hands the script: the process's, minus the lease namespace.

A `make gate-tip` launched with `BED_LEASE_WAIT=<s>` (the documented way to queue behind
another gate) runs the lease tests with that variable in their environment; a test whose
"second acquire is refused" case inherited it waited for the first holder and took the lease
instead (#819). A guard must inject the condition it tests, never inherit it from the runner,
so every ``BED_LEASE_*`` key is dropped here and each case sets only what it needs.
"""

import os

LEASE_ENV_PREFIX = "BED_LEASE_"
"""The namespace of the script's variables; a test pins that every read it makes is inside it."""


def lease_free_environ(**overrides: str) -> "dict[str, str]":
    """Return a copy of ``os.environ`` without any ``BED_LEASE_*`` key, plus *overrides*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(LEASE_ENV_PREFIX)}
    env.update(overrides)
    return env
