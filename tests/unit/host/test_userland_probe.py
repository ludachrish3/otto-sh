"""The ``probe`` verb: what it prints, and what it must never print.

THE PASTEABLE PIN IS THE PRODUCT. A maintainer runs this once against a slow
device and records the answers, and every later connection then issues nothing
-- so these guards are about the PAYLOAD first and the table second. The one
that matters most is
:func:`test_no_assumed_value_can_reach_the_pasteable_pin`: pinning a guess is
exactly what ``Userland.as_lab_json`` exists to prevent, and a formatting layer
is the obvious place to lose that property again.

WHAT THIS FILE DELIBERATELY DOES NOT RE-ASSERT. That the payload validates at
the lab-data boundary, builds runtime options, and then costs a second host
zero commands is ``test_userland.py``'s
``test_the_pasteable_pin_round_trips_through_lab_data_and_then_costs_nothing``,
which executes that whole loop over ``as_lab_json()``. Restating it here
through the renderer would be a second, weaker copy. What is asserted instead
is the LINK: the object this command prints is that same ``as_lab_json()``
payload, carried under the key and the nesting a ``UnixHostSpec`` really
parses. The two halves compose, and neither has to trust a description of the
other.

NO PROBE SPELLING APPEARS HERE, and that is deliberate rather than lazy. Which
command otto issues for a capability is ``test_userland.py``'s subject, under a
three-copy discipline with the product and the Tier 1 measurements; the report
is a function of the ANSWERS, not of the questions, so scripting a device by
behaviour keeps a fourth copy of those spellings from existing. The applet
batch is the one command whose text matters here, and :class:`_Device` builds
it with the product's own ``_applet_probe_command`` rather than writing it out.
"""

import asyncio
import json
import logging
import re
from dataclasses import fields

import pytest
from rich.text import Text

from otto.host.errors import PasswordRefusedError
from otto.host.local_host import LocalHost
from otto.host.options import UserlandOptions
from otto.host.os_profile import OsProfile, ProfileContext
from otto.host.specs import UnixHostSpec
from otto.host.userland import (
    _APPLET_CONTROL,
    _RESOLVE_BUDGET_S,
    _RETRY_COOLDOWN_S,
    PROBED_APPLETS,
    Userland,
    UserlandHost,
    _applet_probe_command,
    _busybox_profile_hint,
    applet_capability,
)
from otto.logger.mode import LogMode
from otto.registry import FrozenMap
from otto.result import CommandResult, Result
from otto.utils import Status

# The capabilities a user may pin, taken from the lab-data side rather than
# from the product's own `_UNASKABLE_DEFAULTS` -- which is what the report
# iterates, so an expectation drawn from it could not catch the report
# iterating the wrong thing. `version` is the one `UserlandOptions` field with
# no probe (a declared version is documentation and must never gate
# behaviour); `test_userland.py::_NEVER_PROBED` is the other copy of that fact
# and states the reasoning in full.
_PINNABLE = sorted(f.name for f in fields(UserlandOptions) if f.name != "version")

# The table's rows: the pinnable options, plus the shell, which a host entry
# declares as its own ``command_frame`` key rather than as a userland option.
_ROWS = sorted([*_PINNABLE, "command_frame"])

# The three words `_resolve_once` classifies a capability with. Spelled out so
# a report that quietly stopped emitting one of them cannot pass by matching
# whatever it does emit instead.
_SOURCES = {"declared", "probed", "assumed"}

# `_resolve_once`'s own per-capability debug line -- the only place its
# `sources` map is observable from outside the method. Anchored and strict: the
# pin line it emits immediately afterwards carries the same `userland: `
# prefix, and a loose pattern would scrape that too.
_SOURCE_LINE = re.compile(r"^userland: (\S+) = (\S+) \((\w+)\)$")


class _Device:
    """A userland scripted by its ANSWERS: one verdict for the probes, one for the batch.

    Deliberately coarse. Every fixed probe is read for its exit code alone, so
    ``retcode=0`` is a device that says yes to all six and ``retcode=1`` one
    that says no to all six -- both MEASUREMENTS. ``raises`` is the third state
    and the one the tri-state exists for: the probe could not be asked at all.

    The applet batch is the exception, because otto parses its output and
    accepts it only when the answered names are EXACTLY the ones it asked
    about. ``applets`` names the list this device expects to be asked for, and
    the command is rebuilt with the product's own ``_applet_probe_command`` --
    so a batch asking about a different set does not match, goes unanswered,
    and surfaces as unasked applet capabilities. ``applets=None`` drives that
    on purpose: six measured capabilities and seven that could not be asked, in
    one resolution.
    """

    def __init__(
        self,
        *,
        retcode: int = 0,
        raises: BaseException | None = None,
        applets: "list[str] | None" = None,
        present: "tuple[str, ...]" = (),
    ) -> None:
        self.calls: list[str] = []
        self._retcode = retcode
        self._raises = raises
        self._batch = None if applets is None else _applet_probe_command(applets)
        self._batch_out = (
            None
            if applets is None
            else "".join(
                f"{name}={answer}\n"
                for name, answer in [
                    (_APPLET_CONTROL, 1),
                    *((a, int(a in present)) for a in applets),
                ]
            )
        )

    async def __call__(self, cmd: str, *, log: LogMode = LogMode.NORMAL, **kw) -> CommandResult:
        self.calls.append(cmd)
        # A real exec suspends on the network; without a suspension point the
        # resolution never yields and no concurrency defect could ever show.
        await asyncio.sleep(0)
        if self._raises is not None:
            raise self._raises
        if self._batch is not None and cmd == self._batch:
            return CommandResult(status=Status.Success, value=self._batch_out or "", retcode=0)
        status = Status.Success if self._retcode == 0 else Status.Error
        return CommandResult(status=status, value="", retcode=self._retcode)


_PROBE_LOGIN = "admin"
"""The login the scripted host's probe asks sudo as."""


class _ScriptedHost(UserlandHost):
    """The mixin under test, plus one :class:`Userland` and nothing else.

    Deliberately not a ``UnixHost``: what is exercised here is the verb's
    rendering over a resolver whose answers the test scripts, and a real host
    would add a connection stack no assertion below touches. That the verb
    reaches the concrete host classes at all is a separate question, answered
    by :func:`test_the_verb_reaches_every_posix_host_and_no_other`.
    """

    def __init__(
        self, userland: "Userland | None", sudo_shell: "bool | str | BaseException" = True
    ) -> None:
        self._u = userland
        self._sudo_shell = sudo_shell

    def _userland(self) -> "Userland | None":
        return self._u

    def _acting_user(self, user: "str | None") -> str:
        return user or _PROBE_LOGIN

    async def _prepare_exec_elevation(self, user: "str | None" = None) -> None:
        """Answer ``sudo -l sh`` as scripted, through the one resolver an exec uses."""

        async def ask() -> "bool | str":
            if isinstance(self._sudo_shell, BaseException):
                raise self._sudo_shell
            return self._sudo_shell

        if self._u is not None and self._u.elevation == "sudo":
            await self._u.resolve_sudo_form(login=self._acting_user(user), probe=ask)


def _userland(options: "UserlandOptions | None" = None, **device_kw) -> Userland:
    return Userland(options or UserlandOptions(), _Device(**device_kw))


async def _report(
    userland: "Userland | None", sudo_shell: "bool | str | BaseException" = True
) -> list[str]:
    """Run the verb and hand back the lines it would print."""
    result = await _ScriptedHost(userland, sudo_shell).probe()
    assert isinstance(result, Result)
    assert result.is_ok, f"the verb reported failure: {result.msg}"
    assert isinstance(result.value, list), (
        f"the CLI renders a list one item per line; got {type(result.value).__name__}"
    )
    return result.value


def _pin_text(lines: list) -> str:
    """The printed pin, from its key to the end -- what a user would select."""
    lines = [line for line in lines if isinstance(line, str)]
    keys = ('"command_frame"', '"userland_options"')
    start = next((i for i, line in enumerate(lines) if line.startswith(keys)), None)
    assert start is not None, "no pasteable pin in the output:\n" + "\n".join(lines)
    return "\n".join(lines[start:])


def _pinned(lines: list) -> dict[str, str]:
    """Parse the printed pin the way pasting it into ``lab.json`` would.

    Wrapped in braces rather than stripped of its key, because the key and the
    nesting are half of what is under test: this parses only if what was
    printed is a well-formed member of a JSON object.
    """
    lines = [line for line in lines if isinstance(line, str)]
    parsed = json.loads("{" + _pin_text(lines) + "}")
    assert set(parsed) <= {"command_frame", "userland_options"}, (
        f"unexpected top-level keys: {list(parsed)}"
    )
    assert "command_frame" not in parsed["userland_options"], (
        "command_frame is the host's own key, never a userland option"
    )
    return parsed["userland_options"]


def _rows(lines: list) -> dict[str, tuple[str, str]]:
    """``{capability: (value, source)}`` read from the Rich table in the report."""
    from rich.table import Table

    table = next(item for item in lines if isinstance(item, Table))
    names = [str(c) for c in table.columns[0]._cells]
    values = [str(c) for c in table.columns[1]._cells]
    sources = [str(c) for c in table.columns[2]._cells]
    return {n: (v, s) for n, v, s in zip(names, values, sources, strict=True)}


# ---------------------------------------------------------------------------
# The pasteable pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_assumed_value_can_reach_the_pasteable_pin():
    """THE PASTE-SAFETY GATE, restated at the layer that renders the payload.

    ``as_lab_json()`` omits an assumed value because inside a JSON payload a
    guess is indistinguishable from a measurement, whatever the surrounding
    lines say. A renderer that rebuilt the payload from the values it already
    holds for the table -- which include the assumed ones, since those are the
    values in force -- would undo that with no other symptom: the output would
    look MORE complete, and answers nobody took would land permanently in a
    maintainer's lab data.

    The device leaves the applet batch unanswered so there is something to
    lose, and the guard asserts that before asserting the absence: on a device
    that settled everything this would pass over an empty set.
    """
    userland = _userland(applets=None)
    lines = await _report(userland)

    assumed = [n for n, (_, source) in _rows(lines).items() if source == "assumed"]
    assert assumed, "the device settled everything — this guard would pass vacuously"

    pinned = _pinned(lines)
    leaked = sorted(set(assumed) & set(pinned))
    assert not leaked, (
        f"assumed values reached the pasteable pin: {leaked}. as_lab_json() drops these on "
        f"purpose; the renderer must not put them back"
    )
    assert pinned == userland.as_lab_json(sudo_login=_PROBE_LOGIN), (
        "the printed pin is not the settled payload — the renderer must format what "
        "as_lab_json() returns rather than build its own"
    )


@pytest.mark.asyncio
async def test_the_printed_pin_parses_as_the_lab_data_key_a_host_entry_really_carries():
    """The key and the nesting, executed against the spec rather than against the log line.

    ``UserlandOptionsSpec`` sets ``extra='forbid'`` and ``UnixHostSpec`` names
    the table ``userland_options``, so a renamed key or a flattened payload
    fails validation here instead of failing quietly in someone's ``lab.json``.
    What happens to the payload next -- runtime options, then a second host
    that issues no command -- is
    ``test_userland.py::test_the_pasteable_pin_round_trips_through_lab_data_and_then_costs_nothing``.
    This says the printed text IS that payload, so the two compose.
    """
    userland = _userland(applets=list(PROBED_APPLETS), present=("base64", "nc"))
    lines = await _report(userland)

    entry = {
        "ip": "10.0.0.1",
        "creds": [{"login": "u", "password": "p"}],
        **json.loads("{" + _pin_text(lines) + "}"),
    }
    spec = UnixHostSpec.model_validate(entry)

    assert spec.userland_options.to_runtime() == UserlandOptions(
        **userland.as_lab_json(sudo_login=_PROBE_LOGIN)
    ), "the host entry built from the printed pin does not carry the answers otto measured"


@pytest.mark.asyncio
async def test_a_device_that_answers_everything_offers_every_capability_for_pinning():
    """The recon-once payoff, stated as a completeness claim.

    A pin that quietly dropped one capability would still validate and still
    round-trip; it would just leave that one to be probed on every future
    connection, which is the cost the whole feature exists to remove. Compared
    against ``UserlandOptions``' own fields, so a capability added later has to
    reach the printed pin or redden here.
    """
    userland = _userland(applets=list(PROBED_APPLETS))
    lines = await _report(userland)

    assert sorted(_pinned(lines)) == _PINNABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(("retcode", "frame"), [(0, "bash"), (1, "ash")])
async def test_the_measured_shell_is_printed_as_the_host_s_command_frame(retcode, frame):
    """The probe still reports the shell it found, as the key a host entry declares it with.

    ``command_frame`` is a host key, so it is printed beside the
    ``userland_options`` table and never inside it, and the old
    ``shell_dialect`` name appears nowhere.
    """
    userland = _userland(retcode=retcode, applets=list(PROBED_APPLETS))
    lines = await _report(userland)

    assert _rows(lines)["command_frame"] == (frame, "probed")
    text = _pin_text(lines)
    assert text.startswith(f'"command_frame": "{frame}",\n"userland_options": {{'), text
    assert json.loads("{" + text + "}")["command_frame"] == frame
    assert "shell_dialect" not in "\n".join(str(line) for line in lines)
    assert "shell_dialect" not in _rows(lines)


@pytest.mark.asyncio
async def test_a_declared_command_frame_is_reported_as_declared_and_never_probed():
    """A host that declares its frame has answered the question, so nothing asks the device."""
    device = _Device(applets=list(PROBED_APPLETS))
    userland = Userland(UserlandOptions(), device, command_frame="ash")
    lines = await _report(userland)

    assert _rows(lines)["command_frame"] == ("ash", "declared")
    assert not [c for c in device.calls if "BASH_VERSION" in c], device.calls
    assert _pin_text(lines).startswith('"command_frame": "ash",\n')


@pytest.mark.asyncio
async def test_a_host_that_settled_nothing_prints_no_pin_and_says_why():
    """An empty pin is the honest output, so the reasons have to travel with it.

    A bare ``"userland_options": {}`` is pasteable, useless, and worse than
    useless: it reads as a verdict about the device. What actually happened is
    that otto measured nothing this round -- bounded by the resolution budget,
    then held off by the retry cooldown -- and the device may well answer a
    later one. Both numbers are interpolated from the constants rather than
    typed out, so a changed bound cannot leave the explanation quoting the old
    one.
    """
    lines = await _report(
        _userland(raises=OSError("channel refused")), sudo_shell="the channel was refused"
    )
    text = "\n".join(str(line) for line in lines if isinstance(line, (str, Text)))

    assert '"userland_options"' not in text, f"an empty pin was printed anyway:\n{text}"
    assert f"{_RESOLVE_BUDGET_S:.0f}s" in text, "the budget is not named as a reason"
    assert f"{_RETRY_COOLDOWN_S:.0f}s" in text, "the retry cooldown is not named as a reason"


# ---------------------------------------------------------------------------
# The human reading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_source_column_agrees_with_the_resolution_that_produced_it(caplog):
    """Every row's source, checked against ``_resolve_once``'s own classification.

    The report RECONSTRUCTS the three arms from ``is_settled()`` and the
    declarations, because ``_resolve_once`` spends its ``sources`` map on the
    debug lines and retains nothing. That reconstruction is the part that can
    drift, so it is not compared against a hand-written expectation here -- it
    is compared against those very debug lines, emitted by the resolution this
    call just performed. A renderer that began calling a probed answer
    "declared", or dropped the column altogether, reddens; so does a change to
    ``_resolve_once``'s own arms that the report fails to follow.

    The device is scripted to produce all three sources at once and the guard
    asserts that first: a run with one arm in it would compare a column that
    agrees trivially.
    """
    caplog.set_level(logging.DEBUG, logger="otto.host.userland")
    userland = _userland(UserlandOptions(elevation="su"), applets=None)
    lines = await _report(userland)

    from_resolution = {}
    for record in caplog.records:
        match = _SOURCE_LINE.match(record.getMessage())
        if match:
            from_resolution[match.group(1)] = (match.group(2), match.group(3))
    assert from_resolution, "no per-capability debug lines — nothing to compare against"
    assert {source for _, source in from_resolution.values()} == _SOURCES, (
        f"the scripted device produced only "
        f"{sorted({s for _, s in from_resolution.values()})}; a source column printing one "
        f"constant would still agree"
    )

    assert _rows(lines) == from_resolution


@pytest.mark.asyncio
async def test_every_capability_appears_in_the_table_with_a_source():
    """The table is the complete picture; the pin is only the safe subset of it.

    A reader has to see the value a capability is IN FORCE at, not just the
    ones that may be recorded -- otherwise the pin's omissions look like
    capabilities otto holds no opinion about, when in fact each has a value
    driving real commands right now.
    """
    lines = await _report(
        _userland(raises=OSError("channel refused")), sudo_shell="the channel was refused"
    )
    rows = _rows(lines)

    assert sorted(rows) == _ROWS
    assert {source for _, source in rows.values()} == {"assumed"}


@pytest.mark.asyncio
async def test_a_declared_applet_reads_as_declared_and_a_measured_one_reaches_the_pin():
    """Applet capabilities travel on exactly the same terms as the fixed six.

    They are the new win -- pinning applet presence is what turns the batched
    round trip into no round trip at all. Nothing in the renderer treats them
    specially, and this is what says so: the declared one is reported
    ``declared`` and dropped from the batch otto sends, the measured one is
    reported ``probed``, and both are pinnable.
    """
    open_applets = [a for a in PROBED_APPLETS if a != "scp"]
    userland = _userland(
        UserlandOptions(applet_scp="absent"), applets=open_applets, present=("nc",)
    )
    lines = await _report(userland)
    rows = _rows(lines)

    assert rows[applet_capability("scp")] == ("absent", "declared")
    assert rows[applet_capability("nc")] == ("present", "probed")
    pinned = _pinned(lines)
    assert pinned[applet_capability("nc")] == "present"
    assert pinned[applet_capability("scp")] == "absent"


# ---------------------------------------------------------------------------
# The two cases that must not be papered over
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_host_with_no_userland_says_so_instead_of_printing_an_empty_pin(monkeypatch):
    """``_userland()`` answering ``None`` is a recorded hole, so the command names it.

    ``LocalHost``, ``DockerContainerHost`` and ``EmbeddedHost`` never build a
    resolver -- :func:`test_the_no_resolver_paragraph_is_about_the_host_being_probed`
    holds that list and checks the paragraph reads correctly for each. The
    silent renderings are both worse than saying so: a crash blames the user's
    command for a property of the host class, and an empty pin is
    indistinguishable from a device that refused every probe. The class name
    has to be in the output, because "nothing here" with no subject is the
    mysterious version of the same message.

    The survey seam is stubbed out because the section under test is the
    userland one: a real local survey opens a session and runs an inventory,
    which would make this a slow test of somebody else's subject.
    """
    from otto.host.survey.engine import Survey

    async def no_survey(host, *, user=None, scan_ports=None):
        return Survey()

    monkeypatch.setattr("otto.host.survey.engine.run_survey", no_survey)
    result = await LocalHost().probe()

    assert result.is_ok, "a recorded hole is an answer, not a failure of the command"
    text = "\n".join(str(line) for line in result.value if isinstance(line, (str, Text)))
    assert "LocalHost" in text, f"the host class is not named:\n{text}"
    assert '"userland_options"' not in text, f"an empty pin was offered anyway:\n{text}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        pytest.param(lambda: _report(_userland(applets=list(PROBED_APPLETS))), id="settled"),
        pytest.param(
            lambda: _report(
                _userland(raises=OSError("refused")), sudo_shell="the channel was refused"
            ),
            id="nothing-settled",
        ),
    ],
)
async def test_otto_s_own_prose_wraps_at_the_terminal_width(monkeypatch, report):
    """The CLI prints a ``str`` result line as written, never re-wrapped.

    That is right for device output and the pin, and wrong for otto's own
    sentences: printed as written, the legend is one 300-column line that the
    terminal breaks mid-word. Rendered here through the CLI's own printer at
    100 columns, the width the docs captures use.
    """
    import io

    import rich
    from rich.console import Console

    from otto.cli.invoke import _rprint_each

    out = io.StringIO()
    monkeypatch.setattr(rich, "_console", Console(file=out, width=100, color_system=None))
    _rprint_each(await report(), literal=True)

    long = [line for line in out.getvalue().splitlines() if len(line) > 100]
    assert not long, "a line of otto's prose was not wrapped:\n" + "\n".join(long)


@pytest.mark.asyncio
async def test_dry_run_reports_nothing_rather_than_a_fabricated_pin(monkeypatch):
    """Under ``--dry-run`` this verb says so, instead of rendering an all-guess table.

    THE PASTE-SAFETY PROPERTY IS NOT THIS BRANCH'S ANY MORE, and the split is
    worth knowing before touching either half. ``Userland._send`` declines to
    issue a probe under a dry run, so nothing settles and the pin is empty for
    EVERY command that resolves a userland, not just this verb --
    ``tests/unit/host/test_dry_run.py::TestDryRunMeasuresNothing`` is that
    guard. What ``_dry_run_report`` decides is only which true answer a user
    gets here, and this pins the choice: not thirteen ``assumed`` rows followed
    by an empty-pin paragraph inviting them to "run this again outside that
    window", when no window will ever make a dry run measure anything.

    The two assertions still hold on both sides of that split, which is the
    point of keeping them: whatever this branch renders, no pasteable payload
    and no device contact may come out of a dry run.
    """
    monkeypatch.setattr("otto.host.userland.is_dry_run", lambda: True)
    device = _Device(applets=list(PROBED_APPLETS))
    result = await _ScriptedHost(Userland(UserlandOptions(), device)).probe()

    assert result.status is Status.Skipped
    text = "\n".join(result.value)
    assert '"userland_options"' not in text, f"a dry run produced a pasteable pin:\n{text}"
    assert device.calls == [], f"a dry run reached the device: {device.calls}"


def test_the_no_resolver_paragraph_is_about_the_host_being_probed():
    """It is printed for three unrelated families now, so it may name only the rule.

    ``EmbeddedHost`` joined ``LocalHost`` and ``DockerContainerHost`` here when
    the verb reached every family, and the paragraph used to end by explaining
    that "neither LocalHost nor DockerContainerHost carries a
    ``userland_options`` field" -- a true sentence about two other machines,
    shown to somebody probing an RTOS. Every family that answers ``None`` from
    ``_userland()`` reads the same text, so the only class it may name as the
    SUBJECT is the one passed in.

    ``UnixHost`` is exempt from that rule and named on purpose: it is the one
    class that overrides the hook, which is what makes the rule legible rather
    than arbitrary. The guard checks that the list of families that answer
    ``None`` really is these three, so a fourth family added later fails here
    rather than quietly inheriting a paragraph nobody re-read.
    """
    from otto.host.docker_host import DockerContainerHost
    from otto.host.embedded_host import EmbeddedHost
    from otto.host.unix_host import UnixHost
    from otto.host.userland import _no_resolver_report

    families = [LocalHost, DockerContainerHost, EmbeddedHost]
    for cls in families:
        assert cls._userland is UserlandHost._userland, (
            f"{cls.__name__} overrides the hook now; re-read the no-resolver paragraph"
        )
    assert UnixHost._userland is not UserlandHost._userland, (
        "UnixHost stopped being the contrast the paragraph names"
    )

    names = [cls.__name__ for cls in families]
    for name in names:
        text = "\n".join(str(line) for line in _no_resolver_report(name))
        assert name in text, f"the host being probed is not named:\n{text}"
        for other in names:
            if other != name:
                assert other not in text, f"probing {name} explains a property of {other}:\n{text}"
        assert "survey" in text, (
            f"the paragraph does not say the protocol survey still runs:\n{text}"
        )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_verb_reaches_every_family():
    """Every family carries ``probe`` now: the survey has an answer for each.

    Scoped by where the ``_userland()`` hook lives, which used to be the
    honest boundary and no longer is. The userland section is one section of
    this verb's output; the protocol survey is the other, and it answers for
    the embedded family too (spec 2026-09-14 host-probe-protocol-survey §9).
    So :class:`~otto.host.embedded_host.EmbeddedHost` inherits the mixin now,
    and its userland section is the same no-resolver paragraph
    ``LocalHost`` and ``DockerContainerHost`` already print -- a case to
    state, not a reason to withhold the verb.

    ``output_dir=False`` because the verb writes nothing: it is a read-only
    reading like ``lsmod`` and ``exists``, and a per-invocation output
    directory for it would be an empty one per run.
    ``dry_run_preview=False`` because a dry run must stop at the CLI seam
    rather than run the body: the survey dials.
    """
    from otto.cli.expose import collect_exposed_methods
    from otto.host.docker_host import DockerContainerHost
    from otto.host.embedded_host import EmbeddedHost
    from otto.host.unix_host import UnixHost

    for cls in (UnixHost, LocalHost, DockerContainerHost, EmbeddedHost):
        assert collect_exposed_methods(cls).get("probe") == "probe", cls.__name__
    assert UserlandHost.probe.__cli_output_dir__ is False
    assert UserlandHost.probe.__cli_dry_run_preview__ is False


@pytest.mark.asyncio
async def test_the_survey_section_follows_the_userland_section(monkeypatch):
    """Mutation: drop the survey call and the report ends at the userland pin."""
    from rich.table import Table

    from otto.host.survey.engine import Survey

    seen = {}

    async def fake_run_survey(host, *, user=None, scan_ports=None):
        seen["args"] = (user, scan_ports)
        return Survey()

    monkeypatch.setattr("otto.host.survey.engine.run_survey", fake_run_survey)
    host = _ScriptedHost(_userland(retcode=0))
    result = await host.probe(user=None, scan_ports="2323")

    assert result.status is Status.Success
    assert seen["args"] == (None, "2323"), "the verb did not thread its own options through"
    tables = [item for item in result.value if isinstance(item, Table)]
    assert [t.title for t in tables] == ["userland", "protocols"]


@pytest.mark.asyncio
async def test_the_profile_hint_sits_between_the_pin_and_the_survey(monkeypatch):
    """The hint is prose after the pin block, and no pasteable line names the profile."""
    from rich.table import Table

    from otto.host.survey.engine import Survey

    async def fake_run_survey(host, *, user=None, scan_ports=None):
        return Survey()

    monkeypatch.setattr("otto.host.survey.engine.run_survey", fake_run_survey)
    host = _ScriptedHost(_userland(retcode=1, applets=list(PROBED_APPLETS)))
    host.os_type = "unix"
    result = await host.probe()

    value = result.value
    hint_at = next(
        i for i, item in enumerate(value) if isinstance(item, Text) and '"busybox"' in item.plain
    )
    last_pin = max(i for i, item in enumerate(value[:hint_at]) if isinstance(item, str) and item)
    survey_at = next(
        i for i, item in enumerate(value) if isinstance(item, Table) and item.title == "protocols"
    )
    assert last_pin < hint_at < survey_at
    assert not any(isinstance(item, str) and "busybox" in item for item in value)


@pytest.mark.asyncio
async def test_bad_scan_ports_is_an_error_before_any_contact():
    """A malformed sweep list is the user's typo, answered without spending a round trip."""
    device = _Device(retcode=0)
    host = _ScriptedHost(Userland(UserlandOptions(), device))
    result = await host.probe(scan_ports="30-20")

    assert result.status is Status.Error
    assert result.msg.startswith("--scan-ports: "), result.msg
    assert device.calls == [], f"a rejected option still reached the device: {device.calls}"


@pytest.mark.asyncio
async def test_user_on_a_family_without_a_session_identity_is_refused_by_the_standard_message():
    """Mutation: skip the refusal and an embedded probe tries to switch a console user.

    The message is ``BaseHost.switch_user``'s own text, not a second spelling:
    a family that cannot switch users refuses ``--user`` the same way
    wherever the option is offered.
    """
    from otto.host.command_frame import ZephyrFrame
    from otto.host.element import Element
    from otto.host.embedded_host import EmbeddedHost

    host = EmbeddedHost(
        ip="192.0.2.1",
        element=Element(name="zephyr37_fat"),
        log=LogMode.QUIET,
        command_frame=ZephyrFrame(),
    )
    result = await host.probe(user="root")

    assert result.status is Status.Error
    assert result.msg == "su/switch_user is not supported on 'EmbeddedHost'"


@pytest.mark.asyncio
async def test_a_bad_scan_ports_outranks_the_user_refusal(monkeypatch):
    """Mutation: swap the two checks and a user with BOTH options wrong is told the wrong one.

    Order is the whole content of this pin. ``EmbeddedHost`` refuses ``--user``
    (``SessionIdentity.none``) AND is handed a reversed range, so it is the one
    call that can tell the two checks apart -- every other ``probe`` call in
    this file trips at most one of them. The parse is first because it is the
    cheaper, more specific complaint: a reversed range is a typo the user can
    fix, while the ``--user`` refusal is a property of the family that will
    still be there afterwards.
    """
    from otto.host.command_frame import ZephyrFrame
    from otto.host.element import Element
    from otto.host.embedded_host import EmbeddedHost

    entered = []

    async def counting_run_survey(host, *, user=None, scan_ports=None):
        entered.append((user, scan_ports))
        raise AssertionError("the survey ran on a refused option pair")

    monkeypatch.setattr("otto.host.survey.engine.run_survey", counting_run_survey)
    host = EmbeddedHost(
        ip="192.0.2.1",
        element=Element(name="zephyr37_fat"),
        log=LogMode.QUIET,
        command_frame=ZephyrFrame(),
    )
    result = await host.probe(user="root", scan_ports="30-20")

    assert result.status is Status.Error
    assert result.msg.startswith("--scan-ports: "), (
        f"the family refusal outranked the option parse: {result.msg!r}"
    )
    assert entered == [], "a refused option pair still reached the survey"


@pytest.mark.asyncio
async def test_dry_run_still_contacts_nothing_and_skips(monkeypatch):
    """The dry-run branch is first, so neither option is parsed and nothing is dialed."""
    monkeypatch.setattr("otto.host.userland.is_dry_run", lambda: True)
    called = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("contacted")

    monkeypatch.setattr("otto.host.survey.engine.run_survey", boom)
    host = _ScriptedHost(_userland(retcode=0))
    result = await host.probe(user="root", scan_ports="garbage")

    assert result.status is Status.Skipped
    assert called == [], "a dry run ran the survey"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "row", "pinned"),
    [
        (True, ("sudo_form", "shell", "probed"), "shell"),
        (False, ("sudo_form", "validate", "probed"), "validate"),
        ("sudo did not answer in time", ("sudo_form", "unmeasured", "assumed"), None),
        (PasswordRefusedError("refused"), ("sudo_form", "unmeasured", "assumed"), None),
    ],
)
async def test_the_sudo_form_is_reported_and_pinned_only_when_measured(answer, row, pinned):
    """``sudo_form`` is a row like the others, and reaches the pin under its option's key."""
    from otto.host.userland import _capability_rows

    userland = _userland(applets=list(PROBED_APPLETS))
    lines = await _report(userland, answer)
    assert row in _capability_rows(userland, sudo_login=_PROBE_LOGIN)
    assert _pinned(lines).get("sudo_form") == pinned
    named = any(_PROBE_LOGIN in str(line) for line in lines)
    assert named is (row[2] == "probed"), "a measured form is reported with the login it is for"


@pytest.mark.asyncio
async def test_a_declared_sudo_form_is_reported_declared_and_never_asked():
    from otto.host.userland import _capability_rows

    userland = _userland(UserlandOptions(sudo_form="validate"), applets=list(PROBED_APPLETS))
    lines = await _report(userland, AssertionError("asked"))
    assert ("sudo_form", "validate", "declared") in _capability_rows(userland)
    assert _pinned(lines)["sudo_form"] == "validate"


class _H:  # the two attributes the hint reads
    def __init__(self, os_type):
        self.os_type = os_type


def _table(name: str) -> ProfileContext:
    return ProfileContext(
        profiles=FrozenMap(mapping={name: OsProfile(name, "unix")}),
        owners=FrozenMap(mapping={name: "proj-a"}),
    )


@pytest.mark.asyncio
async def test_a_unix_host_whose_measured_shell_is_not_bash_is_told_of_the_profile():
    userland = _userland(retcode=1, applets=list(PROBED_APPLETS))  # measures ash
    await userland.resolve()
    hint = _busybox_profile_hint(_H("unix"), userland, data=ProfileContext.empty())
    assert [type(i) for i in hint] == [Text, str]
    assert hint[1] == ""
    assert '"busybox"' in hint[0].plain
    assert "os-profiles.html" in hint[0].plain
    assert "when the entry writes no valid_transfers" in hint[0].plain
    assert "put shell first or remove the list" in hint[0].plain
    assert "is a BusyBox" not in hint[0].plain.replace("If this host is a BusyBox", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "bash",
        "declared",
        "busybox",
        "own-profile",
        "shadowed-busybox",
        "shadowed-unix",
        "unresolved",
        "no-userland",
    ],
)
async def test_no_hint(case):
    os_type = {"busybox": "busybox", "own-profile": "mine"}.get(case, "unix")
    data = {"shadowed-busybox": _table("busybox"), "shadowed-unix": _table("unix")}.get(
        case, ProfileContext.empty()
    )
    ash = _Device(retcode=1, applets=list(PROBED_APPLETS))
    userland: "Userland | None"
    if case == "no-userland":
        userland = None
    elif case == "bash":
        userland = _userland(retcode=0, applets=list(PROBED_APPLETS))
    elif case == "declared":
        userland = Userland(UserlandOptions(), ash, command_frame="ash")
    else:
        userland = Userland(UserlandOptions(), ash)
    if userland is not None and case != "unresolved":
        await userland.resolve()
    if case == "bash":
        assert userland is not None
        assert userland.command_frame == "bash"
    if case not in {"bash", "no-userland", "unresolved"}:
        assert userland is not None
        assert userland.command_frame != "bash"
    assert _busybox_profile_hint(_H(os_type), userland, data=data) == []


@pytest.mark.asyncio
async def test_a_published_project_table_named_busybox_withholds_the_verbs_hint(
    monkeypatch, tmp_path
):
    """The verb reads this run's published projects: a project's own ``busybox`` gets no hint."""
    from types import SimpleNamespace

    from otto.config.project import Project
    from otto.host.survey.engine import Survey
    from tests._fixtures.project_scaffold import make_project

    async def fake_run_survey(host, *, user=None, scan_ports=None):
        return Survey()

    monkeypatch.setattr("otto.host.survey.engine.run_survey", fake_run_survey)

    async def probe() -> list:
        host = _ScriptedHost(_userland(retcode=1, applets=list(PROBED_APPLETS)))
        host.os_type = "unix"
        return (await host.probe()).value

    def hinted(value: list) -> bool:
        return any(isinstance(item, Text) and '"busybox"' in item.plain for item in value)

    assert hinted(await probe()), "nothing published: the hint is given"
    root = make_project(
        tmp_path / "proj-a", name="proj-a", extra='[os_profiles.busybox]\nbase = "unix"\n'
    )
    published = SimpleNamespace(projects=[Project(root=root)])
    monkeypatch.setattr("otto.declared.published_result", lambda: published)
    assert not hinted(await probe())
