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
shortcut: all three stand up a shell this repository controls -- the runner's own
``bash`` in two of them and a real ``sshd`` login in the third -- and none of
them has a lab entry to derive a userland from. It is also the reason the
contracts' bash spellings were universally true of this venue and broke the
moment the bed venue reached a Zephyr shell: a contract's portability is only
tested by the venue it runs in.

EVERY KIND CARRIES THE STAND-IN ELEVATION (:func:`stand_in_elevation`): no
hermetic login has a second user or a sudo this venue may drive, so each
host's shells find the one stand-in ``sudo``, ``su`` and ``id``
(:mod:`tests._fixtures.fake_elevation`) first on ``PATH``, and each cell
names the stand-in's user as its :attr:`~ResolvedCell.switch_user`. The
stand-in ``sudo`` never asks (as the bed's ``NOPASSWD`` logins do), and
``su - USER`` becomes *USER* without a password, so ``exec-options`` can
drive ``sudo=`` and ``user=`` here without touching the runner's own sudo.
"""

import asyncio
import contextlib
import dataclasses
import json
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from functools import partial
from pathlib import Path

from otto.host.factory import create_host_from_dict
from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from otto.models.base import Cred, CredSpec
from otto.utils import process_user
from tests._fixtures.busybox import BUSYBOX_MATRIX, BusyBoxRelease, busybox_binary, can_run
from tests._fixtures.env_guard import preserved_env_var
from tests._fixtures.fake_elevation import install_fake_elevation
from tests._fixtures.labdata import element_of, entry_of, flatten_lab_doc
from tests._fixtures.profiles import Cell
from tests.conformance._bed import bed_space
from tests.conformance._entry import (
    LOCAL_FAMILY,
    family_of,
    hooked,
    proxied_on,
    switch_user_on,
)
from tests.conformance._resolved import ResolvedCell
from tests.conformance._venue import BED, current_venue
from tests.conformance._vocabulary import POSIX
from tests.integration.chaos._sshd import LoopbackEndpoint, loopback_sshd
from tests.integration.chaos._target import make_loopback_target

LOCAL = "local"
LOOPBACK_SSH = "loopback-ssh"
BUSYBOX_ARTIFACT = "busybox-artifact"

RUNNER_LOCAL_KINDS = frozenset({LOCAL, LOOPBACK_SSH, BUSYBOX_ARTIFACT})
"""Every kind this venue builds: each one's host runs its commands on the runner itself, so a
process it started is in the runner's own ``/proc`` (``tests/conformance/_witness.py``)."""

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


#: The port the RESOLVE-time lab entry names. Resolution only reads menus off
#: the host it builds and never connects, so any valid port serves; a literal
#: rather than a reserved one, because reserving a port for a daemon that is
#: never started would hold a lock for nothing.
_RESOLVE_TIME_PORT = 22


STAND_IN_USER = "otto-cf-svc"
"""The second user every hermetic cell can become: a user no machine has, so only the
stand-in ``su - USER`` can become them (:mod:`tests._fixtures.fake_elevation`)."""


def _stand_in_for(login: str) -> str:
    """:data:`STAND_IN_USER`, refusing a venue whose *login* is that user.

    A second user equal to the login would make every user arm vacuous, so
    the cell is never built rather than built and skipped.
    """
    if login == STAND_IN_USER:
        raise RuntimeError(
            f"the hermetic login is {login!r}, the stand-in's own name: no cell can switch "
            f"to a second user"
        )
    return STAND_IN_USER


def stand_in_cred_entry(login: str) -> "dict[str, object]":
    """The stand-in user's credential, as a lab entry spells it; *login* is who switches to them.

    The stand-in ``su - USER`` never asks for a password
    (:mod:`tests._fixtures.fake_elevation`), and otto predicts a challenge
    from who is asking: any caller but root is watched for a password prompt,
    for the whole prompt watch when none comes (about five seconds a switch,
    docs/configuration/host-sources.md, ``expect_prompt``). An undeclared
    stand-in therefore cost every ``user=`` and ``as_user()`` arm that wait,
    which is the documented price of an unchallenged non-root switch and not a
    thing any contract here measures. So each hermetic host declares the
    stand-in the way a lab declares such an account.

    The switch BACK to *login* from inside the stand-in's scope is still
    watched: the login is the host's default cred, which takes no ``params``.
    """
    return {
        "login": STAND_IN_USER,
        "proxy": "su",
        "via": login,
        "params": {"expect_prompt": False},
    }


def _stand_in_creds(login: str) -> "list[Cred]":
    """:func:`stand_in_cred_entry` for a host built without a lab entry (``LocalHost``).

    The stand-in alone: a switch from a login with no cred of its own resolves
    the login by name, as it did when the host knew no cred at all.
    """
    return [CredSpec.model_validate(stand_in_cred_entry(login)).to_cred()]


@dataclasses.dataclass(frozen=True)
class StandIn:
    """Where :func:`stand_in_elevation` put the stand-in, and the variables that use it."""

    bin_dir: Path
    """The directory holding the stand-in ``sudo``, ``su`` and ``id``."""

    env: "dict[str, str]"
    """``PATH`` (:attr:`bin_dir` first) and ``FAKE_SUDO_DIR``."""


@contextlib.contextmanager
def stand_in_elevation() -> Iterator[StandIn]:
    """Install the stand-in elevation in a fresh directory for the life of one host.

    ``NOPASSWD``: the stand-in ``sudo`` never asks, as on the bed's GNU logins
    (``tests/conformance/_bed.py``, ``BED_SUDO_ASKS_A_PASSWORD``).
    """
    with tempfile.TemporaryDirectory(prefix="conformance-elevation-") as tmp:
        root = Path(tmp)
        env = install_fake_elevation(
            root, password="unused-stand-in-sudo", keeps_timestamp=True, nopasswd=True
        )
        yield StandIn(bin_dir=root / "bin", env=env)


@contextlib.contextmanager
def _local_stand_in() -> Iterator[StandIn]:
    """:func:`stand_in_elevation`, in this process's environment, which every local shell inherits.

    :class:`~otto.host.session.LocalSession` spawns its shell with this
    process's environment, so the variables are set here for the block and
    put back exactly after it.
    """
    with stand_in_elevation() as stand_in, contextlib.ExitStack() as stack:
        for name, value in stand_in.env.items():
            stack.enter_context(preserved_env_var(name))
            os.environ[name] = value
        yield stand_in


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
        for entry in flatten_lab_doc(json.loads(path.read_text()))
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
        with _local_stand_in():
            async with LocalHost(creds=_stand_in_creds(process_user() or "")) as host:
                yield host

    return [
        ResolvedCell(
            cell=Cell(LOCAL, LOCAL, LOCAL),
            kind=LOCAL,
            open_host=opener,
            remote_scratch=_runner_scratch,
            vocabulary=POSIX,
            family=LOCAL_FAMILY,
            switch_user=_stand_in_for(process_user() or ""),
            switch_user_stand_in=True,
            switch_user_may_sudo=True,
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
        make_loopback_target(
            root,
            LoopbackEndpoint(
                port=_RESOLVE_TIME_PORT,
                client_key=root / "unused-at-resolve-time",
                known_hosts=root / "unused-at-resolve-time-known-hosts",
            ),
        )
        entry = _loopback_entry(root)
        host = create_host_from_dict(entry_of(entry), element=element_of(entry))
        element = host.element.name
        # Never sorted. Menus are emitted in the order the host reported
        # them, for the reason `axis_space` gives and re-measured here:
        # `axes_for` reports a menu leading with telnet for test2 against
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
        # Read off the same built host as the menus: the lab entry's facts,
        # never the session manager's own route.
        family = family_of(host)
        proxied = {
            term: proxied_on(entry_of(entry), term, family=family) for term in host.valid_terms
        }
        # The entry's own second user where it names one; the stand-in,
        # whom the opener declares as an unchallenged `su` cred
        # (`stand_in_cred_entry`), where it does not (today: it names the
        # login alone).
        login = entry_of(entry)["creds"][0]["login"]
        switch_users = {
            term: switch_user_on(entry_of(entry), term, family=family) for term in host.valid_terms
        }
    return [
        ResolvedCell(
            cell=Cell(element, term, transfer),
            kind=LOOPBACK_SSH,
            open_host=_loopback_opener(term, transfer),
            remote_scratch=_runner_scratch,
            vocabulary=POSIX,
            family=family,
            proxied_login=proxied[term],
            # Never the runner's own sudo: its login's password is a
            # placeholder (the loopback login is by key) and the hermetic venue
            # drives no real elevation. The password arm is the unit tests'.
            password_sudo=False,
            switch_user=switch_users[term] or _stand_in_for(login),
            switch_user_direct=switch_users[term] is not None,
            switch_user_stand_in=switch_users[term] is None,
            switch_user_may_sudo=True,
            open_shell_host=(
                hooked(partial(_loopback_build, term, transfer))
                if term == "ssh" and not proxied[term]
                else None
            ),
        )
        for term, transfer in pairs
    ]


def _loopback_build(
    term: str, transfer: str, session_setup: str
) -> "AbstractAsyncContextManager[BaseHost]":
    """:func:`_loopback_opener`'s host, carrying the hook *session_setup* names."""
    return _loopback_opener(term, transfer, session_setup=session_setup)()


def _loopback_opener(
    term: str, transfer: str, *, session_setup: "str | None" = None
) -> "Callable[[], AbstractAsyncContextManager[BaseHost]]":
    """Stand up a throwaway ``sshd`` and a ``UnixHost`` pinned to this pair.

    Per cell rather than per session, so a contract that leaves the daemon in
    a bad state cannot make the next cell's result depend on the order the
    sampler drew them.

    *session_setup* names a registered hook for the host to carry: the
    hooked cell's opener (``tests/conformance/_entry.py``) is this
    same opener with one.
    """

    @contextlib.asynccontextmanager
    async def opener() -> "AsyncIterator[BaseHost]":
        with (
            tempfile.TemporaryDirectory(prefix="conformance-loopback-") as tmp,
            stand_in_elevation() as stand_in,
            loopback_sshd(Path(tmp), session_env=stand_in.env) as endpoint,
        ):
            root = Path(tmp)
            make_loopback_target(root, endpoint)
            # `term`/`transfer` are the spec's optional ACTIVE PIN, not a
            # menu edit: the host still validates them against the menu it
            # resolved, so a pair this venue drew but the host does not
            # serve fails here loudly rather than being quietly rewritten
            # to the menu's first entry. Measured against this very entry
            # -- `{"term": "telnet"}` raises
            # `ValueError: term 'telnet' is not in this host's term menu
            # ['ssh']`, and `{"transfer": "console"}` and
            # `{"transfer": "nc"}` raise the transfer analogue.
            flat = _loopback_entry(root)
            pinned = entry_of(flat) | {"term": term, "transfer": transfer}
            # Declared whether or not this cell switches to the stand-in: a
            # proxied cred is never read as the entry's own second user
            # (`switch_user_on`), so it changes no cell's route.
            login = pinned["creds"][0]["login"]
            pinned["creds"] = [*pinned["creds"], stand_in_cred_entry(login)]
            if session_setup is not None:
                pinned["session_setup"] = session_setup
            async with create_host_from_dict(pinned, element=element_of(flat)) as host:
                yield host

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
            family=LOCAL_FAMILY,
            switch_user=_stand_in_for(process_user() or ""),
            switch_user_stand_in=True,
            switch_user_may_sudo=True,
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
        with (
            tempfile.TemporaryDirectory(prefix=f"conformance-busybox-{release.version}-") as tmp,
            _local_stand_in() as stand_in,
        ):
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
            async with LocalHost(creds=_stand_in_creds(process_user() or "")) as host:
                # The stand-in elevation ahead of the applets: BusyBox's own
                # `su` and `id` would otherwise shadow it, and the hermetic
                # venue drives no real user switch (module docstring).
                await host.run(f'export PATH="{stand_in.bin_dir}:{applets}:$PATH"')
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
                where = probe.value
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
