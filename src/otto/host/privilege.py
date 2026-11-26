"""
Posix privilege-elevation mixin.

Shared by the posix-shell hosts (:class:`~otto.host.unix_host.UnixHost`,
:class:`~otto.host.local_host.LocalHost`,
:class:`~otto.host.docker_host.DockerContainerHost`). Implements the
``_elevate`` hook (``run(sudo=True)``) plus ``switch_user`` / ``as_user``.

Which mechanism ``_elevate`` reaches for is RESOLVED, not assumed:
:attr:`~otto.host.userland.Userland.elevation` answers ``sudo`` / ``su`` /
``none``, because a BusyBox userland frequently has ``su`` and no ``sudo`` at
all. A host with no resolver wired up keeps today's ``sudo`` construction
exactly — see ``PosixPrivilege._elevate`` below for why that default, and for
where resolution has to happen given that ``_elevate`` is synchronous.

**sudo** auto-answers its password prompt through a
:class:`~otto.host.host.CredentialExpect`: the session answers it at most
once, only after the command has begun, and never logs it. The prompt is a
fresh random string per command, set through ``SUDO_PROMPT`` so it is in no
process's argv, which is what makes output that merely looks like a prompt
unanswerable. The one-shot ``su -c`` form answers ``su``'s own prompt the same
way, using the TARGET account's password rather than the current user's.

**su** (and any other registered login proxy) sends its password via
``send(..., log=LogMode.NEVER)`` so it is delivered to stdin without being
emitted to any sink (console, ``console.log``, or ``verbose.log``).
``switch_user``/``as_user`` route through
:func:`~otto.host.login_proxy.perform_switch`, which recursively resolves
``via``-chains and drives whichever proxy the target cred names (defaulting
to the built-in ``"su"``).

The mixin carries no fields and declares ``__slots__ = ()`` so it composes with
the ``@dataclass(slots=True)`` hosts. Password sourcing is host-specific:
``_switch_creds`` is ``self.creds`` (or ``[]`` when the host has no
``creds`` field), and ``_sudo_password`` looks the acting user's password up
in that same list, so ``switch_user``/``as_user`` targets and sudo's answer
resolve against one cred list on every posix host.
"""

import logging
import re
import secrets
import shlex
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pydantic import SecretStr

from ..logger.mode import LogMode
from ..models.base import Cred
from .command_frame import (
    EXEC_CREDENTIAL_FD,
    SESSION_SHELL,
    frame_or_default,
    shell_prefix,
)
from .errors import PasswordRefusedError, UnsupportedOnUserlandError
from .host import (
    DEFAULT_COMMAND_TIMEOUT,
    CredentialExpect,
    ElevatedCommand,
    is_dry_run,
    refuse_declined_elevation,
)
from .identity import ROOT_LOGIN, is_root
from .login_proxy import (
    _SU_PROMPT,
    _may_challenge,
    cred_for,
    run_undo,
    switch_and_stamp,
)
from .userland import SUDO_FORM_SHELL, UserlandHost

if TYPE_CHECKING:
    from typing_extensions import Never

    from ..result import Results
    from .command_frame import ExecInput
    from .host import Expect, Host
    from .session import ShellSession


async def run_sh_elevated(host: "Host", script: str, *, timeout: float | None = None) -> "Results":
    """Run *script* elevated on *host*, wrapped whole in ``sh -c``.

    The one place an elevated coverage script is wrapped. sudo takes an argv
    tail and ``_elevate`` appends the command to it raw, so a script with a
    redirection, a pipe or a ``||`` would otherwise run only its first word
    elevated (``echo 1 > /sys/x`` would open the file as the session user).
    The script is quoted once, with :func:`shlex.quote`, and run through
    ``host.run(..., sudo=True)``, whose ``_elevate`` decides the mechanism.

    Returns ``host.run``'s :class:`~otto.result.Results` unchanged: a
    ``--dry-run`` decline (:attr:`~otto.utils.Status.NotRun`) is passed
    through as-is, and each caller keeps its own message for a failure.
    """
    command = f"sh -c {shlex.quote(script)}"
    if timeout is None:
        return await host.run(command, sudo=True)
    return await host.run(command, sudo=True, timeout=timeout)


_logger = logging.getLogger(__name__)

_ELEVATING_MECHANISMS = frozenset({"sudo", "su"})
"""The resolved ``Userland.elevation`` values that elevate; any other (``"none"``) cannot."""

_SUDO_FORM_PROBE_TIMEOUT_S = 10.0
"""Seconds the ``sudo -l <shell>`` question may take, its one password answer included."""

_SUDO_SHELL_PROBE = f"-l {SESSION_SHELL}"
"""sudo's argv for "may this user run the session's shell": status 0 is a yes."""

_SUDO_PROBE_REFUSED_HELD = 100
"""The probe's status when ``sudo -l <shell>`` was refused and ``sudo -n -v`` then
succeeded on the same terminal: authenticated, the shell not allowed (``validate``)."""

_SUDO_PROBE_REFUSED_UNHELD = 101
"""The probe's status when ``sudo -l <shell>`` was refused and sudo holds no
timestamp: unread, a refused password as far as otto can tell."""

_SUDO_NONCE_LEN = 16
"""Random characters in each sudo prompt: unguessable, and nothing sudo expands."""

_SUDO_PROMPT_PREFIX = "otto-sudo-"

SUDO_PROMPT_SHAPE = re.compile(
    re.escape(_SUDO_PROMPT_PREFIX) + rf"[A-Za-z0-9]{{{_SUDO_NONCE_LEN}}}:"
)
"""Every sudo prompt ``_elevate`` can build, for text that must not vary by run.

A captured log normalises the nonce with it, since each command's prompt differs.
"""


def _sudo_prompt() -> str:
    """Return a fresh sudo prompt, ``otto-sudo-<16 random letters and digits>:``.

    Letters and digits only: sudo expands ``%`` escapes (``%u``, ``%h``, ``%p``)
    in a prompt, and the prompt is typed inside single quotes.
    """
    token = ""
    while len(token) < _SUDO_NONCE_LEN:
        token += "".join(c for c in secrets.token_urlsafe(_SUDO_NONCE_LEN) if c.isalnum())
    return f"{_SUDO_PROMPT_PREFIX}{token[:_SUDO_NONCE_LEN]}:"


class _HostProxyIO:
    """Adapts a host's ``send``/``expect`` to :class:`~otto.host.login_proxy.ProxyIO`.

    ``PosixPrivilege`` is a mixin — it has no ``send``/``expect`` of its own,
    only what the concrete host it's composed into supplies at runtime.
    Wrapping that access here (rather than in ``switch_user``/``as_user``
    directly) keeps the unavoidable ``ty: ignore`` confined to one small
    adapter instead of scattered through the elevation flow.
    """

    __slots__ = ("_host",)

    def __init__(self, host: "PosixPrivilege") -> None:
        self._host = host

    async def send(self, text: str | SecretStr, *, log: LogMode = LogMode.NORMAL) -> None:
        await self._host.send(text, log=log)  # ty: ignore[missing-attribute]

    async def expect(
        self, pattern: str | re.Pattern[str], *, timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> str:
        return await self._host.expect(pattern, timeout=timeout)  # ty: ignore[missing-attribute]


def _on_credential_fd(fd: int, call: str) -> str:
    """Return *call* reading stdin from credential descriptor *fd*, closed for what it runs."""
    return f"<&{fd} {fd}<&- {call}"


def _exec_inner(cmd: str, exec_input: "ExecInput") -> str:
    """Return the script an exec's ``su -c`` or sudo shell form runs: *cmd*, at end of file if due.

    The elevation reads its password on the caller's terminal, which is
    therefore the stdin it hands what it runs; ``exec </dev/null`` puts the
    command back at end of file when the exec's one stdin decision says so,
    and leaves the terminal to a caller that passed its own ``expects=``.
    """
    return ("exec </dev/null; " if exec_input.stdin_eof else "") + cmd


class PosixPrivilege(UserlandHost):
    """Mixin: ``sudo``/``su`` elevation for posix-shell hosts.

    Inherits the ``_userland()`` hook rather than declaring it, because a
    second mixin now reads the same hook —
    :class:`~otto.host.file_ops.PosixFileOps` asks whether the device has
    ``base64``. It is declared once, on
    :class:`~otto.host.userland.UserlandHost`; see there for why that is not
    this class's field to own, and ``_elevate`` below for what ``None`` means on
    this path.
    """

    __slots__ = ()

    def _sudo_password(self, user: str | None = None) -> SecretStr | None:
        """Return the password ``sudo -S`` must be answered with, or None when none is known.

        ``sudo`` authenticates the user it is typed as, so the answer is
        :meth:`_acting_user`'s credential in :meth:`_switch_creds`, scoped by
        the host's term where it has one. *user* is the identity the elevated
        command runs as -- an ``exec(user=X)`` switches a pooled session to X
        before the command, so it is X's password sudo prompts for; ``None``
        (every ``run``, and an ``exec`` with no user) is the default session's
        current user. A host with no creds knows no password and asks nothing
        of its session.
        """
        creds = self._switch_creds()
        if not creds:
            return None
        # getattr, not self.term: LocalHost has no term concept at all.
        c = cred_for(creds, self._acting_user(user), getattr(self, "term", None))
        return c.password if c else None

    def _switch_creds(self) -> list[Cred]:
        """Creds used to resolve ``switch_user``/``as_user`` targets.

        Default ``getattr(self, "creds", [])`` — hosts with a ``creds`` field
        (:class:`~otto.host.unix_host.UnixHost`) get real cred-chain
        resolution for free; a host without one
        (:class:`~otto.host.docker_host.DockerContainerHost`), or whose
        ``creds`` is ``None``, falls back to an empty list (ad-hoc,
        passwordless ``su`` targets).
        """
        return getattr(self, "creds", None) or []

    def _ambient_user(self) -> str | None:
        """Report the user an enclosing ``as_user()`` switched the default session to, or ``None``.

        The one reading for every posix host: ``as_user`` here is real (a
        ``su`` on the persistent session, recorded on its session manager),
        so an ``exec`` inside one -- a pooled shell, a subprocess, a fresh
        ``docker exec`` -- runs as that user, never as the default ``run``
        left. ``None`` (no default session, or one still running as the user
        it opened as) leaves the call's own default in force: a container's
        declared ``user``, a unix host's login, the local host's invoking user.
        """
        return self._live_session_mgr().ambient_user  # ty: ignore[missing-attribute]

    def _acting_user(self, user: str | None) -> str:
        """Who an elevated command runs as before it elevates: *user*, else the session's user.

        *user* is an ``exec(user=X)``'s switch, made before the command runs;
        ``None`` (every ``run``) means the default session's current user.
        Whose password sudo asks for, and whether ``su`` challenges at all,
        both follow from this one answer.
        """
        return user or self._live_session_mgr().current_user  # ty: ignore[missing-attribute]

    def _acts_as_root(self, user: str | None = None) -> bool:
        """Whether a command run as :meth:`_acting_user` already runs as root.

        The same identity rule ``su``'s challenge follows
        (:func:`~otto.host.identity.is_root`): a root session needs no
        elevation, and an unknown identity is not taken for root.
        """
        return is_root(self._acting_user(user))

    def _su_cred(self) -> Cred | None:
        """Return the cred whose password answers ``su``, or None when none is declared.

        NOT :meth:`_sudo_password`, and the difference is the point: ``sudo``
        authenticates as the user already logged in, while ``su`` authenticates
        as the account being ENTERED.

        A direct ``root`` lookup, which is narrower than
        ``switch_user``/``as_user`` and deliberately so. Those resolve a
        ``via`` chain and RUN each hop; this wraps one command in one ``su -c``
        and cannot replay hops at all. Where the two do agree is whose password
        answers the final prompt — :func:`~otto.host.login_proxy.perform_switch`
        looks the TARGET's cred up the same way and
        :func:`~otto.host.login_proxy._su_proxy` sends
        ``ctx.target.password`` — so a ``via`` on the root cred changes which
        hops run, never which password is typed, and this stays correct for the
        one hop it performs.

        The limitation that leaves is NARROWER than "any chain". A one-hop
        ``proxy="su"`` cred is precisely what ``su -c`` already does, so the
        common proxied shape simply works. What is unserved is a chain of more
        than one hop from the current user, or a hop driven by a CUSTOM
        registered proxy: there the one-shot form types root's password at a
        prompt the chain was supposed to reach first.

        Be exact about how that fails, because "loudly" would be too strong.
        ``su`` rejects the password and the wrapped command does not run, but
        it reports that as a non-zero (or timed-out) ``CommandResult``, not as
        a raise — so it is only as visible as the caller's own result check.
        :meth:`~otto.host.unix_host.UnixHost._soft_reboot` deliberately does
        not check (the issue-race disconnect makes its result untrustworthy),
        so on that path specifically it is NOT visible. It is still a wrong
        password rather than a wrong mechanism, which is why this is a
        documented edge and not an
        :exc:`~otto.host.errors.UnsupportedOnUserlandError`. Use ``as_user``
        for anything needing a real chain.
        """
        # getattr, not self.term: this mixin also backs LocalHost and
        # DockerContainerHost, neither of which has a term concept at all —
        # None there reproduces the untargeted lookup exactly as before.
        return cred_for(self._switch_creds(), ROOT_LOGIN, getattr(self, "term", None))

    def _elevation_password(self, mechanism: str, user: str | None = None) -> SecretStr | None:
        """Return the password otto will answer *mechanism*'s prompt with, or None for no answer.

        The one decision behind :meth:`_elevate`'s answer, readable without
        building a command: sudo answers with the acting user's password; su
        with root's, and not at all when the acting user is root, whom su
        never asks (:func:`~otto.host.login_proxy._may_challenge`, the rule
        ``switch_user`` applies). *mechanism* is ``"sudo"`` or ``"su"``.

        Raises:
            ValueError: *mechanism* is neither: there is no prompt to answer,
                and a guess (su's root password) would answer the wrong one.
        """
        if mechanism not in _ELEVATING_MECHANISMS:
            raise ValueError(f"no elevation prompt for mechanism {mechanism!r}")
        if mechanism == "sudo":
            return self._sudo_password(user)
        cred = self._su_cred()
        if cred is None or not _may_challenge(cred, Cred(login=self._acting_user(user))):
            return None
        return cred.password

    def _can_elevate(self, mechanism: str, user: str | None = None) -> bool:
        """Return whether a command run with *mechanism* would get root without a stuck prompt.

        The one answer to "can this session elevate": true when the acting
        user already is root (no prompt: ``sudo`` and ``su`` both pass root
        through, and :meth:`_elevation_password` rightly has no answer for su
        then), or when otto has the password *mechanism*'s prompt asks for.
        :meth:`_elevation_password` stays the answer to "which password to
        type"; asking it "can I elevate" reads a root session as unable.
        *mechanism* is a resolved ``Userland.elevation``; anything but
        ``"sudo"`` or ``"su"`` (``"none"``) cannot elevate, root or not.
        """
        if mechanism not in _ELEVATING_MECHANISMS:
            return False
        return self._acts_as_root(user) or self._elevation_password(mechanism, user) is not None

    async def _prepare_elevation(self) -> None:
        """Resolve everything :meth:`_elevate` will read. Awaited by ``run(sudo=True)``.

        :meth:`_elevate` is synchronous and every
        :class:`~otto.host.userland.Userland` capability raises when read
        before ``resolve()`` has been awaited, so ``_elevate`` cannot resolve
        on demand and must not guess. This is the async boundary above it:
        :meth:`~otto.host.host.BaseHost.run` awaits this whenever ``sudo=True``,
        ahead of BOTH of its ``_apply_sudo`` call sites, which is what makes
        the read safe rather than lucky. ``resolve()`` is idempotent,
        concurrency-safe and never raises for a failed probe.

        **What it costs, stated rather than implied.** ``resolve()`` has no
        scoped form: it settles all six capabilities, so the first elevated
        command on a host issues up to eleven probes to read the one — settled by
        probes 1-2 — that :meth:`_elevate` looks at. Free thereafter only once
        every capability is SETTLED; a round that could not ask leaves nothing
        settled, and the next elevated call outside ``_RETRY_COOLDOWN_S`` (60s)
        pays again. Worst case is ``_RESOLVE_BUDGET_S`` (30s), it is not
        charged to the caller's ``timeout=``, and callers with a short timeout
        of their own are the ones that notice —
        ``UnixHost._soft_reboot``'s ``timeout=10.0`` becomes up to 40s of wall
        clock on a host that answers nothing. See
        :meth:`otto.host.userland.Userland.resolve` for why the round is whole.
        """
        userland = self._userland()
        if userland is not None:
            await userland.resolve()

    async def _prepare_exec_elevation(self, user: str | None = None) -> None:
        """Resolve what an ``exec``'s :meth:`_elevate` reads: the userland, then the sudo form.

        The sudo form (:meth:`~otto.host.userland.Userland.sudo_form`) is asked
        only here, on the first ``exec`` under sudo by each login (the
        :meth:`_acting_user`, so after ``as_user`` the switched user), and only
        when nothing declares it: one ``sudo -l "$SHELL"``
        (:meth:`_probe_sudo_shell`), as that login. A session that already acts
        as root asks nothing; its exec types the shell form unless one is
        declared (root may run any shell). ``run`` never reads the form and
        never calls this.
        """
        await self._prepare_elevation()
        userland = self._userland()
        if userland is None or userland.elevation != "sudo" or self._acts_as_root(user):
            return
        await userland.resolve_sudo_form(
            login=self._acting_user(user), probe=lambda: self._probe_sudo_shell(user)
        )

    async def _probe_sudo_shell(self, user: str | None) -> "bool | str":
        """Ask the host's sudo whether the acting user may run the session's shell.

        Answers ``True`` (allowed), ``False`` (authenticated, and sudo said
        not allowed), or a string saying why there is no answer. Typed
        through :meth:`_sudo_command`, the same line builder and the same
        password answer as the exec it decides: on the credential descriptor
        with ``-S`` when otto holds a password, at end of file on stdin
        otherwise. At most one password answer, never a pending prompt: the
        exec's own recovery applies.

        Only an authenticated answer is a measurement. With a password, a
        refused one is the session's second-prompt detection and raises
        :exc:`~otto.host.errors.PasswordRefusedError` (so the exec is not
        tried again with it). Any other refusal after otto answered is
        ambiguous (a sudo that tries a password once exits 1 on a wrong one,
        as it does on "not allowed"), so it is settled without reading sudo's
        messages and without a second password: in the SAME typed command,
        so on the same terminal (sudo keeps its timestamp per terminal by
        default), a refused ``-l`` is followed by ``sudo -n -v``, which reads
        no password and succeeds only while sudo holds a valid timestamp.
        The command's status alone answers (:data:`_SUDO_PROBE_REFUSED_HELD`,
        :data:`_SUDO_PROBE_REFUSED_UNHELD`): a timestamp held means the
        authentication succeeded and the shell was refused, the validate form
        (which needs exactly that timestamp). Anything else raises
        :exc:`~otto.host.errors.PasswordRefusedError` naming
        ``userland_options.sudo_form``, so the exec sends the password no
        second time. Without a password a non-zero status is unread: sudo may
        have needed the password otto does not hold. A dry run, a transport
        failure or a timeout is unread too.
        """
        if is_dry_run():
            return "a dry run reaches no device"
        from .command_frame import ExecInput

        x = ExecInput(stdin_eof=True)
        e = self._sudo_command(_SUDO_SHELL_PROBE, user, exec_input=x)
        question = (
            f"{e.command} || if sudo -n -v </dev/null 2>/dev/null; "
            f"then (exit {_SUDO_PROBE_REFUSED_HELD}); else (exit {_SUDO_PROBE_REFUSED_UNHELD}); fi"
        )
        try:
            r = await self._exec_one(  # ty: ignore[missing-attribute]
                question,
                timeout=_SUDO_FORM_PROBE_TIMEOUT_S,
                log=LogMode.NEVER,
                user=user,
                expects=e.expects,
                needs_shell=True,
                exec_input=ExecInput(stdin_eof=True, credential=e.reads_credential_fd),
            )
        except PasswordRefusedError:
            raise
        except Exception as exc:  # noqa: BLE001 — a question that cannot be asked is no answer, not an error
            _logger.debug("sudo -l: could not be asked (%s: %s)", type(exc).__name__, exc)
            return f"the question could not be asked ({type(exc).__name__})"
        if r.timed_out:
            return "sudo did not answer in time"
        if r.retcode == 0:
            return True
        if not e.expects:
            return f"sudo answered {r.retcode} and otto holds no password to authenticate with"
        if r.retcode == _SUDO_PROBE_REFUSED_HELD:
            return False
        raise PasswordRefusedError(
            f"{getattr(self, 'name', '')}: sudo refused {self._acting_user(user)!r}. otto gave "
            "the password once and sudo turned the request down. The cause is one of: "
            "(1) this login may not use sudo on this host; "
            "(2) the configured password is wrong; "
            "(3) sudo refuses the login's shell and keeps no timestamp "
            "(timestamp_timeout=0), where neither sudo form can run. "
            "sudo answers all three the same way, and otto does not send the password a "
            "second time to tell them apart. If the login may use sudo, set "
            "userland_options.sudo_form to 'shell' or 'validate' to skip this check."
        )

    def _sudo_form(self, user: str | None) -> str:
        """Return the sudo form an ``exec`` types: declared, probed, else the shell form.

        A host with no resolver (``LocalHost``, ``DockerContainerHost``) and a
        root session that asked nothing type :data:`~otto.host.userland.SUDO_FORM_SHELL`.
        """
        userland = self._userland()
        if userland is None:
            return SUDO_FORM_SHELL
        login = self._acting_user(user)
        if self._acts_as_root(user) and not userland.sudo_form_known(login=login):
            return SUDO_FORM_SHELL
        return userland.sudo_form(login=login)

    def _sudo_command(
        self, tail: str, user: str | None, *, exec_input: "ExecInput | None" = None
    ) -> ElevatedCommand:
        """Return ONE sudo call on argv *tail*, with its password answer.

        ``SUDO_PROMPT='<prompt>' sudo -S <tail>``; with *exec_input* and a
        password armed, stdin is the caller's terminal on
        ``EXEC_CREDENTIAL_FD`` (``<&3 3<&- ...``), so sudo reads the password
        there and the descriptor is closed for what it runs. The line every
        ``run``, the shell form and the ``sudo -l "$SHELL"`` question share.
        """
        prompt = _sudo_prompt()
        pw = self._elevation_password("sudo", user)
        sudo = f"SUDO_PROMPT='{prompt}' sudo -S {tail}"
        if pw is None:
            return ElevatedCommand(sudo, [])
        answer = CredentialExpect(
            re.escape(prompt),
            pw,
            user=self._acting_user(user),
            host=getattr(self, "name", ""),
            refused_on_repeat=True,
        )
        if exec_input is None:
            return ElevatedCommand(sudo, [answer])
        fd = self._exec_credential_fd()
        return ElevatedCommand(_on_credential_fd(fd, sudo), [answer], reads_credential_fd=True)

    # Spec: docs/superpowers/specs/2026-08-11-busybox-host-support-design.md
    def _elevate(
        self, cmd: str, user: str | None = None, *, exec_input: "ExecInput | None" = None
    ) -> ElevatedCommand:
        """Wrap *cmd* in this host's elevation mechanism, with its password answer.

        **Whose password.** ``sudo`` authenticates as the user running the
        command, so *user* — the effective identity of an ``exec(user=...)``,
        switched to before the command runs — decides which cred answers the
        prompt. ``None`` (every ``run``) keeps the session's current user. The
        ``su`` arm answers with root's password, which no caller-side switch
        changes; *user* decides only whether ``su`` will ask at all.

        **The answer is a** :class:`~otto.host.host.CredentialExpect`: sent at
        most once, only after the command's BEGIN marker, and a second prompt
        raises :exc:`~otto.host.errors.PasswordRefusedError`. Output that
        merely looks like the prompt gets no password, because the sudo
        prompt is a fresh random string per command
        (``SUDO_PROMPT='otto-sudo-<random>:' sudo -S <cmd>``). The variable
        is set on the sudo invocation rather than passed as ``-p``, so the
        prompt is in no process's argv, and sudo's ``env_reset`` keeps it out
        of the command's environment.

        The mechanism is a MEASUREMENT, not an assumption:
        :attr:`~otto.host.userland.Userland.elevation` decides it. BusyBox
        systems frequently ship ``su`` and no ``sudo`` at all, where the old
        hard-coded wrapper produced ``sudo: not found`` — a message about
        otto's own guess, attributed to whatever the caller was doing. See the
        BusyBox host support spec.

        **No resolver means sudo, deliberately.**
        :class:`~otto.host.unix_host.UnixHost` now builds one, so the lab's
        remote hosts answer from measurement; ``None`` remains the state of the
        hosts that reach otto's own machine —
        :class:`~otto.host.local_host.LocalHost` and
        :class:`~otto.host.docker_host.DockerContainerHost` — and there it must
        stay the sudo form. The asymmetry is what
        settles it: a host that has told otto nothing has told it nothing that
        contradicts sudo, and refusing instead would break privileged
        operations everywhere at once in exchange for no information. The
        command lines are pinned exactly by
        ``tests/unit/host/test_privilege.py``, because this one function is how
        the entire lab elevates.

        **``su`` quotes; ``sudo`` does not.** ``sudo`` takes an argv tail, so
        *cmd* is appended raw — a shape ``otto.host.daemon`` depends on and
        documents. ``su -c`` takes a single argument, so *cmd* is quoted into
        one word; unquoted, the calling shell would keep everything after the
        first word and a redirect or ``;`` would run unelevated instead of
        failing.

        The su prompt is matched, not set. ``SUDO_PROMPT`` lets otto choose a
        locale-independent prompt; ``su`` has no counterpart, so the pattern is
        ``login_proxy``'s (shared, not copied) and a non-English locale will
        not match it — the same limitation ``switch_user`` already has there.
        ``su`` asks the caller for a password only when the caller is not
        root, so a session that is already root arms no answer at all, by the
        same identity rule ``switch_user`` applies
        (:func:`~otto.host.login_proxy._may_challenge`): a ``Password:`` in
        such a command's output is output.

        **An ``exec``'s password is read off its own descriptor.** With
        *exec_input* (an ``exec``'s, whose frame may give the command a stdin
        at end of file) and a password armed, the result says so
        (:attr:`~otto.host.host.ElevatedCommand.reads_credential_fd`), the
        exec frame then keeps the caller's terminal on ``EXEC_CREDENTIAL_FD``,
        the password is read from that descriptor and the command reads the
        frame's stdin:
        sudo validates first, ``<&3 SUDO_PROMPT='<prompt>' sudo -S -v || exit;
        exec 3<&-; sudo -n <cmd>``, so a failed authentication runs nothing and
        the command never reads the terminal through sudo; su reads the
        password on ``<&3 3<&- su -c '<cmd>'``, with ``exec </dev/null; ``
        ahead of *cmd* when the frame gives end of file. With no password
        armed (passwordless, or already root) the form is ``run``'s, and the
        elevated command reads whatever stdin the frame gave it, so a sudo that
        asks unexpectedly reads end of file and fails at once.

        Raises:
            RuntimeError: *exec_input* is given with a password armed, on a
                host whose command frame does not isolate ``exec``: there is no
                credential descriptor to read the password from.
            UnsupportedOnUserlandError: the host resolved an elevation of
                ``none`` (neither sudo nor su present), or a spelling this
                build has never been taught. Loudly, at the call site: a
                fallback to sudo here would look like it worked.
            RuntimeError: a userland is wired up but was never resolved. Also
                deliberate — see :meth:`_prepare_elevation`. Catching it and
                answering "sudo" would reinstate exactly the guess this method
                exists to remove, on the path where guessing is most expensive.
        """
        userland = self._userland()
        elevation = "sudo" if userland is None else userland.elevation
        host = getattr(self, "name", "")
        if elevation not in _ELEVATING_MECHANISMS:
            raise UnsupportedOnUserlandError(
                f"{getattr(self, 'name', '')}: this host's userland provides no elevation "
                f"mechanism (resolved {elevation!r}: no sudo, no su), so {cmd!r} cannot be "
                "run with sudo=True. Declare one in the host's userland_options if the "
                "probe is wrong, or run the command unelevated."
            )
        # The response is the cred's own SecretStr, never a formatted
        # `SecretStr(f"{pw}\n")` (that would wrap the mask): the session
        # writes it as its value and a newline, unwrapped inside the
        # transport write (`otto.host.secret_wire.reveal`).
        if elevation == "sudo":
            if exec_input is None:
                return self._sudo_command(cmd, user)
            if self._sudo_form(user) == SUDO_FORM_SHELL:
                shell = f"{SESSION_SHELL} -c {shlex.quote(_exec_inner(cmd, exec_input))}"
                return self._sudo_command(shell, user, exec_input=exec_input)
            if self._elevation_password("sudo", user) is None:
                return self._sudo_command(cmd, user)
            validated = self._sudo_command("-v", user)
            fd = self._exec_credential_fd()
            return ElevatedCommand(
                f"<&{fd} {validated.command} || exit; exec {fd}<&-; sudo -n {cmd}",
                validated.expects,
                reads_credential_fd=True,
            )
        pw = self._elevation_password("su", user)
        su = f"su -c {shlex.quote(cmd)}"
        if pw is None:
            return ElevatedCommand(su, [])
        answers: list[Expect | CredentialExpect] = [
            CredentialExpect(_SU_PROMPT, pw, user=ROOT_LOGIN, host=host)
        ]
        if exec_input is None:
            return ElevatedCommand(su, answers)
        fd = self._exec_credential_fd()
        inner = _exec_inner(cmd, exec_input)
        return ElevatedCommand(
            _on_credential_fd(fd, f"su -c {shlex.quote(inner)}"), answers, reads_credential_fd=True
        )

    def _exec_credential_fd(self) -> int:
        """Return the descriptor an ``exec``'s elevation reads its password from; refuse if none.

        Only a frame that isolates ``exec`` keeps the caller's terminal on
        ``EXEC_CREDENTIAL_FD``; on any other the
        password would be read from a descriptor nothing opened.
        """
        frame = frame_or_default(getattr(self, "command_frame", None))
        if not frame.isolates_exec:
            raise RuntimeError(
                f"{getattr(self, 'name', '')}: an exec's elevation reads its password from "
                f"fd {EXEC_CREDENTIAL_FD}, which the {type(frame).__name__} frame does not "
                "keep (it does not isolate exec)"
            )
        return EXEC_CREDENTIAL_FD

    def _shell_prefix(self) -> str:
        """Return the statements the shell a user switch starts is told first.

        The same composition as the session manager's
        (:func:`~otto.host.command_frame.shell_prefix`): the frame's
        script-line options, then history suppression when the host asked for
        it. ``getattr`` rather than direct attribute access because this mixin
        is also inherited by hosts that declare no ``shell_history`` field
        (LocalHost, DockerContainerHost). ``True`` is the right fallback for
        them: assume history is recorded and leave the shell as we found it.
        """
        return shell_prefix(
            getattr(self, "command_frame", None),
            shell_history=getattr(self, "shell_history", True),
        )

    def _refuse_elevation(self, verb: str, user: str) -> "Never":
        """Bind this host's identity to the shared elevation refusal.

        The reasoning lives with the refusal itself
        (:func:`~otto.host.host.refuse_declined_elevation`), which
        :class:`~otto.host.session.HostSession` shares: same refusal, two
        objects, one wording. ``getattr`` for the name and the log sink
        because this mixin declares neither — it has whatever the concrete
        host it is composed into supplies, the same reason ``_HostProxyIO``
        exists.

        Args:
            verb: the method the caller invoked, named in the caller's words.
            user: the elevation target, already defaulted for display.

        Raises:
            ~otto.result.CommandNotRunError: always.
        """
        refuse_declined_elevation(
            verb,
            user,
            getattr(self, "name", ""),
            self._log_command,  # ty: ignore[missing-attribute]
        )

    async def switch_user(self, *, user: str = "", password: str | SecretStr | None = None) -> None:
        """``su`` the persistent (default) session to *user* (default root).

        Performs the real switch (recursively hopping through any ``via``
        chain via :func:`~otto.host.login_proxy.perform_switch`) and then
        records the new user so ``current_user`` reflects it. Mutates
        session state — affects subsequent ``run`` calls until the user
        exits back.

        See Also:
            :meth:`as_user` — the scoped form; prefer it unless the switch
            should outlive the block.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run — see
                :func:`~otto.host.host.refuse_declined_elevation`.
        """
        self._refuse_establishing("switch_user")  # ty: ignore[missing-attribute]
        if is_dry_run():
            self._refuse_elevation("switch_user", user or ROOT_LOGIN)
        await self._switch_default_session(user, password)

    async def _switch_default_session(
        self, user: str, password: str | SecretStr | None
    ) -> "list[Cred]":
        """Switch the default session to *user* and stamp it.

        The host's adapter onto :func:`~otto.host.login_proxy.switch_and_stamp`.
        """
        mgr = self._live_session_mgr()  # ty: ignore[missing-attribute]
        return await switch_and_stamp(
            io=_HostProxyIO(self),
            creds=self._switch_creds(),
            user=user,
            password=password,
            current_user=mgr.current_user,
            stamp=mgr._set_current_user,  # noqa: SLF001 — intra-package access to SessionManager._set_current_user for user elevation
            host_id=getattr(self, "name", ""),
            shell_prefix=self._shell_prefix(),
            # getattr, not self.term: see _su_cred's note above -- this
            # mixin also backs term-less hosts (LocalHost, DockerContainerHost).
            term=getattr(self, "term", None),
        )

    @asynccontextmanager
    async def as_user(
        self, *, user: str = ROOT_LOGIN, password: str | SecretStr | None = None
    ) -> AsyncGenerator["PosixPrivilege", None]:
        """Run a block as *user*, returning to the original user on exit.

            async with host.as_user("root"):
                await host.run("systemctl restart foo")

        Tracks ``current_user`` across the switch and restores the prior
        user when the block exits, undoing each applied hop in reverse
        (innermost first) so a multi-hop ``via`` chain unwinds correctly.

        The scoped form of :meth:`switch_user` — same engine
        (:func:`~otto.host.login_proxy.perform_switch`), plus undo.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run — see
                :func:`~otto.host.host.refuse_declined_elevation`. It fires on
                ENTRY, above the ``try``, so no undo is scheduled for a switch
                that never happened.
        """
        self._refuse_establishing("as_user")  # ty: ignore[missing-attribute]
        if is_dry_run():
            self._refuse_elevation("as_user", user)
        prev = self._live_session_mgr().current_user  # ty: ignore[missing-attribute]
        applied = await self._switch_default_session(user, password)
        # The session the switch was typed into: the undo types into it, or
        # into nothing (see _undo_switch).
        switched = self._live_session_mgr()._live_default_session()  # noqa: SLF001 — intra-package read of the switched session  # ty: ignore[missing-attribute]
        # The undo chain is a compensating action: an interrupt landing while
        # it runs must not strand the session as the switched user (chaos
        # spec: shielded compensating actions). compensate() holds a
        # cancellation until every hop is unwound (bounded by the teardown
        # deadline), then re-raises it; a cancellation of the body is passed
        # in as held=, so a failing undo cannot replace it. Any other body
        # error keeps its precedence (a failing undo replaces it, as before).
        # Imported here, not at module scope: otto.lifecycle is only needed
        # once a compensating action actually runs, and a top-level import
        # drags it onto every CLI --help path (import-budget guard).
        from ..lifecycle import undone_after

        async with undone_after(
            lambda: self._undo_switch(applied, prev, switched),
            what=f"{getattr(self, 'name', '')}: as_user undo to {prev or 'login user'!r}",
        ):
            yield self

    async def _undo_switch(
        self, applied: "list[Cred]", prev: str, switched: "ShellSession | None"
    ) -> None:
        """Unwind *applied* innermost-first in *switched*, restoring ``current_user`` to *prev*.

        Only the session that was switched is typed into. When it is no
        longer the live default session (a stop that could not confirm the
        shell left it dead), the switch died with it: there is nothing to
        undo, nothing is typed and no session is opened to type into. Its
        stamp is still put back.
        """
        mgr = self._live_session_mgr()  # ty: ignore[missing-attribute]
        if switched is None or mgr._live_default_session() is not switched:  # noqa: SLF001 — intra-package read of the switched session
            if switched is not None:
                switched.current_user = prev
            return
        creds = self._switch_creds()
        for i, hop in enumerate(reversed(applied)):
            via_login = applied[-i - 2].login if i + 1 < len(applied) else prev
            # Look up the full via cred (password/params intact) under the
            # SAME term perform_switch resolved the forward path with — so a
            # custom undo that needs the via user's password sees it, and a
            # term-scoped cred cannot give the two directions different
            # passwords for one login.
            # getattr, not self.term: see _su_cred's note above — this
            # mixin also backs term-less hosts (LocalHost, DockerContainerHost).
            via = cred_for(creds, via_login, getattr(self, "term", None)) or Cred(login=via_login)
            await run_undo(
                _HostProxyIO(self), hop, via, getattr(self, "name", ""), self._shell_prefix()
            )
        mgr._set_current_user(prev)  # noqa: SLF001 — intra-package access to SessionManager._set_current_user to restore prior user
