"""How each userland is ASKED the question every contract in this tree asserts.

THE STIMULUS VARIES PER CELL; THE ASSERTED PROPERTY DOES NOT. That sentence is
the whole design and it was arrived at by getting it wrong first. The first
six contracts were written against bash -- ``printf '%s\\n%s\\n' ...``, ``(exit
42)``, ``sleep N`` -- and a Zephyr RTOS shell has none of those spellings, so
the first reading of the bed venue's red was "Zephyr cannot express these
contracts" and the first proposal was to narrow four of them. That was wrong.
Zephyr returns an exit code (a signed errno: an unknown command answers ``-8``,
``-ENOEXEC``, and ``kernel version`` answers ``0``) and it frames its output (``help``
is multi-line and exits 0). What is missing is bash's VOCABULARY, not the
PROPERTY. A vocabulary layer that also varied the assertion would turn seven
contracts into a per-userland suite that can never disagree with the product --
exactly the drift this suite exists to remove.

So this module carries STIMULI AND EXPECTED VALUES, and nothing else. It holds
no predicate, no callable and no assertion, which
``tests/unit/test_conformance_bed.py`` pins by inspecting the field types: the
moment a vocabulary can carry behaviour, a userland can quietly answer a
different question.

DERIVED FROM LAB DATA, NOT A HAND-WRITTEN TABLE KEYED BY ELEMENT.
:func:`vocabulary_for` splits on the USERLAND axis
(:func:`tests._fixtures.profiles.axes_for`), which is itself resolved from the
host otto builds out of ``lab.json`` -- so a new guest added to the lab gets a
correct dialect for free, which is the same reason ``tests/conftest.py``'s
``_zephyr_kit`` derives rather than tabulates. A hand-written table keyed by
element goes stale silently, because a missing entry looks like a passing cell.
An unrecognised userland RAISES here for that reason. The one per-element
datum, a Zephyr guest's recorded ``help`` (:data:`ZEPHYR_SHELLS`), is derived
from that guest's recording, and a guest with none raises too.

WHY NOT ``HostKit``. ``tests/conftest.py``'s :class:`~tests.conftest.HostKit`
is the prior art and it is not enough: it carries "a command that succeeds" and
"one that fails", while these contracts also need a KNOWN DISTINCT failure
code, a SECOND failing command whose code cannot be confused with the first's,
and a command with KNOWN MULTI-LINE output. It is also keyed by BACKEND ID,
and the bed's own measurements say that cannot be reached from a
:class:`~tests._fixtures.profiles.Cell`: there is no backend id for ``test2``,
``test3`` or ``test4``. This extends the idea rather than restating it.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.
"""

import enum
import functools
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

from otto.testing import EXACT_OUTPUT_CASES, OutputCase
from tests._fixtures.shell_transcripts import (
    ZEPHYR_EXTRA_COMMANDS,
    recordings,
    zephyr_recorded_output,
)

#: The sentinel prefix every otto command frame builds its markers from
#: (``otto.host.command_frame``: ``__OTTO_<session-id>_BEGIN__`` and friends).
#: Named here because the framing contract asserts it never reaches a caller,
#: and the same literal is what
#: ``tests/integration/host/test_embedded_host_integration.py``'s
#: ``TestMultilineOutputClean`` checks against these very guests.
OTTO_SENTINEL_PREFIX = "__OTTO_"

# The two POSIX tokens. Neither is a substring of any prompt, sentinel or shell
# builtin, so a leak of any of those breaks the exact-equality assertions the
# POSIX vocabulary can afford.
_FRAME_TOKEN = "otto-conformance-frame"
_TAIL_TOKEN = "otto-conformance-tail"


class OutputArm(enum.Enum):
    """Which evidence a vocabulary's :attr:`Vocabulary.output_cases` are.

    One rule, two strengths of evidence: the output contract names the arm a
    cell ran in its observable, and this is where that is decided, once, at
    each vocabulary's definition.
    """

    SHIPPED_TABLE = "shipped-table"
    """:data:`otto.testing.EXACT_OUTPUT_CASES`: programs whose bytes the tester chose."""

    FIRMWARE_OUTPUT = "firmware-output"
    """Stock commands whose output the firmware, not the tester, fixes."""

    UNESCAPED_IAC = "unescaped-iac"
    """The shipped table, with each row holding 0xFF expected as a console
    declaring :data:`otto.host.options.UNESCAPED_IAC` carries it. Chosen by the
    output contract from the opened host's declaration, never by a vocabulary."""


@dataclass(frozen=True)
class Vocabulary:
    """One userland's spellings, and what each of them is expected to answer.

    Every field but :attr:`output_cases` and :attr:`output_arm` is a plain
    ``str``, ``int``, ``float`` or ``None`` on purpose; those two are a list
    of frozen records and an enum member.
    A callable field would let a userland supply its own checker, and the
    contract would then be measuring whatever that userland chose to be
    measured on -- the one failure mode this whole layer is built against.
    ``tests/unit/test_conformance_bed.py`` pins the field types for that
    reason.
    """

    succeeding_command: str
    """A command that exits 0. Its output is not asserted on."""

    failing_command: str
    """A command that exits with :attr:`failing_code`, and no other code."""

    failing_code: int
    """The exact code :attr:`failing_command` exits with.

    PINNED EXACTLY, never "any non-zero". ``retcode == -1`` is otto's reserved
    "the command never ran" sentinel -- ``otto.result.CommandResult.exit_code``
    maps it to ssh's 255 -- and Zephyr's signed-errno convention makes ``-1``
    a genuinely reachable value (``-EPERM``), so a command that returned it
    would be reported as a connection failure and be indistinguishable from
    one. NOT MEASURED: whether any bed guest can actually be driven to
    ``-EPERM``. The expectation is pinned rather than the collision asserted.
    ``tests/unit/test_conformance_bed.py`` refuses ``-1`` and ``0`` in every
    vocabulary.
    """

    sequence_failing_command: str
    """The command the SEQUENCE contract fails on. Never :attr:`failing_command`.

    Two different commands so a cross-wired constant cannot make one contract
    pass on the other's evidence -- see :attr:`sequence_failing_code` for the
    half of that discriminator a Zephyr shell cannot supply.
    """

    sequence_failing_code: int
    """The exact code :attr:`sequence_failing_command` exits with.

    ON A POSIX USERLAND THIS DIFFERS FROM :attr:`failing_code`; ON A ZEPHYR
    SHELL IT DOES NOT, AND THAT IS A MEASURED LOSS RATHER THAN AN OVERSIGHT.
    A Zephyr shell has exactly one failure code that is stable across the LTS
    releases in this lab: ``-8`` from an unknown command. The obvious second
    one is not stable -- measured on three guests, ``kernel uptime extra arg``
    answers ``-22`` (``-EINVAL``, "wrong parameter count") on 3.7 and 4.4 and
    ``0`` on 2.7, which ignores the extra words. Using it would have made the
    contract's result depend on the firmware version, which is precisely the
    "conflate a vocabulary difference with a parser bug" trap
    ``TestMultilineOutputClean``'s docstring warns about.

    So on Zephyr the two codes are equal and only the COMMAND NAMES differ.
    What survives is ``Results.first_failure.command``, which still names
    which of the two commands failed; what is lost is the ability of the
    aggregate ``exit_code`` assertion to tell one contract's constant from the
    other's. Said at the assertion in
    ``tests/conformance/test_exec_contract.py`` rather than hidden here.
    """

    multiline_command: str
    """A command whose output is more than one line, and which exits 0."""

    output_cases: "list[OutputCase]"
    """The commands whose exact ``value`` and return code the output contract checks.

    Never empty; ``tests/unit/test_conformance_bed.py`` refuses an empty list.
    On a POSIX shell it is :data:`otto.testing.EXACT_OUTPUT_CASES` itself, the
    one table every route is judged on: programs whose bytes the tester
    chose, so exact equality also catches truncation, reordering,
    interleaving and a leaked prompt. A Zephyr shell has no ``printf`` and no
    ``echo``, so nothing there writes chosen bytes; its list is the stock
    commands whose output the FIRMWARE fixes, as recorded in
    ``tests/unit/host/shell_transcripts/`` -- see :data:`ZEPHYR_SHELLS`. The
    same rule, weaker evidence.

    The one field that is not a scalar, and still data: an
    :class:`~otto.testing.OutputCase` is a frozen record of a command, the
    bytes it writes and its exit status, with no behaviour of its own.
    """

    output_arm: OutputArm
    """Which evidence :attr:`output_cases` are; the output contract's observable names it."""

    single_line_command: str
    """A command whose output is a single line, and which exits 0.

    Used twice, and the sharing is deliberate rather than an economy: the exec
    contract runs it as the entry AFTER a failing one (to prove commands after
    a failure still run) and the timeout contract runs it after a timeout (to
    prove the session survived). Both ask the same thing -- issue a command,
    get its output back -- so one spelling serves both.
    """

    single_line_expected: str | None
    """:attr:`single_line_command`'s exact output, or ``None`` where it cannot be chosen."""

    single_line_pattern: str | None
    """A regex :attr:`single_line_command`'s output must match, or ``None``.

    THE STRENGTH RECOVERED WHERE THE OUTPUT CANNOT BE CHOSEN. A Zephyr
    ``kernel uptime`` cannot be pinned exactly -- it is a clock -- but it does
    have a KNOWN SHAPE, and a shape assertion beats a bare "nothing leaked":
    a backend returning the empty string, or a prompt, or the previous
    command's output, fails it. Deliberately "an integer somewhere in the
    output" rather than "digits only": MEASURED on 2.7, 3.7 and 4.4, the
    output is ``Uptime: 42441320 ms``, not a bare integer, and
    ``tests/integration/host/test_embedded_host_integration.py``'s
    ``test_kernel_uptime_yields_integer_microseconds`` already makes the same
    allowance for the same reason.
    """

    long_running_command: str | None
    """A command that outlives a sub-second budget, or ``None`` where none exists.

    ``None`` DECLARES THE TIMEOUT CONTRACT INAPPLICABLE to every cell of this
    userland; see ``tests/conformance/test_timeout_contract.py``'s
    ``applicable_cell``. It is ``None`` on a Zephyr shell for a reason that is
    a property of the target rather than of this suite: the shell is
    synchronous on the shell thread, so a command that blocked for the
    budget's duration would block the shell ITSELF -- the very thing whose
    survival the contract's second half asserts. A stimulus that made the
    contract's first assertion pass would make its second unmeasurable.
    """

    sentinel_plant_command: str
    """A command whose OWN OUTPUT contains :data:`OTTO_SENTINEL_PREFIX`.

    THE STIMULUS THE FRAMING SURFACE'S POSITIVE CONTROL NEEDS, and it is here
    rather than in the control because it is a spelling and spellings are what
    this module holds. The framing contract asserts that no otto scaffolding
    reaches a caller; a framing check that cannot SEE planted pollution proves
    nothing about real pollution, so its control plants a sentinel lookalike
    in the command's own output and requires
    ``tests/conformance/_framing.py``'s ``framing_leak`` to report it.

    NOT otto's real per-session marker, and it could not be: those are built
    from a session id unique per connection
    (``otto.host.command_frame.SessionMarkers.for_session`` ->
    ``__OTTO_<id>_BEGIN__``), and both frames parse for the escaped id, so a
    literal carrying the PREFIX alone can never be mistaken for a real frame
    by otto while still being exactly what ``framing_leak`` looks for.

    THE ZEPHYR SPELLING IS AN UNKNOWN COMMAND, and that is the measurement
    that made this field possible at all: a Zephyr shell has no ``echo`` and
    no ``printf``, so nothing there can be asked to print an arbitrary
    string -- except by NAMING it, since the shell answers an unrecognised
    command by quoting it back (``<name>: command not found``). MEASURED
    2026-08-24 on all seven bed guests (2.7.6, 3.7.2 x4, 4.4.1 x2): identical
    output, retcode -8.
    """

    remove_file_template: str
    """How this userland is asked to delete a file; ``{path}`` is substituted.

    The "leave the bed as found" half of the two transfer controls, which put
    a file of their own and must not leave it behind.

    ITS RESULT IS ALSO THE VERIFICATION, which is why the POSIX spelling is
    ``rm`` and NOT ``rm -f``. MEASURED on the bed 2026-08-24: ``rm -f``
    answers 0 whether or not the file was there, so an assertion on it could
    never fail -- the guards-that-cannot-fail defect, in the cleanup. Plain
    ``rm`` and Zephyr's ``fs rm`` both answer non-zero for an absent file
    (``fs rm`` gives retcode -8 and ``Failed to remove <path> (-2)``) and 0
    for one that is there, so a success means both *there was a file* and
    *there is not one now*.
    """

    long_running_seconds: float | None
    """How long :attr:`long_running_command` runs. ``None`` exactly when it is.

    Not folded into the command string: the MARGIN between this and the
    contract's budget is a discriminator, not padding -- a backend that
    quietly ran the command to completion and reported success must not be
    confusable with one that timed out -- and
    ``tests/unit/test_conformance_bed.py`` pins that margin over every
    vocabulary rather than leaving it inside a literal only one of them can be
    read out of.
    """


#: Bash, ash and any other POSIX shell: the vocabulary the seven contracts were
#: originally written in. Serves BOTH venues -- every hermetic cell is a GNU or
#: BusyBox userland behind a POSIX shell, and the bed's ``gnu`` and
#: ``busybox-*`` hosts are too.
#:
#: ``(exit N)`` rather than ``sh -c 'exit N'``: ``run()`` drives a PERSISTENT
#: shell session, so a bare ``exit`` would take the session down and the next
#: contract's failure would be this one's fault. The subshell is also free of
#: nested quoting, which is what a shell-dialect difference would bite first.
POSIX = Vocabulary(
    succeeding_command="(exit 0)",
    # Deliberately neither 0 nor 1 nor 255. A backend that collapses every
    # failure to "1", or that reports the ssh-style "the command never ran"
    # 255, is indistinguishable from a correct one under `false`; it is not
    # under this.
    failing_command="(exit 42)",
    failing_code=42,
    sequence_failing_command="(exit 5)",
    sequence_failing_code=5,
    multiline_command=f"printf '%s\\n%s\\n' {_FRAME_TOKEN}-1 {_FRAME_TOKEN}-2",
    output_cases=EXACT_OUTPUT_CASES,
    output_arm=OutputArm.SHIPPED_TABLE,
    single_line_command=f"printf '%s\\n' {_TAIL_TOKEN}",
    single_line_expected=_TAIL_TOKEN,
    # Exact equality already subsumes any shape assertion here.
    single_line_pattern=None,
    sentinel_plant_command=f"printf '%s\\n' {OTTO_SENTINEL_PREFIX}conformance_control__",
    remove_file_template="rm {path}",
    long_running_command="sleep 10",
    long_running_seconds=10.0,
)

#: The Zephyr RTOS shell. Every spelling below is MEASURED on real guests
#: rather than read out of documentation, and every one of them is also
#: exercised by ``tests/integration/host/`` against the same firmware:
#:
#: - ``kernel version`` -> ``0``, ``Zephyr version X.Y.Z``. Measured on ALL
#:   SEVEN bed guests. The bare ``version`` alias was tried first and is
#:   WRONG -- measured, ``zephyr37_llext`` and ``zephyr44_llext`` answer
#:   ``version: command not found`` with retcode ``-8``, and both contract
#:   tests went red on those two cells before this line was corrected. The
#:   reason the mistake was available to make is worth keeping: ``version`` is
#:   ``tests/conftest.py``'s ``_ZEPHYR_COMMON["successful_cmd"]`` and the whole
#:   embedded integration suite runs it, which reads as broad proof -- but
#:   ``EMBEDDED_BACKENDS`` covers five guests and the two ``llext`` builds are
#:   not among them, so that suite has never asked them anything. A command
#:   being exercised across "the matrix" is only as broad as the matrix.
#: - ``definitely_not_a_zephyr_command`` -> ``-8`` (``-ENOEXEC``). Measured on
#:   2.7.6, 3.7.2 and 4.4.1; pinned across the whole embedded matrix by
#:   ``TestSignedRetcode``.
#: - ``help`` -> ``0`` and ~30 lines. Chosen over ``kernel threads`` for the
#:   reason ``TestMultilineOutputClean``'s docstring gives: the subcommand name
#:   moved between LTS releases, which would conflate a vocabulary difference
#:   with a parser bug.
#: - ``kernel uptime`` -> ``0`` and ``Uptime: <int> ms``. Measured on all
#:   three; pinned by ``TestStockBuiltins``.
#:
#: Zephyr's ``-8`` is arguably a BETTER discriminator than bash's 42: a backend
#: that collapses every failure to ``1`` fails it, and so does one that mangles
#: the sign.
#:
#: The base every guest's vocabulary is built from; only
#: :attr:`Vocabulary.output_cases` differs between guests (see
#: :data:`ZEPHYR_SHELLS`).
_ZEPHYR_BASE = Vocabulary(
    succeeding_command="kernel version",
    failing_command="definitely_not_a_zephyr_command",
    failing_code=-8,
    # A DIFFERENT unknown command, so `first_failure.command` still says which
    # of the two failed -- but the same code, because only one is stable
    # across this lab's LTS releases. See `sequence_failing_code`.
    sequence_failing_command="otto_conformance_not_a_command",
    sequence_failing_code=-8,
    multiline_command="help",
    output_cases=[],
    output_arm=OutputArm.FIRMWARE_OUTPUT,
    single_line_command="kernel uptime",
    single_line_expected=None,
    single_line_pattern=r"\d+",
    # The shell quotes an unrecognised command back at the caller, which is
    # the only way to make a Zephyr shell emit a chosen string: it has no
    # `echo` and no `printf`. Measured on all seven bed guests.
    sentinel_plant_command=f"{OTTO_SENTINEL_PREFIX}conformance_control__",
    remove_file_template="fs rm {path}",
    long_running_command=None,
    long_running_seconds=None,
)


#: The Zephyr version string ``kernel version`` prints, per release (the
#: userland :func:`tests._fixtures.profiles.axes_for` resolves). Recorded in
#: ``tests/unit/host/shell_transcripts/`` on every bed guest of that release;
#: ``tests/unit/test_conformance_bed.py`` checks each row against them.
_ZEPHYR_KERNEL_VERSIONS: "dict[str, str]" = {
    "zephyr-2.7": "2.7.6",
    "zephyr-3.7": "3.7.2",
    "zephyr-4.4": "4.4.1",
}


def _zephyr_release_cases(version: str) -> "list[OutputCase]":
    """The stock commands whose output every guest of a Zephyr *version* shares.

    ``kernel version`` names the release; an unknown command is answered by
    the shell itself, identically on every release (its error colour is the
    shell's addition, not output).
    """
    return [
        OutputCase(
            id="kernel version",
            command="kernel version",
            writes=f"Zephyr version {version}\n".encode(),
        ),
        OutputCase(
            id="unknown command",
            command="otto_not_a_command",
            writes=b"otto_not_a_command: command not found\n",
            retcode=-8,
        ),
    ]


def _zephyr_recorded_case(recording: "dict[str, Any]", entry: "dict[str, Any]") -> OutputCase:
    r"""*entry*'s command as *recording*'s guest sent it, by the recordings' README rule.

    Cut from the recording's RAW bytes by
    :func:`tests._fixtures.shell_transcripts.zephyr_recorded_output`: the
    frame's lines, the shell's message-level colouring and the shell's ``\r``
    of each ``\r\n`` removed, everything the command wrote kept -- blank
    lines, leading spaces and the help formatter's cursor moves (``\x1b[17C``)
    included. Never by otto's own frame, which would make the contract agree
    with otto by construction.
    """
    text = zephyr_recorded_output(recording, entry).text
    return OutputCase(
        id=entry["cmd"], command=entry["cmd"], writes=text.encode(), retcode=entry["retcode"]
    )


def _zephyr_recorded_cases(recording: "dict[str, Any]") -> "list[OutputCase]":
    """``help``, then each message-level command *recording*'s guest was asked, as it sent them."""
    entries = {one["cmd"]: one for one in recording["commands"]}
    wanted = ["help", *ZEPHYR_EXTRA_COMMANDS]
    return [_zephyr_recorded_case(recording, entries[cmd]) for cmd in wanted if cmd in entries]


@functools.cache
def _zephyr_recordings() -> "dict[str, dict[str, Any]]":
    """Every recorded Zephyr guest's recording, by element."""
    return {
        recording["element"]: recording
        for recording in recordings().values()
        if recording["release"].startswith("zephyr-")
    }


def _zephyr_shell(recording: "dict[str, Any]") -> Vocabulary:
    """The vocabulary of the guest *recording* was made on."""
    cases = _zephyr_release_cases(_ZEPHYR_KERNEL_VERSIONS[recording["release"]])
    return replace(_ZEPHYR_BASE, output_cases=[*cases, *_zephyr_recorded_cases(recording)])


@functools.cache
def _zephyr_shell_of(element: str) -> Vocabulary:
    return _zephyr_shell(_zephyr_recordings()[element])


class _RecordedShells(Mapping[str, Vocabulary]):
    """:data:`ZEPHYR_SHELLS`: each recorded guest's vocabulary, cut on first use.

    LAZY so importing this module cuts no recording: the bed's topology
    (``tests/conformance/_bed.py``'s ``bed_topology``) imports it, and
    ``make shell-transcripts`` must open a guest whose recording does not
    exist yet, or is being replaced.
    """

    def __getitem__(self, element: str) -> Vocabulary:
        return _zephyr_shell_of(element)

    def __iter__(self) -> "Iterator[str]":
        return iter(_zephyr_recordings())

    def __len__(self) -> int:
        return len(_zephyr_recordings())


#: The Zephyr RTOS shell's vocabulary, per bed GUEST (element): :data:`_ZEPHYR_BASE`
#: with that guest's :attr:`Vocabulary.output_cases`.
#:
#: PER ELEMENT, NOT PER RELEASE, because of ``help``. ``kernel version`` and
#: the unknown-command answer are the same on every guest of a release, and
#: are written per release; ``help`` is not -- its command list and the
#: column its formatter's cursor moves jump to depend on which shell modules
#: the build enabled, so ``zephyr37_fat`` and ``zephyr37_nofs`` (both 3.7.2)
#: answer differently. ``help`` is the one stock command that is multi-line,
#: has blank lines, leading spaces and in-output escapes, and whose bytes the
#: firmware fixes, so it is the case that makes this arm evidence about more
#: than one line. Its expected bytes are read from that guest's recording.
#:
#: Two stock commands stay out. ``kernel uptime`` is a clock. ``device list``
#: prints each device's runtime state (``- eth0 (READY)``), which a device
#: whose initialisation failed answers differently on the same firmware, so
#: its bytes are fixed by the guest's state rather than by the build.
#:
#: Derived from the recordings, so a Zephyr guest with no recording has no
#: vocabulary and :func:`vocabulary_for` raises for it.
ZEPHYR_SHELLS: "Mapping[str, Vocabulary]" = _RecordedShells()


def vocabulary_for(*, userland: str, element: str) -> Vocabulary:
    """The vocabulary *element*, a host of *userland*, is asked the contracts in.

    Split on the USERLAND string
    :func:`tests._fixtures.profiles.axes_for` resolved, for the same three
    measured reasons ``tests/conformance/_bed.py``'s ``_kind_for_userland``
    gives: the element NAME is a naming convention rather than a fact about
    the host, the LAB is wrong (``test4`` is in the ``embedded`` lab and is a
    plain GNU VM), and ``os_type`` is wrong (the five BusyBox guests declare
    ``os_type: "unix"``). *element* only picks WHICH Zephyr guest's recorded
    output the output contract compares (:data:`ZEPHYR_SHELLS`); it never
    decides the dialect.

    An unrecognised userland RAISES rather than falling back to POSIX. A
    default would be the failure this module is shaped to avoid: a new
    userland layer would be asked bash's questions, its cells would go red for
    a reason that looks like a product bug, or -- worse, if its shell happened
    to accept them -- would go green having measured the wrong dialect. A
    Zephyr guest with no recording, or recorded running another release,
    raises for the same reason: its output contract would have nothing true
    to compare.
    """
    if userland in _ZEPHYR_KERNEL_VERSIONS:
        recording = _zephyr_recordings().get(element)
        if recording is None or recording["release"] != userland:
            raise ValueError(
                f"no conformance vocabulary for {element!r} on userland {userland!r} -- "
                f"tests/unit/host/shell_transcripts/ holds no recording of that guest on "
                f"that release (`make shell-transcripts`)"
            )
        return ZEPHYR_SHELLS[element]
    if userland.startswith("busybox-") or userland == "gnu":
        return POSIX
    raise ValueError(
        f"no conformance vocabulary for userland {userland!r} -- "
        f"tests/conformance/_vocabulary.py must name the spellings its shell answers to"
    )
