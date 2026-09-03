"""Serializing this tree's access to the bed's SINGLE-CLIENT consoles.

Zephyr's ``shell_telnet`` backend accepts exactly ONE telnet client per
device. A second client is usually routine -- the guest logs ``Telnet client
already connected`` and keeps serving -- but when a send fails at accept time
it logs ``Failed to send 128, shutting down`` -> ``Telnet socket error`` ->
``Telnet shell backend initialized``, and after that re-init the guest refuses
every connection until someone runs ``make qemu-restart``. That is issue #260,
and it took two guests down in one day. The re-init is terminal: it does not
self-clear.

WHY THIS TREE NEEDS ITS OWN. ``tests/integration/host/conftest.py`` has grown
four protections for that console -- a per-device ``xdist_group`` stamped
``tryfirst``, the writer-fair lock this module reuses, an
``abort_console_transports`` teardown sweep, and ``_unhonored_group``'s
outcome re-check. NONE of them reach a sibling tree: a conftest's fixtures and
hooks apply to items under its own directory. So ``tests/conformance/``
inherits nothing, and the venue's default ``-n auto --dist loadgroup``
(``pyproject.toml`` addopts) would put two workers on one console.

WHAT THE COLLISION ACTUALLY LOOKS LIKE HERE, measured rather than assumed,
because it is not the shape the integration tree faces. The bed space is 49
cells of which 7 are ``bed-zephyr`` -- ONE cell per guest, since a Zephyr host
reports a single ``(telnet, console)`` pair. So two cells can never name the
same guest. The collision is between the CONTRACT ITEMS of ONE cell: every
drawn cell is parametrized into every contract in this tree (re-measured on
the hermetic lane 2026-08-26: 112 cell items over 8 cells, so fourteen items
per cell), and under ``-n auto`` those fourteen scatter across workers and
open the same console at once.

THE MECHANISM: an EXCLUSIVE hold of the repo's writer-fair console lock
(``tests/_fixtures/_console_lock.py``) around every console cell's item, taken
in an autouse fixture so it spans the whole window the test can have a console
open. EXCLUSIVE and not the SHARED hold the integration tree's per-device
tests take, and the difference is not a preference: SHARED is safe THERE only
because the ``xdist_group`` stamp has already pinned one device's tests to one
worker, so the shared holders are never two clients of one console. This tree
has no such stamp (see below), so SHARED would permit exactly the collision
above. EXCLUSIVE costs the parallelism of running two DIFFERENT guests at
once, which is 7 cells' worth of the space and, at the default budget of 8
drawn from 49, usually one.

WHAT THIS DOES NOT PROTECT AGAINST, stated here rather than discovered later:

- ANOTHER PYTEST SESSION on the same VM. Measured: ``getbasetemp().parent`` is
  ``/tmp/pytest-of-<user>`` under ``-n0`` but ``/tmp/pytest-of-<user>/pytest-<N>``
  under xdist, because each worker's basetemp is a child of the session's. So
  the lock is cross-WORKER, not cross-SESSION, whenever xdist is on -- and the
  suite's addopts turn it on. A second session driving the same guests (a
  concurrent ``make coverage-embedded``, say) is not serialized against this
  one. The derivation is kept identical to the integration tree's anyway: two
  spellings would be two locks, and a divergent one would look like protection
  while providing none.
- ANY CLIENT THAT IS NOT A PYTEST ITEM -- a person on ``telnet``, a
  ``scripts/`` tool, a stale forward from an earlier run.
- A SINGLE client wedging a guest on its own. #260's re-init path was not
  reproduced by any one- or two-client probe, so nothing here can claim it is
  impossible.

THE BUSYBOX GUESTS get their own, SEPARATE lock (:func:`serialized_family`),
and for a different reason. Their ``telnetd`` serves several clients, so they
carry no wedge risk; what they carry is a CPU risk (five TCG guests sharing
``test1``'s two cores) that the integration tree answers with a family
``xdist_group``. This tree recorded that THROUGHPUT exposure rather than
fixing it — until 2026-09-02, when a ``make release`` run paid for it: two
``telnet:nc`` cells on DIFFERENT guests (bb1281, bb1161) failed "Remote nc
listener on port 9000 not ready within 5.0s" on two workers simultaneously,
and both passed standalone in 2.3s. Parallel BusyBox cells starve each
other's guest CPU until otto's listener-readiness poll — contention-sensitive
by design, see ``src/otto/host/transfer/nc.py`` — expires. So BusyBox items
now serialize against EACH OTHER on a second writer-fair lock (its own
gate/resource files — holding the console lock for them would serialize them
against the Zephyr cells too, which share no resource with them), while
staying parallel with everything else. The same ``xdist_group`` and
timeout-raising alternatives were considered and rejected for the same
reasons as above — a distribution hint fails silently, and a longer timeout
just moves the load level at which the flake returns.

WHY NOT ``xdist_group``, which is what the integration tree uses -- AND THE
PLAN'S REASON FOR RULING IT OUT DOES NOT HOLD HERE, so this records the one
that does. ``tests/integration/host/conftest.py`` documents an
invocation-shape trap: its hook wins when the directory is reached through a
full-tree run and LOSES when the directory is named, because naming it makes
that conftest an *initial* one that registers at startup and therefore runs
after xdist has read the markers (measured there: zero group suffixes, 2.7
spread over four workers, 4 failed + 3 errors, a guest taken down by a CPU
exception). MEASURED IN THIS TREE, that does not reproduce, and the reason is
structural: ``tests/conformance`` is itself a ``testpaths`` entry, so this
tree's conftest is an *initial* conftest under BOTH shapes and the shape
changes nothing. What decides the stamp's fate is ``tryfirst``. Stamping
``xdist_group`` from this tree's ``pytest_collection_modifyitems`` and running
one cell's six items under ``-n2 --dist loadgroup``: WITHOUT ``tryfirst``,
inert in both shapes (no ``@group`` suffix, items split gw0/gw1); WITH it,
honoured in both (``@local`` suffix, all six on gw0).

So the option was live and it was still not taken, for three reasons that
survive the correction:

- A GROUP IS ONLY READ BY ``--dist loadgroup``. Measured, same stamp, same
  tree, ``-n2 --dist load``: silently ignored, items split, no suffix. The
  addopts do say ``loadgroup``, but a ``--dist`` on the command line then
  removes the protection with nothing to announce it. A lock is taken at
  runtime and is indifferent to how work was distributed.
- A GROUP'S FAILURE MODE IS SILENCE -- which is why the integration tree had
  to grow ``_unhonored_group`` to make it honest at all. The lock's outcome is
  a fact the kernel will answer directly (see
  :func:`unhonored_console_lock`), so its guard reads state rather than
  inferring it from a nodeid suffix.
- THERE IS ALMOST NO PARALLELISM TO PRESERVE. A group's whole advantage over
  an exclusive lock is that two DIFFERENT guests can still run at once; 7 of
  the 49 cells are console cells, one per guest, and at the default budget of
  8 a run draws about one of them.

WHY NOT DROP THE CONSOLE CELLS FROM THE SPACE. That is the safest option and
it was rejected on what it costs: spec s4 names the Zephyr guests as bed
hosts, and a venue that resolves 42 of 49 cells would ship without ever
reaching the one host family whose contract nothing else crosses. The
exclusion would also have to be argued down in the docs as deliberate rather
than accidental. The lock keeps all 49.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from otto.host.telnet import abort_console_transports
from tests._fixtures._console_lock import RESOURCE_NAME, console_access
from tests.conformance._bed import BED_BUSYBOX, BED_ZEPHYR
from tests.conformance._resolved import ResolvedCell

# Which venue kinds stand up a console that serves ONE client at a time.
#
# Read off the venue's KIND rather than off the element's name, for the reason
# `tests/conformance/_bed.py` sets out at length: every Zephyr guest in this
# lab data happens to be named `zephyr*`, so a name sniff passes on all seven
# while being a claim about a naming convention rather than about the host.
#
# The kind is still this suite's own vocabulary, so it can drift away from
# what otto actually builds. `tests/unit/test_conformance_bed.py` holds this
# set against otto's OWN answer -- `isinstance(host, EmbeddedHost)`, the class
# whose `__post_init__` is the only place in `src/` that sets
# `TelnetOptions.single_client_console=True` -- over the whole 49-cell space,
# so a renamed kind reddens there instead of silently unprotecting a guest.
SINGLE_CLIENT_CONSOLE_KINDS = frozenset({BED_ZEPHYR})

# Which venue kinds stand up guests that CONTEND FOR ONE CPU BUDGET (the five
# TCG BusyBox guests on `test1`'s two cores). Family members serialize against
# each other on their own lock (see the module docstring's BusyBox paragraph);
# they neither need nor take the single-client console lock.
SHARED_CPU_FAMILY_KINDS = frozenset({BED_BUSYBOX})

# The family lock's own files — a SEPARATE lock from the console one, so a
# BusyBox item and a Zephyr item still run in parallel (they share no
# resource; only same-family CPU contention is being serialized away).
FAMILY_GATE_NAME = "busybox_family.gate"
FAMILY_RESOURCE_NAME = "busybox_family.resource"

# The lock this process currently holds, innermost last. A list rather than a
# bool so the state is the PATH: the guard below re-derives nothing, it probes
# the very file the hold was taken on.
_HELD: "list[Path]" = []


def opens_a_single_client_console(resolved: ResolvedCell) -> bool:
    """Whether standing *resolved* up puts a client on a single-client console."""
    return resolved.kind in SINGLE_CLIENT_CONSOLE_KINDS


def shares_a_family_cpu_budget(resolved: ResolvedCell) -> bool:
    """Whether *resolved*'s guest contends for one CPU budget with its family."""
    return resolved.kind in SHARED_CPU_FAMILY_KINDS


@contextmanager
def serialized_family(lock_dir: Path) -> "Iterator[None]":
    """Hold the shared-CPU family lock EXCLUSIVELY for the duration of the block.

    One family item at a time across this session's workers — the runtime-lock
    translation of the integration tree's family ``xdist_group``. Unlike
    :func:`serialized_console` there is no held-path record and no transport
    sweep on exit: an interrupted BusyBox item leaves nothing wedged (its
    ``telnetd`` is multi-client), so the only job here is the flock itself,
    and closing the fds releases that even through a pytest-timeout signal.
    """
    with console_access(
        lock_dir,
        exclusive=True,
        gate_name=FAMILY_GATE_NAME,
        resource_name=FAMILY_RESOURCE_NAME,
    ):
        yield


def console_lock_dir(tmp_path_factory) -> Path:
    """The directory every process of this session takes the console lock in.

    ``getbasetemp().parent``, which is what
    ``tests/integration/host/conftest.py`` passes to the same lock -- one
    spelling, so the two trees serialize against each other if they ever share
    a session. Measured, and the measurement is the limitation this venue
    carries: ``-n0`` resolves it to ``/tmp/pytest-of-<user>`` (machine-wide)
    while xdist resolves it to ``/tmp/pytest-of-<user>/pytest-<N>``, because a
    worker's basetemp is a child of the controller's. Under the suite's default
    ``-n auto`` the lock is therefore per-SESSION.
    """
    return Path(tmp_path_factory.getbasetemp()).parent


@contextmanager
def serialized_console(lock_dir: Path) -> "Iterator[None]":
    """Hold the console EXCLUSIVELY for the duration of the block.

    Three things in one context manager because their ORDER is the contract
    and a caller that got it wrong would still look right:

    1. the writer-fair EXCLUSIVE hold (``console_access``), so no other worker
       of this session can be on any single-client console;
    2. the record of WHICH lock file is held, which is the only thing
       :func:`unhonored_console_lock` will accept as evidence;
    3. ``abort_console_transports()`` on the way out, WHILE THE LOCK IS STILL
       HELD. That sweep exists because ``pytest-timeout``'s signal can abort a
       test before its ``close()`` runs, leaving the guest's one client slot
       occupied by a transport nobody owns; releasing the lock first would
       hand the next holder a console that is still taken.
    """
    with console_access(lock_dir, exclusive=True):
        _HELD.append(Path(lock_dir) / RESOURCE_NAME)
        try:
            yield
        finally:
            try:
                abort_console_transports()
            finally:
                _HELD.pop()


def unhonored_console_lock() -> "str | None":
    """Why this process is NOT holding the console exclusively, or None if it is.

    The outcome, not the intent -- the same distinction
    ``tests/integration/host/conftest.py``'s ``_unhonored_group`` draws when it
    refuses to accept the presence of an ``xdist_group`` marker as proof that
    xdist applied it. "The fixture ran" is this mechanism's version of that
    marker: a ``console_access`` that opened its files somewhere else, or that
    was replaced by a no-op, leaves every piece of local bookkeeping looking
    correct and the guest just as exposed.

    So the evidence is the KERNEL's. A second file descriptor on the same lock
    file is opened and asked for a NON-BLOCKING SHARED ``flock``: an exclusive
    hold anywhere on the machine refuses it. ``flock`` locks are tied to the
    open file description rather than to the process (measured on this VM: a
    second fd in the SAME process gets ``BlockingIOError: [Errno 11]``), which
    is what makes this readable from inside the holder.

    Returns the reason rather than a bool so the caller can say which file it
    was and not look it up a second time.
    """
    if not _HELD:
        return (
            "this process holds NO exclusive console lock -- "
            "tests/conformance/conftest.py's autouse console fixture did not run for "
            "this item, or did not recognise its cell as a single-client console one"
        )
    path = _HELD[-1]
    probe = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    else:
        fcntl.flock(probe, fcntl.LOCK_UN)
        return (
            f"the console lock file {path} is NOT actually locked, though this process "
            f"recorded a hold on it -- a shared flock on a second descriptor was granted "
            f"immediately, so nothing is stopping another worker from opening the same "
            f"console"
        )
    finally:
        os.close(probe)
