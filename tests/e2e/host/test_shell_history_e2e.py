"""End-to-end proof that otto's commands stay out of a real host's shell history.

Runs against the live veggies Unix bed. The unit suite proves the payload is
composed and written; only a real interactive shell can prove the *effect* —
that bash, having read the user's rc files and holding a real ``HISTFILE``,
actually stops recording, and that the file on disk is untouched after the
session closes (bash writes history at exit, so nothing before close proves
anything).

Two observations per case, because either alone is weak:

- ``$HISTFILE`` **as the live shell sees it** — the mechanism, read back
  through the very session under test.
- the **sha256 of the history file on disk**, sampled before and after through
  a *separate* connection. It must be sampled over ``exec`` rather than the
  session: an ``exec`` channel has no PTY and bash disables history there
  outright, so the measurement cannot perturb what it measures.

``test_opting_in_still_records`` is the positive control. Without it the
suppression assertions could pass against a bed where nothing writes history
for some unrelated reason (no rc file, ``HISTFILE`` already unset, a
restricted shell), and the suite would look green while proving nothing.
"""

import uuid
from collections.abc import Iterator

import pytest

from otto import register_login_proxy
from otto.host.factory import create_host_from_dict
from otto.utils import Status
from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("shell_history_e2e")]


# root has NO password on the vagrant bed, so a plain `su root` fails auth.
# vagrant does have passwordless sudo, so elevation must be root-mediated —
# the same constraint (and the same shape of proxy) as test_login_proxy_e2e.py.
# Registered with overwrite=True so re-import under xdist is idempotent.
async def _sudo_su_root(io, ctx):
    await io.send("sudo su -\n")


async def _sudo_su_root_undo(io, ctx):
    await io.send("exit\n")


register_login_proxy("sudo-su-root", _sudo_su_root, undo=_sudo_su_root_undo, overwrite=True)

_CREDS: list[dict[str, str]] = [
    {"login": "vagrant", "password": "vagrant"},
    {"login": "root", "proxy": "sudo-su-root", "via": "vagrant"},
]


@pytest.fixture
def leased_host(tmp_path_factory) -> Iterator[tuple[str, str]]:
    """Lease one Unix host from the pool; yield ``(element, ip)``."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        yield element, host_data(element)["ip"]


def _host(ip: str, element: str, **overrides: object):
    data: dict[str, object] = {
        "ip": ip,
        "element": element,
        "board": "seed",
        "creds": [dict(c) for c in _CREDS],
    }
    data.update(overrides)
    return create_host_from_dict(data)


async def _history_digest(host, element: str) -> str:
    """sha256 of ~/.bash_history, measured over a PTY-less exec channel.

    ``exec`` is deliberate: it is non-interactive, so bash keeps no history
    there and the measurement cannot pollute the thing being measured. A
    missing file reads as a stable sentinel rather than an error — a fresh VM
    legitimately has none yet.
    """
    result = await host.exec("cat ~/.bash_history 2>/dev/null | sha256sum || true")
    if result.status is not Status.Success:
        raise AssertionError(f"{element}: could not sample history file: {result!r}")
    return str(result.value).strip()


async def _shells_histfile(host) -> str:
    """What ``$HISTFILE`` is inside the interactive session otto actually drives."""
    result = (await host.run('echo "HISTFILE=[$HISTFILE]"')).only
    assert result.status is Status.Success, f"probe failed: {result!r}"
    return str(result.value).strip()


@pytest.mark.asyncio
async def test_default_host_leaves_history_untouched(leased_host):
    """The whole point: a default UnixHost writes nothing to ~/.bash_history."""
    element, ip = leased_host
    host = _host(ip, element)

    before = await _history_digest(host, element)

    # A marker unique per run, so a failure names exactly which run leaked.
    marker = f"otto-history-probe-{uuid.uuid4().hex[:8]}"
    async with host:
        assert await _shells_histfile(host) == "HISTFILE=[/dev/null]"
        for _ in range(3):
            assert (await host.run(f"echo {marker}")).only.status is Status.Success
    # Leaving the context closes the session — the moment bash flushes history.

    after = await _history_digest(host, element)
    assert after == before, f"{element}: otto's commands reached ~/.bash_history"

    # Emit a verdict word, not a count: `grep -c` prints "0" AND exits 1 on no
    # match, so any `|| fallback` fires too and the output is ambiguous —
    # while asserting the *marker* is absent from a count can never fail at all.
    leaked = await host.exec(
        f"if grep -q -F {marker} ~/.bash_history 2>/dev/null; then echo LEAKED; else echo CLEAN; fi"
    )
    assert str(leaked.value).strip() == "CLEAN", (
        f"{element}: marker {marker} found in ~/.bash_history"
    )


@pytest.mark.asyncio
async def test_opting_in_still_records(leased_host):
    """Positive control: with ``shell_history=True`` otto's commands DO land.

    This is what makes every suppression assertion in this module meaningful.
    It is not enough to check that ``$HISTFILE`` looks sane under opt-in — the
    digest comparison the other tests rely on has to be shown capable of
    *detecting* pollution, or a bed that silently never writes history would
    make them all pass while proving nothing.

    So this deliberately pollutes, asserts the digest moved, and then restores
    the file byte-for-byte, verifying the digest returns to its original
    value. The bed is left exactly as found.
    """
    element, ip = leased_host
    host = _host(ip, element, shell_history=True)
    backup = f"/tmp/otto-history-backup-{uuid.uuid4().hex[:8]}"

    before = await _history_digest(host, element)
    # Record whether the file existed at all: restoring a `cp` of a missing
    # file would CREATE an empty ~/.bash_history where the bed had none. The
    # digest wouldn't notice (both hash as empty) but the bed would be altered.
    existed = str((await host.exec("test -f ~/.bash_history && echo yes || echo no")).value).strip()
    await host.exec(f"cp ~/.bash_history {backup} 2>/dev/null || : > {backup}")
    try:
        marker = f"otto-control-probe-{uuid.uuid4().hex[:8]}"
        async with host:
            seen = await _shells_histfile(host)
            assert seen != "HISTFILE=[/dev/null]", f"{element}: suppressed despite opt-in"
            assert seen != "HISTFILE=[]", (
                f"{element}: the bed's shell has no HISTFILE at all, so the "
                f"suppression assertions here prove nothing — fix the bed, not the test"
            )
            for _ in range(3):
                assert (await host.run(f"echo {marker}")).only.status is Status.Success

        assert await _history_digest(host, element) != before, (
            f"{element}: history did not change even with recording ENABLED — the "
            f"digest cannot detect pollution, so this module's suppression "
            f"assertions are vacuous"
        )
    finally:
        # Restore byte-for-byte; leave no trace of the control on a shared bed.
        # `rm` is unconditional (`;` not `&&`) so a failed restore can't also
        # leak the backup file, and a bed that had no history file gets none.
        restore = f"cp {backup} ~/.bash_history" if existed == "yes" else "rm -f ~/.bash_history"
        await host.exec(f"{restore}; rm -f {backup}")

    assert await _history_digest(host, element) == before, (
        f"{element}: failed to restore ~/.bash_history after the positive control"
    )


@pytest.mark.asyncio
async def test_suppression_survives_a_login_proxy_hop(leased_host):
    """``su`` starts a fresh shell that re-reads rc files, resetting HISTFILE.

    This is the only test covering the resync-probe half of the feature: the
    payload rides ``_resync_shell``'s probe so the elevated shell is quieted by
    the very first line it executes.
    """
    element, ip = leased_host
    host = _host(ip, element)

    async with host:
        assert await _shells_histfile(host) == "HISTFILE=[/dev/null]"
        async with host.as_user("root"):
            assert await _shells_histfile(host) == "HISTFILE=[/dev/null]", (
                f"{element}: HISTFILE reset when su'ing to root — the resync "
                f"probe did not carry the suppression payload"
            )
        # Back in the original shell, still quiet.
        assert await _shells_histfile(host) == "HISTFILE=[/dev/null]"


@pytest.mark.asyncio
async def test_root_history_untouched_across_elevation(leased_host):
    """Root's own history file must not collect otto's liveness probes either."""
    element, ip = leased_host
    host = _host(ip, element)

    async def _root_digest() -> str:
        result = await host.exec("sudo cat /root/.bash_history 2>/dev/null | sha256sum || true")
        return str(result.value).strip()

    before = await _root_digest()
    marker = f"otto-root-probe-{uuid.uuid4().hex[:8]}"
    async with host, host.as_user("root"):
        assert (await host.run(f"echo {marker}")).only.status is Status.Success

    assert await _root_digest() == before, f"{element}: otto polluted root's history"
