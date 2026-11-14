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

import pytest

from tests._fixtures.interactive_shell import (
    HISTORY_LINES,
    PROBE_LINES,
    PROBE_STATES,
    RESEND_MARKERS,
    SHELLS,
    TAIL_MARKERS,
    cut_probe,
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
    """``(kind, history, established, offset)`` for every tail of every probe line.

    Each line is rendered once, here.

    A line outside ``HISTORY_LINES`` never carries history suppression, so
    it is cut once, not twice over the same text. Each line is cut in each
    shell state ``PROBE_STATES`` names for it: fresh, or with the exec
    frame's variables established, as a handshake leaves them.
    """
    return [
        pytest.param(
            kind,
            history,
            established,
            offset,
            id=f"{kind}-{label}-{'established' if established else 'fresh'}-{offset:03d}",
        )
        for kind in PROBE_LINES
        for history, label in (
            ((True, "history"), (False, "no-history"))
            if kind in HISTORY_LINES
            else ((False, "no-history"),)
        )
        for established in PROBE_STATES[kind]
        for offset in range(1, len(render_probe(kind, TAIL_MARKERS, history)))
    ]


def _cuts() -> list:
    """The cases of the shard :data:`SHARD_ENV` selects (every case when it is unset)."""
    return shard(all_cuts(), os.environ.get(SHARD_ENV))


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("kind", "history", "established", "offset"), _cuts())
@pytest.mark.parametrize("name", [name for name, _ in SHELLS])
def test_every_tail_leaves_the_whole_resend_answering(
    name, kind, history, established, offset, nounset
):
    """The tail from *offset*, then one whole resend, which answers (calibrated, if a handshake)."""
    cut_probe(
        name,
        kind,
        offset,
        TAIL_MARKERS,
        RESEND_MARKERS,
        history=history,
        setup="set -u" if nounset else "",
        established=established,
    )
