"""A dry run still reads its own checkout's HEAD — and still declines device commands.

Spec: ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md``. The
contract is "a dry run never runs a command on any DEVICE". Reading the SUT
checkout's git HEAD to stamp provenance on the run is not a device command:
:meth:`otto.config.repo.Repo.run_git_command` builds a throwaway
:class:`~otto.host.local_host.LocalHost` purely as a subprocess runner for
otto's own bookkeeping. The dry-run guard sits at the command boundary, so it
fired on WHICH ABSTRACTION was used rather than on WHAT WAS MEANT, declined
otto's own ``git log``, and the caller's ``result.value`` raised.

Every assertion here carries its positive control in the same test, because
"the exemption works" and "the primitive is still armed" are the two halves of
one claim: an exemption that leaked to ordinary ``LocalHost`` use would satisfy
the first half perfectly.
"""

import dataclasses
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto.config.repo import Repo
from otto.host.docker_host import DockerContainerHost
from otto.host.embedded_host import EmbeddedHost, ZephyrHost
from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from otto.host.remote_host import RemoteHost
from otto.host.unix_host import UnixHost
from otto.lifecycle import run_command
from otto.utils import Status
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import TESTS_ROOT
from tests.conftest import active_context

pytestmark = pytest.mark.hostless

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# `tests/repo1` is a SUT-repo fixture that lives inside otto's own git
# checkout, so `git -C tests/repo1 log -1 --format=%H` answers with otto's HEAD
# — a real SHA from a real git invocation, which is the whole point.
REPO1 = TESTS_ROOT / "repo1"


def _head_sha(home: "Path") -> str:
    """otto's own HEAD, read WITHOUT going through the code under test.

    Hermetic: the env comes from :func:`tests._fixtures.gitrepo.git_env`, whole
    and unmerged, so this spawn cannot inherit the developer's ``HOME``, global
    or system gitconfig, or credential prompts. *home* confines ``HOME`` and
    should be the calling test's ``tmp_path``.

    The env is closed but the REPOSITORY is real, and that distinction is the
    design: ``tests/repo1`` lives inside otto's own checkout, so this answers
    with otto's actual HEAD. Reading a commit needs no configuration, so
    neutering the config cannot change which SHA comes back — and the caller
    asserts the result is a 40-hex SHA before comparing, so a hermeticity
    change that broke the read would fail loudly instead of quietly comparing
    two empty strings.
    """
    return subprocess.run(
        ["git", "-C", str(REPO1), "log", "-1", "--format=%H"],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(home),
    ).stdout.strip()


async def _both_hosts_run(cmd: str) -> "tuple[Any, Any]":
    """Run *cmd* on a default and an exempt ``LocalHost``, closing both.

    The ``close()`` calls are not tidiness. An exempt host really spawns a
    persistent shell, and a subprocess transport outliving the
    ``run_command`` loop is the leak
    ``tests/unit/config/test_repo_git_subprocess_leak.py`` exists to catch —
    it fails the NEXT test to land on the worker, not this one.
    """
    default = LocalHost()
    exempt = LocalHost(dry_run_exempt=True)
    try:
        return (await default.run(cmd), await exempt.run(cmd))
    finally:
        await default.close()
        await exempt.close()


async def _exempt_host_run_and_exec(cmd: str) -> "tuple[Any, Any]":
    """Drive both command paths on ONE exempt host, and close it."""
    host = LocalHost(dry_run_exempt=True)
    try:
        return (await host.run(cmd), await host.exec(cmd))
    finally:
        await host.close()


class TestProvenanceSurvivesADryRun:
    """``Repo.commit`` is exempt; nothing else on ``LocalHost`` is."""

    def test_commit_is_the_real_sha_under_a_dry_run(self, tmp_path: Path) -> None:
        """The exemption's PURPOSE, not merely its absence of a crash.

        Asserting only "did not raise" would stay green against an exemption
        that returned an empty string, or a ``Repo.commit`` that swallowed the
        decline — both of which lose the provenance a real run records. So the
        assertion is equality with the SHA an independent ``git`` call reports.

        *tmp_path* is only the hermetic ``HOME`` for that control call; the
        repository it reads is otto's own checkout, not a scratch one.
        """
        expected = _head_sha(tmp_path)
        # ANTI-DEGRADATION, and it guards the comparison below rather than the
        # SHA above: if a hermetic env ever broke the control read, `expected`
        # would be "" and the equality assertion could be satisfied by a
        # `commit` that also answered "" — the green-on-nothing shape.
        assert SHA_RE.match(expected), f"the control `git` call did not give a SHA: {expected!r}"

        with active_context(dry_run=True):
            got = Repo(sut_dir=REPO1).commit

        assert got == expected, (
            "a dry run did not read the checkout's HEAD. Provenance is otto's "
            "own bookkeeping about the machine it is already running on — see "
            "`LocalHost.dry_run_exempt` for the three-part test"
        )

    def test_an_ordinary_local_command_still_declines_under_the_same_dry_run(self) -> None:
        """POSITIVE CONTROL for the test above, and the half that must not move.

        ``LocalHost`` is also a lab host (``otto host local run …``). If the
        exemption reached the default instance, the test above would pass and
        the contract would be gone.
        """
        with active_context(dry_run=True):
            default, exempt = run_command(_both_hosts_run("git --version"))

        assert default.only.status is Status.NotRun, (
            "a plain LocalHost ran a command under a dry run — the exemption "
            "leaked off its own instance"
        )
        # The same command, the same dry run, the same class: the ONLY
        # difference is the flag, so the decline above is attributable to it.
        assert exempt.only.status is Status.Success, (
            "the exempt instance declined too, so the test above cannot be measuring the flag"
        )

    def test_the_exemption_covers_run_and_nothing_else(self) -> None:
        """Narrow by construction: ``exec`` on an exempt host still declines.

        ``_run_one`` is the single seam that reads the flag. Nothing in the
        justification (a git query through the persistent shell) covers the
        stateless exec path, the transfer verbs or sessions, so none of them
        is exempt — and a future widening should have to argue for itself
        rather than arrive for free.
        """
        with active_context(dry_run=True):
            ran, execed = run_command(_exempt_host_run_and_exec("git --version"))

        assert ran.only.status is Status.Success, "the exempt `run` path stopped working"
        assert execed.status is Status.NotRun, (
            "`exec` on an exempt host ran a command: the exemption is wider "
            "than the call site that justified it"
        )


# Every host class a lab can hand out, plus the two abstract bases. `LocalHost`
# is deliberately absent — it is the allowed carrier and is asserted separately,
# so this list can only ever grow in the direction of more scrutiny.
NON_EXEMPTABLE_HOST_CLASSES = [
    BaseHost,
    RemoteHost,
    UnixHost,
    DockerContainerHost,
    EmbeddedHost,
    ZephyrHost,
]


class TestTheExemptionCannotSpreadToOtherHostClasses:
    """`dry_run_exempt` exists on ``LocalHost`` and on no other host class.

    THE STRUCTURAL FENCE, in the registration-shape-pin style. The other three
    fences on this exemption (declared at the construction site, read by one
    seam, justified in a docstring) are conventions a reader can honour or
    ignore. This one runs in CI.

    What it is defending against is not a bug today — blast radius is currently
    zero — but the first hole in "a dry run runs no command" being widened by
    someone tidying: move the field up to ``BaseHost`` "for symmetry" and every
    host class in the fleet gains a spellable exemption, with nothing to notice.

    **The obvious version of this pin does not work, and that is worth knowing
    before anyone simplifies it.** ``BaseHost`` is a plain ABC, not a
    dataclass, so a ``dry_run_exempt: bool = False`` annotation added to it
    does NOT appear in any concrete subclass's ``dataclasses.fields()`` and
    does NOT make the constructor accept the keyword — a fields-only or
    TypeError-only pin stays GREEN against the exact mutation this class
    exists to catch. Verified by performing the move: `dataclasses.fields`
    membership and `UnixHost(dry_run_exempt=True)` were both unchanged, while
    ``hasattr`` flipped to True on every class. So attribute presence is the
    load-bearing check, and the other two are kept because they catch the
    other shape — the field copied into a sibling concrete class as a real
    dataclass field.
    """

    def test_no_other_host_class_carries_the_attribute(self) -> None:
        """Three observables, because no one of them catches both mutations."""
        carriers = [c.__name__ for c in NON_EXEMPTABLE_HOST_CLASSES if _carries_exemption(c)]
        assert not carriers, (
            f"{', '.join(carriers)} gained a `dry_run_exempt` attribute. The "
            f"exemption from the dry-run contract is scoped to LocalHost, the "
            f"one host class that is not necessarily a device; a host that can "
            f"be reached over a network has no read that qualifies for it. See "
            f"`LocalHost.dry_run_exempt` for the three-part test."
        )

        # POSITIVE CONTROL, same test — without it every assertion above is
        # satisfied by a probe that inspects nothing. `LocalHost` really does
        # carry the attribute, by all three observables that the loop checks.
        assert _carries_exemption(LocalHost), (
            "LocalHost lost `dry_run_exempt`, so the loop above is asserting "
            "the absence of something that no longer exists anywhere"
        )

    def test_a_remote_host_cannot_be_constructed_exempt(self) -> None:
        """The behavioural half: the keyword is not merely absent, it is refused."""
        args: "dict[str, Any]" = {"ip": "192.0.2.1", "element": "pinned", "creds": []}

        with pytest.raises(TypeError, match="dry_run_exempt"):
            UnixHost(**args, dry_run_exempt=True)

        # POSITIVE CONTROL: the same constructor, the same arguments, minus the
        # one keyword — so the raise above is attributable to `dry_run_exempt`
        # and not to a signature that rejects these arguments outright.
        assert UnixHost(**args) is not None

    def test_the_enumeration_actually_sees_fields(self) -> None:
        """POSITIVE CONTROL for the whole class: `dataclasses.fields` works here.

        A shared field must be visible on every concrete class through the same
        call the loop above uses. Without this, a `dataclasses.fields` that
        returned nothing useful would make the absence assertions vacuous.
        """
        concrete = [UnixHost, DockerContainerHost, EmbeddedHost, ZephyrHost, LocalHost]
        for cls in concrete:
            names = {f.name for f in dataclasses.fields(cls)}
            assert "log" in names, f"{cls.__name__} has no `log` field to see"


def _carries_exemption(cls: type) -> bool:
    """Whether *cls* carries ``dry_run_exempt`` by ANY of the three routes."""
    fields = {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()
    return (
        "dry_run_exempt" in fields
        or hasattr(cls, "dry_run_exempt")
        or "dry_run_exempt" in getattr(cls, "__annotations__", {})
    )
