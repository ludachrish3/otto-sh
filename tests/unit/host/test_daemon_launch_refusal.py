"""A tagged-daemon launch is refused on a host that declares it has no bash.

The ``daemon-launch`` record in :data:`~otto.host.userland.GAPS` says why:
:func:`~otto.host.daemon.launch_command` wraps every daemon in
``bash -c 'exec -a "$1" "${@:2}"'``, ``exec -a`` is a bash builtin, and a stock
BusyBox userland has no bash. Measurement went further — the three newest matrix
rows DO parse ``exec -a`` and then mis-expand ``"${@:2}"`` into a substring of
``$1``, so a naive ``bash``→``sh`` swap trades a clean ``not found`` for a
corrupted program name. There is no substitution to make; there is only a
refusal to raise.

**This module pins the GUARD's contract.** Whether the guard is reachable from
a product path is a different question and is pinned where those call sites live
— ``tests/unit/link/test_manage_impair.py::TestExpireOnAHostWithoutBash``
arrives at otto's only caller of it today with a bash-less host and observes the
refusal end to end. The two halves are deliberately not in one file: a guard
that fires correctly at a place nothing reaches is this repo's most common
defect, so "it fires" and "something gets here" are asserted separately.

Nothing here talks to a device, and that is the point twice over — the
predicate is DECLARED, so the refusal costs no probe, no userland resolution
and no connection.
"""

import pytest

from otto.host import userland
from otto.host.daemon import launch_command, refuse_if_launch_wrapper_needs_bash
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.userland import MEASURED_BROKEN, UNTESTED, Gap, gap_for

SURFACE = "daemon-launch"
"""The registry record this guard is the second product consumer of."""

SENTINEL = "otto-impair:v1:edge:eth1.100"
"""A real sentinel shape, so the rendered message is the one an operator sees."""


class _Host:
    """The narrowest thing the guard reads: an id and a declaration.

    Not a real host class, because the guard is deliberately duck-typed — it is
    called from ``otto.link.manage``, where every host is ``Any`` and may be a
    project-supplied class otto has never seen.
    ``TestThroughAHostBuiltFromLabData`` covers the real chain.
    """

    def __init__(self, host_id: str = "bb1", **declared) -> None:
        self.id = host_id
        for name, value in declared.items():
            setattr(self, name, value)


# ===========================================================================
# The premise: the wrapper really does need bash
# ===========================================================================


class TestTheWrapperIsWhyThisGuardExists:
    """If this ever fails, the guard's reason has gone away — re-derive it.

    Every other assertion in this module is about a refusal whose whole
    justification is the two tokens below. A rewrite of ``launch_command``
    that dropped them would leave the guard refusing hosts that could now be
    launched on, and nothing else here would notice.
    """

    def test_the_launch_line_invokes_bash_and_exec_a(self) -> None:
        cmd = launch_command(SENTINEL, ["sleep", "5"])
        assert "bash -c" in cmd, (
            "launch_command no longer spawns bash; if the wrapper became portable, this "
            "guard refuses hosts that can now run it — re-read the daemon-launch record"
        )
        assert "exec -a" in cmd, (
            "launch_command no longer uses the bash builtin that made this a gap"
        )


# ===========================================================================
# The refusal itself
# ===========================================================================


class TestAHostWithoutBashIsRefused:
    """Loud and up front, where it used to be a discarded ``bash: not found``."""

    def test_a_declared_has_bash_false_is_refused(self) -> None:
        with pytest.raises(UnsupportedOnUserlandError):
            refuse_if_launch_wrapper_needs_bash(_Host(has_bash=False))

    def test_the_message_is_rendered_from_the_registry_record(self) -> None:
        """Not a hand-written string: the record's own reason, evidence and anchor."""
        gap = gap_for(SURFACE)
        assert gap is not None
        assert gap.status == MEASURED_BROKEN
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            refuse_if_launch_wrapper_needs_bash(_Host(has_bash=False))
        message = str(excinfo.value)
        assert SURFACE in message
        assert gap.reason in message
        assert gap.measured_on in message
        assert gap.docs_anchor in message

    def test_the_message_names_the_host_and_what_the_caller_was_doing(self) -> None:
        """An operator has to be able to act on this without reading the source.

        The host id comes from the host, the description from the caller: the
        record covers a CLASS of userland and cannot know either.
        """
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            refuse_if_launch_wrapper_needs_bash(
                _Host("edge-router-3", has_bash=False),
                attempted=f"an expire-timer daemon tagged {SENTINEL!r}",
            )
        message = str(excinfo.value)
        assert "edge-router-3" in message
        assert SENTINEL in message

    def test_a_host_with_no_id_still_refuses(self) -> None:
        """The id decorates the message; it must not be able to gate the verdict.

        A project-supplied host class is duck-typed here, so ``id`` may be
        absent. Refusing anyway is the point — the alternative is an
        ``AttributeError`` in place of otto's clearest error message.
        """

        class _Idless:
            has_bash = False

        with pytest.raises(UnsupportedOnUserlandError):
            refuse_if_launch_wrapper_needs_bash(_Idless())


class TestTheTableDecidesWhetherTheClassIsRefused:
    """The registry half of the rule, and the reason this is not a hard-coded block."""

    def test_flipping_the_record_to_untested_stops_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proof the REGISTRY decides, not a predicate that happens to sit nearby.

        If the guard kept refusing with the record downgraded, it would be an
        ordinary capability check wearing the registry's message, and the table
        would be decoration.
        """
        gap = gap_for(SURFACE)
        assert gap is not None
        downgraded = Gap(
            surface=gap.surface,
            status=UNTESTED,
            reason=gap.reason,
            measured_on="",
            queued_for=gap.queued_for,
        )
        monkeypatch.setattr(userland, "GAPS", [downgraded])
        assert refuse_if_launch_wrapper_needs_bash(_Host(has_bash=False)) is None

    def test_the_record_names_the_function_the_launch_line_comes_from(self) -> None:
        """The record is what an operator reads; it must point at real code.

        Pinned because the record's ``reason`` is prose no other assertion can
        check, and this one clause of it — the dotted path — is checkable.
        """
        gap = gap_for(SURFACE)
        assert gap is not None
        assert "otto.host.daemon.launch_command" in gap.reason


# ===========================================================================
# Scope: refusing something that works is the expensive mistake
# ===========================================================================


class TestWhoIsNotRefused:
    """Two declarations and one absence, and none of them may raise."""

    def test_a_host_declaring_bash_is_not_refused(self) -> None:
        assert refuse_if_launch_wrapper_needs_bash(_Host(has_bash=True)) is None

    def test_a_host_declaring_nothing_at_all_is_not_refused(self) -> None:
        """Undeclared is not the measured class.

        The ``getattr`` default is ``True`` — the OPPOSITE of
        ``otto.tunnel.discovery``'s ``getattr(h, "has_bash", False)``, and
        deliberately so. There the conservative answer is to keep a host out of
        a scan; here it is to keep it out of a refusal, because refusing a host
        otto was never told about converts "we were not told" into "does not
        work". Every fake in otto's own link tests is exactly this shape, which
        is the other reason the default matters.
        """
        assert refuse_if_launch_wrapper_needs_bash(_Host()) is None

    def test_only_a_declaration_of_false_refuses(self) -> None:
        """``is False``, not falsiness.

        ``has_bash`` is a declared ``bool`` field on every host class otto
        ships. A host whose attribute is some other falsy value has not made
        this declaration, and reading one out of it would be otto inventing a
        measurement.
        """
        assert refuse_if_launch_wrapper_needs_bash(_Host(has_bash=None)) is None


# ===========================================================================
# End to end, through the profile a lab entry actually declares
# ===========================================================================


class TestThroughAHostBuiltFromLabData:
    """`os_type: busybox` is what a user writes; the chain has to reach the guard.

    Built through the real factory rather than a fake with ``has_bash`` hand-set:
    a fake would only restate the guard's own condition, not prove the profile
    produces it.
    """

    def test_a_busybox_profile_host_is_refused(self) -> None:
        from otto.host.factory import create_host_from_dict

        host = create_host_from_dict(
            {
                "element": "bb1",
                "os_type": "busybox",
                "ip": "192.0.2.1",
                "creds": [{"login": "v", "password": "v"}],
            }
        )
        assert host.has_bash is False
        with pytest.raises(UnsupportedOnUserlandError, match="bb1"):
            refuse_if_launch_wrapper_needs_bash(host)

    def test_a_plain_unix_profile_host_is_not_refused(self) -> None:
        """The same factory, one field different — so the profile is what did it."""
        from otto.host.factory import create_host_from_dict

        host = create_host_from_dict(
            {
                "element": "gnu1",
                "os_type": "unix",
                "ip": "192.0.2.2",
                "creds": [{"login": "v", "password": "v"}],
            }
        )
        assert host.has_bash is True
        assert refuse_if_launch_wrapper_needs_bash(host) is None

    def test_a_unix_lab_entry_may_declare_it_directly(self) -> None:
        """``has_bash`` is the fact and the profile is only one thing that sets it.

        A dash-only or otherwise bash-less GNU-ish host is declared, not
        profiled, and it cannot run the wrapper either. This is why the
        predicate is not the profile name.
        """
        from otto.host.factory import create_host_from_dict

        host = create_host_from_dict(
            {
                "element": "dash1",
                "os_type": "unix",
                "ip": "192.0.2.3",
                "has_bash": False,
                "creds": [{"login": "v", "password": "v"}],
            }
        )
        with pytest.raises(UnsupportedOnUserlandError, match="dash1"):
            refuse_if_launch_wrapper_needs_bash(host)
