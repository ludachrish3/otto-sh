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
``_sudo_password`` defaults to ``None`` (passwordless) and
:class:`~otto.host.unix_host.UnixHost` overrides it from ``creds``.
``_switch_creds`` defaults to ``self.creds`` (or ``[]`` when the host has no
``creds`` field), so ``switch_user``/``as_user`` targets resolve against the
same cred list ``_sudo_password`` does.
"""

import re
import secrets
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pydantic import SecretStr

from ..logger.mode import LogMode
from ..models.base import Cred
from .command_frame import history_prefix
from .errors import UnsupportedOnUserlandError
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
    perform_switch,
    run_undo,
)
from .userland import UserlandHost

if TYPE_CHECKING:
    from typing_extensions import Never

    from .host import Expect

_ELEVATING_MECHANISMS = frozenset({"sudo", "su"})
"""The resolved ``Userland.elevation`` values that elevate; any other (``"none"``) cannot."""

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
        await self._host.send(text, log=log)  # ty: ignore[unresolved-attribute]

    async def expect(
        self, pattern: str | re.Pattern[str], timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> str:
        return await self._host.expect(pattern, timeout)  # ty: ignore[unresolved-attribute]


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

    def _sudo_password(self, user: str | None = None) -> SecretStr | None:  # noqa: ARG002 — the parameter belongs to the seam: this default knows no passwords for any user
        """Password for ``sudo -S``, or None when sudo is passwordless here.

        *user* is the identity the elevated command runs as; ``None`` means the
        session's own current user. The default knows no passwords either way.
        """
        return None

    def _switch_creds(self) -> list[Cred]:
        """Creds used to resolve ``switch_user``/``as_user`` targets.

        Default ``getattr(self, "creds", [])`` — hosts with a ``creds`` field
        (:class:`~otto.host.unix_host.UnixHost`) get real cred-chain
        resolution for free; hosts without one (:class:`~otto.host.local_host.LocalHost`,
        :class:`~otto.host.docker_host.DockerContainerHost`) fall back to an
        empty list (ad-hoc, passwordless ``su`` targets).
        """
        return getattr(self, "creds", [])

    def _acting_user(self, user: str | None) -> str:
        """Who an elevated command runs as before it elevates: *user*, else the session's user.

        *user* is an ``exec(user=X)``'s switch, made before the command runs;
        ``None`` (every ``run``) means the default session's current user.
        Whose password sudo asks for, and whether ``su`` challenges at all,
        both follow from this one answer.
        """
        return user or self._live_session_mgr().current_user  # ty: ignore[unresolved-attribute]

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

    # Spec: docs/superpowers/specs/2026-08-11-busybox-host-support-design.md
    def _elevate(self, cmd: str, user: str | None = None) -> ElevatedCommand:
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

        Raises:
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
        acting = self._acting_user(user)
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
        answers: list[Expect | CredentialExpect] = []
        if elevation == "sudo":
            prompt = _sudo_prompt()
            pw = self._elevation_password("sudo", user)
            if pw is not None:
                answers.append(
                    CredentialExpect(
                        re.escape(prompt), pw, user=acting, host=host, refused_on_repeat=True
                    )
                )
            return ElevatedCommand(f"SUDO_PROMPT='{prompt}' sudo -S {cmd}", answers)
        pw = self._elevation_password("su", user)
        if pw is not None:
            answers.append(CredentialExpect(_SU_PROMPT, pw, user=ROOT_LOGIN, host=host))
        return ElevatedCommand(f"su -c {shlex.quote(cmd)}", answers)

    def _history_prefix(self) -> str:
        """Return this host's history-suppression payload ("" when history is kept).

        ``getattr`` rather than direct attribute access because this mixin is
        also inherited by hosts that declare no ``shell_history`` field
        (LocalHost, DockerContainerHost). ``True`` is the right fallback for
        them — assume history is recorded and leave the shell as we found it.
        """
        return history_prefix(
            getattr(self, "command_frame", None), getattr(self, "shell_history", True)
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
            self._log_command,  # ty: ignore[unresolved-attribute]
        )

    async def switch_user(self, user: str = "", password: str | SecretStr | None = None) -> None:
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
        if is_dry_run():
            self._refuse_elevation("switch_user", user or ROOT_LOGIN)
        applied = await perform_switch(
            _HostProxyIO(self),
            self._switch_creds(),
            user,
            password,
            self._live_session_mgr().current_user,  # ty: ignore[unresolved-attribute]
            getattr(self, "name", ""),
            self._history_prefix(),
            # getattr, not self.term: see _su_cred's note above -- this
            # mixin also backs term-less hosts (LocalHost, DockerContainerHost).
            protocol=getattr(self, "term", None),
        )
        self._live_session_mgr()._set_current_user(applied[-1].login or ROOT_LOGIN)  # noqa: SLF001 — intra-package access to SessionManager._set_current_user for user elevation  # ty: ignore[unresolved-attribute]

    @asynccontextmanager
    async def as_user(
        self, user: str = ROOT_LOGIN, password: str | SecretStr | None = None
    ) -> AsyncIterator["PosixPrivilege"]:
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
        if is_dry_run():
            self._refuse_elevation("as_user", user)
        prev = self._live_session_mgr().current_user  # ty: ignore[unresolved-attribute]
        applied = await perform_switch(
            _HostProxyIO(self),
            self._switch_creds(),
            user,
            password,
            prev,
            getattr(self, "name", ""),
            self._history_prefix(),
            protocol=getattr(self, "term", None),
        )
        self._live_session_mgr()._set_current_user(applied[-1].login or ROOT_LOGIN)  # noqa: SLF001 — intra-package access to SessionManager._set_current_user for user elevation  # ty: ignore[unresolved-attribute]
        try:
            yield self
        finally:
            # The undo chain is a compensating action: an interrupt landing
            # while it runs must not strand the session as the switched user
            # (chaos spec: shielded compensating actions). compensate() holds
            # the cancellation until every hop is unwound (bounded by the
            # teardown deadline), then re-raises it.
            # Imported here, not at module scope: otto.lifecycle is only needed
            # once a compensating action actually runs, and a top-level import
            # drags it onto every CLI --help path (import-budget guard).
            from ..lifecycle import compensate

            await compensate(
                self._undo_switch(applied, prev),
                what=f"{getattr(self, 'name', '')}: as_user undo to {prev or 'login user'!r}",
            )

    async def _undo_switch(self, applied: "list[Cred]", prev: str) -> None:
        """Unwind *applied* innermost-first, restoring ``current_user`` to *prev*."""
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
                _HostProxyIO(self), hop, via, getattr(self, "name", ""), self._history_prefix()
            )
        self._live_session_mgr()._set_current_user(prev)  # noqa: SLF001 — intra-package access to SessionManager._set_current_user to restore prior user  # ty: ignore[unresolved-attribute]
