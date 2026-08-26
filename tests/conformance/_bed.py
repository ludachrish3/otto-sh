"""Which host-contract cells the BED venue can build, resolved from lab data.

Bed means the dev VM's real lab: the unix guests, the five pinned-userland
BusyBox guests behind ``test1``, and the Zephyr guests behind ``test4``. The
cells are crossed by :func:`tests._fixtures.profiles.axis_space`, which reads
each host's ``(term, transfer)`` menus off the host **otto builds** rather
than off ``lab.json``, so a host that gains a transfer changes its own cell
count with no edit here.

WHAT THIS VENUE CLAIMS THAT ``tests/integration/host/`` DOES NOT.
``tests/integration/host/test_host_contract.py`` already asserts the same
host contracts against these same bed hosts. It parametrizes by BACKEND ID --
``("ssh", "telnet", "local", *EMBEDDED_BACKENDS, *BUSYBOX_BACKENDS)`` -- which
is ONE transport per backend, and the transfer is whatever that backend's
host defaults to. This venue's distinct claim is the CROSSING: the same
contract over every ``(term, transfer)`` pair a host's own menus permit. That
is where ``test1`` stops being one row and becomes eight, and it is the only
place a contract that holds over ``scp`` and breaks over ``nc`` on the same
host can be seen. A contract asserted here that does not cross is not
earning its place; it is a copy of one over there.

RESOLUTION IS HOSTLESS AND MUST STAY THAT WAY. Everything in this module is a
function of committed lab data under ``tests/_fixtures/lab_data/``: no host is
contacted, no socket is opened, nothing is fetched. That is what lets the
space, its order and its exclusions be asserted in ``tests/unit/``. Reaching
the bed is the OPENER's job, and openers run only for the cells a run actually
draws.

ORDER IS PART OF THE CONTRACT, and be exact about why, because the obvious
reason is not the true one. MEASURED against
``tests/conformance/_sample.draw``: with a budget below the space size the
draw ranks each cell by ``blake2b(seed:label)``, so it is keyed on the cell's
LABEL and is INDEPENDENT of the space's order -- five shuffles of the
hermetic space at each of three seeds returned the identical draw. Order is
still load-bearing at two points that a moving order would break silently:

- ``draw`` returns ``list(space)`` UNCHANGED when the budget is ``None``
  (``OTTO_CONFORMANCE_CELLS=all``), when the budget is at or above the space
  size, or when there is no root seed -- measured: the same shuffle changes
  the result on that path. There the space's order IS the parametrization
  order.
- every process in an xdist session resolves this space independently, and
  ``tests/conformance/conftest.py`` records that xdist rejects a session
  whose workers collected different test ids. A space ordered by anything
  process-local (a ``set`` of strings, say) would differ per worker.

So the order is pinned to a recorded expectation in
``tests/unit/test_conformance_bed.py`` rather than merely compared with
itself. Two calls of a stable-but-wrong implementation agree with each other.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from otto.host.embedded_host import EmbeddedHost
from otto.host.factory import create_host_from_dict
from otto.host.host import BaseHost
from otto.host.remote_host import RemoteHost
from tests._fixtures.labdata import host_data
from tests._fixtures.profiles import Cell, _ensure_custom_frames, axes_for, axis_space
from tests.conformance._lab_context import bed_lab_context
from tests.conformance._resolved import ResolvedCell
from tests.conformance._vocabulary import Vocabulary, vocabulary_for_userland

# The lab-data tech the bed venue is built from. `axis_space` and `axes_for`
# both default to it; naming it once here keeps `BED_LABS` below and the
# hosts it crosses reading off the same file.
BED_TECH = "tech1"

# Pinned, not derived from a set or from a dict iteration: this is the order
# the space is concatenated in, and the module docstring above sets out the
# two paths a moving order breaks.
#
# `tech2`'s `unix_alt` lab is deliberately absent, and NOT because those hosts
# are imaginary. Measured: alt1/alt2/alt3 carry the IPs 10.10.200.11/12/13 --
# byte-identical to test1/test2/test3. They are the same three VMs declared a
# second time under a second tech, which is the fixture for otto's
# multi-source lab loading. Crossing them here would re-offer the same
# hardware under different element names -- measured, 24 more cells for the
# sampler to spend its budget on and no new coverage at all.
BED_LABS: list[str] = ["unix", "busybox", "embedded"]

BED_UNIX = "bed-unix"
BED_BUSYBOX = "bed-busybox"
BED_ZEPHYR = "bed-zephyr"


def _kind_for_userland(userland: str) -> str:
    """The venue kind that stands up a host of this userland.

    Split on the USERLAND string
    :func:`tests._fixtures.profiles.axes_for` resolved, which is the one axis
    that is neither the element's name nor its lab nor its ``os_type`` --
    and each of those three would be wrong on a host in this very lab data:

    - the name: every Zephyr guest here happens to be called ``zephyr*``, so
      an ``element.startswith("zephyr")`` sniff passes on all seven while
      being a guess about a naming convention rather than about the host;
    - the lab: ``test4`` declares membership in ``embedded`` and is a plain
      GNU unix VM (measured: ``axes_for("test4").userland == "gnu"``), so a
      kind read off the lab name calls it a Zephyr guest;
    - ``os_type``: the five BusyBox guests declare ``os_type: "unix"``
      (measured, ``tests/_fixtures/lab_data/tech1/lab.json``), so a kind read
      off it calls them plain unix hosts and the pinned-userland axis
      disappears.

    An unrecognised userland RAISES rather than falling into a default
    bucket: a new userland layer must announce itself here, not be quietly
    filed under unix and then asserted against with the wrong expectations.
    """
    if userland.startswith("zephyr-"):
        return BED_ZEPHYR
    if userland.startswith("busybox-"):
        return BED_BUSYBOX
    if userland == "gnu":
        return BED_UNIX
    raise ValueError(
        f"no bed venue kind for userland {userland!r} -- "
        f"tests/conformance/_bed.py must name the machinery that stands it up"
    )


def bed_vocabulary(element: str) -> "Vocabulary":
    """The spellings *element*'s shell answers the contracts in.

    THE SAME AXIS ``bed_kind`` SPLITS ON, and for the same three measured
    reasons: the element name is a naming convention, the lab is wrong
    (``test4`` is in ``embedded`` and is a plain GNU VM), and ``os_type`` is
    wrong (the BusyBox guests declare ``os_type: "unix"``). Two questions, one
    fact about the host.

    DERIVED, NOT TABULATED, which is the whole reason this is a function of
    ``element`` rather than a dict literal keyed by one. ``axes_for`` resolves
    the userland from the host otto BUILDS out of ``lab.json``, so a new guest
    gets a correct vocabulary for free -- the reason ``tests/conftest.py``'s
    ``_zephyr_kit`` derives its ``temp_remote_dir`` rather than listing it. A
    table keyed by element goes stale silently: a missing entry looks exactly
    like a passing cell, because nothing would ever ask for it.

    Keyed by ELEMENT and not by ``Cell`` for the same reason
    :func:`bed_scratch_dir` is: the pin a cell carries is a TRANSPORT choice,
    and no transport changes which commands a shell understands.
    """
    return vocabulary_for_userland(axes_for(element, BED_TECH).userland)


def bed_kind(element: str) -> str:
    """How the bed venue stands *element* up: the ``kind`` its cells carry.

    ``kind`` is what :func:`tests.conformance._sample.cell_label` puts in
    front of every cell in the run's log and in every parametrization id, so
    a reader of a failure line can tell a BusyBox guest from the unix VM it
    is hopped through without looking the element up.
    """
    return _kind_for_userland(axes_for(element, BED_TECH).userland)


# How long a bed host has to answer the connect probe before this venue calls
# it unreachable. A RUNAWAY BOUND, not a discriminator: no assertion reads it,
# so widening it can only make the venue more patient. It exists because the
# failure it bounds has no natural end -- `verify_connection` is unbounded,
# and a VM that is powered off blackholes the SYN rather than refusing it, so
# without a bound one dead host hangs the whole lane instead of naming itself.
_ANSWER_TIMEOUT_S = 60.0


def build_bed_host(cell: "Cell") -> "RemoteHost":
    """Build *cell*'s host through otto's factory, PINNED to the cell's pair.

    THE SAME CALL THE BED'S OWN FIXTURES MAKE. ``tests/conftest.py`` builds
    every real bed host as ``create_host_from_dict`` over that element's raw
    ``lab.json`` entry -- ``host1``'s embedded and BusyBox branches, and
    ``transfer_host``'s guest branch, which pins its transfer into the entry
    exactly as this does. So this is that path with the cell's pair pinned,
    not a second construction: re-deriving a host here would be this suite
    restating otto's answer, the drift ``tests/_fixtures/profiles.py`` exists
    to prevent, and it would keep passing after the factory stopped agreeing.

    THE CALL IS NOT THE WHOLE STORY, and the difference cost this item a task.
    The bed's own fixtures make that call INSIDE an installed ``OttoContext``
    whose lab holds the hop targets, and a host this function builds carries
    no lab of its own -- so for the 17 hopped cells the call alone produces a
    host that cannot resolve its hop. ``_opener_for`` below supplies the
    context; ``tests/conformance/_lab_context.py`` is where it comes from.

    THE PIN IS THE WHOLE POINT OF THIS VENUE. An opener that built the entry
    unpinned would get whichever ``(term, transfer)`` the host defaults to,
    which is one row per host -- exactly what
    ``tests/integration/host/test_host_contract.py`` already covers -- and the
    crossing this lane exists for would silently collapse into it while every
    cell id still claimed the pair it was drawn for.

    Two layers make a WRONG pin visible, because they catch different wrongs:

    - the factory validates the pin against the menu the host resolved, so a
      pair outside it raises. Measured against this lab data:
      ``{"term": "ssh"}`` on ``bb1161`` raises ``term 'ssh' is not in this
      host's term menu ['telnet']``, and ``{"transfer": "scp"}`` on
      ``zephyr37_fat`` raises the transfer analogue.
    - that check cannot see a pin that is wrong but IN the menu -- passing
      ``host.valid_terms[0]`` for every cell would satisfy it on all 49 --
      so the built host is asked what pair it came out with and compared
      against the cell's. ``tests/unit/test_conformance_bed.py`` runs that
      comparison over the whole space with no bed.

    ``_ensure_custom_frames`` first, and not defensively: ``zephyr27_fat``
    declares ``command_frame: "zephyr-inline"``, a class that lives in
    ``tests/custom_hosts`` rather than in otto, and the root conftest's
    ``_isolate_registries`` fixture evicts the registering module per test.
    Resolution registers it once at collection; an opener that relied on that
    surviving would build or fail depending on what else the session had
    collected -- the invocation-shape dependence that function's own
    docstring was written for.
    """
    _ensure_custom_frames()
    entry = dict(host_data(cell.element, BED_TECH))
    host = create_host_from_dict({**entry, "term": cell.term, "transfer": cell.transfer})
    if (host.term, host.transfer) != (cell.term, cell.transfer):
        raise RuntimeError(
            f"bed cell {cell.element}:{cell.term}:{cell.transfer} built a host reporting "
            f"{host.term}/{host.transfer} instead -- the cell's pair did not reach the "
            f"factory, so this cell is measuring a transport nobody drew"
        )
    return host


async def _require_an_answer(cell: "Cell", host: "RemoteHost") -> None:
    """Fail loudly, naming the host, unless *host* answers a connect probe.

    Spec s4, verbatim: *"A host that should be reachable in the selected venue
    and is not fails loudly, naming the host -- never a skip."* This is where
    that happens, and it is not decoration over what otto already does.
    MEASURED, both halves:

    - :meth:`~otto.host.host.BaseHost.__aenter__` returns ``self`` and
      contacts NOTHING (``src/otto/host/host.py``), so an opener that only
      entered the host would hand a contract a host it had never reached.
    - the first command against an unreachable host raises a bare
      ``ConnectionRefusedError: [Errno 111] Connect call failed
      ('127.0.0.1', 45247)`` -- measured here against a closed local port.
      That names an address and a port and NOT the element, which is the one
      thing spec s4 requires it to name.

    :meth:`~otto.host.remote_host.RemoteHost.verify_connection` is otto's own
    answer to "is this host there", family-specific through
    ``_probe_connection`` (unix dials its term channel and warms FTP when the
    transfer is ``ftp``; embedded opens its single console), and it runs NO
    command -- so this needs no per-kind probe command and cannot drift from
    what otto considers a connection. It reports the reason as a
    ``CommandResult`` rather than raising, which is why this reads
    ``status.is_ok`` instead of catching.

    NEVER ``pytest.skip``. A skipped cell reports success for a contract
    nobody ran, and the sampler has already announced that cell as drawn.
    """
    try:
        probe = await asyncio.wait_for(host.verify_connection(), _ANSWER_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise RuntimeError(_did_not_answer(cell, f"no answer within {_ANSWER_TIMEOUT_S}s")) from exc
    if not probe.status.is_ok:
        raise RuntimeError(_did_not_answer(cell, str(probe.value)))


def _did_not_answer(cell: "Cell", detail: str) -> str:
    """The loud-failure text, in one place so both callers name the host alike."""
    return (
        f"bed host {cell.element!r} is in the bed lab data but did not answer over "
        f"{cell.term}/{cell.transfer}: {detail}. The bed venue never skips -- a "
        f"skipped cell reports success for a contract nobody ran."
    )


# Where a transfer to a POSIX bed host is staged. THE ONE VALUE ON THIS PATH
# OTTO DOES NOT ANSWER, and it is named here rather than derived because there
# is nothing to derive it from: `UnixHost.default_dest_dir` defaults to an
# empty `Path()`, which `RemoteHost._resolve_dest` returns UNCHANGED -- so the
# landing directory becomes whatever the transport resolves a relative path
# against, the login directory, whose writability is a property of the account
# rather than of the host. `tests/conftest.py` already made this call for
# these hosts: `_UNIX_KIT` and `_BUSYBOX_KIT` both declare
# `temp_remote_dir="/tmp"`, and `tests/integration/host/test_host_contract.py`
# transfers there against `test1` and all five BusyBox guests on every run of
# that tree. This is that value, not a second scheme -- and a host that
# DECLARES a `default_dest_dir` in lab data overrides it below without an edit
# here.
_POSIX_SCRATCH_DIR = Path("/tmp")


def bed_scratch_dir(element: str) -> "Path | None":
    """Where a transfer to *element*'s host lands ON THE DEVICE, or None when nowhere does.

    OTTO'S ANSWER, ASKED OF THE HOST OTTO BUILT, in the order otto itself
    resolves it -- not a table keyed by element and not a predicate of this
    suite's own. `tests/conftest.py`'s `_zephyr_kit` is the prior art and its
    docstring is the reason: `build_filesystem` is *"one source of truth for
    'where does this FS live on the device', shared with the production
    factory"*, so a new guest added to `lab.json` gets a correct path here for
    free. This asks one layer higher than that helper does -- the built host
    rather than `build_filesystem` directly -- because
    :class:`~otto.host.embedded_host.EmbeddedHost` is what turns the declared
    variant into a landing directory, and asking it covers the `fat-ram` ->
    ``/RAM:`` and `littlefs` -> ``/lfs`` resolution AND a per-host
    `default_dest_dir` override in one read.

    KEYED BY ELEMENT AND BUILT UNPINNED, which is the same shape
    :func:`tests._fixtures.profiles.axes_for` uses and not an accident of
    convenience. A cell's ``(term, transfer)`` pin cannot move where a file
    lands -- measured, `test1`'s eight cells all answer ``/tmp`` -- so the
    question has no cell in it. Going through :func:`build_bed_host` instead
    would ALSO make space RESOLUTION depend on the seam this module's opener
    tests stub to control what gets OPENED: measured, four of them replace
    `build_bed_host` with a probe double and every one of them broke the
    moment resolution started calling it. Two concerns, one seam, and the
    double would have had to grow fields it has no business carrying.

    Three answers, measured against this lab data:

    - ``None`` for the three guests declaring ``filesystem: "none"``
      (`zephyr37_nofs`, `zephyr37_llext`, `zephyr44_llext`). NOT this
      module's judgement: their filesystem reports ``supports_transfer``
      False, which is the very flag
      ``otto.host.transfer.console.ConsoleFileTransfer`` short-circuits
      ``_run_put`` and ``_run_get`` on -- so otto answers "nowhere", loudly
      and by design, and this reads that answer rather than restating it.
      Verified rather than assumed: ``build_filesystem("none").mount`` is
      ``None`` and ``supports_transfer`` is ``False``.
    - the resolved mount for the four guests that have one: ``/RAM:`` for
      `zephyr37_fat` and `zephyr27_fat`, ``/lfs`` for `zephyr37_lfs` and
      `zephyr44_lfs`. Read off ``default_dest_dir``, which
      ``EmbeddedHost.__post_init__`` resolves FROM ``filesystem.mount``, so a
      lab entry that overrides the landing directory is honoured here without
      an edit.
    - :data:`_POSIX_SCRATCH_DIR` for the nine POSIX hosts, whose
      ``default_dest_dir`` otto leaves at its empty default.

    THE NO-FILESYSTEM CHECK MUST PRECEDE THE ``/tmp`` FALLBACK, and be exact
    about which ordering that is -- an earlier draft of this paragraph said
    the check had to come before the ``default_dest_dir`` branch, and a
    mutation measured that FALSE: swapping those two adjacent branches leaves
    all 67 hostless tests green, because a filesystem-less host reports the
    empty ``default_dest_dir`` (measured, `zephyr37_nofs` reports
    ``PosixPath('.')``) and so never takes that branch either way. What is
    real is the fallback: drop the check, or move it behind
    :data:`_POSIX_SCRATCH_DIR`, and all three no-filesystem guests are handed
    ``/tmp``. Their cells then look applicable while every transfer against
    them fails -- measured, both mutations red on four tests, the first being
    ``assert [] == ['zephyr37_llext', 'zephyr37_nofs', 'zephyr44_llext']``.
    """
    _ensure_custom_frames()
    host = create_host_from_dict(dict(host_data(element, BED_TECH)))
    if isinstance(host, EmbeddedHost) and not host.filesystem.supports_transfer:
        return None
    if host.default_dest_dir != Path():
        return host.default_dest_dir
    return _POSIX_SCRATCH_DIR


def _scratch_for(dest: "Path | None") -> "Callable[[Path], Path] | None":
    """Wrap *dest* in the shape :class:`ResolvedCell.remote_scratch` carries.

    The argument is the test's local ``tmp_path`` and this venue ignores it:
    a bed cell's remote directory is on the far side of a hop, and the runner
    path the hermetic venue answers with would be exactly the bug this task
    was written to fix (``scp: /tmp/pytest-of-vagrant/.../remote: No such file
    or directory``, on every bed cell). The parameter exists because the
    HERMETIC venue's answer genuinely needs it.
    """
    if dest is None:
        return None

    def scratch(_tmp_path: Path) -> Path:
        return dest

    return scratch


def _opener_for(cell: "Cell") -> "Callable[[], AbstractAsyncContextManager[BaseHost]]":
    """Open *cell*'s real bed host, pinned to its pair, or fail naming the host.

    ONE opener for all three kinds, and that is a finding rather than a
    shortcut. The plan asked for one per kind; measured, the three kinds
    differ in nothing this function does. otto's factory already dispatches
    on ``os_type`` to the right class, and ``verify_connection`` already
    dispatches on the family to the right probe, so a per-kind opener here
    would be the test tree restating otto's dispatch -- and would keep
    agreeing with itself after otto's stopped.

    THE ONE REAL PER-KIND DIFFERENCE IS NOT HANDLED HERE, and it is not
    absent either: the Zephyr console serves exactly ONE client, and the
    serialization for it lives in ``tests/conformance/_console_safety.py``,
    which holds that console EXCLUSIVELY around every item whose DRAWN cell
    opens one. Its docstring is where the mechanism, the rejected options and
    the gaps are argued; the gaps are real (nothing serializes a second pytest
    SESSION, and an item that is not parametrized over ``resolved_cell`` is not
    covered at all). Two clients on one console can re-initialise the guest's
    telnet backend, after which it refuses every connection until ``make
    qemu-restart`` (issue #260). Worth repeating one measurement from there,
    because the hazard is not the obvious shape: a Zephyr host reports a single
    ``(telnet, console)`` pair, so there is ONE cell per guest and two cells can
    never name the same one -- what collides is the seven contract items of ONE
    cell, which this tree's default ``-n auto`` scatters across workers.

    Built per cell, never per session: a contract that leaves a host's
    session in a bad state must not make the next cell's result depend on the
    order the sampler drew them. The host is entered as a context manager so
    its transports are closed even when the probe below raises.

    THE LAB CONTEXT WRAPS THE WHOLE OPEN, and 17 of the 49 cells cannot open
    without it. A host `create_host_from_dict` builds from one lab entry has
    no `_lab` back-reference, so `RemoteHost._build_hop_transport` falls back
    to the active `OttoContext` to resolve its hop -- and this tree installs
    none, which is why the BusyBox and Zephyr guests failed before any
    transport existed. `tests/conformance/_lab_context.py` is the installer
    and its docstring argues why it lives in the OPENER rather than in a
    conftest fixture, and why this venue duplicates
    `tests/integration/host/conftest.py`'s `_install_integration_lab` instead
    of sharing it. It brackets `build_bed_host` as well as the open, which is
    what the integration tree's module-scoped install also does -- a host
    built outside a context and opened inside one would be a third
    construction condition nothing else in the repo uses.
    """

    @contextlib.asynccontextmanager
    async def opener() -> "AsyncIterator[BaseHost]":
        with bed_lab_context(BED_TECH):
            host = build_bed_host(cell)
            async with host:
                await _require_an_answer(cell, host)
                yield host

    return opener


def bed_space() -> "list[ResolvedCell]":
    """Every ``(host, term, transfer)`` the bed's lab data permits, in pinned order.

    Concatenated lab by lab in :data:`BED_LABS` order, and each lab's cells
    in the order :func:`~tests._fixtures.profiles.axis_space` emits them --
    the lab file's own host order, and within a host the term-major crossing
    of the menus the built host reported. Nothing here sorts: measured,
    ``test2`` reports its terms as ``['telnet', 'ssh']`` while ``test1`` and
    ``test3`` report ``['ssh', 'telnet']``, so a sort would be this module
    inventing a value the host did not give.

    DE-DUPLICATED ACROSS LABS, first appearance winning, and this fires today
    rather than being defensive: ``test1`` declares membership in BOTH
    ``unix`` and ``busybox`` (measured, ``lab.json``), so the raw
    concatenation is 57 cells of which 8 are ``test1``'s, listed twice. A
    duplicate is not cosmetic here. :func:`tests.conformance._sample.draw`
    samples without replacement and keys its rank on the cell's LABEL, so two
    identical cells get identical ranks and take two of the budget's slots
    between them -- one contract asserted twice while another goes unasserted,
    with the run's own ``drew N of M`` line still reading correctly. They
    would also collide as pytest parametrization ids. 57 raw, 49 after.

    Exclusions: none. Every cell the lab data permits is offered, because
    nothing has been shown to be unbuildable -- and the spec's rule is that a
    cell the venue cannot build leaves the SPACE, never that it is drawn and
    skipped. The console-safety decision was the one place an exclusion might
    have landed, and it went the other way: the seven single-client console
    cells are SERIALIZED rather than dropped
    (``tests/conformance/_console_safety.py`` argues why), so the space is all
    49 and the docs page has no exclusion to state.

    Nor is a cell that has NOWHERE TO PUT A FILE an exclusion from here. The
    three guests declaring ``filesystem: "none"`` carry
    ``remote_scratch=None`` (:func:`bed_scratch_dir`) and stay in the space,
    because a Zephyr host reports a single ``(telnet, console)`` pair -- so
    dropping the cell would delete the guest and take its exec and timeout
    coverage with it, paying for one inapplicable contract with two
    applicable ones. The narrowing belongs to the CONTRACT that needs a
    remote directory, which declares its applicable domain; see
    ``tests/conformance/test_transfer_contract.py`` and
    ``tests/conformance/conftest.py``'s ``pytest_generate_tests``.
    """
    kinds: dict[str, str] = {}
    scratch: dict[str, Path | None] = {}
    words: dict[str, Vocabulary] = {}
    seen: set[Cell] = set()
    resolved: list[ResolvedCell] = []
    for lab in BED_LABS:
        for cell in axis_space(lab, BED_TECH):
            if cell in seen:
                continue
            seen.add(cell)
            if cell.element not in kinds:
                kinds[cell.element] = bed_kind(cell.element)
                # Per ELEMENT because the QUESTION is per element --
                # `bed_scratch_dir` and `bed_vocabulary` take no cell, and
                # their docstrings give the measurement. 16 host builds at
                # resolve time rather than 49.
                scratch[cell.element] = bed_scratch_dir(cell.element)
                words[cell.element] = bed_vocabulary(cell.element)
            resolved.append(
                ResolvedCell(
                    cell=cell,
                    kind=kinds[cell.element],
                    open_host=_opener_for(cell),
                    remote_scratch=_scratch_for(scratch[cell.element]),
                    vocabulary=words[cell.element],
                )
            )
    return resolved
