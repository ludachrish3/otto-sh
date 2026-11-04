"""``otto.host.cov_sweep``: the presence probe, the leftover probe and the removal (#777).

The tree cases run the generated scripts through a real ``sh`` and the
host's GNU ``find`` (:class:`~tests._fixtures.shell_host.ShellHost`); the
parser cases feed the exact lines GNU and BusyBox print.
"""

import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.host import cov_sweep
from otto.host.errors import HostCommandError
from otto.host.product import GcdaFindError, gcda_remove_dirs, gcda_remove_files
from otto.result import CommandResult, NotRunResult, Results
from otto.utils import Status
from tests._fixtures.shell_host import ELEVATED_MARK, CovShellProduct, ShellHost, fake_bin

_SUDO_NOISE = (
    "sudo: unable to resolve host h1: Name or service not known\notto-sudo-AAAAAAAAAAAAAAAA:"
)
"""What a session prints before the script runs: a sudo warning and a newline-less prompt."""


def _product(name: str, cov_dir: str | Path):
    return SimpleNamespace(name=name, cov_dir=str(cov_dir))


class _ScriptedHost:
    """Host double answering ``exec`` with canned results; ``run`` must never be called."""

    def __init__(self, *answers):
        self.id = "h1"
        self.calls: list[tuple[str, str]] = []
        self._answers = list(answers)

    async def exec(self, cmd, **kwargs):
        self.calls.append(("exec", cmd))
        return self._answers.pop(0)

    async def run(self, cmd, **kwargs):
        raise AssertionError(f"the presence probe never elevates, got run({cmd!r})")


def _answer(value: str, retcode: int = 0) -> CommandResult:
    status = Status.Success if retcode == 0 else Status.Error
    return CommandResult(status, value=value, command="probe", retcode=retcode)


# ── the presence probe (§3) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_presence_reports_present_and_absent_by_index(tmp_path):
    present = tmp_path / "cov"
    present.mkdir()
    spaced = tmp_path / "My App's cov"
    spaced.mkdir()
    products = [
        _product("app", present),
        _product("gone", tmp_path / "never"),
        _product("spaced", spaced),
    ]
    host = ShellHost()
    assert await cov_sweep.probe_presence(host, products) == frozenset({"app", "spaced"})
    assert [kind for kind, _ in host.calls] == ["exec"]


@pytest.mark.asyncio
async def test_probe_presence_ignores_what_the_session_printed_first(tmp_path):
    present = tmp_path / "cov"
    present.mkdir()
    host = ShellHost(noise=_SUDO_NOISE + "\notto-cov 1 present\n")
    products = [_product("app", present), _product("gone", tmp_path / "never")]
    assert await cov_sweep.probe_presence(host, products) == frozenset({"app"})


_MARK = "\notto-cov-presence\n"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_answer(_MARK + "otto-cov 0 present\notto-cov 1 absent\n", 1), id="exit-1"),
        pytest.param(_answer(_MARK + "otto-cov 0 present\n"), id="missing-index"),
        pytest.param(_answer(_MARK + "otto-cov 0 present\notto-cov 1 gone\n"), id="unknown-word"),
        pytest.param(
            _answer(_MARK + "otto-cov 0 present\notto-cov 0 absent\notto-cov 1 absent\n"),
            id="duplicate-index",
        ),
        pytest.param(
            _answer(_MARK + "otto-cov 0 present\notto-cov 1 absent\notto-cov 2 absent\n"),
            id="index-out-of-range",
        ),
        pytest.param(_answer(_MARK + "otto-cov 0 present\nnoise\notto-cov 1 absent\n"), id="noise"),
        pytest.param(_answer("otto-cov 0 present\notto-cov 1 absent\n"), id="no-marker"),
        pytest.param(
            NotRunResult(status=Status.NotRun, command="probe", retcode=-1), id="declined"
        ),
    ],
)
@pytest.mark.asyncio
async def test_probe_presence_is_none_on_a_failed_or_malformed_batch(answer):
    host = _ScriptedHost(answer)
    products = [_product("a", "/var/cov/a"), _product("b", "/var/cov/b")]
    assert await cov_sweep.probe_presence(host, products) is None


@pytest.mark.asyncio
async def test_probe_presence_reads_a_well_formed_canned_batch():
    host = _ScriptedHost(_answer(_MARK + "otto-cov 0 present\notto-cov 1 absent\n"))
    products = [_product("a", "/var/cov/a"), _product("b", "/var/cov/b")]
    assert await cov_sweep.probe_presence(host, products) == frozenset({"a"})


@pytest.mark.asyncio
async def test_probe_presence_sends_nothing_without_products():
    host = _ScriptedHost()
    assert await cov_sweep.probe_presence(host, []) == frozenset()
    assert host.calls == []


@pytest.mark.asyncio
async def test_probe_presence_never_elevates(tmp_path):
    host = _ScriptedHost(_answer(_MARK + "otto-cov 0 absent\n"))
    assert await cov_sweep.probe_presence(host, [_product("a", tmp_path / "x")]) == frozenset()
    assert [kind for kind, _ in host.calls] == ["exec"]


# ── the leftover probe and the removal (§4) ──────────────────────────────────
#
# Real sh + GNU find over tmp trees. A root-owned product's elevated path is
# the double's `run(sudo=True)`, which prefixes `env OTTO_TEST_ELEVATED=1`.


class _RootOwned(CovShellProduct):
    """A kind whose ``cov_dir`` root writes (docker_image, kmod): elevated by its class."""

    cov_dir_root_owned = True


def _cov(name: str, cov_dir: Path, *, root: bool = False) -> CovShellProduct:
    cls = _RootOwned if root else CovShellProduct
    return cls(
        artifact=Path(f"/build/{name}"), name=name, cov_dir=str(cov_dir), instrumented_override=True
    )


def _tree(root: Path, *files: str, dirs: tuple[str, ...] = ()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"")
    for rel in dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


_ROOT_SKIP = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a chmod 000 dir, so the permission error never happens"
)


def _elevated_only_find(tmp_path: Path) -> Path:
    """A ``find`` that works only elevated: the dir is root's, the session user cannot read it."""
    real_find = shutil.which("find")
    assert real_find is not None
    return fake_bin(
        tmp_path,
        "find",
        f'[ "${ELEVATED_MARK}" = 1 ] || {{ echo "find: \'$1\': Permission denied" >&2; exit 1; }}\n'
        f'exec {real_find} "$@"',
    )


@pytest.mark.asyncio
async def test_a_tree_of_only_counters_is_listed_then_removed_whole(tmp_path):
    d = _tree(tmp_path / "cov", "a.gcda", "sub/b.gcda")
    host, app = ShellHost(), _cov("app", d)
    left = await cov_sweep.coverage_left(host, [app])
    assert sorted(left["app"]) == [str(d / "a.gcda"), str(d / "sub" / "b.gcda")]
    assert (await cov_sweep.remove_coverage(host, [app])).status is Status.Success
    assert not d.exists()
    assert await cov_sweep.coverage_left(host, [app]) == {}


@pytest.mark.asyncio
async def test_nested_counters_and_empty_dirs_all_go(tmp_path):
    d = _tree(tmp_path / "cov", "x/z/c.gcda", dirs=("x/y", "w"))
    host, app = ShellHost(), _cov("app", d)
    left = await cov_sweep.coverage_left(host, [app])
    assert set(left["app"]) == {str(d / "x/z/c.gcda"), str(d / "x/y"), str(d / "w")}
    assert (await cov_sweep.remove_coverage(host, [app])).status is Status.Success
    assert not d.exists()


@pytest.mark.asyncio
async def test_a_user_file_keeps_itself_and_its_parents_and_counts_as_clean(tmp_path, caplog):
    d = _tree(tmp_path / "cov", "keep/notes.txt", "keep/a.gcda", "sub/b.gcda", dirs=("empty",))
    host, app = ShellHost(), _cov("app", d)
    with caplog.at_level(logging.INFO, logger="otto.host.cov_sweep"):
        assert (await cov_sweep.remove_coverage(host, [app])).status is Status.Success
    assert (d / "keep" / "notes.txt").exists()
    assert not (d / "keep" / "a.gcda").exists()
    assert not (d / "sub").exists()
    assert not (d / "empty").exists()
    assert await cov_sweep.coverage_left(host, [app]) == {}
    assert [r.getMessage() for r in caplog.records if "kept" in r.getMessage()] == [
        f"cov_dir {d} kept: holds non-coverage files, or the session cannot remove it"
    ]


@pytest.mark.asyncio
async def test_a_symlinked_cov_dir_removes_nothing_under_its_target(tmp_path):
    target = _tree(tmp_path / "home", "notes.txt", "a.gcda", dirs=("empty",))
    d = tmp_path / "cov"
    d.symlink_to(target)
    host, app = ShellHost(), _cov("app", d)
    assert (await cov_sweep.remove_coverage(host, [app])).status is Status.Success
    assert (target / "notes.txt").exists()
    assert (target / "a.gcda").exists()
    assert (target / "empty").exists()
    assert d.is_symlink()


@pytest.mark.asyncio
async def test_an_absent_cov_dir_is_a_no_op_and_answers_absent(tmp_path, caplog):
    d = tmp_path / "never"
    host, app = ShellHost(), _cov("app", d)
    assert await cov_sweep._probe(host, [app]) == {"app": "absent"}
    with caplog.at_level(logging.INFO, logger="otto.host.cov_sweep"):
        assert (await cov_sweep.remove_coverage(host, [app])).status is Status.Success
    assert await cov_sweep.coverage_left(host, [app]) == {}
    assert not [r for r in caplog.records if "kept" in r.getMessage()]


@_ROOT_SKIP
@pytest.mark.asyncio
async def test_an_unreadable_dir_is_a_probe_error_never_clean(tmp_path):
    d = _tree(tmp_path / "cov", "a.gcda", dirs=("locked",))
    (d / "locked").chmod(0)
    try:
        answer = (await cov_sweep._probe(ShellHost(), [_cov("app", d)]))["app"]
        with pytest.raises(HostCommandError, match=r"h1: cannot tell whether app's cov_dir"):
            await cov_sweep.coverage_left(ShellHost(), [_cov("app", d)])
    finally:
        (d / "locked").chmod(0o755)
    assert isinstance(answer, GcdaFindError)
    assert f"otto-ls-failed {d / 'locked'}" in answer.detail


@pytest.mark.asyncio
async def test_a_healthy_non_empty_dir_is_present_and_clean(tmp_path):
    d = _tree(tmp_path / "cov", "notes.txt", "sub/more.txt")
    assert await cov_sweep._probe(ShellHost(), [_cov("app", d)]) == {"app": []}


@pytest.mark.asyncio
async def test_a_failing_rm_is_named_by_the_postcondition(tmp_path):
    d = _tree(tmp_path / "cov", "a.gcda")
    host = ShellHost(bin_dir=fake_bin(tmp_path, "rm", "exit 1"))
    result = await cov_sweep.remove_coverage(host, [_cov("app", d)])
    assert result.status is Status.Error
    assert result.msg == f"h1: cleanup left coverage data: app: left {d / 'a.gcda'}"


@_ROOT_SKIP
@pytest.mark.asyncio
async def test_a_removal_failure_is_attributed_to_its_own_product(tmp_path):
    locked = tmp_path / "locked"
    first = _tree(locked / "cov", "a.gcda")
    second = _tree(tmp_path / "ok", "b.gcda")
    host = ShellHost()
    locked.chmod(0)
    try:
        result = await cov_sweep.remove_coverage(host, [_cov("one", first), _cov("two", second)])
    finally:
        locked.chmod(0o755)
    assert result.status is Status.Error
    assert result.msg.startswith(f"h1: cleanup left coverage data: one: find: '{first}'")
    assert f"one: cannot tell whether {first} is clean" in result.msg
    assert "two" not in result.msg
    assert not second.exists()
    assert (first / "a.gcda").exists()


@pytest.mark.parametrize("path", ["plain", "elevated"])
@pytest.mark.asyncio
async def test_the_probe_and_removal_ignore_what_the_session_printed_first(tmp_path, path):
    bin_dir = _elevated_only_find(tmp_path) if path == "elevated" else None
    root = path == "elevated"
    clean = _tree(tmp_path / "clean", "notes.txt")
    absent = tmp_path / "absent"
    survivor_dir = _tree(tmp_path / "survivor", "a.gcda")
    products = [
        _cov("clean", clean, root=root),
        _cov("absent", absent, root=root),
        _cov("survivor", survivor_dir, root=root),
    ]
    noisy = ShellHost(bin_dir=bin_dir, noise=_SUDO_NOISE)
    assert await cov_sweep.coverage_left(noisy, products) == {
        "survivor": [str(survivor_dir / "a.gcda")]
    }
    assert (await cov_sweep.remove_coverage(noisy, products)).status is Status.Success
    assert await cov_sweep.coverage_left(noisy, products) == {}
    assert (clean / "notes.txt").exists()


class _BrokenRemovalHost(ShellHost):
    """Answers one group's removal batch with a canned failure; everything else runs for real."""

    def __init__(self, group: str, answer: CommandResult):
        super().__init__()
        self._group, self._answer = group, answer

    async def exec(self, cmd, **kwargs):
        if self._group == "plain" and "otto-cov-remove" in cmd:
            self.calls.append(("exec", cmd))
            return self._answer
        return await super().exec(cmd, **kwargs)

    async def run(self, cmd, *, sudo=False, **kwargs):
        if self._group == "elevated" and "otto-cov-remove" in cmd:
            self.calls.append(("run", cmd))
            return Results.collect([self._answer])
        return await super().run(cmd, sudo=sudo, **kwargs)


@pytest.mark.parametrize("group", ["plain", "elevated"])
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            CommandResult(Status.Error, value="", command="x", retcode=-1, timed_out=True),
            id="timed-out",
        ),
        pytest.param(
            CommandResult(Status.Error, value="", command="x", retcode=-1), id="never-ran"
        ),
        pytest.param(
            CommandResult(
                Status.Success,
                value="\notto-cov-remove\notto-cov-begin 0\n",
                command="x",
                retcode=0,
            ),
            id="truncated",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_removal_batch_names_every_product_of_that_batch_only(
    tmp_path, group, answer
):
    plain_dir = _tree(tmp_path / "plain", "a.gcda")
    root_dir = _tree(tmp_path / "root", "b.gcda")
    host = _BrokenRemovalHost(group, answer)
    result = await cov_sweep.remove_coverage(
        host, [_cov("p", plain_dir), _cov("r", root_dir, root=True)]
    )
    assert result.status is Status.Error
    broken, fine = ("p", "r") if group == "plain" else ("r", "p")
    assert f"{broken}: {group} removal batch failed" in result.msg
    assert f"{fine}:" not in result.msg
    # The postcondition still ran: the broken batch's counters are named.
    left = plain_dir / "a.gcda" if group == "plain" else root_dir / "b.gcda"
    assert f"{broken}: left {left}" in result.msg


class _KeptHost(ShellHost):
    """Removal batch fails outright; the probe runs for real."""

    async def exec(self, cmd, **kwargs):
        if "otto-cov-remove" in cmd:
            self.calls.append(("exec", cmd))
            return CommandResult(Status.Error, value="", command=cmd, retcode=-1)
        return await super().exec(cmd, **kwargs)


@pytest.mark.asyncio
async def test_kept_is_logged_from_the_postcondition_even_after_a_failed_batch(tmp_path, caplog):
    d = _tree(tmp_path / "cov", "notes.txt")
    with caplog.at_level(logging.INFO, logger="otto.host.cov_sweep"):
        result = await cov_sweep.remove_coverage(_KeptHost(), [_cov("app", d)])
    assert result.status is Status.Error
    kept = f"cov_dir {d} kept: holds non-coverage files, or the session cannot remove it"
    assert kept in caplog.text


class _ProbeFailsAfterRemoval(ShellHost):
    """The removal runs; every probe answers garbage, so nothing is established."""

    async def exec(self, cmd, **kwargs):
        if "otto-cov-probe" in cmd:
            self.calls.append(("exec", cmd))
            return CommandResult(Status.Success, value="garbage", command=cmd, retcode=0)
        return await super().exec(cmd, **kwargs)


@pytest.mark.asyncio
async def test_no_kept_line_when_the_postcondition_could_not_tell(tmp_path, caplog):
    d = _tree(tmp_path / "cov", "notes.txt", "a.gcda")
    with caplog.at_level(logging.INFO, logger="otto.host.cov_sweep"):
        result = await cov_sweep.remove_coverage(_ProbeFailsAfterRemoval(), [_cov("app", d)])
    assert result.status is Status.Error
    assert f"app: cannot tell whether {d} is clean" in result.msg
    assert "kept" not in caplog.text


@pytest.mark.asyncio
async def test_a_cov_dir_with_a_space_and_a_quote_is_one_operand(tmp_path):
    d = _tree(tmp_path / "My App's cov", "a.gcda", dirs=("e",))
    host, app = ShellHost(), _cov("app", d)
    assert set((await cov_sweep.coverage_left(host, [app]))["app"]) == {
        str(d / "a.gcda"),
        str(d / "e"),
    }
    assert (await cov_sweep.remove_coverage(host, [app])).status is Status.Success
    assert not d.exists()


@pytest.mark.asyncio
async def test_nothing_eligible_sends_nothing():
    host = ShellHost()
    assert (await cov_sweep.remove_coverage(host, [])).status is Status.Success
    assert await cov_sweep.coverage_left(host, []) == {}
    assert host.calls == []


@pytest.mark.asyncio
async def test_the_only_deleting_commands_are_the_two_spellings(tmp_path):
    # A misconfigured cov_dir (a user's home, say) swept elevated: only
    # .gcda regular files and dirs rmdir accepts can go.
    home = _tree(tmp_path / "alice", "thesis.tex", "build/a.gcda", "notes/todo.txt", dirs=("e",))
    host = ShellHost()
    result = await cov_sweep.remove_coverage(host, [_cov("app", home, root=True)])
    assert result.status is Status.Success
    assert (home / "thesis.tex").exists()
    assert (home / "notes" / "todo.txt").exists()
    allowed = (gcda_remove_files(str(home)), gcda_remove_dirs(str(home)))
    deleting = re.compile(r"\b(rm|rmdir|unlink)\b|-delete")
    for kind, cmd in host.calls:
        script = shlex.split(cmd)[2] if kind == "run" else cmd
        assert "rm -rf" not in script
        for line in script.splitlines():
            if deleting.search(line):
                assert line.startswith(allowed), line


# ── elevation ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_probe_re_runs_elevated_only_for_a_root_owned_product(tmp_path, caplog):
    host = ShellHost(bin_dir=_elevated_only_find(tmp_path))
    root_dir = _tree(tmp_path / "root", "a.gcda")
    user_dir = _tree(tmp_path / "user", "b.gcda")
    with caplog.at_level(logging.DEBUG, logger="otto.host.cov_sweep"):
        answers = await cov_sweep._probe(
            host, [_cov("root", root_dir, root=True), _cov("user", user_dir)]
        )
    assert answers["root"] == [str(root_dir / "a.gcda")]
    assert isinstance(answers["user"], GcdaFindError)
    assert [kind for kind, _ in host.calls] == ["exec", "run"]
    assert str(user_dir) not in host.calls[1][1]
    assert "coverage probe on h1: 1 product(s) answered unelevated, 1 re-probed elevated" in (
        caplog.text
    )


@pytest.mark.asyncio
async def test_a_readable_root_owned_dir_is_answered_without_sudo(tmp_path, caplog):
    d = _tree(tmp_path / "root", "a.gcda")
    host = ShellHost()
    with caplog.at_level(logging.DEBUG, logger="otto.host.cov_sweep"):
        left = await cov_sweep.coverage_left(host, [_cov("app", d, root=True)])
    assert left == {"app": [str(d / "a.gcda")]}
    assert [kind for kind, _ in host.calls] == ["exec"]
    assert "coverage probe on h1: 1 product(s) answered unelevated, 0 re-probed elevated" in (
        caplog.text
    )


@pytest.mark.asyncio
async def test_an_elevated_re_probe_that_fails_too_raises_with_its_output(tmp_path):
    host = ShellHost(
        bin_dir=fake_bin(tmp_path, "find", "echo \"find: '$1': Input/output error\" >&2; exit 1")
    )
    d = _tree(tmp_path / "root", "a.gcda")
    with pytest.raises(HostCommandError, match="Input/output error"):
        await cov_sweep.coverage_left(host, [_cov("app", d, root=True)])
    assert [kind for kind, _ in host.calls] == ["exec", "run"]


@pytest.mark.asyncio
async def test_the_removal_of_a_root_owned_product_always_runs_elevated(tmp_path, caplog):
    plain_dir = _tree(tmp_path / "plain", "a.gcda")
    root_dir = _tree(tmp_path / "root", "b.gcda")
    host = ShellHost()
    with caplog.at_level(logging.DEBUG, logger="otto.host.cov_sweep"):
        result = await cov_sweep.remove_coverage(
            host, [_cov("p", plain_dir), _cov("r", root_dir, root=True)]
        )
    assert result.status is Status.Success
    removals = [(kind, cmd) for kind, cmd in host.calls if "otto-cov-remove" in cmd]
    assert [kind for kind, _ in removals] == ["exec", "run"]
    assert str(root_dir) in removals[1][1]
    assert str(root_dir) not in removals[0][1]
    assert "coverage removal on h1: 1 plain, 1 elevated" in caplog.text


def test_docker_image_and_kmod_cov_dirs_are_root_owned_by_class():
    from otto.host.docker_image_kind import DockerImageProduct
    from otto.host.kmod_kind import KmodProduct
    from otto.host.product import Product, ShellProduct

    assert DockerImageProduct.cov_dir_root_owned is True
    assert KmodProduct.cov_dir_root_owned is True
    assert ShellProduct.cov_dir_root_owned is False
    assert Product.cov_dir_root_owned is False


@pytest.mark.asyncio
async def test_a_dry_run_removal_declines_without_a_re_probe(tmp_path, caplog):
    class _Declining(ShellHost):
        async def exec(self, cmd, **kwargs):
            self.calls.append(("exec", cmd))
            return NotRunResult(status=Status.NotRun, command=cmd, retcode=-1)

    host = _Declining()
    with caplog.at_level(logging.INFO, logger="otto.host.cov_sweep"):
        result = await cov_sweep.remove_coverage(host, [_cov("app", tmp_path / "cov")])
    assert result.status is Status.NotRun
    assert len(host.calls) == 1
    assert "kept" not in caplog.text


@pytest.mark.asyncio
async def test_a_dry_run_probe_refuses_to_invent_an_answer(tmp_path):
    from otto.result import CommandNotRunError

    class _Declining(ShellHost):
        async def exec(self, cmd, **kwargs):
            return NotRunResult(status=Status.NotRun, command=cmd, retcode=-1)

    with pytest.raises(CommandNotRunError):
        await cov_sweep.coverage_left(_Declining(), [_cov("app", tmp_path / "cov")])


# ── eligibility ──────────────────────────────────────────────────────────────


def test_only_instrumented_non_embedded_products_on_a_non_embedded_host_are_eligible():
    from otto.host.embedded_host import EmbeddedHost
    from otto.host.product import cov_dir_products

    known = _cov("known", Path("/var/cov/known"))
    unknown = CovShellProduct(artifact=Path("/b/u"), name="unknown", instrumented_override=None)
    unknown.instrumented = lambda: None
    embedded = _cov("ext", Path("/var/cov/ext"))
    embedded.kind = "embedded"
    host = SimpleNamespace(id="h1", products=[known, unknown, embedded])
    assert cov_dir_products(host, host.products) == [known]
    board = EmbeddedHost.__new__(EmbeddedHost)
    assert cov_dir_products(board, [known]) == []


# ── the parsers, from the exact lines GNU and BusyBox print ──────────────────

_D = "/var/cov/app"


def _probe_output(*sections: tuple[list[str], list[str], int, int], mark=True) -> str:
    lines = ["", "otto-cov-probe"] if mark else []
    for i, (files, dirs, r, s) in enumerate(sections):
        lines += [f"otto-cov-begin {i}", *files, f"otto-cov-mid {i}", *dirs]
        lines.append(f"otto-cov-end {i} {r} {s}")
    return "\n".join(lines) + "\n"


_GNU_ABSENT = f"/usr/bin/find: '{_D}': No such file or directory"
_BUSYBOX_ABSENT = f"find: {_D}: No such file or directory"


@pytest.mark.parametrize("line", [_GNU_ABSENT, _BUSYBOX_ABSENT], ids=["gnu", "busybox"])
def test_parse_probe_both_sections_enoent_for_the_start_path_is_absent(line):
    assert cov_sweep._parse_probe(_probe_output(([line], [line], 1, 1)), [_D]) == ["absent"]


@pytest.mark.parametrize(
    "section",
    [
        pytest.param(([_GNU_ABSENT], [], 1, 0), id="only-files-failed"),
        pytest.param(([], [_GNU_ABSENT], 0, 1), id="only-dirs-failed"),
        pytest.param(([], [_GNU_ABSENT], 1, 1), id="silent-files-failure"),
        pytest.param(
            (
                [f"find: '{_D}/sub': No such file or directory"],
                [f"find: '{_D}/sub': No such file or directory"],
                1,
                1,
            ),
            id="descendant-enoent",
        ),
        pytest.param(
            (
                [f"find: '{_D}No such file or directory': No such file or directory"],
                [f"find: '{_D}No such file or directory': No such file or directory"],
                1,
                1,
            ),
            id="misleading-path",
        ),
        pytest.param(([_GNU_ABSENT, "x"], [_GNU_ABSENT], 1, 1), id="two-lines"),
        pytest.param(([], [f"otto-ls-failed {_D}/x"], 0, 0), id="ls-failed"),
        pytest.param(([f"{_D}/a.gcno"], [], 0, 0), id="not-a-counter"),
        pytest.param(([f"{_D}_ref/a.gcda"], [], 0, 0), id="sibling-prefix"),
        pytest.param(([], ["/elsewhere"], 0, 0), id="dir-outside"),
        pytest.param(([_SUDO_NOISE.splitlines()[0]], [], 0, 0), id="noise-after-the-mark"),
    ],
)
def test_parse_probe_refuses_what_it_cannot_attribute(section):
    [answer] = cov_sweep._parse_probe(_probe_output(section), [_D])
    assert isinstance(answer, GcdaFindError)


def test_parse_probe_lists_counters_and_empty_dirs_even_with_misleading_names():
    odd = f"{_D}/No such file or directory.gcda"
    files, dirs = [odd, f"{_D}/b.gcda"], [f"{_D}/e"]
    assert cov_sweep._parse_probe(_probe_output((files, dirs, 0, 0)), [_D]) == [files + dirs]


def test_parse_probe_a_lone_empty_cov_dir_is_clean_but_an_empty_subdir_is_left():
    """The start path itself is never a leftover (maintainer ruling 2026-11-04, review I1).

    An empty ``cov_dir`` the session cannot remove (a root-owned parent) is
    kept; anything under it, an empty subdir or a counter, is still left.
    """
    sections = (([], [_D], 0, 0), ([f"{_D}/a.gcda"], [f"{_D}/e", _D], 0, 0))
    assert cov_sweep._parse_probe(_probe_output(*sections), [_D, _D]) == [
        [],
        [f"{_D}/a.gcda", f"{_D}/e"],
    ]


@_ROOT_SKIP
@pytest.mark.asyncio
async def test_an_empty_cov_dir_the_session_cannot_remove_is_kept_at_info(tmp_path, caplog):
    parent = tmp_path / "rootish"
    d = _tree(parent / "cov", "sub/a.gcda", dirs=("nested/deeper",))
    parent.chmod(0o555)  # like /var/cov: the session may empty cov_dir, never rmdir it
    host, app = ShellHost(), _cov("app", d)
    try:
        with caplog.at_level(logging.INFO, logger="otto.host.cov_sweep"):
            result = await cov_sweep.remove_coverage(host, [app])
        left = await cov_sweep.coverage_left(host, [app])
    finally:
        parent.chmod(0o755)
    assert result.status is Status.Success, result.msg
    assert d.is_dir()
    assert list(d.iterdir()) == []  # the nested empty subdirs and the counter went
    assert left == {}  # is_clean agrees: a lone empty cov_dir is clean
    assert [r.getMessage() for r in caplog.records if "kept" in r.getMessage()] == [
        f"cov_dir {d} kept: holds non-coverage files, or the session cannot remove it"
    ]


@pytest.mark.asyncio
async def test_an_empty_cov_dir_under_a_writable_parent_is_removed(tmp_path, caplog):
    d = _tree(tmp_path / "cov", "sub/a.gcda", dirs=("nested/deeper",))
    host, app = ShellHost(), _cov("app", d)
    with caplog.at_level(logging.INFO, logger="otto.host.cov_sweep"):
        assert (await cov_sweep.remove_coverage(host, [app])).status is Status.Success
    assert not d.exists()
    assert not [r for r in caplog.records if "kept" in r.getMessage()]


def test_parse_probe_present_and_clean_is_an_empty_list():
    assert cov_sweep._parse_probe(_probe_output(([], [], 0, 0)), [_D]) == [[]]


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(
            _probe_output(([], [], 0, 0), ([], [], 0, 0)).replace("otto-cov-end 1 0 0\n", ""),
            id="missing-end",
        ),
        pytest.param(
            _probe_output(([], [], 0, 0), ([], [], 0, 0)).replace(
                "otto-cov-begin 1", "otto-cov-begin 2"
            ),
            id="wrong-index",
        ),
        pytest.param(
            "\notto-cov-probe\notto-cov-begin 1\notto-cov-mid 1\notto-cov-end 1 0 0\n"
            "otto-cov-begin 0\notto-cov-mid 0\notto-cov-end 0 0 0\n",
            id="out-of-order",
        ),
        pytest.param(
            _probe_output(([], [], 0, 0), ([], [], 0, 0)).replace("end 1 0 0", "end 1 x 0"),
            id="non-integer-status",
        ),
        pytest.param(_probe_output(([], [], 0, 0), ([], [], 0, 0)) + "stray\n", id="stray-line"),
        pytest.param(_probe_output(([], [], 0, 0), ([], [], 0, 0), mark=False), id="no-mark"),
    ],
)
def test_parse_probe_trusts_no_partial_batch(output):
    answers = cov_sweep._parse_probe(output, [_D, "/var/cov/lib"])
    assert len(answers) == 2
    assert all(isinstance(a, GcdaFindError) for a in answers)


def test_parse_probe_discards_what_precedes_the_mark():
    noisy = _SUDO_NOISE + _probe_output(([f"{_D}/a.gcda"], [], 0, 0))
    assert cov_sweep._parse_probe(noisy, [_D]) == [[f"{_D}/a.gcda"]]


def _removal_output(*sections: tuple[list[str], int], mark=True) -> str:
    lines = ["", "otto-cov-remove"] if mark else []
    for i, (out, f) in enumerate(sections):
        lines += [f"otto-cov-begin {i}", *out, f"otto-cov-end {i} {f}"]
    return "\n".join(lines) + "\n"


def test_parse_removal_reads_each_products_find_status():
    output = _removal_output(
        ([], 0), ([_BUSYBOX_ABSENT], 1), (["find: '/x': Permission denied"], 1)
    )
    assert cov_sweep._parse_removal(output, [_D, _D, "/x"]) == [
        None,
        None,
        "find: '/x': Permission denied",
    ]


def test_parse_removal_a_silent_failure_says_so():
    assert cov_sweep._parse_removal(_removal_output(([], 2)), [_D]) == ["exit 2, no output"]


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(_removal_output(([], 0)).replace("otto-cov-end 0 0\n", ""), id="missing-end"),
        pytest.param(_removal_output(([], 0), mark=False), id="no-mark"),
        pytest.param(_removal_output(([], 0)) + "stray\n", id="stray-line"),
    ],
)
def test_parse_removal_trusts_no_partial_batch(output):
    [answer] = cov_sweep._parse_removal(output, [_D])
    assert answer is not None
    assert answer.startswith("removal batch output was incomplete")


# ── one classifier: the reset's check and the sweep's files section agree ──────


@pytest.mark.parametrize(
    ("retcode", "lines"),
    [
        pytest.param(0, [], id="clean"),
        pytest.param(0, [f"{_D}/a.gcda", f"{_D}/sub/b.gcda"], id="counters-left"),
        pytest.param(0, [f"{_D}/a.gcno"], id="not-a-counter"),
        pytest.param(0, [f"{_D}_ref/a.gcda"], id="sibling-prefix"),
        pytest.param(1, [_GNU_ABSENT], id="absent-gnu"),
        pytest.param(1, [_BUSYBOX_ABSENT], id="absent-busybox"),
        pytest.param(1, [f"find: '{_D}/sub': No such file or directory"], id="descendant-enoent"),
        pytest.param(1, [f"find: '{_D}': Permission denied"], id="permission"),
        pytest.param(1, [], id="silent-failure"),
    ],
)
def test_the_reset_check_and_the_sweeps_files_section_answer_alike(retcode, lines):
    """The same ``find -print`` answer means the same thing to the reset and to cleanup.

    The reset reads it through ``classify_gcda_check``; the sweep's probe
    reads it as its files section (its dirs section, the same ENOENT when the
    files say absent, else empty and clean). One classifier decides both.
    """
    from otto.host.product import classify_gcda_check

    body = "".join(f"{line}\n" for line in lines)
    status = Status.Success if retcode == 0 else Status.Failed
    check = CommandResult(
        status, value=f"\notto-gcda-check\n{body}", command="find", retcode=retcode
    )
    reset = classify_gcda_check(_D, check)
    absent = retcode != 0 and len(lines) == 1 and "No such file" in lines[0]
    dirs, s = (list(lines), 1) if absent else ([], 0)
    [sweep] = cov_sweep._parse_probe(_probe_output((list(lines), dirs, retcode, s)), [_D])
    if isinstance(reset, GcdaFindError):
        assert isinstance(sweep, GcdaFindError)
    else:
        assert sweep == reset
