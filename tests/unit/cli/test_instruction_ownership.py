"""``InstructionEntry.registered_by`` and the ``otto run`` dispatch gate (spec §5).

An instruction records the repo whose init modules registered it, and dispatch
refuses one whose owner is inactive for this invocation. Three things this
module is deliberate about, each of them a defect this branch already ate once:

* **The refusal is a RENDERED SENTENCE, not a return value.** Every assertion
  about it reads captured output through the real :func:`~otto.cli.invoke.fail`
  path at a pinned console width, and two cells prove the bracketed tokens
  (``[bench]``, ``[project]``) survive rich's markup parser — the exact shape
  that turned the bootstrap demotion warning's ``lab(s) [unix]`` into
  ``lab(s) )``.
* **The gate has to be REACHED.** ``TestPreambleWiring`` drives the real
  ``command_preamble`` and observes the refusal come out of *it*; testing
  :func:`~otto.cli.invoke.refuse_inactive_instruction` alone could not tell a
  wired gate from an orphaned function.
* **The ctx-chain walk has to be NARROW.** ``otto host <id> get`` must reach
  its body untouched, so the "not dispatched through run" arm is injected
  (a real ``host``-parented chain), never inherited.

Registry isolation is the ROOT conftest's ``_isolate_registries``, which
snapshots every ``otto.registry.Registry`` around every test — the same thing
``tests/unit/cli/test_default_instructions.py`` relies on rather than rolling
its own. ``test_a_registration_here_does_not_leak`` pins that reliance.
"""

import io
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from rich.console import Console
from rich.markup import escape

from otto.cli import invoke
from otto.cli.invoke import refuse_inactive_instruction
from otto.cli.registry import CommandSpec
from otto.instructions import INSTRUCTIONS, InstructionEntry
from otto.registry import registering_repo
from tests._fixtures.bootstrapstub import bootstrap_stub
from tests._fixtures.clickctx import chain
from tests._fixtures.rootoptions import make_root_options
from tests._fixtures.scoping import verdict


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin rich's width so a WRAP cannot masquerade as a missing word.

    Under capture rich falls back to 80 columns and hard-wraps, so a refusal
    longer than that fails ``message in out`` for a wrapping reason — a false
    red that sends the next reader hunting an escaping bug. Same pin, same
    reason, as ``tests/unit/cli/test_error_render.py``.
    """
    monkeypatch.setenv("COLUMNS", "300")


def _install_entry(name: str, registered_by: "str | None") -> None:
    """Register *name* owned by *registered_by* (``None`` = first-party)."""
    INSTRUCTIONS.register(
        name,
        InstructionEntry(name=name, sub_app=typer.Typer(), module="m", registered_by=registered_by),
        origin="m",
    )


_NARROW_WIDTH = 80
"""The width rich falls back to when it cannot detect a terminal — the narrowest
a real invocation ever renders at, and so the one every hint has to survive."""


def _hard_wrapped_lines(text: str, width: int = _NARROW_WIDTH) -> int:
    """Lines *text* occupies when rendered the DEFAULT, hard-wrapping way.

    The control behind every width assertion below: it answers "would this
    string have folded WITHOUT the fix?" against a real ``Console`` rather than
    against a character count someone did in their head. ``escape`` mirrors what
    ``print_error`` does, and markup tags occupy no width, so this measures the
    same text the refusal renders.
    """
    buf = io.StringIO()
    Console(file=buf, width=width, no_color=True).print(escape(text))
    return len(buf.getvalue().rstrip("\n").splitlines())


# ── The narrow-terminal case table ───────────────────────────────────────────
#
# Every case FOLDS at 80 columns without the fix, and each cell asserts that
# before asserting the fix undid it — so none of them can quietly go inert.
# The name lengths are measured, not guessed: per shape, the shortest repo name
# whose hint reaches the wrap column is
#
#   switch hint           name >= 15    `firmware-integration` (20) -> 92 chars
#   lab hint, 3 patterns  name >= 13    `firmware-integration` (20) -> 88 chars
#   lab hint, 1 pattern   name >= 24    `firmware-integration-app` (24) -> 81
#   host-starved hint     name >= 38    the shape is almost entirely FIXED text
#
# The single-pattern lab hint is why the previous version of this table could
# not fail: the shared `verdict` fixture only ever produced ONE lab_pattern,
# capping that hint at 73 characters for a 20-char name, and the host-starved
# hint at 62. A repo declaring three labs is ordinary, and is the shape that
# folds in the field.
#
# Short names are deliberately NOT included. At 80 columns `repo2` fits in every
# shape whatever the renderer does, so a short-name width cell asserts nothing;
# the short-name copy is already pinned by `TestRefusal`, at a wide console.
_SWITCH_OWNER = "firmware-integration"
_LAB_OWNER = "firmware-integration"
_LAB_ONE_PATTERN_OWNER = "firmware-integration-app"
#: 38 characters — long, and stated rather than hidden. `  activate it: widen
#: host_patterns, or: -I ` is 43 fixed characters, so no SHORTER name can fold
#: this shape at all. The alternative was leaving a cell that reads as coverage
#: and asserts nothing, which is worse than having none.
_STARVED_OWNER = "zephyr-firmware-integration-regression"

_FOLDING_CASES = [
    (
        "switch",
        {"owner": _SWITCH_OWNER, "excluded_by_switch": True},
        (
            f"'flash-b' belongs to repo '{_SWITCH_OWNER}', which was switched off for "
            f"this run (--exclude-projects {_SWITCH_OWNER})"
        ),
        f"  activate it: remove --exclude-projects {_SWITCH_OWNER}    or: -I {_SWITCH_OWNER}",
    ),
    (
        "lab-three-patterns",
        {
            "owner": _LAB_OWNER,
            "scope": verdict(
                _LAB_OWNER,
                excluded=True,
                lab_patterns=("bench-lab", "unix-lab", "zephyr-lab"),
            ),
        },
        (
            f"'flash-b' belongs to repo '{_LAB_OWNER}', which is inactive for the "
            f"loaded lab(s) [bench] (lab_patterns: bench-lab, unix-lab, zephyr-lab)"
        ),
        f"  activate it: -l bench-lab / -l unix-lab / -l zephyr-lab    or: -I {_LAB_OWNER}",
    ),
    (
        "lab-one-pattern",
        {
            "owner": _LAB_ONE_PATTERN_OWNER,
            "scope": verdict(_LAB_ONE_PATTERN_OWNER, excluded=True),
        },
        (
            f"'flash-b' belongs to repo '{_LAB_ONE_PATTERN_OWNER}', which is inactive "
            f"for the loaded lab(s) [bench] (lab_patterns: {_LAB_ONE_PATTERN_OWNER}-lab)"
        ),
        f"  activate it: -l {_LAB_ONE_PATTERN_OWNER}-lab    or: -I {_LAB_ONE_PATTERN_OWNER}",
    ),
    (
        "host-starved",
        {"owner": _STARVED_OWNER, "scope": verdict(_STARVED_OWNER, universe=())},
        (
            f"'flash-b' belongs to repo '{_STARVED_OWNER}', which is inactive: its "
            f"[project] host_patterns (.*) match no host in the loaded lab(s) [bench]"
        ),
        f"  activate it: widen host_patterns, or: -I {_STARVED_OWNER}",
    ),
]

#: Readable node ids — the tuples themselves stringify into unusable ones.
_FOLDING_CASE_IDS = [case[0] for case in _FOLDING_CASES]


def _dispatch_ctx(instruction_name: str) -> Any:
    """The chain ``otto run <instruction>`` builds: root -> run -> leaf.

    ``info_name`` is what click 8.3's ``Group.resolve_command`` hands the child
    ``Context`` — the name the user typed, which for a registry-backed group is
    the registry key.
    """
    return chain("otto", "run", instruction_name)


class TestRegisteredBy:
    """The field, and who fills it in."""

    def test_decorator_records_the_registering_repo(self) -> None:
        from otto.cli.run import instruction

        with registering_repo("repo9"):

            @instruction()
            async def probe_owner() -> None: ...

        assert INSTRUCTIONS.get("probe-owner").registered_by == "repo9"

    def test_first_party_registration_records_none(self) -> None:
        from otto.cli.run import instruction

        @instruction()
        async def probe_first_party() -> None: ...

        assert INSTRUCTIONS.get("probe-first-party").registered_by is None

    def test_a_hand_built_entry_defaults_to_none(self) -> None:
        """The field is DEFAULTED, so every existing construction still builds.

        A repo that hand-registers an ``InstructionEntry`` therefore gets
        first-party treatment. Conservative on purpose: nothing new is ever
        refused by omission.
        """
        entry = InstructionEntry(name="hand", sub_app=typer.Typer(), module="m")
        assert entry.registered_by is None

    def test_a_registration_here_does_not_leak(self) -> None:
        """The root conftest's ``_isolate_registries`` is what cleans up after these.

        ``probe-owner`` is registered by the first test in this class; if the
        snapshot/restore did not cover ``INSTRUCTIONS`` it would still be here
        (or, under the nightly repeat, collide loudly on re-registration) and
        this module would need an isolation fixture of its own.
        """
        assert "probe-owner" not in INSTRUCTIONS
        assert "flash-b" not in INSTRUCTIONS


class TestRefusal:
    """The gate itself: who is refused, with which sentence, at which exit code."""

    def _wire_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        include: "tuple[str, ...]" = (),
        exclude: "tuple[str, ...]" = (),
        scopes: "dict[str, Any] | None" = None,
    ) -> Any:
        ctx = SimpleNamespace(
            include_projects=tuple(include),
            exclude_projects=tuple(exclude),
            scopes=dict(scopes or {}),
        )
        monkeypatch.setattr("otto.context.get_context", lambda: ctx)
        return ctx

    def test_switched_off_owner_refuses_exit_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _install_entry("flash-b", "repo2")
        self._wire_context(monkeypatch, exclude=("repo2",))
        with pytest.raises(typer.Exit) as excinfo:
            refuse_inactive_instruction(_dispatch_ctx("flash-b"))
        assert excinfo.value.exit_code == 1
        out = capsys.readouterr().out
        assert "'flash-b' belongs to repo 'repo2'" in out
        assert "which was switched off for this run (--exclude-projects repo2)" in out
        assert "activate it: remove --exclude-projects repo2    or: -I repo2" in out

    def test_active_owner_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_entry("flash-b", "repo2")
        self._wire_context(monkeypatch)
        refuse_inactive_instruction(_dispatch_ctx("flash-b"))  # must not raise

    def test_first_party_is_never_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_entry("install", None)
        self._wire_context(monkeypatch, exclude=("otto",))
        refuse_inactive_instruction(_dispatch_ctx("install"))  # must not raise

    def test_lab_excluded_owner_names_both_fixes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _install_entry("flash-b", "repo2")
        self._wire_context(monkeypatch, scopes={"repo2": verdict("repo2", excluded=True)})
        with pytest.raises(typer.Exit) as excinfo:
            refuse_inactive_instruction(_dispatch_ctx("flash-b"))
        assert excinfo.value.exit_code == 1
        out = capsys.readouterr().out
        assert "inactive for the loaded lab(s) [bench] (lab_patterns: repo2-lab)" in out
        assert "activate it: -l repo2-lab    or: -I repo2" in out

    def test_host_starved_owner_names_its_host_patterns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The OTHER skip shape: labs match, no host does (spec §5).

        Distinct copy, distinct fix — "wrong lab loaded" and "no host here
        matches your host_patterns" send the reader to different places, and a
        gate that rendered one message for both would send half of them wrong.
        """
        _install_entry("flash-b", "repo2")
        self._wire_context(monkeypatch, scopes={"repo2": verdict("repo2", universe=())})
        with pytest.raises(typer.Exit) as excinfo:
            refuse_inactive_instruction(_dispatch_ctx("flash-b"))
        assert excinfo.value.exit_code == 1
        out = capsys.readouterr().out
        assert "its [project] host_patterns (.*) match no host" in out
        assert "in the loaded lab(s) [bench]" in out
        assert "activate it: widen host_patterns, or: -I repo2" in out
        assert "inactive for the loaded lab(s)" not in out

    def test_an_undeclared_owner_is_never_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole-lab fallback (scoping spec §6) stays active — no ``[project]``, no gate."""
        _install_entry("flash-b", "repo2")
        self._wire_context(
            monkeypatch, scopes={"repo2": verdict("repo2", declared=False, universe=())}
        )
        refuse_inactive_instruction(_dispatch_ctx("flash-b"))  # must not raise

    def test_include_rescues_an_owner_its_lab_verdict_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hint the refusal prints has to WORK: ``-I repo2`` beats the lab verdict."""
        _install_entry("flash-b", "repo2")
        self._wire_context(
            monkeypatch,
            include=("repo2",),
            scopes={"repo2": verdict("repo2", excluded=True)},
        )
        refuse_inactive_instruction(_dispatch_ctx("flash-b"))  # must not raise

    def test_an_unregistered_leaf_name_is_not_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A statically-declared ``run`` child (not in the registry) has no owner to judge."""
        self._wire_context(monkeypatch, exclude=("repo2",))
        refuse_inactive_instruction(_dispatch_ctx("not-an-instruction"))  # must not raise

    def test_non_run_leaves_pass_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``otto host <id> get`` is not instruction dispatch — the gate must not look.

        The context is wired to refuse EVERYTHING (``get`` is registered and
        excluded), so a walk that stopped at any parent rather than the ``run``
        group would exit 1 here.
        """
        _install_entry("get", "repo2")
        self._wire_context(monkeypatch, exclude=("repo2",))
        refuse_inactive_instruction(chain("otto", "host", "get"))  # must not raise

    def test_a_sub_group_under_run_is_judged_by_the_group_name(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``otto run <group> <leaf>``: the registry key is the GROUP, not the leaf.

        An instruction registered as a sub-group (``add_typer``) puts a node
        between ``run`` and the leaf, which is why the walk climbs to the child
        of ``run`` instead of reading the leaf it was handed.
        """
        _install_entry("flash-b", "repo2")
        self._wire_context(monkeypatch, exclude=("repo2",))
        with pytest.raises(typer.Exit) as excinfo:
            refuse_inactive_instruction(chain("otto", "run", "flash-b", "sub"))
        assert excinfo.value.exit_code == 1
        assert "'flash-b' belongs to repo 'repo2'" in capsys.readouterr().out


class TestRefusalRendering:
    """The sentence has to REACH a user, on a known stream, with its brackets."""

    def _refuse(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scope: Any = None,
        *,
        owner: str = "repo2",
        excluded_by_switch: bool = False,
    ) -> None:
        _install_entry("flash-b", owner)
        ctx = SimpleNamespace(
            include_projects=(),
            exclude_projects=(owner,) if excluded_by_switch else (),
            scopes={} if scope is None else {owner: scope},
        )
        monkeypatch.setattr("otto.context.get_context", lambda: ctx)
        with pytest.raises(typer.Exit) as excinfo:
            refuse_inactive_instruction(_dispatch_ctx("flash-b"))
        assert excinfo.value.exit_code == 1

    def test_the_lab_list_brackets_survive_rendering(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``[bench]`` must print, not vanish.

        Rich reads ``[word]`` as a style tag and DELETES it — the bootstrap
        demotion warning was caught rendering ``lab(s) [unix]`` as
        ``lab(s) )``. ``fail`` escapes, so this passes; a hand-rolled
        ``rprint(f"[red]{msg}[/red]")`` would not.
        """
        self._refuse(monkeypatch, verdict("repo2", excluded=True))
        assert "[bench]" in capsys.readouterr().out

    def test_the_project_table_name_survives_rendering(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``[project]`` names a TOML table; eaten, the sentence tells the reader nothing."""
        self._refuse(monkeypatch, verdict("repo2", universe=()))
        assert "[project]" in capsys.readouterr().out

    def test_the_refusal_lands_on_stdout_and_not_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pinned deliberately: ``fail`` renders through rich to STDOUT.

        Every other CLI refusal in otto goes there (the ast-grep rule
        ``error-render-through-helper`` requires the helper), and the exit code
        carries the machine-readable half. A reader piping stdout away loses
        this line — that is the shipped trade-off for consistency, and this
        cell is where it changes if that is ever revisited.
        """
        self._refuse(monkeypatch, verdict("repo2", excluded=True))
        captured = capsys.readouterr()
        assert "belongs to repo 'repo2'" in captured.out
        assert captured.err == ""

    @pytest.mark.parametrize(
        ("case_id", "kwargs", "detail", "hint"), _FOLDING_CASES, ids=_FOLDING_CASE_IDS
    )
    def test_no_hint_shape_is_split_by_a_narrow_terminal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        case_id: str,
        kwargs: "dict[str, Any]",
        detail: str,
        hint: str,
    ) -> None:
        """At 80 columns every hint stays COPYABLE — and every case here CAN fail.

        Rich hard-wraps at the console width and folds between WORDS, so a hint
        ending ``or: -I firmware-integration`` breaks after the ``-I`` and hands
        the reader a switch they cannot paste. The refusal renders with
        ``soft_wrap=True`` for exactly this reason: a long line beats a broken
        argv.

        The FIRST assertion is what keeps this honest. A width cell whose fixture
        is too short to reach the wrap column asserts nothing — it passes with
        the fix and without it — while reading as coverage. So each case proves,
        against a real hard-wrapping console, that its own hint DOES fold before
        asserting that the fix undid it. Shorten a fixture and this cell fails
        loudly instead of going quietly inert.
        """
        assert _hard_wrapped_lines(hint) > 1, (
            f"{case_id}: the hint is {len(hint)} chars and never reaches the "
            f"{_NARROW_WIDTH}-column wrap point, so this case cannot fail"
        )

        monkeypatch.setenv("COLUMNS", str(_NARROW_WIDTH))
        self._refuse(monkeypatch, **kwargs)
        assert hint in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("case_id", "kwargs", "detail", "hint"), _FOLDING_CASES, ids=_FOLDING_CASE_IDS
    )
    def test_no_detail_line_is_cropped_by_a_narrow_terminal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        case_id: str,
        kwargs: "dict[str, Any]",
        detail: str,
        hint: str,
    ) -> None:
        """Soft wrap must cost the message its FOLDS, never its CONTENT.

        ``soft_wrap=True`` sets ``no_wrap``, sets ``overflow="ignore"`` AND turns
        cropping off. The last of those is the one nobody expects: with
        ``no_wrap`` alone, a line longer than the console is TRUNCATED at the
        width rather than folded at it, so the sentence loses its tail and still
        looks like a complete error. Asserting the whole detail line — the long
        prose half, at a width it exceeds, in every shape — is what pins it.

        Same self-proving control as the cell above, for the same reason.
        """
        assert _hard_wrapped_lines(detail) > 1, (
            f"{case_id}: the detail line is {len(detail)} chars and never reaches "
            f"the {_NARROW_WIDTH}-column mark, so cropping could not show here"
        )
        monkeypatch.setenv("COLUMNS", str(_NARROW_WIDTH))
        self._refuse(monkeypatch, **kwargs)
        assert detail in capsys.readouterr().out


class _PreambleCtx:
    """A leaf ctx the real ``command_preamble`` accepts, dispatched under ``run``."""

    def __init__(self, name: str, spec: CommandSpec) -> None:
        self.command = SimpleNamespace(
            name=name, callback=SimpleNamespace(__cli_output_dir__=False)
        )
        self.info_name = name
        self.parent = chain("otto", "run")
        self.meta: "dict[str, Any]" = {
            "_otto_command_spec": spec,
            "_otto_root_options": make_root_options(),
        }


class TestPreambleWiring:
    """The gate is CALLED, from the right branch, in the right order."""

    @pytest.fixture(autouse=True)
    def _quiet_preamble(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Everything the preamble does EXCEPT the gate, stubbed to nothing."""
        monkeypatch.setattr("otto.bootstrap.bootstrap", bootstrap_stub)
        monkeypatch.setattr(invoke, "stop_at_dry_run_seam", lambda ctx, spec: None)

    def _excluded_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_entry("flash-b", "repo2")
        monkeypatch.setattr(
            "otto.context.get_context",
            lambda: SimpleNamespace(include_projects=(), exclude_projects=("repo2",), scopes={}),
        )

    def test_the_preamble_refuses_an_inactive_owner(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Delete the call from ``command_preamble`` and this cell goes red."""
        monkeypatch.setattr(invoke, "ensure_lab_session", lambda ctx, spec: None)
        self._excluded_context(monkeypatch)
        spec = CommandSpec(name="run", loader=None, gate=False)
        with pytest.raises(typer.Exit) as excinfo:
            invoke.command_preamble(_PreambleCtx("flash-b", spec))  # ty: ignore[invalid-argument-type]
        assert excinfo.value.exit_code == 1
        assert "belongs to repo 'repo2'" in capsys.readouterr().out

    def test_the_gate_runs_after_the_lab_session_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verdict comes from the context ``ensure_lab_session`` installs.

        Hostile condition INJECTED, not inherited: ``get_context`` raises until
        the lab session has run, so a gate hoisted above ``ensure_lab_session``
        dies with that ``RuntimeError`` instead of refusing. Ordering is not a
        stylistic detail here — before the session there are no lab verdicts,
        so every owner would resolve ACTIVE and the gate would never refuse.
        """
        _install_entry("flash-b", "repo2")
        state = {"session": False}

        def _session(ctx: Any, spec: Any) -> None:
            state["session"] = True

        def _get_context() -> Any:
            if not state["session"]:
                raise RuntimeError("the gate ran before ensure_lab_session")
            return SimpleNamespace(include_projects=(), exclude_projects=("repo2",), scopes={})

        monkeypatch.setattr(invoke, "ensure_lab_session", _session)
        monkeypatch.setattr("otto.context.get_context", _get_context)
        spec = CommandSpec(name="run", loader=None, gate=False)
        with pytest.raises(typer.Exit) as excinfo:
            invoke.command_preamble(_PreambleCtx("flash-b", spec))  # ty: ignore[invalid-argument-type]
        assert excinfo.value.exit_code == 1

    def test_the_gate_runs_before_the_reservation_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An excluded repo's instruction must not first warn about a reservation.

        The reservation gate speaks about a run that is about to happen; this
        one is not going to.
        """
        from unittest.mock import MagicMock

        from otto.reservations import ReservationGateResult

        monkeypatch.setattr(invoke, "ensure_lab_session", lambda ctx, spec: None)
        self._excluded_context(monkeypatch)
        reservation = MagicMock()
        # A REAL outcome, not a bare MagicMock: with the latter, a gate moved
        # after the reservation check fails on a TypeError inside rich rather
        # than on the assertion below — red for an incidental reason, which
        # would stop discriminating the moment that rendering changed.
        reservation.evaluate.return_value = ReservationGateResult(
            checked=True, skipped=False, warning=None
        )
        spec = CommandSpec(name="run", loader=None, gate=True)
        ctx = _PreambleCtx("flash-b", spec)
        ctx.meta["otto_reservation"] = reservation
        with pytest.raises(typer.Exit) as excinfo:
            invoke.command_preamble(ctx)  # ty: ignore[invalid-argument-type]
        assert excinfo.value.exit_code == 1
        reservation.evaluate.assert_not_called()

    def test_a_lab_free_spec_never_reaches_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``lab_free`` means "no lab session", so there is no verdict to consult.

        The gate lives inside the ``if not spec.lab_free`` branch; hoisting it
        out would ask ``active()`` about a context with no scopes and, worse,
        gate ``otto monitor``-shaped commands that never dispatch instructions.
        """
        monkeypatch.setattr(
            invoke,
            "ensure_lab_session",
            lambda ctx, spec: pytest.fail("lab_free must not build a lab session"),
        )
        self._excluded_context(monkeypatch)
        spec = CommandSpec(name="run", loader=None, lab_free=True)
        invoke.command_preamble(_PreambleCtx("flash-b", spec))  # ty: ignore[invalid-argument-type]
        assert capsys.readouterr().out == ""
