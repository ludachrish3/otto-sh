"""Every tail of the probe lines, typed into every interactive shell, then the line whole (#853).

A console server that delivers input in pieces lets login(1) or su flush the
head of otto's first line, so the shell on the pty runs only a tail. The
session then resends the line whole, once, and that resend must answer: no
tail may leave the shell inside an open quote, an open brace or a ``cat``
that eats the resend. ``test_history_suppression_portability.py`` cuts at
each known hazard on every push; this sweep cuts at EVERY offset, so a
hazard nobody has named yet is found too.

The case space is every shell, the probe lines (``PROBE_LINES``: the two
handshakes, resync, recovery and the post-recovery restore line), history
suppression on and off for those that carry it, each shell state
``PROBE_STATES`` names (the resync probe on a fresh shell and an established
one), ``set -u`` off and on, and every offset ``1..len(line)-1``: tens of
thousands of cases on the dev VM's seven shells, each in a fresh shell on a
fresh controlling pty, each paying the session's one probe interval between
tail and resend. At about 0.56 s a case (measured over bash's handshake cases
at 4 workers) that is hours of serial time, so the sweep is a nightly lane
(``make stability-cut-sweep``) and the ``stability`` marker keeps it out of
every default gate. The case, the fixture and the lines are
``tests/_fixtures/interactive_shell.py``'s; this module only enumerates.
"""

import os
import re

import pytest

from otto.host.command_frame import BashFrame
from otto.host.session import ready_pattern
from otto.host.terminal_mapping import ONLCR, measure
from tests._fixtures.interactive_shell import (
    HISTORY_LINES,
    PROBE_LINES,
    PROBE_STATES,
    READY_TIMEOUT,
    RESEND_MARKERS,
    SHELLS,
    TAIL_MARKERS,
    WORK_OWNERSHIP,
    cut_probe,
    interactive_shell,
    probe_answer,
    read_person_state,
    render_probe,
)

pytestmark = pytest.mark.stability


SHARD_ENV = "CUT_SWEEP_SHARD"
"""The environment variable selecting one shard of the sweep: ``i/n``, ``1 <= i <= n``.

Unset, the sweep runs every case (the local ``make stability-cut-sweep``).
Nightly runs the ``n`` shards as parallel jobs, so each fits its bound."""


def shard(cases: list, spec: "str | None") -> list:
    """The cases of shard *spec* (``i/n``): every ``n``-th, from the ``i``-th; all when ``None``.

    Round robin over the case list, so the shards are balanced across line
    kinds and every case is in exactly one shard.
    """
    if not spec:
        return cases
    index, _, count = spec.partition("/")
    refused = ValueError(f"{SHARD_ENV}={spec!r}: want i/n with 1 <= i <= n")
    try:
        i, n = int(index), int(count)
    except ValueError:
        raise refused from None
    if not 1 <= i <= n:
        raise refused
    return cases[i - 1 :: n]


def all_cuts() -> list:
    """``(kind, history, established, ownership, offset)`` for every tail of every probe line.

    Each line is rendered once, here.

    A line outside ``HISTORY_LINES`` never carries history suppression, so
    it is cut once, not twice over the same text. Each line is cut in each
    shell state ``PROBE_STATES`` names for it: fresh, or with the exec
    frame's variables established, as a handshake leaves them. The work
    shell's entry proof is cut in each ``WORK_OWNERSHIP`` state as well; every
    other line has none (``None``).
    """
    return [
        pytest.param(
            kind,
            history,
            established,
            ownership,
            offset,
            id=(
                f"{kind}-{label}-{'established' if established else 'fresh'}"
                f"{'' if ownership is None else '-' + ownership}-{offset:03d}"
            ),
        )
        for kind in PROBE_LINES
        for history, label in (
            ((True, "history"), (False, "no-history"))
            if kind in HISTORY_LINES
            else ((False, "no-history"),)
        )
        for established in PROBE_STATES[kind]
        for ownership in (WORK_OWNERSHIP if kind == "work-entry" else (None,))
        for offset in range(1, len(render_probe(kind, TAIL_MARKERS, history)))
    ]


def _cuts() -> list:
    """The cases of the shard :data:`SHARD_ENV` selects (every case when it is unset)."""
    return shard(all_cuts(), os.environ.get(SHARD_ENV))


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("kind", "history", "established", "ownership", "offset"), _cuts())
@pytest.mark.parametrize("name", [name for name, _ in SHELLS])
def test_every_tail_leaves_the_whole_resend_answering(
    name, kind, history, established, ownership, offset, nounset
):
    """The tail from *offset*, then one whole resend, which answers (calibrated, if a handshake).

    An entry proof's resend answers the work mark exactly where the shell
    owns it (``WORK_OWNERSHIP``): an unowned shell never answers ``1``, at
    any offset, and a started or leftover one always does.
    """
    cut_probe(
        name,
        kind,
        offset,
        TAIL_MARKERS,
        RESEND_MARKERS,
        history=history,
        setup="set -u" if nounset else "",
        established=established,
        ownership=ownership,
    )


_WORK = BashFrame().work_shell(prefix="")
_START = _WORK.lead + _WORK.start
"""The start line as typed, its lead included."""


@pytest.mark.parametrize("kind", ["work-console-handshake", "work-entry"])
@pytest.mark.parametrize("name", [name for name, _ in SHELLS])
def test_a_cut_inside_line_ones_lead_answers_as_the_whole_line(name, kind):
    """Line 1, or the entry proof, less its lead is the line itself: that tail answers.

    Line 1's answer is calibrated; the proof's, in a shell with no mark,
    reads back none.
    """
    line = _WORK.lead + render_probe(kind, TAIL_MARKERS, history=True)
    with interactive_shell(name) as shell:
        shell.send(line[len(_WORK.lead) :])
        out = shell.wait_for_output(probe_answer(kind, TAIL_MARKERS), READY_TIMEOUT)
    found = probe_answer(kind, TAIL_MARKERS).search(out)
    assert found is not None
    if kind == "work-console-handshake":
        assert measure(out, TAIL_MARKERS.ready).mapping == ONLCR, f"{name}: {out[-300:]!r}"
    else:
        assert found.group(3) == "", f"{name}: {out[-300:]!r}"


@pytest.mark.parametrize("offset", range(1, len(_START)))
@pytest.mark.parametrize("name", [name for name, _ in SHELLS])
def test_no_tail_of_the_start_line_lets_the_persons_shell_claim_the_mark(name, offset):
    """A tail of the start line, then the whole proof: only a child the tail started owns the mark.

    The person's shell ran line 1. A tail that still starts a child is
    allowed; the proof then answers ``1`` from that child, whose parent is
    the person's shell. Whatever the tail did, the person's shell ends with
    its own pid, no work mark, and the rest of its state as before
    (:class:`~tests._fixtures.interactive_shell.PersonState`'s shell half).
    """
    proof_answer = probe_answer("work-entry", RESEND_MARKERS)
    with interactive_shell(name) as shell:
        shell.type_line(
            _WORK.lead + render_probe("work-console-handshake", TAIL_MARKERS),
            ready_pattern(TAIL_MARKERS, line_ending=True),
            READY_TIMEOUT,
        )
        before = read_person_state(shell)
        mark = len(shell.output)
        shell.send(_START[offset:])
        shell.send(_WORK.lead + render_probe("work-entry", RESEND_MARKERS, history=True))
        shell.wait_for_output(proof_answer, READY_TIMEOUT)
        found = proof_answer.search(shell.output, mark)
        assert found is not None
        if found.group(3) == "1":
            parent = re.search(r"ppid=(\d+)", shell.run("echo ppid=$PPID"))
            assert parent is not None
            assert parent.group(1) == str(before.shell.pid), (
                f"{name}: the mark was claimed by a shell that is not the person's child"
            )
            shell.send("exit\n")  # the child ends; the next line is the person's shell's
        after = read_person_state(shell)
    assert not before.shell.work_mark
    assert after.shell == before.shell, (
        f"{name}: the person's shell changed after the tail {_START[offset:]!r}"
    )
