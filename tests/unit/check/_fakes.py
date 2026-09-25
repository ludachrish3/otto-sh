"""A host double for check tests: first matching substring rule wins."""

import shlex
from dataclasses import dataclass, field

from otto.result import CommandResult, Results, Status


@dataclass
class ScriptedHost:
    """Answers each command from ``rules`` (substring -> (ok, output)); records every call.

    Unmatched commands succeed with empty output. ``fail_all`` makes every call raise
    ``ConnectionError`` (a down host).

    ``run`` plays the shell: a ``sh -c <script>`` (what ``check_root_run``
    sends) is recorded and answered as the *script* it runs, and the text
    ``run`` really received is kept in ``raw_commands``.
    """

    id: str
    ip: str = "10.0.0.1"
    current_user: str = "vagrant"
    has_bash: bool = True
    impairer: str = "netem"
    rules: list[tuple[str, bool, str]] = field(default_factory=list)
    fail_all: bool = False
    commands: list[str] = field(default_factory=list)
    sudo_commands: list[str] = field(default_factory=list)
    raw_commands: list[str] = field(default_factory=list)

    def answer(self, needle: str, output: str = "", *, ok: bool = True) -> "ScriptedHost":
        """Add a rule and return self, for chaining."""
        self.rules.append((needle, ok, output))
        return self

    def _result(self, cmd: str) -> CommandResult:
        if self.fail_all:
            raise ConnectionError(f"{self.id} is down")
        for needle, ok, output in self.rules:
            if needle in cmd:
                status = Status.Success if ok else Status.Failed
                return CommandResult(
                    status=status, value=output, command=cmd, retcode=0 if ok else 1
                )
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

    async def exec(self, cmd: str, timeout: float | None = None, **kw: object) -> CommandResult:
        self.commands.append(cmd)
        if kw.get("sudo"):
            self.sudo_commands.append(cmd)
        return self._result(cmd)

    async def run(self, cmd: str, sudo: bool = False, **_: object) -> Results:
        self.raw_commands.append(cmd)
        cmd = _script(cmd)
        self.commands.append(cmd)
        if sudo:
            self.sudo_commands.append(cmd)
        return Results.collect([self._result(cmd)])


def _script(cmd: str) -> str:
    """The script a ``sh -c <script>`` runs, or *cmd* itself when it is not one."""
    if not cmd.startswith("sh -c "):
        return cmd
    words = shlex.split(cmd)
    assert len(words) == 3, f"not one sh -c word: {cmd!r}"
    return words[2]
