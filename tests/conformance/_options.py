"""The arms of ``exec-options``: each option of ``run`` and ``exec``, as one call and its answer.

An arm (:class:`OptionArm`) is one call: the options it passes, the command it
issues, and what the call must give back. What it must give back is read off
the opened host's declaration
(:attr:`~otto.host.host.BaseHost.capabilities`): an option the family
honours owes an :class:`Answer` (the value ends with a word the option
decides), and one it declares refused owes
:exc:`~otto.host.errors.UnsupportedOptionError` with nothing typed.

The stimulus is the cell's vocabulary
(:attr:`~tests.conformance._vocabulary.Vocabulary.prompting_command`,
:attr:`~tests.conformance._vocabulary.Vocabulary.whoami_command`); the users
are the cell's facts (:attr:`~tests.conformance._resolved.ResolvedCell.switch_user`
and its neighbours) and the login the contract measured before the arms ran.
:func:`arms` builds the table, :func:`run_arm` issues one arm through the
cell's entry, and :func:`arm_mismatch` is the one comparison, shared by the
contract and its control.
"""

import dataclasses
import re
import secrets
from typing import Any

from otto.host.errors import UnsupportedOptionError
from otto.host.host import BaseHost, ShellCommand
from otto.host.identity import ROOT_LOGIN
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.conformance._entry import issue
from tests.conformance._log import logged_output, records_containing
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label

REFUSED_USER = "otto-cf-nobody"
"""The user a refusal arm names: a refused ``user=`` is refused whoever it names."""


def fresh_token() -> str:
    """A word no shell prints by itself, fresh per call."""
    return f"otto-cf-{secrets.token_hex(4)}"


@dataclasses.dataclass(frozen=True)
class Answer:
    """What an honoured arm owes: its ``value`` ends with :attr:`word`."""

    word: str
    """The last word of the value: the answer typed, or the user the command ran as.

    A word, not the whole value: a pooled shell's sudo prompt
    (``otto-sudo-<letters>:``) and a prompting command's own prompt come
    ahead of it on the line.
    """

    logged_nothing: bool = False
    """The host log must receive no output line of the call (a ``log=NEVER`` command's)."""


@dataclasses.dataclass(frozen=True)
class OptionArm:
    """One call of ``exec-options``, and what it must give back."""

    name: str
    """The arm's name in the observable note and in a failure."""

    kwargs: "dict[str, Any]"
    """The options the call passes."""

    command: "str | ShellCommand"
    """The command it issues."""

    expected: "Answer | type[UnsupportedOptionError]"
    """An :class:`Answer` where the host honours the option, else the refusal."""

    scope_user: "str | None" = None
    """Issued inside ``host.as_user(user=scope_user)``, or after it (:attr:`after_scope`)."""

    after_scope: bool = False
    """Issued after the :attr:`scope_user` scope is left, not inside it."""

    refused_options: "list[str]" = dataclasses.field(default_factory=list)
    """A refusal arm's options the host declares refused: the refusal must name one of them.

    For a two-option arm both may be refused, and the refusal names either.
    """

    token: "str | None" = None
    """A refusal arm's command prints this fresh word, so a record holding it
    means the command was typed (:attr:`Outcome.recorded`)."""


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What one arm's call gave back."""

    value: "str | None"
    """The result's value; ``None`` when the call raised."""

    raised: "BaseException | None"
    """What the call raised, or ``None``."""

    logged: "list[str]"
    """The output lines the host log received during the call."""

    typed_nothing: bool
    """A plain command after the call answered exactly what it answered before."""

    succeeded: bool = False
    """The call's result was ``Status.Success`` with ``retcode`` 0."""

    recorded: "list[str]" = dataclasses.field(default_factory=list)
    """Every record of otto's loggers, at any level, that held the arm's
    :attr:`OptionArm.token` during the call: a refusal that typed its command."""


def _expects(prompt: str, answer: str) -> "list[tuple[str, str]]":
    return [(re.escape(prompt), f"{answer}\n")]


def _refusal(
    name: str, kwargs: "dict[str, Any]", resolved: ResolvedCell, host: BaseHost
) -> OptionArm:
    """The arm a host declaring an option of *kwargs* refused owes a refusal by name."""
    token = fresh_token()
    return OptionArm(
        name,
        kwargs,
        resolved.vocabulary.distinct_answer_template.format(token=token),
        UnsupportedOptionError,
        refused_options=[
            o for o in kwargs if host.capabilities.option_limitation(option=o) is not None
        ],
        token=token,
    )


def arms(resolved: ResolvedCell, host: BaseHost, *, login: "str | None") -> "list[OptionArm]":
    """Every arm *resolved*'s opened *host* gives a stimulus for, in the order they run.

    *login* is the user a plain whoami answered on this host before any arm,
    or ``None`` where the vocabulary has no whoami. An honoured option whose
    stimulus the userland lacks has no arm (:func:`unrun_notes` says so); a
    refused option always has one, since its refusal needs no stimulus.
    """
    words, caps = resolved.vocabulary, host.capabilities
    prompting, prompt, whoami = words.prompting_command, words.prompt_text, words.whoami_command
    expects_ok, sudo_ok = caps.expects.limitation is None, caps.sudo.limitation is None
    user_ok = caps.user_refusal is None
    other = resolved.switch_user
    built: "list[OptionArm]" = []

    if not expects_ok:
        built.append(_refusal("expects", {"expects": _expects("x", "y")}, resolved, host))
    elif prompting is not None and prompt is not None:
        token = fresh_token()
        built.append(
            OptionArm("expects", {"expects": _expects(prompt, token)}, prompting, Answer(token))
        )
        own, call = fresh_token(), fresh_token()
        built.append(
            OptionArm(
                "ShellCommand",
                {"expects": _expects(prompt, call), "log": LogMode.NORMAL},
                ShellCommand(cmd=prompting, expects=_expects(prompt, own), log=LogMode.NEVER),
                Answer(own, logged_nothing=True),
            )
        )
    if not sudo_ok:
        built.append(_refusal("sudo", {"sudo": True}, resolved, host))
    elif whoami is not None:
        built.append(OptionArm("sudo", {"sudo": True}, whoami, Answer(ROOT_LOGIN)))
        prompting_whoami = words.prompting_whoami_command
        if expects_ok and prompting_whoami is not None and prompt is not None:
            # The effective user and the answer, so the arm cannot pass with
            # sudo ignored where the login is not root (unrun_notes says so).
            token = fresh_token()
            built.append(
                OptionArm(
                    "expects+sudo",
                    {"expects": _expects(prompt, token), "sudo": True},
                    prompting_whoami,
                    Answer(f"{ROOT_LOGIN} {token}"),
                )
            )
    if not user_ok:
        built.append(_refusal("user", {"user": REFUSED_USER}, resolved, host))
    elif whoami is not None and other is not None:
        built.append(OptionArm("user", {"user": other}, whoami, Answer(other)))
        if login is not None:
            built += [
                OptionArm("as_user", {}, whoami, Answer(other), scope_user=other),
                OptionArm(
                    "as_user+user",
                    {"user": login},
                    whoami,
                    Answer(login),
                    scope_user=other,
                ),
                OptionArm(
                    "after as_user", {}, whoami, Answer(login), scope_user=other, after_scope=True
                ),
            ]
    if not (sudo_ok and user_ok):
        built.append(_refusal("sudo+user", {"sudo": True, "user": REFUSED_USER}, resolved, host))
    elif whoami is not None and other is not None and resolved.switch_user_may_sudo:
        built.append(
            OptionArm("sudo+user", {"sudo": True, "user": other}, whoami, Answer(ROOT_LOGIN))
        )
    return built


def unrun_notes(resolved: ResolvedCell, host: BaseHost, *, login: "str | None") -> "list[str]":
    """Why this cell skips an arm, or runs one that cannot discriminate; in a reader's words."""
    words, caps = resolved.vocabulary, host.capabilities
    notes: "list[str]" = []
    if caps.expects.limitation is None and words.prompting_command is None:
        notes.append(
            "expects= is honoured but this userland has no command that prompts and reads a "
            "line, so it is asserted by the unit tests, not here"
        )
    honoured_users = caps.user_refusal is None and words.whoami_command is not None
    if honoured_users and resolved.switch_user is None:
        notes.append("the login has no second user, so user= and as_user() are not run")
    if (
        honoured_users
        and resolved.switch_user is not None
        and caps.sudo.limitation is None
        and not resolved.switch_user_may_sudo
    ):
        notes.append("the second user may not run sudo, so sudo+user is not run")
    if (
        honoured_users
        and resolved.switch_user is not None
        and caps.sudo.limitation is None
        and resolved.switch_user_may_sudo
        and resolved.switch_user_stand_in
    ):
        notes.append("sudo+user is proved by the stand-in sudo only; no bed login may do it")
    if honoured_users and resolved.switch_user_stand_in:
        notes.append(
            f"the second user {resolved.switch_user!r} is a stand-in: `su - USER` sets the "
            "identity `id` reports, with no login, rc or profile of theirs"
        )
    if login == ROOT_LOGIN and caps.sudo.limitation is None:
        notes.append(
            "the login is root, so the sudo and expects+sudo arms cannot tell an elevation "
            "from none"
        )
    return notes


async def _plain_answer(host: BaseHost, resolved: ResolvedCell, token: str) -> str:
    command = resolved.vocabulary.distinct_answer_template.format(token=token)
    return (await issue(host, resolved, command)).value


async def _issue_arm(host: BaseHost, resolved: ResolvedCell, arm: OptionArm) -> Any:
    if arm.scope_user is None:
        return await issue(host, resolved, arm.command, **arm.kwargs)
    async with host.as_user(user=arm.scope_user):
        if not arm.after_scope:
            return await issue(host, resolved, arm.command, **arm.kwargs)
    return await issue(host, resolved, arm.command, **arm.kwargs)


async def run_arm(host: BaseHost, resolved: ResolvedCell, arm: OptionArm) -> Outcome:
    """Issue *arm* through *resolved*'s entry, with a plain command before and after it.

    The plain command prints a fresh token
    (:attr:`~tests.conformance._vocabulary.Vocabulary.distinct_answer_template`);
    its answer after the arm must equal its answer before, so a refusal that
    typed something onto a shared line shows in the next command's value. A
    refusal arm's own command prints its :attr:`OptionArm.token`, and every
    record of otto's loggers that holds it during the call is kept: a refusal
    that typed its command shows there, on every venue. Only
    :exc:`~otto.host.errors.UnsupportedOptionError` is caught: any other
    failure is the cell's, raised as it came.
    """
    token = fresh_token()
    before = await _plain_answer(host, resolved, token)
    raised: "BaseException | None" = None
    value: "str | None" = None
    succeeded = False
    with logged_output(host) as logged, records_containing(arm.token or token) as recorded:
        try:
            result = await _issue_arm(host, resolved, arm)
        except UnsupportedOptionError as refused:
            raised = refused
        else:
            value = result.value
            succeeded = (result.status, result.retcode) == (Status.Success, 0)
    after = await _plain_answer(host, resolved, token)
    return Outcome(
        value=value,
        raised=raised,
        logged=[found.text for found in logged],
        typed_nothing=before == after and token in before,
        succeeded=succeeded,
        recorded=list(recorded) if arm.token is not None else [],
    )


def _ends_with_word(value: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}$", value) is not None


def arm_mismatch(arm: OptionArm, outcome: Outcome) -> "str | None":
    """Why *outcome* is not what *arm* owes; ``None`` if it is."""
    if not isinstance(arm.expected, Answer):
        wrong = []
        if not isinstance(outcome.raised, UnsupportedOptionError):
            wrong.append(f"returned {outcome.value!r} instead of raising UnsupportedOptionError")
        elif outcome.raised.option not in arm.refused_options:
            wrong.append(
                f"refused {outcome.raised.option}=, not one of {arm.refused_options}, "
                "the options this host declares refused"
            )
        if outcome.recorded:
            wrong.append(f"the command was typed: {outcome.recorded!r}")
        if outcome.logged:
            wrong.append(f"logged {outcome.logged!r}")
        if not outcome.typed_nothing:
            wrong.append("the next plain command answered differently, so something was typed")
        return f"{arm.name}: {'; '.join(wrong)}" if wrong else None
    if outcome.raised is not None:
        return (
            f"{arm.name}: raised {outcome.raised!r}, expected a value ending {arm.expected.word!r}"
        )
    assert outcome.value is not None
    if not outcome.succeeded:
        return f"{arm.name}: the call did not succeed (value {outcome.value!r})"
    if not _ends_with_word(outcome.value, arm.expected.word):
        return f"{arm.name}: value {outcome.value!r} does not end with {arm.expected.word!r}"
    if arm.expected.logged_nothing and outcome.logged:
        return f"{arm.name}: a log=NEVER command logged {outcome.logged!r}"
    return None


async def measured_login(host: BaseHost, resolved: ResolvedCell) -> "str | None":
    """The user a plain whoami answers on *host*; ``None`` where the vocabulary has none."""
    whoami = resolved.vocabulary.whoami_command
    if whoami is None:
        return None
    result = await issue(host, resolved, whoami)
    why = f"{cell_label(resolved)}: `{whoami}` answered {result.value!r} (retcode {result.retcode})"
    assert result.retcode == 0, why
    assert result.value, why
    return result.value.splitlines()[-1]
