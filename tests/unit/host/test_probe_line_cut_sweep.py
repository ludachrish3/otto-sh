"""Every tail of the probe lines, typed into every interactive shell, then the line whole (#853).

A console server that delivers input in pieces lets login(1) or su flush the
head of otto's first line, so the shell on the pty runs only a tail. The
session then resends the line whole, once, and that resend must answer: no
tail may leave the shell inside an open quote, an open brace or a ``cat``
that eats the resend. ``test_history_suppression_portability.py`` cuts at
each known hazard on every push; this sweep cuts at EVERY offset, so a
hazard nobody has named yet is found too.

The case space is every shell, the three lines (handshake, resync, recovery),
history suppression on and off for the two that carry it, ``set -u`` off and
on, and every offset ``1..len(line)-1``: 13,384 cases on the dev VM's seven
shells, each in a fresh shell on a fresh controlling pty, each paying the
session's one probe interval between tail and resend. At about 0.56 s a case
(measured over bash's handshake cases at 4 workers) that is about two hours
serial, so the sweep is a nightly lane (``make stability-cut-sweep``) and the
``stability`` marker keeps it out of every default gate. The case, the fixture and the lines are
``tests/_fixtures/interactive_shell.py``'s; this module only enumerates.
"""

import pytest

from tests._fixtures.interactive_shell import (
    HISTORY_LINES,
    PROBE_LINES,
    RESEND_MARKERS,
    SHELLS,
    TAIL_MARKERS,
    cut_probe,
    render_probe,
)

pytestmark = pytest.mark.stability


def _cuts() -> list:
    """``(kind, history, offset)`` for every tail of every probe line, rendered once here.

    A line outside ``HISTORY_LINES`` never carries history suppression, so
    it is cut once, not twice over the same text.
    """
    return [
        pytest.param(kind, history, offset, id=f"{kind}-{label}-{offset:03d}")
        for kind in PROBE_LINES
        for history, label in (
            ((True, "history"), (False, "no-history"))
            if kind in HISTORY_LINES
            else ((False, "no-history"),)
        )
        for offset in range(1, len(render_probe(kind, TAIL_MARKERS, history)))
    ]


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("kind", "history", "offset"), _cuts())
@pytest.mark.parametrize("name", [name for name, _ in SHELLS])
def test_every_tail_leaves_the_whole_resend_answering(name, kind, history, offset, nounset):
    """The tail from *offset*, then one whole resend, which answers (calibrated, if a handshake)."""
    cut_probe(
        name,
        kind,
        offset,
        TAIL_MARKERS,
        RESEND_MARKERS,
        history=history,
        setup="set -u" if nounset else "",
    )
