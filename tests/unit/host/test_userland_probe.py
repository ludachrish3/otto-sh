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

from otto.host.local_host import LocalHost
from otto.host.options import UserlandOptions
from otto.host.userland import (
    _APPLET_CONTROL,
    _RESOLVE_BUDGET_S,
    _RETRY_COOLDOWN_S,
    PROBED_APPLETS,
    Userland,
    UserlandHost,
    _applet_probe_command,
    applet_capability,
)
from otto.logger.mode import LogMode
from otto.models.host import UnixHostSpec
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
            return CommandResult(Status.Success, value=self._batch_out or "", retcode=0)
        status = Status.Success if self._retcode == 0 else Status.Error
        return CommandResult(status, value="", retcode=self._retcode)


class _ScriptedHost(UserlandHost):
    """The mixin under test, plus one :class:`Userland` and nothing else.

    Deliberately not a ``UnixHost``: what is exercised here is the verb's
    rendering over a resolver whose answers the test scripts, and a real host
    would add a connection stack no assertion below touches. That the verb
    reaches the concrete host classes at all is a separate question, answered
    by :func:`test_the_verb_reaches_every_posix_host_and_no_other`.
    """

    def __init__(self, userland: "Userland | None") -> None:
        self._u = userland

    def _userland(self) -> "Userland | None":
        return self._u


def _userland(options: "UserlandOptions | None" = None, **device_kw) -> Userland:
    return Userland(options or UserlandOptions(), _Device(**device_kw))


async def _report(userland: "Userland | None") -> list[str]:
    """Run the verb and hand back the lines it would print."""
    result = await _ScriptedHost(userland).probe()
    assert isinstance(result, Result)
    assert result.is_ok, f"the verb reported failure: {result.msg}"
    assert isinstance(result.value, list), (
        f"the CLI renders a list one item per line; got {type(result.value).__name__}"
    )
    return result.value


def _pin_text(lines: list) -> str:
    """The printed pin, from its key to the end -- what a user would select."""
    lines = [line for line in lines if isinstance(line, str)]
    start = next((i for i, line in enumerate(lines) if line.startswith('"userland_options"')), None)
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
    assert list(parsed) == ["userland_options"], f"unexpected top-level keys: {list(parsed)}"
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
    assert pinned == userland.as_lab_json(), (
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

    assert spec.userland_options.to_runtime() == UserlandOptions(**userland.as_lab_json()), (
        "the host entry built from the printed pin does not carry the answers otto measured"
    )


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
    lines = await _report(_userland(raises=OSError("channel refused")))
    text = "\n".join(line for line in lines if isinstance(line, str))

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
    lines = await _report(_userland(raises=OSError("channel refused")))
    rows = _rows(lines)

    assert sorted(rows) == _PINNABLE
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

    monkeypatch.setattr("otto.host.survey.run_survey", no_survey)
    result = await LocalHost().probe()

    assert result.is_ok, "a recorded hole is an answer, not a failure of the command"
    text = "\n".join(line for line in result.value if isinstance(line, str))
    assert "LocalHost" in text, f"the host class is not named:\n{text}"
    assert '"userland_options"' not in text, f"an empty pin was offered anyway:\n{text}"


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
        text = "\n".join(_no_resolver_report(name))
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

    monkeypatch.setattr("otto.host.survey.run_survey", fake_run_survey)
    host = _ScriptedHost(_userland(retcode=0))
    result = await host.probe(user=None, scan_ports="2323")

    assert result.status is Status.Success
    assert seen["args"] == (None, "2323"), "the verb did not thread its own options through"
    tables = [item for item in result.value if isinstance(item, Table)]
    assert [t.title for t in tables] == ["userland", "protocols"]


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
        element=Element("zephyr37_fat"),
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

    monkeypatch.setattr("otto.host.survey.run_survey", counting_run_survey)
    host = EmbeddedHost(
        ip="192.0.2.1",
        element=Element("zephyr37_fat"),
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

    monkeypatch.setattr("otto.host.survey.run_survey", boom)
    host = _ScriptedHost(_userland(retcode=0))
    result = await host.probe(user="root", scan_ports="garbage")

    assert result.status is Status.Skipped
    assert called == [], "a dry run ran the survey"
