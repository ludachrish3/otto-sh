"""The example project's init module — imported by otto at config load.

Each section registers one extension the worked example uses; the pages
include them between the ``# doc: begin`` / ``# doc: end`` markers.
"""

# doc: begin register-frame
from otto.host.command_frame import FRAME_CLASSES, register_command_frame

from .zephyr_inline import ZephyrInlineRetcodeFrame

# Idempotent on purpose: a shared library may already have registered the
# same dialect, and a second registration under one name is refused.
if ZephyrInlineRetcodeFrame.type_name not in FRAME_CLASSES.names():
    register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)
# doc: end register-frame

# doc: begin register-backend
from otto.reservations import register_reservation_backend
from otto.reservations.registry import RESERVATION_BACKENDS

from .reservations import TeamFileBackend

# Idempotent for the same reason as the frame above: a second registration
# under one name is refused.
if "team-file" not in RESERVATION_BACKENDS.names():
    register_reservation_backend("team-file", TeamFileBackend)
# doc: end register-backend

# doc: begin register-parsers
import re

from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers, register_parsers

from . import proxies  # importing registers the proxy
from .monitor import BusyBoxSocketsParser, EntropyParser

# Project-wide: every host that has no per-host set of its own also charts entropy.
register_parsers([EntropyParser()])

# BusyBox guests only: drop the `ss -s` parser their userland cannot run and
# put the netstat one in its place, keyed by command like every parser.
_busybox = {cmd: p for cmd, p in DEFAULT_PARSERS.items() if cmd != "ss -s"}
_busybox[BusyBoxSocketsParser().command] = BusyBoxSocketsParser()
# A per-host set replaces the defaults outright, so the project-wide entropy
# parser goes back in.
_busybox[EntropyParser().command] = EntropyParser()
register_host_parsers(re.compile(r"bb.*_qemu"), _busybox)
# doc: end register-parsers
