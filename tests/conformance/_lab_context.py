"""The lab context a HOPPED bed cell needs before it can open at all.

17 of the bed venue's 49 cells name a host that is only reachable through
another one: the five BusyBox guests hop ``test1`` and the seven Zephyr
guests hop ``test4`` (measured, ``tests/_fixtures/lab_data/tech1/lab.json``).
:meth:`otto.host.remote_host.RemoteHost._build_hop_transport` resolves that
hop id against the host's own ``_lab`` back-reference and, when there is
none, against the active :class:`~otto.context.OttoContext`. A host built by
``create_host_from_dict`` from a single lab entry has neither -- measured,
``create_host_from_dict(host_data("bb1161")).._lab is None`` -- so every one
of those 17 cells failed BEFORE any transport was created:

    RuntimeError: Host 'bb1161 qemu' cannot resolve hop 'test1': the host has
    no lab back-reference and there is no active OttoContext. Add the host to
    a Lab (Lab.add_host) or run within `otto.open_context(...)`.

Nothing had ever opened one, which is why item 4's plan reached Task 4 before
anyone noticed: the space, the pins and the openers' shapes are all
assertable hostlessly, and all of them were green.

DUPLICATED FROM ``tests/integration/host/conftest.py``'s
``_install_integration_lab`` (and from ``tests/integration/busybox_bed/
conftest.py``'s ``_load_lab``, which installs the same thing for ``test1``),
and the duplication is DELIBERATE for this item rather than an oversight.
Sharing a helper means editing that tree, and verifying an edit there means
RUNNING it -- and every pytest run under ``tests/integration/`` triggers a
session-scoped autouse fixture that SSHes to a lab host and ``docker rm -f``s
matching containers. Consolidating belongs to an item that can afford to
re-verify both trees; until then this file carries the mechanism and the
pointer rather than pretending the two are already one.

WHAT IS NOT COPIED, and why the difference is an improvement rather than a
drift: the integration tree hand-builds its hop hosts as ``UnixHost(...)``
from raw lab fields. ``tests/integration/busybox_bed/conftest.py``'s own
module docstring argues against exactly that for the guests -- *"Direct
``UnixHost(...)`` would default ``term="ssh"`` and fail menu validation --
don't"* -- and the same reasoning applies one hop up. The hop hosts here go
through ``create_host_from_dict`` over the committed entry, which is the call
:func:`tests.conformance._bed.build_bed_host` already makes for the cell's
own host, so there is ONE construction path in this venue and it is otto's.

INSTALLED BY THE OPENER, NOT BY A CONFTEST FIXTURE. That was a live choice
and the fixture loses on two counts measured in this tree:

- ``tests/conformance/test_bed_opener_witness.py`` carries no
  ``resolved_cell`` and is marked ``conformance_bed`` precisely so it reaches
  the bed WITHOUT ``OTTO_CONFORMANCE_BED=1``. A fixture gated on the venue
  would therefore step aside for the one file whose whole job is to open a
  bed host, and a fixture NOT gated on the venue would install bed lab data
  into the hermetic lane -- the lane CI runs nightly with no lab at all.
- the window a hop needs the lab open is exactly the window the host is
  open. Scoping the install to the opener's ``async with`` makes that
  literal, so nothing outside it inherits an ambient context it did not ask
  for, and no test can be ordered in a way that opens a bed host without one.

The remaining cost is that this mutates a process-global ``ContextVar`` for
the duration of an open. It is bracketed by the token
:func:`otto.context.set_context` hands back, and
``tests/unit/test_conformance_bed.py`` pins that the context is gone again on
the way out.

WHY NOT GIVE THE CELL'S OWN HOST A ``Lab`` INSTEAD -- the third option, and
the one that needs no ambient state at all: ``Lab.add_host`` sets the host's
``_lab`` back-reference, which would make the hop resolve with no context in
sight. It was rejected because it MUTATES THE HOST UNDER TEST. ``add_host``
also stamps ``source_lab`` (measured: ``create_host_from_dict`` leaves it
``""``, and the dev-tool ingest gate reads it), so the object this venue
asserts contracts against would stop being the object
``tests/integration/host/`` and ``tests/integration/busybox_bed/`` assert
against. A venue whose job is to cross ``(term, transfer)`` over otto's own
hosts should not be handing the contracts a host it has edited.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.host.factory import create_host_from_dict
from tests._fixtures.labdata import flatten_lab_doc, host_data, lab_data_path

# The name the venue's lab answers to. It reaches a reader in two places: the
# ``hop 'x' not in lab 'conformance_bed'`` KeyError otto raises when a hop
# cannot be resolved, and the ``source_lab`` stamp ``Lab.add_host`` puts on
# the hop hosts built here (they are built unattributed, so the lab's name is
# what they end up carrying -- the same thing happens to the integration
# tree's ``integration_host`` hosts).
BED_LAB_NAME = "conformance_bed"


def hop_targets(tech: str) -> "list[str]":
    """Every host the *tech* lab data hops through, in first-declared order.

    DERIVED, never listed. A literal ``["test4", "test1"]`` would be correct
    against today's lab data and silently wrong against tomorrow's -- a guest
    repointed at a different jump host would go back to failing at hop
    resolution, with this module still looking right. The derivation reads
    the same committed file :func:`tests.conformance._bed.bed_space` is built
    from, and ``tests/unit/test_conformance_bed.py`` injects lab data naming
    a hop target the real bed does not have, because a hard-coded pair passes
    every real-data assertion in that file (measured: 50 of them).

    ONE PASS COVERS A CHAIN OF ANY DEPTH, and the walk that looks like it is
    needed is not. ``_create_tunnel`` does build its hop's OWN hop transport
    when the hop host declares a ``hop`` of its own, resolving it against the
    same lab -- so a two-deep chain really does need both links present. But
    every link of such a chain is itself a host that DECLARES a hop, so its
    hop is already collected by this single pass over the entries. Measured,
    and it is the reason this function is a loop and not a graph walk: a
    transitive version was written first, and deleting its recursive step
    left all 54 tests green because the step could never add an element the
    first pass had missed. The chain test in
    ``tests/unit/test_conformance_bed.py`` asserts the OUTCOME -- both links
    present -- which is what a narrowed pass would break.

    A hop naming an element the lab data does not declare raises here rather
    than at open time, where it would arrive as an otto ``KeyError`` about a
    lab this tree assembled.
    """
    entries = flatten_lab_doc(json.loads(lab_data_path(tech).read_text()))
    declared = {entry["element"] for entry in entries}
    targets: "list[str]" = []
    for entry in entries:
        hop = entry.get("hop")
        if hop is None or hop in targets:
            continue
        if hop not in declared:
            raise KeyError(
                f"the {tech!r} lab data hops through {hop!r}, which it does not "
                f"declare as a host -- the bed venue cannot build a lab that resolves "
                f"it. Declared: {sorted(declared)}"
            )
        targets.append(hop)
    return targets


def bed_lab(tech: str) -> Lab:
    """A :class:`~otto.config.lab.Lab` holding every hop target *tech* names.

    The hop targets ONLY. A lab carrying all sixteen bed hosts would resolve
    the same hops and say something this venue does not mean: the cell's own
    host is built per open by :func:`tests.conformance._bed.build_bed_host`
    and is deliberately NOT a member of this lab, so the contract under test
    is asserted against the same object every other bed suite builds.

    The membership check is not decoration. ``Lab.add_host`` keys on
    ``host.id``, which ``make_host_id`` derives from the element AND its
    board/slot -- measured, ``bb1161`` builds as ``bb1161_qemu`` -- while a
    ``hop`` field names an ELEMENT. Today every hop target is boardless and
    the two coincide; a jump host that gained a board would make ``lab.hosts``
    miss the key otto looks up, and the failure would surface at open time as
    an otto ``KeyError`` about a lab this tree assembled. It is asserted here,
    where the name is still in hand.
    """
    lab = Lab(name=BED_LAB_NAME)
    targets = hop_targets(tech)
    for element in targets:
        lab.add_host(create_host_from_dict(dict(host_data(element, tech))))
    unresolvable = [element for element in targets if element not in lab.hosts]
    if unresolvable:
        raise RuntimeError(
            f"the bed venue's lab cannot answer to {unresolvable} -- a hop names an "
            f"ELEMENT while a Lab is keyed by host ID, and these built under a "
            f"different id. Lab holds: {sorted(lab.hosts)}"
        )
    return lab


@contextmanager
def bed_lab_context(tech: str) -> "Iterator[Lab]":
    """Install a context whose lab resolves *tech*'s hops, for the block's duration.

    Bracketed by the token :func:`otto.context.set_context` returns rather
    than by re-setting ``None`` on the way out: the block is entered from
    inside a test that may already be running under a context of its own, and
    restoring the previous value is the only exit that leaves such a caller
    where it started. The root conftest's ``_reset_otto_context`` would catch
    a leak at the end of the test; this catches it at the end of the OPEN,
    which is where a second cell drawn into the same test would notice.
    """
    lab = bed_lab(tech)
    token = set_context(OttoContext(lab=lab))
    try:
        yield lab
    finally:
        reset_context(token)
