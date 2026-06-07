from .install import *
from .nc_smoke import *

# The Zephyr 2.7 ``zephyr-inline`` command frame moved to the shared
# ``custom_hosts`` module — a third-party-style package this repo now depends on
# via ``settings.toml`` ``libs`` + ``init``. The 2.7 host lives in the shared
# ``embedded`` lab and more than one repo needs the dialect, so it is owned in
# one shared place rather than registered here. See ``tests/custom_hosts/README.md``.
