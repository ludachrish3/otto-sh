from otto import register_options

from .install import test_instruction
from .nc_smoke import nc_smoke
from .run_on_container import run_on_container

# Verb-wide options: ``ProjectOptions`` adds ``--device-type``/``--lab-env`` to
# ``otto run`` and ``otto test``; ``DeviceTestOptions`` adds ``--firmware`` and
# ``--check-interfaces`` to ``otto test`` only.
register_options("project1_common.options:ProjectOptions", verbs=["run", "test"])
register_options("project1_common.options:DeviceTestOptions", verbs=["test"])

# The Zephyr 2.7 ``zephyr-inline`` command frame moved to the shared
# ``custom_hosts`` module — a third-party-style package this project now depends on
# via ``settings.toml`` ``libs`` + ``init``. The 2.7 host lives in the shared
# ``embedded`` lab and more than one project needs the dialect, so it is owned in
# one shared place rather than registered here. See ``tests/custom_hosts/README.md``.
