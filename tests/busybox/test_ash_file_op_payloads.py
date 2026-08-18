"""Run `PosixFileOps`' OWN glob payload under each matrix row's real ash.

`PosixFileOps.glob` is the one file op whose shell line asks a DIALECT
question rather than an applet one. It expands the caller's pattern on the
device (`for p in <pattern>`) precisely so that no transfer backend ever has
to agree with any other about glob semantics — which moves the whole question
onto the device's shell, and makes "BusyBox ash expands and filters this the
way POSIX sh does" a fact worth measuring rather than assuming. Two halves,
both load-bearing and both measured here per row:

1. **Expansion selects.** The pattern must expand to the matching paths and
   only those — a row where ash returned the pattern unexpanded would hand
   `get_debug_logs` a path that does not exist, on a device where the logs
   plainly do.
2. **An unmatched pattern yields NOTHING.** POSIX sh leaves an unmatched
   pattern LITERAL, and `glob`'s `[ -e "$p" ]` guard is the only thing
   standing between that literal and a caller that believes it is holding a
   real path. The empty answer is the contract (zero logs is success), so the
   row that breaks it breaks it silently. What this half pins is THE COMPOSED
   PAYLOAD, not the row's raw dialect: an ash with `nullglob`-like behaviour
   satisfies it legitimately — the word expands to nothing, the loop body
   never runs, the answer is empty — while on a literal-returning ash (what
   POSIX asks for, and what these rows are expected to have) it goes red the
   moment the `-e` guard leaves the shell line. Both halves are asserted on
   every row because the payload has to hold on whichever dialect the row
   turns out to have.

THE PAYLOAD IS TAKEN FROM THE PRODUCT, never retyped — `_glob_payload` drives
the real `PosixFileOps.glob` with a recording `exec` and uses the exact string
it tried to send. This is `test_ash_frame_payloads.py`'s principle applied to
a second module: a change to the shell line that ash cannot support fails
here, and a test carrying its own private copy of the line would keep passing
while the product broke.

`printf` presence is probed in the same script and asserted separately, so a
row whose build lacks the applet reports THAT rather than an empty match list
— the failure mode the matrix exists to distinguish (see
`test_shell_codec_contracts.py`'s `_assert_base64_absent` for the same
"prove absence structurally" move on `base64`).

Parametrized over the full `BUSYBOX_MATRIX` directly, never pre-filtered by
`can_run`, with `require_interpreter`/`require_userns` called INSIDE the test
body — the reason is `test_ash_frame_payloads.py`'s and unchanged: a filtered
row list makes a row with no qemu interpreter vanish silently instead of
failing loudly.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from otto.host.local_host import LocalHost
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.busybox import BUSYBOX_MATRIX, require_interpreter
from tests._fixtures.busybox_rootfs import busybox_rootfs, require_userns, run_in_rootfs
from tests.busybox.test_shell_codec_contracts import _fields

pytestmark = [pytest.mark.busybox]

# Under `/tmp`, which this rootfs provides (`_ROOTFS_DIRS`) — the same
# directory `test_shell_codec_contracts.py`'s staged files use, and for the
# same reason: nothing else in the root is writable by contract.
_GLOB_DIR = "/tmp/otto-glob"

# Two that match and one that must NOT. The non-matching file is what makes
# this a test of SELECTION rather than of listing: a payload that expanded to
# the whole directory would satisfy an assertion that only counted matches.
_MATCHING = ("messages", "messages.1")
_NON_MATCHING = "syslog"


def _glob_payload(pattern: str) -> str:
    """The exact shell line `PosixFileOps.glob` sends, taken from the product.

    A `LocalHost` is the cheapest carrier of the mixin that constructs no
    connection; its `exec` is replaced before the call, so nothing runs
    anywhere — the point is only to capture the command the product built.
    """
    host = LocalHost()
    host.exec = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(status=Status.Success, value="", command="", retcode=0)
    )
    asyncio.run(host.glob(pattern))
    return host.exec.await_args.args[0]


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_glob_payload_expands_and_filters_under_ash(release):
    """Matching paths come back sorted; an unmatched pattern comes back empty."""
    require_interpreter(release.arch)
    require_userns()

    staged = "".join(f": > {_GLOB_DIR}/{name}; " for name in (*_MATCHING, _NON_MATCHING))
    # `echo KEY=$VALUE` UNQUOTED on purpose: the payload prints one path per
    # line, and word-splitting collapses that to a single space-joined line so
    # the `KEY=value` parser above can read it. Quoting would keep the
    # newlines and hide every path after the first from the assertion.
    script = (
        f"mkdir -p {_GLOB_DIR}; {staged}"
        "command -v printf >/dev/null 2>&1 && echo PRINTF=PRESENT || echo PRINTF=ABSENT; "
        f"MATCHED=$({_glob_payload(f'{_GLOB_DIR}/messages*')}); echo MATCHED=$MATCHED; "
        f"MISSED=$({_glob_payload(f'{_GLOB_DIR}/nothing-here-*.log')}); echo MISSED=$MISSED"
    )

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, script)

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    fields = _fields(result.stdout, "PRINTF", "MATCHED", "MISSED")

    assert fields["PRINTF"] == "PRESENT", (
        f"BusyBox {release.version} has no `printf`, which is what `glob`'s "
        f"payload emits each path with — every glob on this row would answer "
        f"an empty list for a directory full of matches ({result.stdout!r})"
    )
    assert fields["MATCHED"] == f"{_GLOB_DIR}/messages {_GLOB_DIR}/messages.1", (
        f"BusyBox {release.version} ash did not expand `messages*` to exactly "
        f"the two matching paths ({result.stdout!r}) — either the pattern came "
        f"back unexpanded, or the expansion swept in {_NON_MATCHING!r}, and "
        f"either way `glob` hands its caller paths the device disagrees with"
    )
    assert fields["MISSED"] == "", (
        f"BusyBox {release.version} ash answered {fields['MISSED']!r} for a "
        f"pattern that matches nothing — the `[ -e ]` guard is what must drop "
        f"the literal POSIX sh leaves behind, and a caller handed that literal "
        f"believes a file exists that does not"
    )
