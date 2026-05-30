from repo1_common.zephyr_inline import ZephyrInlineRetcodeFrame

from otto.host.command_frame import register_command_frame

from .install import *
from .nc_smoke import *

# Register this repo's custom Zephyr 2.7 shell dialect so lab-data entries can
# select it by name (``"command_frame": "zephyr-inline"``). otto imports this
# init module at config load (see settings.toml ``init``), the same hook
# ``register_filesystem`` / ``register_host_parsers`` use.
register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)
