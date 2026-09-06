"""The example project's init module — imported by otto at config load.

Each section registers one extension the worked example uses; the pages
include them between the ``# doc: begin`` / ``# doc: end`` markers.

Most sections keep their imports beside their code. The rule that decides
whether they can is ruff's "imports at the top of the file" (E402): its
preamble tolerates ``if`` / ``try`` blocks between imports — which is why the
three sections below still carry their own — but the first *plain* statement
ends it for good, and the taint never resets. The parser section runs plain
assignments and calls, so every section after it hoists its imports up here
instead; a tutorial block should not have to teach a ``noqa``.
"""

from otto import register_session_setup

from .enter_python import enter_python
from .land_on_zephyr import land_on_zephyr
from .pyrepl_frame import PyReplFrame
from .setup import provision_app

# Sort the sections below independently of the hoist above, so every
# doc-included block keeps the imports it teaches and gains none of these.
# isort: split

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

# doc: begin register-setup
# Guarded like the frame above: a second registration under one name is refused.
if PyReplFrame.type_name not in FRAME_CLASSES.names():
    register_command_frame(PyReplFrame.type_name, PyReplFrame)
# `overwrite=True` rather than a guard: importing this module a second time —
# a reloaded project, or a `--doctest-modules` pass — must replace the hooks
# rather than fail on the names it registered the first time.
for _name, _fn in (
    ("provision-app", provision_app),
    ("enter-python", enter_python),
    ("land-on-zephyr", land_on_zephyr),
):
    register_session_setup(_name, _fn, overwrite=True)
# doc: end register-setup
