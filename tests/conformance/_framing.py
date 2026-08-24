"""The half of the output contract that is identical on every userland.

TWO CONTRACT FILES ASK THE SAME QUESTION OF THE SAME REPLY, so they ask it in
one spelling. ``test_exec_contract.py`` runs the vocabulary's single-line
command after a failing one (to prove commands after a failure still run) and
``test_timeout_contract.py`` runs it after a timeout (to prove the session
survived); a second copy of "and the answer came back clean" would let the two
drift into asserting differently about the same thing.

WHY THESE ASSERTIONS LIVE OUTSIDE A CONTRACT FILE RATHER THAN BEING DUPLICATED
INTO BOTH: they are the part that must NOT vary per cell. The stimulus varies
-- ``printf`` on a POSIX shell, ``kernel uptime`` on a Zephyr one -- and the
expected values vary with it, but the property asserted does not, and a shared
function is the cheapest way to make that structural rather than a promise.
``tests/unit/test_conformance_bed.py`` proves it holds by running the
contracts themselves under every vocabulary against honest and lying hosts.

Every assertion here carries its own message, which is what pays for living in
a module pytest does not rewrite assertions in: the introspected
``assert a == b`` diff is unavailable, so nothing may rely on it.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.
"""

import re

from tests._fixtures.profiles import Cell
from tests.conformance._vocabulary import OTTO_SENTINEL_PREFIX, Vocabulary


def framing_leak(output: str, command: str) -> "str | None":
    """What the session added to *command*'s output, or None if it added nothing.

    THE UNIVERSAL HALF OF THE FRAMING CONTRACT: it runs on every cell, in
    every venue, whatever the userland, because none of these four artifacts
    is a property of a dialect. They are otto's own scaffolding
    (``otto.host.command_frame`` builds ``__OTTO_<id>_BEGIN__`` and the
    ``retval`` read-back; the Zephyr prompt is colourised, so ANSI is real
    here) plus the echoed command line an interactive session produces.
    ``tests/integration/host/test_embedded_host_integration.py``'s
    ``TestMultilineOutputClean`` asserts exactly this set against these very
    guests; this is that check, made venue-wide.

    THE WEAKENING IS HERE AND IS NAMED RATHER THAN HIDDEN. On a userland whose
    output the tester CHOOSES, the callers also assert exact equality, which
    additionally catches truncation, reordering, interleaving and a leaked
    shell PROMPT -- the ``vagrant@otto:~$`` measured on a loopback-ssh cell in
    this worktree is caught by nothing weaker. This function cannot see a
    prompt unless it arrives as the echoed command line or carries ANSI,
    because a prompt is not distinguishable from output in the general case
    and otto exposes no prompt string to compare against. So on a cell with no
    chosen output the framing contract is genuinely weaker, and the run's own
    parametrization ids say which cells those are.

    Returns the reason rather than a bool so the caller reports which artifact
    leaked instead of only that one did.
    """
    if OTTO_SENTINEL_PREFIX in output:
        return f"an otto frame sentinel ({OTTO_SENTINEL_PREFIX}...) reached the caller"
    if "\x1b[" in output:
        return "a raw ANSI escape survived; the shell's colourised prompt was not stripped"
    for line in output.splitlines():
        # `retval` only on a line of its own: it is a Zephyr shell command in
        # its own right, so `help` legitimately lists it mid-line.
        if line.strip() == "retval":
            return "the Zephyr `retval` read-back leaked in as a line of output"
        if line.strip() == command:
            return f"the session echoed the command line back ({command!r})"
    return None


def assert_single_line_answer(output: str, words: Vocabulary, cell: Cell, what: str) -> None:
    """The vocabulary's single-line command answered, and nothing else came back.

    THE SAME THREE-TIER SHAPE AS THE MULTI-LINE FRAMING CONTRACT, for the same
    reason. Non-empty and framing-clean on EVERY cell; exact equality where
    the vocabulary chose the output; and a SHAPE where it could not. The shape
    is what recovers strength on a Zephyr cell: ``kernel uptime`` is a clock
    and cannot be pinned, but a reply with no integer in it is not an uptime
    -- so an empty answer, a prompt, or the previous command's output still
    fails, which a bare "nothing leaked" would not.

    *what* names the moment being measured ("the command after the failure",
    "the command after the timeout") so one failure line says which contract
    was asking.
    """
    body = output.strip()
    assert body, (
        f"{cell}: {what} produced no output at all; `{words.single_line_command}` "
        f"answers on every host this contract runs against"
    )
    leak = framing_leak(body, words.single_line_command)
    assert leak is None, f"{cell}: {what} -- {leak} -- {output!r}"

    if words.single_line_expected is not None:
        assert body == words.single_line_expected, (
            f"{cell}: {what} returned {output!r}, not {words.single_line_expected!r}"
        )
    if words.single_line_pattern is not None:
        assert re.search(words.single_line_pattern, body), (
            f"{cell}: {what} returned {output!r}, which does not match the shape "
            f"`{words.single_line_pattern}` that `{words.single_line_command}` answers in"
        )
