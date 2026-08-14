"""What an operator gets from `UnixHost.put` over sftp to a device with no sftp-server.

THE MEASUREMENT THAT DECIDED THE `sftp-transfer` RECORD'S SHAPE, kept runnable.
Everything else in the busybox series answers "is this device refused"; this
one answers "what does the caller SEE", because for this surface those are
different questions and only the second has an answer worth acting on. There is
no fact otto can read before the operation that distinguishes a device serving
sftp from one that is not — `sftp-server` is off `PATH` even on Debian, its
absolute path is compiled into dropbear, and the daemon is not the authority —
so the operation is its own probe and the only thing a call site can improve is
the sentence the failure arrives as.

`tests/unit/host/test_sftp_transfer_attribution.py` pins the guard's contract
against a scripted manager: the record's four facts, the `untested` downgrade,
the arms that must not fire. It cannot show that the arm is REACHED on a real
device, because it supplies the exception itself — the reachability blind spot
`test_gap_registry.py` names in its own header. This module closes exactly that
gap and nothing else: a host built the way a lab builds one, a real dropbear, a
real chroot into the pinned BusyBox root, and an assertion about the exception
that comes back out of the product's own entry point.

WHY THE TIER IS THE RIGHT DEVICE FOR IT. `test_tier3_session.py` proves sftp is
unreachable inside this root, in two independent ways — the mask over
dropbear's compiled-in `/usr/lib/sftp-server` and the chroot wrapper — and
proves each is load-bearing by removing it and watching the outcome change. So
a failure measured here is a failure caused by the absent subsystem, not by a
daemon that happened to be unhappy.

COST, AND THE WINDOW IT IS SPENT FROM. One CALL-phase test against the
already-running session daemon: measured warm on this VM, 2026-08-14, 26ms for
the PUT and 10ms for the GET, one ssh connection and one exec channel each.
Nothing here starts a daemon. Against pyproject's `timeout = 180` with
`timeout_func_only = true` that is not a budget worth arithmetic.

NOTHING HERE IS TIMING-BASED. The 22ms in the record's `measured_on` is a
recorded observation, not a bound, and no assertion below reads a clock — a
"the refusal should be fast" check would be a discriminator built from one
machine on one day. See `docs/architecture/quality-gates.md` and issue #229.
"""

import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from otto.host.errors import UnsupportedOnUserlandError
from otto.host.factory import create_host_from_dict
from otto.host.unix_host import UnixHost
from otto.host.userland import PATH_ATTRIBUTED, gap_for
from tests._fixtures import busybox_dropbear as bbd
from tests.busybox.conftest import TIER3_GROUP

# `busybox` is LOAD-BEARING despite this tree's directory stamp (the stamp
# depends on collection-hook order and dies with its own file — see the
# conftest), and `xdist_group` is the ANSWER to dropbear's measured
# MAX_UNAUTH_PER_IP=5: it keeps the whole tier sequential on one worker sharing
# one daemon.
pytestmark = [pytest.mark.busybox, pytest.mark.xdist_group(TIER3_GROUP)]

_SURFACE = "sftp-transfer"


@contextlib.asynccontextmanager
async def sftp_host(daemon: bbd.LoopbackDropbear) -> "AsyncIterator[UnixHost]":
    """A real `UnixHost` pointed at *daemon*, pinned to the `sftp` backend.

    Through :func:`~otto.host.factory.create_host_from_dict` from an
    ordinary host dict, exactly as `test_tier3_shell_transfer.py`'s
    `busybox_host` does, with ONE difference that is the whole point: it pins
    `transfer: "sftp"` rather than taking the `busybox` profile's `shell`
    default. That pin is a thing a lab may legitimately write — the profile
    lists `sftp` in `valid_transfers` deliberately, because a BusyBox device
    with an sftp-server installed transfers over it perfectly well — so this is
    a supported configuration meeting an unsupported device, not a
    misconfiguration.

    The precondition below is what stops this module quietly measuring the
    `shell` backend if that pin ever stops taking effect, which would turn every
    assertion here into a test of the wrong code path that still passed.

    Closes the host in a `finally`: otto holds an asyncssh connection and its
    transport open behind `_connections`, and a leaked one is reported by the
    suite's leak detectors at some later test's teardown, naming neither this
    module nor the connection.
    """
    host = create_host_from_dict(
        {
            "os_type": "busybox",
            "ip": "127.0.0.1",
            "element": "bbsftp",
            "is_virtual": True,
            "creds": [{"login": daemon.username, "password": "unused-pubkey-auth"}],
            "term": "ssh",
            "transfer": "sftp",
            "ssh_options": {"port": daemon.port, "client_keys": [str(daemon.keys.client)]},
        }
    )
    assert host.transfer == "sftp", (
        f"the host dict pinned `sftp` and the built host's transfer is {host.transfer!r}, "
        f"so this module would measure a different backend than the one the "
        f"`sftp-transfer` record is about"
    )
    try:
        yield host
    finally:
        await host.close()


@pytest.mark.busybox
@pytest.mark.asyncio
async def test_a_put_over_sftp_fails_in_the_records_words_not_asyncsshs(
    tier3_dropbear, tmp_path: Path
) -> None:
    """The whole chain, on a real device: attempt, failure, attribution.

    WHAT THIS REPLACED, measured against this same tier on 2026-08-14 before
    the guard existed: `asyncssh.sftp.SFTPConnectionLost: 0 bytes read on a
    total of 4 expected bytes`, raised straight out of `UnixHost.put`. Prompt
    and residue-free and naming nothing an operator can act on — not the
    subsystem, not the device, not the `shell` backend the record says to use
    instead. It reads as a truncated connection, so the diagnosis it invites is
    that the link is flaky.

    The four fragments asserted are read OFF THE RECORD rather than written
    here, so this cannot pass against a hard-coded message that happens to
    mention sftp, and it does not need editing when the record's prose is
    reworded.
    """
    gap = gap_for(_SURFACE)
    assert gap is not None
    src = tmp_path / f"sftp-{uuid.uuid4().hex[:8]}.bin"
    src.write_bytes(b"never leaves this machine\n")

    async with sftp_host(tier3_dropbear) as host:
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await host.put(src, Path("/tmp"), show_progress=False)

    message = str(caught.value)
    for fragment in (gap.surface, gap.reason, gap.measured_on, gap.docs_anchor):
        assert fragment in message, (
            f"the failure a real BusyBox device produced does not carry the record's "
            f"{fragment[:60]!r}. The guard is reachable in the unit tests and not here, "
            f"which is the one thing this module exists to check"
        )
    assert "SFTPConnectionLost" in message, (
        f"asyncssh's own error did not survive into the message, so an operator has "
        f"otto's theory and no way to check it: {message[:200]!r}"
    )

    # Nothing moved, on either side of the chroot. Both paths are checked
    # because `/tmp` names a real directory on this machine AND one inside the
    # root, and a transfer that escaped the chroot would land in the first.
    assert not (tier3_dropbear.rootfs / "tmp" / src.name).exists()
    assert not (Path("/tmp") / src.name).exists()


@pytest.mark.busybox
@pytest.mark.asyncio
async def test_a_get_over_sftp_fails_the_same_way(tier3_dropbear, tmp_path: Path) -> None:
    """The other direction, which needs the same absent server side.

    Its own test rather than a parametrization of the one above because the two
    reach the guard through different methods — `_run_get` and `_run_put` are
    two declared paths on the record — and a parametrized failure would not say
    which.
    """
    gap = gap_for(_SURFACE)
    assert gap is not None
    dest = tmp_path / "down"
    dest.mkdir()

    async with sftp_host(tier3_dropbear) as host:
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await host.get(Path("/bin/busybox"), dest, show_progress=False)

    assert gap.docs_anchor in str(caught.value)
    assert not list(dest.iterdir()), f"the GET failed and still wrote {list(dest.iterdir())}"


@pytest.mark.busybox
def test_the_record_still_describes_this_module_s_subject() -> None:
    """Both sftp paths are `ATTRIBUTED`, which is what makes the above the right test.

    If either path were downgraded to `OPEN`, or wired into a pre-emptive
    refusal, the two tests above would be asserting a contract the table no
    longer claims — and a pre-emptive refusal in particular is the change this
    surface must NOT take, because every fact one could key on answers `absent`
    on hosts where sftp works. So the state is pinned here, next to the
    measurement that argues for it.
    """
    gap = gap_for(_SURFACE)
    assert gap is not None
    assert [p.state for p in gap.paths] == [PATH_ATTRIBUTED, PATH_ATTRIBUTED], (
        f"the `{_SURFACE}` record's paths are {[p.state for p in gap.paths]}. This module "
        f"measures what a caller GETS from a failure; a record that had moved to a "
        f"pre-emptive refusal would need a different test, and one that had moved back to "
        f"OPEN would need this module deleted"
    )
