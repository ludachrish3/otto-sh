"""Shared, third-party-style otto extension module.

``custom_hosts`` emulates an out-of-tree package that SUT repos depend on for
extra host/shell capabilities — the kind of thing a vendor or a shared internal
library would ship. Repos pull it in through ``settings.toml``::

    libs = ["${sutDir}/../custom_hosts"]   # add this dir to PYTHONPATH
    init = ["custom_hosts"]                 # import at config load

otto imports each ``init`` module at config load (after ``libs`` land on the
path), the same hook ``register_filesystem`` / ``register_host_parsers`` use.
Importing this package registers its command frames, so any repo that lists it
in ``init`` can construct lab hosts that reference those frames by name.

It exists so a frame needed by a *shared* lab host is owned in one shared place
rather than duplicated per repo or absorbed into otto core: the ``embedded``
lab's Zephyr 2.7 host (``sprout27``, ``command_frame: "zephyr-inline"``) is used
by more than one repo, so every consumer registers the dialect from here.
"""

from otto.host.command_frame import register_command_frame

from .zephyr_inline import ZephyrInlineRetcodeFrame

register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)
