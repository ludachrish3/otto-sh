"""Cleanup removes the files otto staged: the shared spellings, the eligibility rule, the step.

The step is :meth:`~otto.host.host.BaseHost.remove_staged_files` and its question
:meth:`~otto.host.host.BaseHost.staged_files_absent`. What these pin: ONE function names
where an entry's staged file is, ONE builder spells "remove this directory if it is empty",
one rule says which entries are covered, and the two verbs send one command each, framed
and parsed fail-closed. The orders of the cleanup verbs and the dispatch are pinned beside
the verbs (``test_host_lifecycle_filters.py``, ``tests/unit/project/test_actions.py``).
"""

from pathlib import Path, PurePosixPath

import pytest

from otto.utils import Status

# ── the shared spellings ─────────────────────────────────────────────────────


def test_staged_file_is_the_directory_joined_with_the_artifact_basename():
    from otto.host.product import staged_file

    assert staged_file(Path("/opt/otto"), Path("build/out/app.bin")) == Path("/opt/otto/app.bin")
    assert staged_file("/opt/otto", "app.bin") == Path("/opt/otto/app.bin")


def test_the_staged_path_of_an_entry_resolves_its_directory_through_the_host():
    import asyncio

    from otto.host.declared_product import DeclaredProduct
    from otto.host.product import staged_path

    class _Home:
        id = "h1"
        default_dest_dir = Path()

        async def login_home(self):
            return Path("/home/u")

    product = DeclaredProduct(artifact=Path("x/app.bin"), name="app")
    declared = DeclaredProduct(artifact=Path("x/app.bin"), name="d", stage_dir=Path("/opt/otto"))
    assert asyncio.run(staged_path(product, _Home())) == Path("/home/u/app.bin")
    assert asyncio.run(staged_path(declared, _Home())) == Path("/opt/otto/app.bin")


def test_rm_f_command_quotes_the_path():
    from otto.host.shell_quote import rm_f_command

    assert rm_f_command("/opt/o t/app.bin") == "rm -f '/opt/o t/app.bin'"
    assert rm_f_command(PurePosixPath("/opt/otto/app.bin")) == "rm -f /opt/otto/app.bin"


def test_rmdir_if_empty_is_quiet_and_never_fails():
    from otto.host.shell_quote import rmdir_if_empty

    assert rmdir_if_empty("/a/b", "/a") == "{ rmdir /a/b /a 2>/dev/null || true; }"
    assert rmdir_if_empty("/a b") == "{ rmdir '/a b' 2>/dev/null || true; }"


def test_the_docker_build_input_cleanup_uses_the_shared_rmdir_builder():
    from otto.docker.staging import build_input_cleanup_command
    from otto.host.shell_quote import rmdir_if_empty

    command = build_input_cleanup_command(Path("/tmp/r"), "p", Path("/tmp/r/p/build/x"))
    assert command.endswith(f"&& {rmdir_if_empty('/tmp/r/p/build', '/tmp/r/p')}")


# ── which entries are covered ────────────────────────────────────────────────


def _custom_stage_product():
    """A ``DeclaredProduct`` subclass whose ``stage`` is code of its own."""
    from otto.host.declared_product import DeclaredProduct

    class _Custom(DeclaredProduct):
        async def stage(self, host):
            return await super().stage(host)

    return _Custom(artifact=Path("x/custom.bin"), name="custom", stage_dir=Path("/opt/c"))


def _entries(tmp_path):
    """One of each kind the rule names, plus the ones it leaves out."""
    from otto.host.declared_product import DeclaredProduct
    from otto.host.docker_image_kind import DockerImageProduct
    from otto.host.kmod_kind import KmodProduct
    from otto.host.kmod_tool_kind import KmodcovTool, KmodTool

    return {
        "shell": DeclaredProduct(artifact=Path("x/app.bin"), name="shell"),
        "tarball": DockerImageProduct(
            artifact=Path("x/img.tar"), name="tarball", image="x/img.tar", is_tarball=True
        ),
        "reference": DockerImageProduct(artifact=Path("nginx"), name="reference", image="nginx"),
        "kmod": KmodProduct(artifact=tmp_path / "m.ko", name="kmod"),
        "kmod-tool": KmodTool(name="kmod-tool", artifact=tmp_path / "t.ko"),
        "kmodcov-tool": KmodcovTool(name="kmodcov-tool", artifact=tmp_path / "c.ko"),
    }


def test_every_kind_that_stages_a_file_is_covered_and_a_reference_image_is_not(
    recording_host, tmp_path
):
    from otto.host.staged_files import staged_entries

    recording_host.default_dest_dir = Path("/srv/stage")
    entries = _entries(tmp_path)
    covered = staged_entries(recording_host, list(entries.values()))
    assert [e.name for e in covered] == ["shell", "tarball", "kmod", "kmod-tool", "kmodcov-tool"]


def test_a_class_entry_that_replaced_stage_is_not_covered(recording_host):
    from otto.host.staged_files import staged_entries

    assert staged_entries(recording_host, [_custom_stage_product()]) == []


def test_a_provider_product_without_an_artifact_is_not_covered(recording_host):
    from otto.host.staged_files import staged_entries

    class _NoFile:
        name = "bare"
        stages_artifact = True

    assert staged_entries(recording_host, [_NoFile()]) == []


def test_an_embedded_host_is_sent_nothing():
    from otto.host.command_frame import ZephyrFrame
    from otto.host.declared_product import DeclaredProduct
    from otto.host.element import Element
    from otto.host.embedded_host import EmbeddedHost
    from otto.host.staged_files import staged_entries
    from otto.logger.mode import LogMode

    board = EmbeddedHost(
        ip="192.0.2.1",
        element=Element(name="zephyr37_fat"),
        log=LogMode.QUIET,
        command_frame=ZephyrFrame(),
    )
    shell = DeclaredProduct(artifact=Path("x/app.bin"), name="shell", stage_dir=Path("/opt/otto"))
    assert staged_entries(board, [shell]) == []


def test_an_entry_with_no_staging_destination_at_all_is_not_covered(recording_host):
    """No declared directory, no default_dest_dir, a host with no login-home concept."""
    from otto.host.declared_product import DeclaredProduct
    from otto.host.staged_files import staged_entries

    bare = DeclaredProduct(artifact=Path("x/app.bin"), name="shell")
    assert staged_entries(recording_host, [bare]) == []

    async def _home():
        return Path("/home/u")

    recording_host.login_home = _home  # a host WITH a login home: the destination exists
    assert staged_entries(recording_host, [bare]) == [bare]


@pytest.mark.asyncio
async def test_a_local_entry_staged_onto_its_own_artifact_is_left_alone(recording_host, tmp_path):
    """``shutil.copy2`` refuses to copy a file onto itself; the file is the user's build output."""
    from otto.host.declared_product import DeclaredProduct
    from otto.host.local_host import LocalHost
    from otto.host.staged_files import staged_entries, staged_files_left

    own = DeclaredProduct(artifact=tmp_path / "app.bin", name="own", stage_dir=tmp_path)
    elsewhere = DeclaredProduct(
        artifact=tmp_path / "app.bin", name="elsewhere", stage_dir=tmp_path / "stage"
    )
    local = LocalHost()
    assert staged_entries(local, [own, elsewhere]) == [elsewhere]
    assert await staged_files_left(local, [own]) == []  # nothing probed: it was never staged
    # the same declaration on a remote host stages a copy there, so it is covered
    assert staged_entries(recording_host, [own, elsewhere]) == [own, elsewhere]


# ── the removal and the question ─────────────────────────────────────────────


def _two(recording_host, tmp_path):
    """A product that declares its stage directory, a tool that falls to default_dest_dir."""
    from otto.host.declared_product import DeclaredProduct
    from otto.host.kmod_tool_kind import KmodTool

    recording_host.default_dest_dir = Path("/srv/stage")
    app = DeclaredProduct(artifact=Path("x/app.bin"), name="app", stage_dir=Path("/opt/otto/app"))
    tool = KmodTool(name="probe", artifact=tmp_path / "probe.ko")
    recording_host.products = [app]
    recording_host.dev_tools = [tool]
    return app, tool


_REMOVAL = (
    "printf '\\n%s\\n' otto-staged-removal\n"
    'rm -f /opt/otto/app/app.bin 2>&1; echo "otto-staged-rm 0 $?"\n'
    'rm -f /srv/stage/probe.ko 2>&1; echo "otto-staged-rm 1 $?"\n'
    '{ for d in /opt/otto/app; do test -k "$d" || rmdir "$d" 2>/dev/null; done; true; }\n'
    "if test -e /opt/otto/app || test -L /opt/otto/app; then echo 'otto-staged-dir 0 kept'; fi"
)


def _removed_ok(*, kept=False):
    lines = ["otto-staged-removal", "otto-staged-rm 0 0", "otto-staged-rm 1 0"]
    return "\n".join(lines + (["otto-staged-dir 0 kept"] if kept else []))


@pytest.mark.asyncio
async def test_the_removal_is_one_command_over_every_covered_entry(recording_host, tmp_path):
    _two(recording_host, tmp_path)
    recording_host.script_exec(_removed_ok())
    result = await recording_host.remove_staged_files()
    assert result.is_ok
    assert recording_host.exec_calls == [_REMOVAL]


@pytest.mark.asyncio
async def test_the_removal_is_scoped_by_owner(recording_host, tmp_path):
    app, tool = _two(recording_host, tmp_path)
    app.owner, tool.owner = "A", "B"
    recording_host.script_exec("otto-staged-removal\notto-staged-rm 0 0\notto-staged-dir 0 kept")
    assert (await recording_host.remove_staged_files(owner="A")).is_ok
    [sent] = recording_host.exec_calls
    assert "app.bin" in sent
    assert "probe.ko" not in sent


@pytest.mark.asyncio
async def test_nothing_is_sent_when_no_entry_is_covered(recording_host):
    assert (await recording_host.remove_staged_files()).is_ok
    assert recording_host.exec_calls == []


@pytest.mark.asyncio
async def test_a_default_stage_directory_is_never_tried(recording_host, tmp_path):
    from otto.host.kmod_tool_kind import KmodTool

    recording_host.default_dest_dir = Path("/srv/stage")
    recording_host.dev_tools = [KmodTool(name="probe", artifact=tmp_path / "probe.ko")]
    recording_host.script_exec("otto-staged-removal\notto-staged-rm 0 0")
    assert (await recording_host.remove_staged_files()).is_ok
    [sent] = recording_host.exec_calls
    assert "rmdir" not in sent


@pytest.mark.asyncio
async def test_a_shared_and_a_nested_stage_directory_are_tried_once_deepest_first(
    recording_host,
):
    from otto.host.declared_product import DeclaredProduct

    recording_host.products = [
        DeclaredProduct(artifact=Path("a.bin"), name="a", stage_dir=Path("/opt/otto")),
        DeclaredProduct(artifact=Path("b.bin"), name="b", stage_dir=Path("/opt/otto/deep/er")),
        DeclaredProduct(artifact=Path("c.bin"), name="c", stage_dir=Path("/opt/otto")),
    ]
    recording_host.script_exec(
        "otto-staged-removal\n" + "\n".join(f"otto-staged-rm {i} 0" for i in range(3))
    )
    assert (await recording_host.remove_staged_files()).is_ok
    [sent] = recording_host.exec_calls
    assert (
        '{ for d in /opt/otto/deep/er /opt/otto; do test -k "$d" || rmdir "$d" 2>/dev/null; '
        "done; true; }"
    ) in sent


@pytest.mark.asyncio
async def test_a_single_component_directory_is_never_tried(recording_host):
    from otto.host.declared_product import DeclaredProduct
    from otto.host.staged_files import declared_stage_dirs

    entries = [
        DeclaredProduct(artifact=Path("a.bin"), name="a", stage_dir=Path("/tmp")),
        DeclaredProduct(artifact=Path("b.bin"), name="b", stage_dir=Path("/opt")),
        DeclaredProduct(artifact=Path("c.bin"), name="c", stage_dir=Path("/opt/otto/app")),
    ]
    assert declared_stage_dirs(entries) == ["/opt/otto/app"]
    recording_host.products = entries[:2]
    recording_host.script_exec("otto-staged-removal\notto-staged-rm 0 0\notto-staged-rm 1 0")
    assert (await recording_host.remove_staged_files()).is_ok
    [sent] = recording_host.exec_calls
    assert "rmdir" not in sent
    assert "/tmp/a.bin" in sent  # the files are still removed


@pytest.mark.asyncio
async def test_a_kept_directory_is_logged_and_is_not_a_failure(recording_host, tmp_path, caplog):
    import logging

    _two(recording_host, tmp_path)
    recording_host.script_exec(_removed_ok(kept=True))
    with caplog.at_level(logging.INFO, logger="otto.host.staged_files"):
        result = await recording_host.remove_staged_files()
    assert result.is_ok
    assert [r.getMessage() for r in caplog.records] == [
        "stage directory /opt/otto/app kept: not empty, sticky, or the session cannot remove it"
    ]


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_removed_is_an_error_naming_it(recording_host, tmp_path):
    _two(recording_host, tmp_path)
    recording_host.script_exec(
        "otto-staged-removal\n"
        "rm: cannot remove '/opt/otto/app/app.bin': Permission denied\n"
        "otto-staged-rm 0 1\n"
        "otto-staged-rm 1 0\n"
    )
    result = await recording_host.remove_staged_files()
    assert result.status is Status.Error
    assert result.msg == (
        "h1: cleanup left staged files: /opt/otto/app/app.bin: "
        "rm: cannot remove '/opt/otto/app/app.bin': Permission denied"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "",
        "otto-staged-removal\notto-staged-rm 0 0",
        "otto-staged-removal\notto-staged-rm 1 0\notto-staged-rm 0 0",
        "otto-staged-removal\notto-staged-rm 0 0\notto-staged-rm 1 0\nsurprise",
        "otto-staged-removal\notto-staged-rm 0 0\notto-staged-rm 1 0\notto-staged-dir 3 kept",
    ],
)
async def test_an_answer_that_is_not_exactly_framed_is_an_error(recording_host, tmp_path, answer):
    _two(recording_host, tmp_path)
    recording_host.script_exec(answer)
    result = await recording_host.remove_staged_files()
    assert result.status is Status.Error
    assert "staged-file removal output was incomplete" in result.msg


@pytest.mark.asyncio
async def test_a_dry_run_declines_and_sends_nothing(recording_host, tmp_path):
    from tests.conftest import active_context

    _two(recording_host, tmp_path)
    with active_context(dry_run=True):
        result = await recording_host.remove_staged_files()
    assert result.status is Status.NotRun
    assert recording_host.exec_calls == []


@pytest.mark.asyncio
async def test_a_failed_home_discovery_is_an_error_never_nothing_staged(recording_host):
    from otto.host.declared_product import DeclaredProduct
    from otto.host.errors import HostCommandError

    async def _home():
        raise HostCommandError("h1: could not read the login user's home directory")

    recording_host.login_home = _home
    recording_host.products = [DeclaredProduct(artifact=Path("a.bin"), name="a")]
    result = await recording_host.remove_staged_files()
    assert result.status is Status.Error
    assert "could not read the login user's home directory" in result.msg
    assert recording_host.exec_calls == []


@pytest.mark.asyncio
async def test_cleanup_twice_is_the_same_command_and_still_success(recording_host, tmp_path):
    _two(recording_host, tmp_path)
    recording_host.script_exec(_removed_ok())
    recording_host.script_exec(_removed_ok())
    assert (await recording_host.remove_staged_files()).is_ok
    assert (await recording_host.remove_staged_files()).is_ok
    assert recording_host.exec_calls[0] == recording_host.exec_calls[1]


@pytest.mark.asyncio
async def test_a_container_host_runs_the_removal_and_the_probe_as_root(tmp_path):
    from unittest.mock import MagicMock

    from otto.host.declared_product import DeclaredProduct
    from otto.host.docker_host import DockerContainerHost
    from otto.result import CommandResult

    sent = []

    class _Box(DockerContainerHost):
        async def exec(self, cmd, **kwargs):
            sent.append((cmd, kwargs.get("user")))
            word = "removal" if "otto-staged-removal" in cmd else "probe"
            body = (
                "otto-staged-removal\notto-staged-rm 0 0"
                if word == "removal"
                else "otto-staged-probe\notto-staged 0 absent"
            )
            return CommandResult(status=Status.Success, value=body, command=cmd, retcode=0)

    parent = MagicMock()
    parent.id = "parent"
    box = _Box(
        parent=parent,
        container_id="abc123def456",
        project="p",
        service="api",
        compose_project="otto-p",
    )
    box.products = [DeclaredProduct(artifact=Path("a.bin"), name="a", stage_dir=Path("/opt/a"))]
    assert (await box.remove_staged_files()).is_ok
    assert await box.staged_files_absent() is True
    assert [user for _cmd, user in sent] == ["root", "root"]


# ── the question ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("words", "absent"),
    [(("absent", "absent"), True), (("absent", "present"), False), (("present", "present"), False)],
)
async def test_the_question_is_true_only_when_every_file_is_confirmed_absent(
    recording_host, tmp_path, words, absent
):
    _two(recording_host, tmp_path)
    answer = "otto-staged-probe\n" + "\n".join(f"otto-staged {i} {w}" for i, w in enumerate(words))
    recording_host.script_exec(answer)
    assert await recording_host.staged_files_absent() is absent


@pytest.mark.asyncio
async def test_the_question_is_one_command_asking_the_removals_paths(recording_host, tmp_path):
    _two(recording_host, tmp_path)
    recording_host.script_exec("otto-staged-probe\notto-staged 0 absent\notto-staged 1 absent")
    await recording_host.staged_files_absent()
    [sent] = recording_host.exec_calls
    assert sent.splitlines()[0] == "printf '\\n%s\\n' otto-staged-probe"
    assert sent.count("if { __otto_d=") == 2
    # A dangling symlink is present: the probe asks -L as well as -e, and climbs past neither.
    assert "test -e /opt/otto/app/app.bin || test -L /opt/otto/app/app.bin" in sent
    assert "test -e /srv/stage/probe.ko || test -L /srv/stage/probe.ko" in sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "otto-staged-probe\notto-staged 0 unknown\notto-staged 1 absent",
        "otto-staged-probe\notto-staged 0 absent",
        "otto-staged 0 absent\notto-staged 1 absent",
        "otto-staged-probe\notto-staged 0 absent\notto-staged 1 maybe",
        "",
    ],
)
async def test_a_path_it_cannot_look_at_or_a_probe_that_did_not_answer_raises(
    recording_host, tmp_path, answer
):
    from otto.host.errors import HostCommandError

    _two(recording_host, tmp_path)
    recording_host.script_exec(answer)
    with pytest.raises(HostCommandError, match="cannot tell"):
        await recording_host.staged_files_absent()


@pytest.mark.asyncio
async def test_a_failed_home_discovery_makes_the_question_raise(recording_host):
    from otto.host.declared_product import DeclaredProduct
    from otto.host.errors import HostCommandError

    async def _home():
        raise HostCommandError("h1: could not read the login user's home directory")

    recording_host.login_home = _home
    recording_host.products = [DeclaredProduct(artifact=Path("a.bin"), name="a")]
    with pytest.raises(HostCommandError, match="home directory"):
        await recording_host.staged_files_absent()


@pytest.mark.asyncio
async def test_the_question_refuses_under_a_dry_run(recording_host, tmp_path):
    from otto.result import CommandNotRunError
    from tests.conftest import active_context

    _two(recording_host, tmp_path)
    with active_context(dry_run=True), pytest.raises(CommandNotRunError):
        await recording_host.staged_files_absent()
    assert recording_host.exec_calls == []


@pytest.mark.asyncio
async def test_the_question_with_nothing_covered_sends_nothing_and_is_true(recording_host):
    assert await recording_host.staged_files_absent() is True
    assert recording_host.exec_calls == []


@pytest.mark.asyncio
async def test_the_probe_is_true_to_a_real_shell(tmp_path):
    """Run both scripts through ``sh``: removed, a dangling symlink, a hidden parent."""
    import subprocess

    from otto.host.staged_files import _parse_probe, _parse_removal, probe_script, removal_script

    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "other").write_text("x")
    emptied = tmp_path / "emptied"
    emptied.mkdir()
    gone = emptied / "app.bin"
    gone.write_text("x")
    dangling = kept / "link.bin"
    dangling.symlink_to(tmp_path / "nowhere")
    paths = [gone, dangling, kept / "absent.bin"]
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)
    dirs = [str(kept), str(sticky), str(emptied)]

    def sh(script):
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, check=True
        ).stdout

    before = _parse_probe(sh(probe_script(paths)), 3)
    assert before == ["present", "present", "absent"]
    errors, left = _parse_removal(sh(removal_script(paths, dirs)), 3, 3)
    assert errors == [None, None, None]
    assert left == [0, 1]  # kept holds `other`, sticky is never removed; emptied is gone
    assert _parse_probe(sh(probe_script(paths)), 3) == ["absent", "absent", "absent"]
    assert not emptied.exists()
    assert (kept / "other").exists()
    assert sticky.is_dir()
