"""pytest plugin: assert collection-time ambient hermeticity (G11).

Loaded via ``-p tests._collect_env_probe`` by the inner subprocess of
``tests/unit/test_env_hermeticity.py``'s per-tree pin. Collection imports
every conftest on the target's chain, so any IMPORT-TIME ``os.environ``
write (the class the gate bans) has already happened by
``pytest_collection_finish`` — while session fixtures have NOT run, so a
runtime-scoped write (the sanctioned spelling) is invisible here. That is
exactly the boundary G11 draws.

The marker line is the pin's positive control: an outer assertion requires
it in stdout, so a typo'd ``-p`` flag cannot pass vacuously as "no leak".
"""

import os

import pytest


def pytest_collection_finish(session):
    from tests._ambient_env import ambient_opt_ins

    leaked = sorted(k for k in os.environ if k.startswith("OTTO_") and k not in ambient_opt_ins())
    print(f"collect-env-probe: checked ({len(leaked)} leaked)")  # noqa: T201 — the pin greps this
    if leaked:
        # 7: clear of pytest's own exit codes (1-5, incl. INTERNAL_ERROR=3),
        # so the pin can tell a leak from a broken inner run exactly.
        pytest.exit(
            f"conftest chain wrote ambient otto env at import/collection time: {leaked}",
            returncode=7,
        )
