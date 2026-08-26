"""Which host-contract cells the hermetic venue can actually build.

Hermetic means NO LAB. Cells resolve to a loopback ``sshd``, to BusyBox
artifacts run as local subprocesses, and to the runner's own userland. A cell
this venue cannot build is EXCLUDED from the space rather than skipped, so a
shrinking space is visible in the run's own log rather than showing up as a
green run that asserted nothing.

ALSO HOLDS :func:`resolve_space`, which is the switch between this venue and
the bed one -- so this module imports ``tests.conformance._bed``, and the
traffic runs one way only. The bed resolver takes the shared
:class:`~tests.conformance._resolved.ResolvedCell` from its own leaf module
precisely so that it does not import this one back: the cycle that would
create is measured in ``_resolved.py``'s docstring, and the machinery this
module pulls in (a loopback ``sshd``, the BusyBox artifact matrix) has no
business being imported to reach real hardware.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.

WHAT THE THREE KINDS REALLY ARE, because the labels are shorter than the
truth and a conformance suite that overstates its own venue is the defect it
exists to catch:

``local``
    A real :class:`~otto.host.local_host.LocalHost`. Measured: it declares
    neither ``valid_terms`` nor ``valid_transfers`` (``hasattr`` is ``False``
    for both), because it has no transport to choose between -- so unlike the
    loopback host below there is no menu to ask it for, and the ``"local"``
    term/transfer names that absence instead of claiming a registered backend.

``loopback-ssh``
    A throwaway non-root ``sshd`` on 127.0.0.1 and a real ``UnixHost`` over
    it, both from the tier-2 chaos lane's own fixtures. GNU userland, real
    ssh transport, real ``sftp``/``scp``.

``busybox-artifact``
    A ``LocalHost`` whose persistent session has a directory of BusyBox
    applet symlinks (``busybox --install -s``) prepended to ``PATH``, so the
    commands otto issues resolve to the pinned artifact rather than to the
    runner's coreutils. Be exact about the half this does NOT cover: the
    session's shell stays the runner's ``bash``, because
    :class:`~otto.host.session.LocalSession` spawns ``bash --norc
    --noprofile`` by name (``src/otto/host/session.py``) and changing that
    would be a change to otto. So APPLET behaviour is genuinely measured
    against the artifact; SHELL-DIALECT behaviour is not, and belongs to the
    bed venue's real BusyBox guests.

ALL THREE KINDS SPEAK POSIX, which is why every cell here carries
:data:`~tests.conformance._vocabulary.POSIX` outright while the bed venue
derives each cell's vocabulary from its host's userland axis. That is not a
shortcut: all three stand up a shell this repo controls -- the runner's own
``bash`` in two of them and a real ``sshd`` login in the third -- and none of
them has a lab entry to derive a userland from. It is also the reason the
contracts' bash spellings were universally true of this venue and broke the
moment the bed venue reached a Zephyr shell: a contract's portability is only
tested by the venue it runs in.
"""

import asyncio
import contextlib
import getpass
import json
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from otto.host.factory import create_host_from_dict
from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from tests._fixtures.busybox import BUSYBOX_MATRIX, BusyBoxRelease, busybox_binary, can_run
from tests._fixtures.profiles import Cell
from tests.conformance._bed import bed_space
from tests.conformance._resolved import ResolvedCell
from tests.conformance._venue import BED, current_venue
from tests.conformance._vocabulary import POSIX
from tests.integration.chaos._sshd import (
    LoopbackSshd,
    free_port,
    generate_keypairs,
    write_sshd_config,
)
from tests.integration.chaos._target import make_loopback_target

LOCAL = "local"
LOOPBACK_SSH = "loopback-ssh"
BUSYBOX_ARTIFACT = "busybox-artifact"

# What no hermetic cell can be, whatever menu a host reports. A ``telnet``
# term needs a console server in front of a real device and ``console``
# transfer is the embedded filesystem path -- both are properties of a lab,
# and the venue that has none must drop them from the SPACE. Dropping is not
# skipping: a skip inside a drawn cell reports success for a contract nobody
# ran, which is the failure this suite exists to make impossible.
#
# Stated as what the venue CANNOT serve rather than as what it can. An
# allow-list would silently drop a pair the loopback host genuinely serves the
# day the chaos fixture gains one (``nc``, say), and that loss would look
# exactly like this rule working.
_NEEDS_A_LAB_TERMS = frozenset({"telnet"})
_NEEDS_A_LAB_TRANSFERS = frozenset({"console"})

# `busybox --install -s DIR` symlinks every compiled-in applet into DIR.
# Measured against all five pinned artifacts on this VM under qemu-user:
# rc=0 and 323/347/389/396/402 symlinks for 1.16.1/1.21.1/1.28.1/1.31.0/1.35.0
# -- including 1.16.1, which does NOT support the newer `--list` an applet
# enumerator would otherwise have used (measured: `--list: applet not found`).
# 60s is a runaway bound for an emulated start-up plus a few hundred symlinks,
# not a discriminator: no assertion reads it, so widening it can only make this
# more patient.
_INSTALL_TIMEOUT_S = 60


def _servable(term: str, transfer: str) -> bool:
    """Whether the hermetic venue can actually stand this pair up."""
    return term not in _NEEDS_A_LAB_TERMS and transfer not in _NEEDS_A_LAB_TRANSFERS


def _loopback_entry(root: Path) -> dict:
    """The single lab entry ``make_loopback_target`` just wrote under *root*.

    Found by walking *root* rather than by restating the ``labdata/chaostech``
    path the generator chose: this module already depends on that generator's
    CONTENT, and a second copy of its LAYOUT is one more thing that can drift
    without either side noticing. Exactly one host is expected, and anything
    else raises -- a silent ``[0]`` on a two-host file would resolve menus for
    whichever entry happened to sort first.
    """
    hosts = [
        entry
        for path in sorted(root.rglob("lab.json"))
        for entry in json.loads(path.read_text())["hosts"]
    ]
    if len(hosts) != 1:
        raise RuntimeError(
            f"make_loopback_target wrote {len(hosts)} host entries under {root}; "
            f"the hermetic venue reads its loopback menus off exactly one"
        )
    return hosts[0]


def _runner_scratch(tmp_path: Path) -> Path:
    """This venue's remote directory: a runner path, because the far side IS the runner.

    Every cell here shares one filesystem with the process asserting on it --
    a ``LocalHost``, a loopback ``sshd`` running as the same user, a
    ``LocalHost`` with BusyBox applets on ``PATH`` -- so a directory under the
    test's own ``tmp_path`` is reachable from both sides. That is a property
    of THIS VENUE and not of the contracts, which is why the answer travels
    on the cell (:class:`~tests.conformance._resolved.ResolvedCell`) rather
    than being written into the contract: the bed venue's far side is a
    device, and its cells answer with a device path.

    Created here rather than by the contract, so that a venue whose remote
    directory needs no creation (the bed's device paths already exist) is not
    forced to pretend it does.
    """
    remote = tmp_path / "remote"
    remote.mkdir(parents=True, exist_ok=True)
    return remote


def _local_cells() -> "list[ResolvedCell]":
    """The runner's own userland. Always resolvable -- otto is running on it."""

    @contextlib.asynccontextmanager
    async def opener() -> "AsyncIterator[BaseHost]":
        async with LocalHost() as host:
            yield host

    return [
        ResolvedCell(
            cell=Cell(LOCAL, LOCAL, LOCAL),
            kind=LOCAL,
            open_host=opener,
            remote_scratch=_runner_scratch,
            vocabulary=POSIX,
        )
    ]


def _loopback_ssh_cells() -> "list[ResolvedCell]":
    """One cell per ``(term, transfer)`` the loopback host itself reports.

    The menus are read off the host otto BUILDS from the chaos lane's own lab
    entry, never re-derived here -- the rule ``tests/_fixtures/profiles.py``
    was written to enforce, whose docstring records that re-deriving what the
    factory already resolved gives wrong axes for more than half the bed. The
    entry is generated by the real ``make_loopback_target``, so if that
    fixture's declared menus change, this space changes with them and no edit
    here is needed.

    Resolution builds the host but starts NO daemon: the port and client key
    below are placeholders, because nothing on this path reaches the network.
    The ``sshd`` starts inside the opener, once per cell that is actually
    drawn.
    """
    with tempfile.TemporaryDirectory(prefix="conformance-resolve-") as tmp:
        root = Path(tmp)
        make_loopback_target(root, port=free_port(), client_key=root / "unused-at-resolve-time")
        entry = _loopback_entry(root)
        host = create_host_from_dict(dict(entry))
        element = host.element
        # Never sorted. Menus are emitted in the order the host reported
        # them, for the reason `axis_space` gives and re-measured here:
        # `axes_for` reports ['telnet', 'ssh'] for test2 against
        # ['ssh', 'telnet'] for test1 and test3, so a sort would be this
        # module inventing a value the host did not give. The loopback host
        # has a one-entry term menu and so cannot show the difference --
        # which is exactly why the rule is written down rather than left to
        # be rediscovered when a second term appears.
        pairs = [
            (term, transfer)
            for term in host.valid_terms
            for transfer in host.valid_transfers
            if _servable(term, transfer)
        ]
    return [
        ResolvedCell(
            cell=Cell(element, term, transfer),
            kind=LOOPBACK_SSH,
            open_host=_loopback_opener(term, transfer),
            remote_scratch=_runner_scratch,
            vocabulary=POSIX,
        )
        for term, transfer in pairs
    ]


def _loopback_opener(
    term: str, transfer: str
) -> "Callable[[], AbstractAsyncContextManager[BaseHost]]":
    """Stand up a throwaway ``sshd`` and a ``UnixHost`` pinned to this pair.

    Per cell rather than per session, so a contract that leaves the daemon in
    a bad state cannot make the next cell's result depend on the order the
    sampler drew them.
    """

    @contextlib.asynccontextmanager
    async def opener() -> "AsyncIterator[BaseHost]":
        with tempfile.TemporaryDirectory(prefix="conformance-loopback-") as tmp:
            root = Path(tmp)
            host_key, client_key = generate_keypairs(root / "keys")
            port = free_port()
            config = write_sshd_config(
                root / "sshd_config",
                port=port,
                host_key=host_key,
                authorized_keys=root / "keys" / "authorized_keys",
                user=getpass.getuser(),
            )
            sshd = LoopbackSshd(config, root / "sshd.log")
            sshd.start(port)
            try:
                make_loopback_target(root, port=port, client_key=client_key)
                # `term`/`transfer` are the spec's optional ACTIVE PIN, not a
                # menu edit: the host still validates them against the menu it
                # resolved, so a pair this venue drew but the host does not
                # serve fails here loudly rather than being quietly rewritten
                # to the menu's first entry. Measured against this very entry
                # -- `{"term": "telnet"}` raises
                # `ValueError: term 'telnet' is not in this host's term menu
                # ['ssh']`, and `{"transfer": "console"}` and
                # `{"transfer": "nc"}` raise the transfer analogue.
                pinned = dict(_loopback_entry(root)) | {"term": term, "transfer": transfer}
                async with create_host_from_dict(pinned) as host:
                    yield host
            finally:
                sshd.stop()

    return opener


def _busybox_cells() -> "list[ResolvedCell]":
    """One cell per pinned BusyBox release this machine can execute.

    Gated on :func:`~tests._fixtures.busybox.can_run` per ARCH, which is the
    only exclusion here that fires on a differently-provisioned machine: the
    artifacts are x86 userland (``tests/_fixtures/busybox.py`` records that
    upstream publishes no aarch64 build for any version) and this project's
    dev VM is aarch64, so they run only where qemu-user-static has its binfmt
    handlers registered. A release that cannot run is dropped from the SPACE.
    The alternative -- drawing it and skipping -- is what makes a lane keep
    reporting green while testing nothing.

    Deliberately does NOT call :func:`~tests._fixtures.busybox.busybox_binary`
    here. That fetches (release mirror first, busybox.net behind it) on a cold
    cache, and resolving the
    space must not depend on the network; the fetch happens inside the opener,
    for the releases actually drawn.
    """
    return [
        ResolvedCell(
            cell=Cell(f"busybox-{release.version}", LOCAL, LOCAL),
            kind=BUSYBOX_ARTIFACT,
            open_host=_busybox_opener(release),
            remote_scratch=_runner_scratch,
            # EVERY hermetic cell is POSIX, and the BusyBox one is the cell
            # that could look otherwise. Its applets are the pinned artifact's,
            # but the SHELL those commands are issued to is the runner's bash:
            # `LocalSession` spawns `bash --norc --noprofile` by name
            # (`src/otto/host/session.py`), which this module's own docstring
            # records as the half the artifact does NOT cover. So the shell
            # dialect here is bash whatever the applets are, and the vocabulary
            # is not derived from a userland axis the way the bed's is
            # (`tests/conformance/_bed.py`'s `bed_vocabulary`) -- there is no
            # lab entry to derive one from.
            vocabulary=POSIX,
        )
        for release in BUSYBOX_MATRIX
        if can_run(release.arch)
    ]


def _busybox_opener(
    release: BusyBoxRelease,
) -> "Callable[[], AbstractAsyncContextManager[BaseHost]]":
    """A ``LocalHost`` whose session resolves commands to *release*'s applets."""

    @contextlib.asynccontextmanager
    async def opener() -> "AsyncIterator[BaseHost]":
        binary = busybox_binary(release)
        with tempfile.TemporaryDirectory(prefix=f"conformance-busybox-{release.version}-") as tmp:
            applets = Path(tmp)
            # Off the event loop thread: a blocking `subprocess.run` here
            # would stall every other coroutine for the (emulated, and so not
            # negligible) duration of the install. Same shape as
            # `tests/unit/host/transfer/test_shell_transfer.py`'s fake shell.
            await asyncio.to_thread(
                subprocess.run,
                [str(binary), "--install", "-s", str(applets)],
                check=True,
                capture_output=True,
                timeout=_INSTALL_TIMEOUT_S,
            )
            async with LocalHost() as host:
                await host.run(f'export PATH="{applets}:$PATH"')
                # Confirm the prepend took, rather than assuming it. The
                # session is a real shell and `export` is a real command; if
                # it did not take, every later assertion would measure the
                # runner's GNU coreutils while the run's log said BusyBox --
                # a green result for a userland nobody exercised.
                #
                # `.only`, not `.value`: `run()` returns `Results`, whose
                # `value` is the LIST of per-command results. Reading it as
                # the output string is not a type error -- it stringifies to
                # `[CommandResult(...)]`, which contains the path and so
                # passes a naive `in` check while failing `startswith`. This
                # guard caught exactly that bug in its own first run.
                probe = (await host.run("command -v md5sum")).only
                where = str(probe.value).strip()
                if not probe.is_ok or not where.startswith(f"{applets}/"):
                    raise RuntimeError(
                        f"BusyBox {release.version}: PATH prepend did not take -- "
                        f"`command -v md5sum` answered {where!r}, not a path under {applets}"
                    )
                yield host

    return opener


def hermetic_space() -> "list[ResolvedCell]":
    """Every cell the hermetic venue can build here, in a stable build order.

    Order is local, then loopback-ssh, then the BusyBox matrix's own order.
    Not sorted and not shuffled -- though not for the reason first written
    here, which said the sampler draws from a seeded ``Random`` so a moved
    order would change the draw. That described an implementation this same
    item REJECTED: :func:`tests.conformance._sample.draw` ranks by
    ``blake2b(seed:label)``, keyed on the cell's LABEL and not its index, so
    the SAMPLED draw is independent of this order.

    The order is still load-bearing, for two narrower reasons. ``draw``
    returns ``list(space)`` VERBATIM whenever ``budget is None``, ``seed is
    None``, or ``budget >= len(space)`` -- and the hermetic default is a
    budget of 8 against a space of 8, so today every default run takes that
    path and this order IS the run order. Separately, every xdist worker must
    collect the same parametrization in the same order or the ids do not agree
    across workers.
    """
    return _local_cells() + _loopback_ssh_cells() + _busybox_cells()


def resolve_space() -> "list[ResolvedCell]":
    """The selected venue's resolvable cells: the bed's under the knob, else this one's.

    This used to raise ``NotImplementedError`` for ``bed``, and the reason it
    raised rather than returning ``[]`` outlives the raise: an empty space
    satisfies every sampling assertion vacuously, so a run that resolved
    nothing is indistinguishable from a run that certified everything. What
    replaces it is not a check here -- it is three layers that each catch a
    different shape of that vacuum, and none of them is this function:

    - a lab no host declares raises ``KeyError`` inside
      :func:`tests._fixtures.profiles.axis_space`, so the bed space cannot
      quietly shrink when the lab data is renamed under it;
    - ``tests/conformance/conftest.py`` refuses an empty ``_SPACE`` at import,
      which is the run-ending failure spec s4 asks for. Item 3 wrote that
      check "for the venue that can shrink" and it could not fire while the
      hermetic venue -- which always has the local cell -- was the only one.
      This is the venue it was written for;
    - ``tests/unit/test_conformance_cells.py`` injects an empty bed space and
      observes that refusal, so the guard above is exercised rather than
      merely present, and pins that a NON-empty bed venue resolves the whole
      bed space rather than a truncation of it or the hermetic space by
      mistake. A truthy space is not the guarantee: one cell is truthy, and
      it certifies the 48 it dropped.

    Reads the venue at call time, not at import, for the reason
    :func:`tests.conformance._venue.current_venue` gives.
    """
    if current_venue() == BED:
        return bed_space()
    return hermetic_space()
